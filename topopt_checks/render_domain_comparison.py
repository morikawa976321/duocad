"""実行フォルダの入力から製品の固定凸包の塊を導出し、外枠・余白付き凸包・接続後を同じ縮尺で描く。

    .\\.venv\\Scripts\\python.exe topopt_checks\\render_domain_comparison.py output\\optimization_runs\\<実行ID>

塊は `optimize_run.build_conditions` → `design_space.derive` そのもの(このスクリプトに導出の複製は
無い)。3枚の領域は製品の部品で再計算する: 外枠 − 除外 / 余白付き凸包 − 除外 / 条件を含まない
成分を落とした確定マスク(`DesignSpace.mask`)。要素サイズは実行の `resolved-conditions` から読む。
出力は `analysis/domain_fixed_hull.png`(既存の解析画像は上書きしない)。
"""
import argparse
import itertools
import json
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np
from skimage.measure import marching_cubes
from PIL import Image, ImageDraw, ImageFont
from duocad import optimize_run
from duocad.session import Session
from duocad.topopt import design_space as ds
from duocad.topopt import voxelize as vx
from duocad.topopt.trace import load_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--out', type=Path, default=None)
    args = parser.parse_args()
    run = args.run
    spacing = float(load_snapshot(next(run.glob('*-resolved-conditions.json')))['spacing'])
    session = Session(None, .2, models_dir=run)
    session.open_document('input.json')
    bundle = optimize_run.build_conditions(session.document)
    space = ds.derive(bundle.conditions, spacing=spacing)
    grid = space.grid
    origin, spacing = np.asarray(grid.origin), grid.spacing
    frame = ds.box_mask(grid, np.asarray(space.bbox_min) - space.margin, np.asarray(space.bbox_max) + space.margin)
    hull = frame & ds.offset_hull_mask(grid, space.hull_planes, space.margin)
    obstacle = np.zeros(grid.shape, dtype=bool)
    if bundle.conditions.obstacle is not None:
        soup = vx.triangles_from_shape(vx._unwrap(bundle.conditions.obstacle), deviation=space.stats['deviation'])
        obstacle, _ = vx.occupancy(soup, grid)
    keep = space.model.keep
    masks = [frame & ~obstacle, hull & ~obstacle, space.mask]
    titles = ['基準：外枠(外接箱+余白) − 除外', '余白付き凸包 − 除外', '製品の塊：条件を含まない成分を除いた確定マスク']
    subtitles = ['凸包で切る前の直方体', f'凸包の各面を {space.margin:.1f} mm 外側へ押し出した領域', '材料量によらず固定。全候補がこれを使う']
    colors = [(131, 148, 164), (68, 128, 180), (33, 155, 139)]
    summary = dict(run=run.name, derivation=space.stats['derivation'], margin_mm=space.margin,
                   spacing_mm=spacing, grid_shape=list(grid.shape), cells=dict(space.stats['cells']),
                   component_sizes=space.stats['connectivity'].get('component_sizes'),
                   mask_hash=space.mask_hash)
    font_path = 'C:/Windows/Fonts/meiryo.ttc'
    fonts = {size: ImageFont.truetype(font_path, size) for size in (18, 22, 28, 35)}
    width, height = 720, 1040
    sheet = Image.new('RGB', (width*len(masks), height+130), '#f4f6f8')
    draw = ImageDraw.Draw(sheet)
    draw.text((32, 18), f'設計空間(固定凸包)  |  {run.name}', font=fonts[35], fill='#172d3e')
    draw.text((34, 73), '同じ格子・視点・縮尺。色のついた領域は「材料を置ける範囲」であり、最終製品ではありません。', font=fonts[22], fill='#465868')
    forward = np.array([2.7, -3., 1.6]); forward /= np.linalg.norm(forward)
    right = np.cross([0, 0, 1], forward); right /= np.linalg.norm(right)
    up = np.cross(forward, right)
    rot = np.stack([right, up, forward], axis=1)
    bounds = np.array(list(itertools.product(*zip(origin, origin+spacing*np.array(grid.shape)))))
    projected_bounds = bounds@rot
    center = (projected_bounds.min(0)+projected_bounds.max(0))/2
    scale = min((width-70)/np.ptp(projected_bounds[:, 0]), 430/np.ptp(projected_bounds[:, 1]))

    def surface(mask):
        v, f, _, _ = marching_cubes(np.pad(mask.astype(np.float32), 1), .5)
        return origin+(v-.5)*spacing, f
    keep_v, keep_f = surface(keep)

    def paint(im, vertices, faces, color, overlay=False):
        canvas = Image.new('RGBA', im.size, (0, 0, 0, 0)) if overlay else im
        d = ImageDraw.Draw(canvas)
        tri = vertices[faces]
        p = tri@rot-center
        pix = p[:, :, :2]*[scale, -scale]+[width/2, 355]
        n = np.cross(tri[:, 1]-tri[:, 0], tri[:, 2]-tri[:, 0])
        n /= np.maximum(np.linalg.norm(n, axis=1)[:, None], 1e-30)
        light = .48+.52*np.abs(n@forward)
        for i in np.argsort(p[:, :, 2].mean(1)):
            tint = tuple((np.array(color)*light[i]).astype(int))
            d.polygon([tuple(q) for q in pix[i]], fill=(*tint, 220) if overlay else tint)
        return Image.alpha_composite(im.convert('RGBA'), canvas).convert('RGB') if overlay else im
    for column, (mask, title, subtitle, color) in enumerate(zip(masks, titles, subtitles, colors)):
        panel = Image.new('RGB', (width, height), 'white')
        v, f = surface(mask)
        panel = paint(panel, v, f, color)
        panel = paint(panel, keep_v, keep_f, (245, 155, 42), True)
        d = ImageDraw.Draw(panel)
        d.text((25, 18), title, font=fonts[28], fill='#173247')
        d.text((25, 62), subtitle, font=fonts[18], fill='#526472')
        d.text((25, 94), f'{int(mask.sum()):,} セル  /  基準の {mask.sum()/masks[0].sum():.1%}', font=fonts[22], fill=color)
        d.text((25, 590), '側面からの投影（Y–Z）', font=fonts[22], fill='#173247')
        d.text((25, 625), '奥行き方向に1セルでもある領域を表示', font=fonts[18], fill='#526472')
        raster = np.full((grid.shape[2], grid.shape[1], 3), 244, dtype=np.uint8)
        raster[mask.any(axis=0).T[::-1]] = color
        raster[keep.any(axis=0).T[::-1]] = (245, 155, 42)
        side = Image.fromarray(raster).resize((414, 327), Image.Resampling.NEAREST)
        panel.paste(side, ((width-414)//2, 670))
        d = ImageDraw.Draw(panel)
        d.text((25, 1010), '橙：保持領域（上段は透視して重ね表示）', font=fonts[18], fill='#9c5b14')
        sheet.paste(panel, (column*width, 130))
    out = args.out or run/'analysis'/'domain_fixed_hull.png'
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    print(out, flush=True)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
