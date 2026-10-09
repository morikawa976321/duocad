"""2つの採用形を、平滑化した表面に応力の色を塗って並べた PNG にする(Pillow だけ。研究用)。

色は探索格子の要素中心の von Mises [MPa](三角形の重心を含むセル、材料でなければ最寄りの材料セル)。
左右で同じ色の範囲を使う。

    .venv\\Scripts\\python.exe topopt_checks\\stress_gate\\render_compare.py --model hanger --load 1000 --h 2.4 --w 10 \\
        --left <マスク npz> "99.5%点" --right-run <製品の実行記録フォルダ> "部材域" --out <png>
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
from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from scipy import ndimage  # noqa: E402

import search_methods as smeth  # noqa: E402
from duocad.topopt import mesh as topo_mesh, trace, voxelize  # noqa: E402

sys.path.insert(0, str(HERE.parent))
from render_mask_png import colormap, render_view  # noqa: E402

FONT = "C:/Windows/Fonts/meiryo.ttc"


def surface_from_mask(mask, grid):
    s = topo_mesh.build_surface_from_mask(mask)
    return np.asarray(grid.origin) + np.asarray(s.verts_smooth) * grid.spacing, np.asarray(s.faces)


def surface_from_run(run: Path):
    snap = sorted(run.glob("*-pre-boolean-mesh-world.json"))[-1]
    data = trace.load_snapshot(snap)
    return np.asarray(data["vertices"], float), np.asarray(data["faces"], int)


def triangle_stress(v, f, mask, vm, grid):
    centroid = v[f].mean(axis=1)
    idx = np.floor((centroid - np.asarray(grid.origin)) / grid.spacing).astype(int)
    idx = np.clip(idx, 0, np.asarray(grid.shape) - 1)
    _, nearest = ndimage.distance_transform_edt(~mask, return_indices=True)
    i, j, k = (nearest[a][idx[:, 0], idx[:, 1], idx[:, 2]] for a in range(3))
    return vm[i, j, k]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, choices=list(pa.MODELS))
    ap.add_argument("--load", type=float, default=None)
    ap.add_argument("--h", type=float, default=None)
    ap.add_argument("--w", type=float, default=None)
    ap.add_argument("--left", nargs=2, required=True, metavar=("MASK", "LABEL"))
    ap.add_argument("--right-mask", nargs=2, metavar=("MASK", "LABEL"))
    ap.add_argument("--right-run", nargs=2, metavar=("RUN", "LABEL"))
    ap.add_argument("--vmax", type=float, default=20.0)
    ap.add_argument("--size", type=int, default=520)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    spec = dict(pa.MODELS[a.model])
    for key, val in (("load_n", a.load), ("h", a.h), ("w", a.w)):
        if val is not None:
            spec[key] = val
    ctx = smeth.build_context(spec["model"], load_n=spec["load_n"], element_size_mm=spec["h"], min_width_mm=spec["w"])
    grid, keep = ctx.grid, np.asarray(ctx.cond.keep_cells, bool)
    panels = []
    sides = [("mask", *a.left)]
    if a.right_mask:
        sides.append(("mask", *a.right_mask))
    if a.right_run:
        sides.append(("run", *a.right_run))
    for kind, src, label in sides:
        if kind == "mask":
            with np.load(src) as z:
                mask = np.asarray(z["mask"], bool)
            v, f = surface_from_mask(mask, grid)
        else:
            v, f = surface_from_run(Path(src))
            soup = voxelize.TriangleSoup(positions=v, triangles=f, face_index=np.zeros(len(f), dtype=np.int64))
            mask = (voxelize.occupancy(soup, grid, allow_empty=True)[0] & np.asarray(ctx.space.mask, bool)) | keep
        vm, _ = pa.solve(mask, ctx)
        val = triangle_stress(v, f, mask, vm, grid) / a.vmax
        rgb = colormap(val)
        views = [render_view(v, f, rgb, el, az, a.size) for el, az in ((20, -60), (0, 0), (90, 0))]
        panels.append((label, float(mask.sum() * grid.element_volume), views, float(np.percentile(vm[mask], 99.5))))
    font = ImageFont.truetype(FONT, 26)
    small = ImageFont.truetype(FONT, 20)
    head = 70
    canvas = Image.new("RGB", (a.size * 3, (a.size + head) * len(panels) + 60), "white")
    d = ImageDraw.Draw(canvas)
    for r, (label, volume, views, p995) in enumerate(panels):
        y = r * (a.size + head)
        d.text((12, y + 10), f"{label}   材料 {volume:,.0f} mm³   (斜め・正面・上から)", fill="black", font=font)
        for c, im in enumerate(views):
            canvas.paste(im, (c * a.size, y + head))
    # 色の目盛り
    y = (a.size + head) * len(panels) + 10
    bar = (colormap(np.linspace(0, 1, 400)) * 255).astype(np.uint8)
    canvas.paste(Image.fromarray(np.repeat(bar[None], 24, axis=0)), (12, y))
    d.text((420, y), f"0 → {a.vmax:g} MPa 以上(探索格子の要素中心の von Mises、{spec['load_n']:g} N)", fill="black", font=small)
    canvas.save(a.out)
    print(json.dumps([dict(label=p[0], material_mm3=p[1], p995_mpa=p[3]) for p in panels], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
