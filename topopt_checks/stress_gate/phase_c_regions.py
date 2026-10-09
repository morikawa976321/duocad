"""段階C: 採用形を領域ごとに採点し、固定面近傍の取り付き部が細ったかを確かめる(研究用)。

段階Bが保存したマスク(output/stress_gate_20261008/masks/*.npz)を k=1,2,3 で解き直し、拘束節点からの
距離で分けた帯(0–2mm・2–4mm・4mm超)、保持セル、荷重面から4mm以内、の各領域で
Q_{w³}・最大・99.5%点・20MPa超と目標応力超の体積を出す。拘束面ごとの反力と、部材域(保持外・固定面と
荷重面から4mm超)の最大セルの位置も記録する。投影図(von Mises の最大値投影、3方向)を PNG で残す。

    .venv\\Scripts\\python.exe topopt_checks\\stress_gate\\phase_c_regions.py --model dilution --mask <npz> [--shift 0.5]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import phase_a_fields as pa  # noqa: E402

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
from scipy import ndimage  # noqa: E402

import _fem_probe  # noqa: E402
import refine_shape  # noqa: E402
import search_methods as smeth  # noqa: E402
import synthetic_matrix_support as sup  # noqa: E402
from duocad.topopt import stress  # noqa: E402

import gate_metrics as gm  # noqa: E402


def solve_probe(mask, ctx):
    c = ctx.cond
    probe = _fem_probe.evaluate_full(mask, ctx.grid, ctx.table, c.fixed_faces, c.load_faces, c.forces,
                                     allowable_mpa=c.allowable_mpa, youngs_modulus_mpa=c.youngs_modulus_mpa,
                                     percentile=stress.DEFAULT_PERCENTILE, solver=c.solver)
    if probe["status"] != "ok":
        raise RuntimeError(probe.get("error"))
    return probe


def region_rows(vm, mask, keep, dfix, dload, h, w, target):
    cell = h ** 3
    regions = {
        "fixed_0_2mm": mask & (dfix <= 2.0),
        "fixed_2_4mm": mask & (dfix > 2.0) & (dfix <= 4.0),
        "beyond_4mm": mask & (dfix > 4.0),
        "keep": mask & keep,
        "load_0_4mm": mask & (dload <= 4.0),
        "member": mask & ~keep & (dfix > 4.0) & (dload > 4.0),
    }
    out = {}
    for name, reg in regions.items():
        if not reg.any():
            out[name] = None
            continue
        v = vm[reg]
        out[name] = dict(cells=int(reg.sum()), volume_mm3=float(reg.sum() * cell),
                         QV_w3=gm.top_volume_value(vm, reg, cell, w ** 3) if reg.sum() * cell > w ** 3 else None,
                         max=float(v.max()), p995=float(np.percentile(v, 99.5)),
                         over_target_mm3=float((v > target).sum() * cell), over20_mm3=float((v > 20.0).sum() * cell))
    return out


def projection_png(vm, mask, path, vmax):
    imgs = []
    for axis in (2, 1, 0):
        proj = np.where(mask, vm, -1.0).max(axis=axis)
        t = np.clip(proj / vmax, 0, 1)
        rgb = np.stack([255 * t, 255 * (1 - np.abs(2 * t - 1)), 255 * (1 - t)], axis=-1)
        rgb[proj < 0] = 255
        im = Image.fromarray(rgb.astype(np.uint8).transpose(1, 0, 2)[::-1])
        imgs.append(im.resize((im.width * 6, im.height * 6), Image.NEAREST))
    W = sum(i.width for i in imgs) + 20 * (len(imgs) - 1)
    H = max(i.height for i in imgs)
    canvas = Image.new("RGB", (W, H), "white")
    x = 0
    for im in imgs:
        canvas.paste(im, (x, 0))
        x += im.width + 20
    canvas.save(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, choices=list(pa.MODELS))
    ap.add_argument("--mask", required=True)
    ap.add_argument("--shift", type=float, default=0.0)
    ap.add_argument("--load", type=float, default=None)
    ap.add_argument("--h", type=float, default=None)
    ap.add_argument("--w", type=float, default=None)
    ap.add_argument("--refine", nargs="*", type=int, default=[2, 3])
    a = ap.parse_args()
    spec = dict(pa.MODELS[a.model])
    for key, val in (("load_n", a.load), ("h", a.h), ("w", a.w)):
        if val is not None:
            spec[key] = val
    h, w = spec["h"], spec["w"]

    def context(size):
        if a.shift:
            return sup.build_context_with_origin_shift(spec["model"], load_n=spec["load_n"], element_size_mm=size,
                                                       min_width_mm=w, shift_mm=a.shift * h)
        return smeth.build_context(spec["model"], load_n=spec["load_n"], element_size_mm=size, min_width_mm=w)

    with np.load(a.mask) as z:
        mask = np.asarray(z["mask"], bool)
    ctx = context(h)
    target = ctx.cond.allowable_mpa / ctx.cond.target_safety_factor
    out = dict(mask=str(a.mask), model=a.model, load_n=spec["load_n"], h=h, w=w, shift=a.shift, levels={})
    stem = Path(a.mask).stem
    (pa.OUT / "regions").mkdir(parents=True, exist_ok=True)
    for k in [1] + list(a.refine):
        c = ctx if k == 1 else context(h / k)
        keep = np.asarray(c.cond.keep_cells if c.cond.keep_cells is not None else np.zeros(c.grid.shape, bool))
        if k == 1:
            m = mask
        else:
            m, _ = refine_shape._project_mask_exact_subdivide(mask, k, c.space, ctx.grid.origin, h)
            m = (m & np.asarray(c.space.mask, bool)) | keep
        dfix = gm.distance_to_points(c.grid, c.table.positions(list(c.cond.fixed_faces)))
        dload = gm.distance_to_points(c.grid, c.table.positions(list(c.cond.load_faces)))
        probe = solve_probe(m, c)
        vm = probe["von_mises_mpa"]
        row = dict(material_mm3=float(m.sum() * (h / k) ** 3), regions=region_rows(vm, m, keep, dfix, dload, h / k, w, target),
                   reactions_n=_fem_probe.per_face_reactions(probe, c.table, c.cond.fixed_faces))
        member = m & ~keep & (dfix > 4.0) & (dload > 4.0)
        if member.any():
            idx = np.unravel_index(np.argmax(np.where(member, vm, -1)), vm.shape)
            row["member_max_world_mm"] = [float(c.grid.centers_1d(i)[idx[i]]) for i in range(3)]
            row["member_max_dist_fixed_mm"] = float(dfix[idx])
            row["member_max_dist_load_mm"] = float(dload[idx])
            dkeep = ndimage.distance_transform_edt(~keep) * (h / k) if keep.any() else np.full(keep.shape, np.inf)
            row["member_max_dist_keep_mm"] = float(dkeep[idx])
            # 保持ボディ際(2h以内)の設計材料と、それ以外の部材域を分けた Q_V
            near_keep = member & (dkeep <= 2 * h)
            far = member & (dkeep > 2 * h)
            for name, reg in (("member_near_keep", near_keep), ("member_far_keep", far)):
                if reg.any():
                    row[name] = dict(volume_mm3=float(reg.sum() * (h / k) ** 3), max=float(vm[reg].max()),
                                     **{f"QV_{vf:g}w3": (gm.top_volume_value(vm, reg, (h / k) ** 3, vf * w ** 3)
                                                         if reg.sum() * (h / k) ** 3 > vf * w ** 3 else None)
                                        for vf in (0.125, 1.0)})
        cell = (h / k) ** 3
        # d・V の感度(部材域 = 保持外・固定面と荷重面から d 超)と、境界域(保持 ∨ 固定面・荷重面から d 以内)の球平均
        sweep = {}
        ball = gm.ball_mean_field(vm, m, w, h / k)
        for d in (1.0, 2.0, 3.0, 4.0, 6.0):
            mem = m & ~keep & (dfix > d) & (dload > d)
            bnd = m & (keep | (dfix <= d) | (dload <= d))
            row_d = {}
            for vf in (0.125, 0.5, 1.0, 2.0):
                row_d[f"QV_{vf:g}w3"] = (gm.top_volume_value(vm, mem, cell, vf * w ** 3)
                                         if mem.sum() * cell > vf * w ** 3 else None)
            if bnd.any():
                bi = np.unravel_index(np.argmax(np.where(bnd, ball, -1)), vm.shape)
                row_d["boundary_ball_w_max"] = float(ball[bi])
                row_d["boundary_ball_w_at_mm"] = [float(c.grid.centers_1d(i)[bi[i]]) for i in range(3)]
            sweep[f"d{d:g}"] = row_d
        row["sweep"] = sweep
        # 取り付きの公称応力: 拘束面ごとに、その面の節点から距離 (j·h, (j+1)·h] の殻の体積 ÷ h を断面積とみなし、
        # その面の反力の大きさで割る(曲げは入らない。細りの比較用の粗い指標)
        att = {}
        rx = row["reactions_n"]
        per_face = {f: gm.distance_to_points(c.grid, c.table.positions([f])) for f in c.cond.fixed_faces}
        nearest = np.argmin(np.stack([per_face[f] for f in c.cond.fixed_faces]), axis=0)
        for i, f in enumerate(c.cond.fixed_faces):
            df = per_face[f]
            own = m & (nearest == i)
            rmag = float(np.linalg.norm(rx.get(str(f), [0, 0, 0])))
            shells = []
            for j in range(int(np.ceil(6.0 / (h / k)))):
                shell = own & (df > j * h / k) & (df <= (j + 1) * h / k)
                area = float(shell.sum() * cell / (h / k))
                shells.append(dict(r_mm=(j + 1) * h / k, area_mm2=area, nominal_mpa=(rmag / area if area else None)))
            att[str(f)] = dict(reaction_n=rmag, shells=shells)
        row["attachment"] = att
        out["levels"][f"k{k}"] = row
        if k == 1:
            projection_png(vm, m, pa.OUT / "regions" / f"{stem}_k1.png", vmax=2 * target)
        r = row["regions"]
        print(f"k{k}", {n: (None if v is None else {x: round(v[x], 2) for x in ("max", "QV_w3", "over_target_mm3", "over20_mm3")
                                                    if v[x] is not None}) for n, v in r.items()},
              "member_max_at", row.get("member_max_world_mm"), flush=True)
    (pa.OUT / "regions" / f"{stem}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float),
                                                     encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
