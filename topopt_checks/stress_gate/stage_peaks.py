r"""付け根の最大応力が完成化のどの段で生まれるかを、同じマスク・同じ細格子・同じ境界条件で比べる(docs/TOPOPT_PEAK_GATE.md)。

保存済みマスクを探索の確定形として製品の入口 `optimize_run.run` に通し(探索は再実行しない)、完成形検査の直前で次の3形を作る:
- stair: 空隙を埋めた確定マスクを細格子へ割っただけ(平滑化前)
- smooth: 平滑化後の表面の占有に、保持セルを足したもの(集合演算なし)
- final: 製品の完成メッシュ(平滑化 + 保持を足し除外を引く集合演算)の占有
いずれも完成形検査と同じ切り出し・分割数・確定表の作り方・部材域で解き、部材域の表面の最大と、`--center` から `--radius` mm 以内の最大を出す。

    .venv\Scripts\python.exe topopt_checks\stress_gate\stage_peaks.py --model hanger --load 1000 --h 2.4 --w 10 ^
        --mask output\stress_gate_20261008\peak_gate\hanger_L1000_qv20_r2.npz --refine 2 --center 3.58 -5.88 -61.41 --radius 6
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import phase_a_fields as pa  # noqa: E402

import numpy as np  # noqa: E402
from scipy import ndimage  # noqa: E402

import refine_shape  # noqa: E402
from duocad import optimize_run  # noqa: E402
from duocad.topopt import acceptance, anchor, binary_search, final_check, stress, voxelize  # noqa: E402
from duocad.topopt import finalize as fz  # noqa: E402


class Done(Exception):
    pass


def measure(material, fine, conditions, layer, keep, center, radius, factor):
    table = anchor.build_anchors(conditions['anchor_soup'], fine, allowed_nodes=acceptance.active_nodes(material))
    cond = conditions['conditions']
    report, fixed, load = acceptance.condition_check(material, table, cond.fixed_faces, cond.load_faces, cond.forces)
    if report['status'] != 'pass':
        return dict(error=f"conditions {report['status']}")
    model, _d, displacement, scale, relative = acceptance.solve_material(
        material, fine, fixed, load, youngs_modulus_mpa=cond.youngs_modulus_mpa, solver=cond.solver)
    region = material & ~layer & ~keep
    faces = stress.exposed_face_von_mises(model, displacement, material, scale, cells=region)
    cell_max = np.nan_to_num(faces, nan=0.0).max(axis=-1)
    centers = np.stack(np.meshgrid(*[fine.centers_1d(a) for a in range(3)], indexing='ij'), axis=-1)
    near = np.linalg.norm(centers - np.asarray(center), axis=-1) <= radius

    def top(mask):
        values = np.where(mask, cell_max, 0.0)
        i = np.unravel_index(int(np.argmax(values)), values.shape)
        return dict(mpa=float(values[i]), position_mm=[round(float(v), 2) for v in centers[i]])

    band = ndimage.binary_dilation(keep, structure=np.ones((3, 3, 3), bool), iterations=factor) if keep.any() else keep
    return dict(material_cells=int(material.sum()), member=top(region), near_center=top(region & near),
                junction=top(region & band), away=top(region & ~band), relative_residual=relative)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True, choices=list(pa.MODELS))
    ap.add_argument("--load", type=float, default=None)
    ap.add_argument("--h", type=float, default=None)
    ap.add_argument("--w", type=float, default=None)
    ap.add_argument("--mask", help="保存済みマスク(npz の mask)。--full なら不要")
    ap.add_argument("--full", action="store_true", help="設計空間の全材料をそのまま確定形にする")
    ap.add_argument("--refine", type=int, default=2)
    ap.add_argument("--center", type=float, nargs=3, required=True)
    ap.add_argument("--radius", type=float, default=6.0)
    a = ap.parse_args()
    spec = dict(pa.MODELS[a.model])
    for key, val in (("load_n", a.load), ("h", a.h), ("w", a.w)):
        if val is not None:
            spec[key] = val
    saved = None
    if not a.full:
        with np.load(a.mask) as z:
            saved = np.asarray(z["mask"], dtype=bool)
    doc = refine_shape.open_session(spec["model"]).document
    refine_shape._set_load_doc(doc, spec["load_n"])
    captured: dict = {}
    real_finalize = fz.finalize

    def fake_find(design_space_mask, conditions, **kwargs):
        if a.full:
            initial = binary_search.find_initial_feasible(design_space_mask, conditions)
            mask = initial.best_feasible.mask  # 条件に触れない成分を除いた全材料(段階3の出発形)
        else:
            mask = saved
        report = acceptance.inspect(mask, mask, conditions.grid, **conditions.inspect_kwargs())
        best = binary_search.FeasibleModel(mask=mask, before_mask=mask, report=report, trial_index=0)
        return binary_search.SearchResult(status="feasible", termination="replayed", best_feasible=best, history=[],
                                          evaluations=1, conditions_identifier="replayed")

    def finalize(vertices, faces, preserve, exclude, progress=None):
        captured["smoothed"] = (np.asarray(vertices, dtype=float), np.asarray(faces))
        return real_finalize(vertices, faces, preserve, exclude, progress=progress)

    def inspect(final_vertices, final_faces, *, grid, conditions, mask, keep_cells=None, anchor_soup, **_):
        out = {}
        lo, hi = final_check._crop(grid, np.asarray(final_vertices, dtype=float))
        k = a.refine
        h = float(grid.spacing)
        fine = voxelize.Grid(origin=tuple(float(o) + h * int(i) for o, i in zip(grid.origin, lo)),
                             spacing=h / k, shape=tuple(int(n) * k for n in hi - lo))
        nodes = [anchor.face_node_ids(conditions.table, f) for f in tuple(conditions.fixed_faces) + tuple(conditions.load_faces)]
        layer = final_check._refine_cells(stress.boundary_layer_cells(grid.shape, np.concatenate(nodes)), lo, hi, k)
        keep = (np.zeros(layer.shape, dtype=bool) if keep_cells is None
                else final_check._refine_cells(keep_cells, lo, hi, k))
        ctx = dict(conditions=conditions, anchor_soup=anchor_soup)

        def soup(vertices, faces):
            return voxelize.TriangleSoup(positions=np.asarray(vertices, dtype=float), triangles=np.asarray(faces),
                                         face_index=np.zeros(len(faces), dtype=np.int64))

        shapes = dict(stair=final_check._refine_cells(mask, lo, hi, k),
                      smooth=voxelize.occupancy(soup(*captured["smoothed"]), fine, allow_empty=True)[0] | keep,
                      final=voxelize.occupancy(soup(final_vertices, final_faces), fine, allow_empty=True)[0])
        for name, material in shapes.items():
            started = time.perf_counter()
            row = measure(material, fine, ctx, layer, keep, a.center, a.radius, k)
            row["seconds"] = round(time.perf_counter() - started, 1)
            out[name] = row
            print(name, json.dumps(row, ensure_ascii=False), flush=True)
        captured["stages"] = dict(refine=k, spacing_mm=float(fine.spacing), **out)
        raise Done

    with patch.object(binary_search, "find_best_feasible", fake_find), patch.object(fz, "finalize", finalize), \
            patch.object(final_check, "inspect", inspect):
        try:
            optimize_run.run(doc, optimize_run.OptimizeOptions(element_size_mm=spec["h"], min_width_mm=spec["w"]))
        except Done:
            pass
    stem = f"{a.model}_L{spec['load_n']:g}_full" if a.full else Path(a.mask).stem
    dest = pa.OUT / "peak_gate" / f"stages_{stem}_r{a.refine}.json"
    dest.write_text(json.dumps(dict(mask=a.mask or "full", center=a.center, radius=a.radius, **captured.get("stages", {})),
                               ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
