"""記録された形状の全景・局所比較と、選択候補の密度推移を保存する。"""
import argparse
import json
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np
from scipy import ndimage
from PIL import Image, ImageDraw, ImageFont
from duocad.topopt.trace import load_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    args = parser.parse_args()
    run, out = args.run, args.run/'analysis'
    events = [json.loads(s) for s in (run/'events.jsonl').read_text().splitlines()]
    snapshots = [e for e in events if e['kind']=='snapshot']
    def raw(e):
        return json.loads((run/e['file']).read_text(encoding='utf-8'))
    def load(e):
        return load_snapshot(run/e['file'])
    def last(name):
        return next(e for e in reversed(snapshots) if e['name']==name)
    stages = json.loads((out/'stages.json').read_text(encoding='utf-8'))
    surface = stages['selected_surface_phase']
    parent = next(e['phase'] for e in events if e['kind']=='begin' and e['child']==surface)
    simp = next(e['child'] for e in events if e['kind']=='begin' and e['name']=='simp' and e['phase']==parent)
    report = dict(candidates=[], selected_iterations=[], linear={})
    for phase in stages['phases']:
        if phase['name']=='safety-evaluation':
            index = len(report['candidates'])
            report['candidates'].append(dict(phase=phase['id'], seconds=phase['seconds'],
                                             **stages['result']['history'][index]))
        if phase['name']=='simp':
            output = next((e for e in snapshots if e['phase']==phase['id'] and e['name']=='simp-output'), None)
            if output:
                data = raw(output)
                print(phase['id'], len(data['history']), data['stopped_by'], data['history'][-1]['change'], flush=True)
    world = load(last('final-mesh-world'))
    cell_volume = world['grid']['spacing']**3
    for e in snapshots:
        if e['phase']!=simp or e['name']!='iteration-output':
            continue
        data = raw(e)
        iteration = data['iteration']['it']
        if iteration not in (1, 5, 10, 20, 30, 40):
            continue
        data = load(e)
        rho = data['rho']
        labels, count = ndimage.label(rho>.5)
        report['selected_iterations'].append(dict(iteration=iteration, components=int(count),
            dense_cells=int((rho>.5).sum()), gray_cells=int(((rho>0)&(rho<=.5)).sum()),
            density_integral_mm3=float(rho.sum()*cell_volume),
            low_density_integral_mm3=float(rho[rho<=.5].sum()*cell_volume)))
    solves = [raw(e) for e in snapshots if e['name']=='linear-solve']
    residuals = [raw(e)['relative_residual'] for e in snapshots if e['name']=='linear-residual']
    report['linear'] = dict(solves=len(solves), fallback=sum(bool(s['fallback']) for s in solves),
        seconds=sum(s['seconds'] for s in solves), max_residual=max(residuals),
        cg_iterations_min=min(s['cg_iterations'] for s in solves),
        cg_iterations_max=max(s['cg_iterations'] for s in solves))
    (out/'numerics.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(iterations=report['selected_iterations'], linear=report['linear'])), flush=True)
    intersections = json.loads((out/'intersections.json').read_text())
    points = []
    for row in intersections:
        lo = np.maximum(*[b['min'] for b in row['bounds']])
        hi = np.minimum(*[b['max'] for b in row['bounds']])
        points.append((lo+hi)/2)
    center = np.mean(points, axis=0)
    font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 23)
    def render(vertices, title, closeup=False):
        w,h = 850,700
        im = Image.new('RGB',(w,h),'#f3f6f9')
        draw = ImageDraw.Draw(im)
        tri = vertices[world['faces']]
        forward = np.array([2.,-3.,1.6]); forward /= np.linalg.norm(forward)
        right = np.cross([0,0,1],forward); right /= np.linalg.norm(right)
        up = np.cross(forward,right)
        rot = np.stack([right,up,forward],axis=1)
        mid = center.copy() if closeup else (vertices.min(0)+vertices.max(0))/2
        if not closeup:
            bounds = (np.concatenate([world['raw_vertices'], world['vertices']])-mid)@rot
            midpoint = (bounds.min(0)+bounds.max(0))/2
            mid = mid+rot@midpoint
        transformed = (tri-mid)@rot
        scale = 38 if closeup else min((w-100)/np.ptp(transformed[:,:,0]),(h-130)/np.ptp(transformed[:,:,1]))
        projected = transformed[:,:,:2]*[scale,-scale]+[w/2,h/2+25]
        normals = np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0])
        normals /= np.maximum(np.linalg.norm(normals,axis=1)[:,None],1e-30)
        shade = .3+.7*np.abs(normals@forward)
        for i in np.argsort(transformed[:,:,2].mean(1)):
            p = projected[i]
            if p[:,0].max()<0 or p[:,0].min()>w or p[:,1].max()<65 or p[:,1].min()>h:
                continue
            draw.polygon([tuple(q) for q in p],fill=tuple((np.array([100,165,193])*shade[i]).astype(int)),
                         outline='#557787' if closeup else None)
        marker = (center-mid)@rot
        x,y = marker[:2]*[scale,-scale]+[w/2,h/2+25]
        draw.ellipse((x-12,y-12,x+12,y+12),outline='#d83c35',width=3)
        draw.rectangle((0,0,w,62),fill='white')
        draw.text((20,18),title,font=font,fill='#203342')
        return im
    sheet = Image.new('RGB',(1700,1400),'white')
    for i,(key,title,zoom) in enumerate([
        ('raw_vertices','Before smoothing: valid single solid',False),
        ('vertices','After smoothing: self-interference',False),
        ('raw_vertices','Same location / raw triangles',True),
        ('vertices','Same location / after 30 iterations',True)]):
        sheet.paste(render(world[key],title,zoom),((i%2)*850,(i//2)*700))
    sheet.save(out/'geometry_comparison.png')
    print('Interference center:', center.tolist(), flush=True)


if __name__=='__main__':
    main()
