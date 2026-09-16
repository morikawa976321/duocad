"""保存済み現行設計空間と余白付き凸包案を同じ縮尺で比較描画する。"""
import argparse
import itertools
import json
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np
from scipy import ndimage
from scipy.spatial import ConvexHull
from skimage.measure import marching_cubes
from PIL import Image, ImageDraw, ImageFont
from duocad import kernel
from duocad.session import Session
from duocad.topopt.trace import load_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--refinement', action='store_true', help='部品ごとの余白案と元の新案を比較')
    args = parser.parse_args()
    run = args.run
    base = load_snapshot(sorted(run.glob('*-derive-bbox-output.json'))[0])
    candidate_path = sorted(run.glob('*-derive-candidate-output.json'))[-1]
    selected = json.loads(next(run.glob('*-selected-result.json')).read_text(encoding='utf-8'))
    candidate_json = json.loads(candidate_path.read_text(encoding='utf-8'))
    assert selected['result']['space']['mask'] == candidate_json['mask']
    current = load_snapshot(candidate_path)
    bundle = load_snapshot(next(run.glob('*-resolved-conditions.json')))['bundle']
    session = Session(None, .2, models_dir=run)
    session.open_document('input.json')
    corners = []
    local_corners, local_margins = [], []
    for bid in sorted(set(bundle['anchor_body_ids']) | set(bundle['keep_body_ids'])):
        bb = kernel.bounding_box(session.document.get_body(bid).shape)
        corners.extend(itertools.product(*zip(bb['min'], bb['max'])))
        lo, hi = np.asarray(bb['min']), np.asarray(bb['max'])
        local_margin = max(.25*float((hi-lo).max()), 10.8)
        local_corners.extend(itertools.product(*zip(lo-local_margin, hi+local_margin)))
        local_margins.append(dict(body=bid, margin_mm=local_margin))
    corners = np.asarray(corners)
    hull = ConvexHull(corners)
    margin = .25*np.ptp(corners, axis=0).max()
    grid = base['grid']
    origin, spacing = np.asarray(grid['origin']), grid['spacing']
    coords = np.indices(grid['shape'], dtype=float)
    xyz = [origin[i]+spacing*(coords[i]+.5) for i in range(3)]
    proposal = base['mask'].copy()
    for a,b,c,d in hull.equations:
        proposal &= a*xyz[0]+b*xyz[1]+c*xyz[2]+d <= margin
    keep = base['model']['keep']
    labels, _ = ndimage.label(proposal)
    needed = np.unique(labels[keep]); needed = needed[needed != 0]
    proposal &= np.isin(labels, needed)
    masks = [base['mask'], current['mask'], proposal]
    titles = ['基準：膨張外接箱', '現行：前回の採用候補', '新案：余白付き凸包']
    subtitles = ['除外領域を反映した元の空間', '材料量 67,957 mm³ に対応する空間', '材料量によらず固定する空間']
    colors = [(131,148,164),(68,128,180),(33,155,139)]
    if args.refinement:
        local_hull = ConvexHull(np.asarray(local_corners))
        refined = base['mask'].copy()
        for a,b,c,d in local_hull.equations:
            refined &= a*xyz[0]+b*xyz[1]+c*xyz[2]+d <= 1e-9
        local_labels, _ = ndimage.label(refined)
        used = np.unique(local_labels[keep]); used = used[used != 0]
        refined &= np.isin(local_labels, used)
        masks = [proposal, refined]
        titles = ['元の新案：全体に広い余白', '最終候補：部品ごとに余白']
        subtitles = [f'凸包の各面を {margin:.1f} mm 外側へ', '部品最長辺の25%、ただし10.8 mm以上']
        colors = [(33,155,139),(68,128,180)]
        rho = load_snapshot(sorted(run.glob('*-surface-input.json'))[-1])['rho']
        details = dict(original_cells=int(proposal.sum()), refined_cells=int(refined.sum()),
                       reduction_fraction=float(1-refined.sum()/proposal.sum()),
                       local_margins=local_margins, keep_cells_outside=int((keep&~refined).sum()),
                       components=int(ndimage.label(refined)[1]),
                       added_cells=int((refined&~proposal).sum()),
                       previous_dense_cells_outside=int(((rho>.5)&~refined).sum()),
                       previous_density_outside_mm3=float(rho[~refined].sum()*spacing**3),
                       note='Geometric trial only; padding values are provisional; no structural reanalysis.')
        (run/'analysis'/'domain_refinement.json').write_text(json.dumps(details,indent=2),encoding='utf-8')
        print(json.dumps(details),flush=True)
    font_path = 'C:/Windows/Fonts/meiryo.ttc'
    fonts = {size: ImageFont.truetype(font_path,size) for size in (18,22,28,35)}
    width, height = 720, 1040
    sheet = Image.new('RGB',(width*len(masks), height+130),'#f4f6f8')
    draw = ImageDraw.Draw(sheet)
    draw.text((32,18),'設計空間の比較  |  hanger_bar_attachment',font=fonts[35],fill='#172d3e')
    draw.text((34,73),'同じ格子・視点・縮尺。色のついた領域は「材料を置ける範囲」であり、最終製品ではありません。',font=fonts[22],fill='#465868')
    forward = np.array([2.7,-3.,1.6]); forward /= np.linalg.norm(forward)
    right = np.cross([0,0,1],forward); right /= np.linalg.norm(right)
    up = np.cross(forward,right)
    rot = np.stack([right,up,forward],axis=1)
    bounds = np.array(list(itertools.product(*zip(origin, origin+spacing*np.array(grid['shape'])))))
    projected_bounds = bounds@rot
    center = (projected_bounds.min(0)+projected_bounds.max(0))/2
    scale = min((width-70)/np.ptp(projected_bounds[:,0]),430/np.ptp(projected_bounds[:,1]))
    def surface(mask):
        v,f,_,_ = marching_cubes(np.pad(mask.astype(np.float32),1),.5)
        return origin+(v-.5)*spacing, f
    keep_v, keep_f = surface(keep)
    def paint(im,vertices,faces,color,overlay=False):
        canvas = Image.new('RGBA',im.size,(0,0,0,0)) if overlay else im
        d = ImageDraw.Draw(canvas)
        tri = vertices[faces]
        p = tri@rot-center
        pix = p[:,:,:2]*[scale,-scale]+[width/2,355]
        n = np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0])
        n /= np.maximum(np.linalg.norm(n,axis=1)[:,None],1e-30)
        light = .48+.52*np.abs(n@forward)
        for i in np.argsort(p[:,:,2].mean(1)):
            tint = tuple((np.array(color)*light[i]).astype(int))
            d.polygon([tuple(q) for q in pix[i]],fill=(*tint,220) if overlay else tint)
        return Image.alpha_composite(im.convert('RGBA'),canvas).convert('RGB') if overlay else im
    for column,(mask,title,subtitle,color) in enumerate(zip(masks,titles,subtitles,colors)):
        panel = Image.new('RGB',(width,height),'white')
        v,f = surface(mask)
        panel = paint(panel,v,f,color)
        panel = paint(panel,keep_v,keep_f,(245,155,42),True)
        d = ImageDraw.Draw(panel)
        d.text((25,18),title,font=fonts[28],fill='#173247')
        d.text((25,62),subtitle,font=fonts[18],fill='#526472')
        d.text((25,94),f'{int(mask.sum()):,} セル  /  基準の {mask.sum()/masks[0].sum():.1%}',font=fonts[22],fill=color)
        d.text((25,590),'側面からの投影（Y–Z）',font=fonts[22],fill='#173247')
        d.text((25,625),'奥行き方向に1セルでもある領域を表示',font=fonts[18],fill='#526472')
        raster = np.full((grid['shape'][2],grid['shape'][1],3),244,dtype=np.uint8)
        if args.refinement:
            raster[proposal.any(axis=0).T[::-1]] = (221,227,232)
        raster[mask.any(axis=0).T[::-1]] = color
        raster[keep.any(axis=0).T[::-1]] = (245,155,42)
        if args.refinement and column == 1:
            raster[((rho>.5)&~refined).any(axis=0).T[::-1]] = (216,55,65)
        side = Image.fromarray(raster).resize((414,327),Image.Resampling.NEAREST)
        panel.paste(side,((width-414)//2,670))
        d = ImageDraw.Draw(panel)
        legend = '橙：保持 ／ 薄灰：元の新案 ／ 赤：前回の高密度部の除外' if args.refinement else '橙：保持領域（上段は透視して重ね表示）'
        d.text((25,1010),legend,font=fonts[18],fill='#9c5b14')
        sheet.paste(panel,(column*width,130))
    out = run/'analysis'/('domain_refinement.png' if args.refinement else 'domain_comparison.png')
    sheet.save(out)
    print(out, flush=True)
    print([int(mask.sum()) for mask in masks],flush=True)


if __name__ == '__main__':
    main()
