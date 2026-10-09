"""段階D: 最高応力(ピーク)を、印刷される形(平滑化後)で測ると何が収束するか(研究用)。

採用マスクを製品と同じ手順で平滑化(`mesh.build_surface_from_mask`)し、k=1..4 の細格子へ占有化して解く(「平滑」)。
比較に、マスクをそのまま k³ に割った形(「階段」= 段階Cと同じ)も解く。保持・除外の集合演算はかけず、細格子の保持セルを足し、
設計空間の外を落とす。

部材域(保持外・拘束/荷重節点から探索格子の h 超)で、素の最大・物理体積 V の上位値 Q_V・物理半径 r の球平均の最大と、
最大セルの位置(拘束面・荷重面・保持からの距離)を出す。

    .venv\\Scripts\\python.exe topopt_checks\\stress_gate\\phase_d_peak.py --model dilution --mask <npz> [--k 1 2 3 4]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import phase_a_fields as pa  # noqa: E402

import numpy as np  # noqa: E402
from scipy import ndimage  # noqa: E402

import _fem_probe  # noqa: E402
import refine_shape  # noqa: E402
import search_methods as smeth  # noqa: E402
from duocad import optimize_run  # noqa: E402
from duocad.topopt import mesh as topo_mesh, stress, voxelize  # noqa: E402

import gate_metrics as gm  # noqa: E402

VOLUMES_MM3 = (0.5, 1.0, 8.0, 27.0, 64.0)
AREAS_MM2 = (1.0, 4.0, 16.0)
RADII_MM = (0.5, 1.0, 2.0)
MAX_CELLS = 450_000


def surface_measures(face_vm, member, keep, dfix, hf):
    """表面(露出面)の面中心 von Mises: 部材域の要素の露出面の最大と、面積 A の上位値。"""
    vals = face_vm[member]  # (n,6) nan 混じり
    flat = vals[np.isfinite(vals)]
    out = {}
    if flat.size == 0:
        return out
    out["surf_max"] = float(flat.max())
    s = np.sort(flat)[::-1]
    for a in AREAS_MM2:
        n = a / hf ** 2
        out[f"surf_Q_{a:g}mm2"] = float(s[min(s.size - 1, max(0, int(np.ceil(n)) - 1))])
    elem = np.nanmax(np.where(np.isfinite(face_vm), face_vm, -1), axis=-1)
    idx = np.unravel_index(np.argmax(np.where(member, elem, -1)), elem.shape)
    dkeep = ndimage.distance_transform_edt(~keep) * hf if keep.any() else np.full(keep.shape, np.inf)
    out["surf_max_at"] = dict(dist_fixed=float(dfix[idx]), dist_keep=float(dkeep[idx]))
    return out


def measures(vm, mask, keep, dfix, dload, h_search, hf):
    cell = hf ** 3
    member = mask & ~keep & (dfix > h_search) & (dload > h_search)
    out = dict(material_mm3=float(mask.sum() * cell), member_cells=int(member.sum()))
    if not member.any():
        return out
    out["max"] = float(vm[member].max())
    for v in VOLUMES_MM3:
        out[f"Q_{v:g}mm3"] = gm.top_volume_value(vm, member, cell, v) if member.sum() * cell > v else None
    for r in RADII_MM:
        if r >= hf:
            out[f"ball_{r:g}mm"] = float(gm.ball_mean_field(vm, mask, r, hf)[member].max())
    idx = np.unravel_index(np.argmax(np.where(member, vm, -1)), vm.shape)
    dkeep = ndimage.distance_transform_edt(~keep) * hf if keep.any() else np.full(keep.shape, np.inf)
    out["max_at"] = dict(dist_fixed=float(dfix[idx]), dist_load=float(dload[idx]), dist_keep=float(dkeep[idx]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, choices=list(pa.MODELS))
    ap.add_argument("--mask", required=True)
    ap.add_argument("--k", nargs="*", type=int, default=[1, 2, 3, 4])
    ap.add_argument("--load", type=float, default=None)
    ap.add_argument("--h", type=float, default=None)
    ap.add_argument("--w", type=float, default=None)
    a = ap.parse_args()
    spec = dict(pa.MODELS[a.model])
    for key, val in (("load_n", a.load), ("h", a.h), ("w", a.w)):
        if val is not None:
            spec[key] = val
    h, w = spec["h"], spec["w"]
    with np.load(a.mask) as z:
        mask = np.asarray(z["mask"], bool)
    ctx = smeth.build_context(spec["model"], load_n=spec["load_n"], element_size_mm=h, min_width_mm=w)
    surface = topo_mesh.build_surface_from_mask(mask)
    world = np.asarray(optimize_run._mesh_to_world(surface.verts_smooth, ctx.grid)).reshape(-1, 3)
    soup = voxelize.TriangleSoup(positions=world, triangles=np.asarray(surface.faces),
                                 face_index=np.zeros(len(surface.faces), dtype=np.int64))
    out = dict(mask=str(a.mask), model=a.model, load_n=spec["load_n"], h=h, w=w, levels={})
    for k in a.k:
        c = ctx if k == 1 else smeth.build_context(spec["model"], load_n=spec["load_n"], element_size_mm=h / k, min_width_mm=w)
        hf = float(c.grid.spacing)
        keep = np.asarray(c.cond.keep_cells if c.cond.keep_cells is not None else np.zeros(c.grid.shape, bool))
        space = np.asarray(c.space.mask, bool)
        dfix = gm.distance_to_points(c.grid, c.table.positions(list(c.cond.fixed_faces)))
        dload = gm.distance_to_points(c.grid, c.table.positions(list(c.cond.load_faces)))
        shapes = {}
        smooth = (voxelize.occupancy(soup, c.grid, allow_empty=True)[0] & space) | keep
        shapes["smooth"] = smooth
        if k == 1:
            shapes["stair"] = mask
        else:
            try:
                st, _ = refine_shape._project_mask_exact_subdivide(mask, k, c.space, ctx.grid.origin, h)
                shapes["stair"] = (st & space) | keep
            except ValueError as exc:
                out["levels"].setdefault(f"k{k}", {})["stair_error"] = str(exc)
        for name, m in shapes.items():
            if m.sum() > MAX_CELLS:
                out["levels"].setdefault(f"k{k}", {})[name] = dict(skipped=f"{int(m.sum())} cells")
                continue
            t0 = time.perf_counter()
            try:
                cnd = c.cond
                probe = _fem_probe.evaluate_full(m, c.grid, c.table, cnd.fixed_faces, cnd.load_faces, cnd.forces,
                                                 allowable_mpa=cnd.allowable_mpa, youngs_modulus_mpa=cnd.youngs_modulus_mpa,
                                                 percentile=stress.DEFAULT_PERCENTILE, solver=cnd.solver)
                if probe["status"] != "ok":
                    raise RuntimeError(probe.get("error"))
                vm = probe["von_mises_mpa"]
            except RuntimeError as exc:
                out["levels"].setdefault(f"k{k}", {})[name] = dict(error=str(exc))
                continue
            row = measures(vm, m, keep, dfix, dload, h, hf)
            factor = float(np.nanmax(vm[m]) / np.nanmax(stress.element_stress(probe["_model"], probe["_displacement"]).von_mises[m]))
            face = gm.exposed_face_vm(probe["_model"], probe["_displacement"], m, factor)
            member = m & ~keep & (dfix > h) & (dload > h)
            row.update(surface_measures(face, member, keep, dfix, hf))
            row["fem_s"] = time.perf_counter() - t0
            out["levels"].setdefault(f"k{k}", {})[name] = row
            print(f"k{k} {name}", json.dumps({x: (round(v, 2) if isinstance(v, float) else v) for x, v in row.items()},
                                             ensure_ascii=False), flush=True)
    (pa.OUT / "peak").mkdir(parents=True, exist_ok=True)
    (pa.OUT / "peak" / f"{Path(a.mask).stem}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float),
                                                                encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
