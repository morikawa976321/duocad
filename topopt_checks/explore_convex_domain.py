"""保持・アンカーボディの外接箱の凸包で、保存済みの外接直方体マスクを切る予備調査。"""
import argparse
import itertools
import json
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np
from scipy import ndimage
from scipy.spatial import ConvexHull
from duocad import kernel
from duocad.session import Session
from duocad.topopt.trace import load_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    args = parser.parse_args()
    run = args.run
    session = Session(None, .2, models_dir=run)
    session.open_document('input.json')
    bundle = load_snapshot(next(run.glob('*-resolved-conditions.json')))['bundle']
    ids = sorted(set(bundle['anchor_body_ids']) | set(bundle['keep_body_ids']))
    corners = []
    for bid in ids:
        bounds = kernel.bounding_box(session.document.get_body(bid).shape)
        corners.extend(itertools.product(*zip(bounds['min'], bounds['max'])))
    corners = np.asarray(corners)
    hull = ConvexHull(corners)
    base = load_snapshot(sorted(run.glob('*-derive-bbox-output.json'))[0])
    grid = base['grid']
    indices = np.indices(grid['shape'], dtype=float)
    centers = [grid['origin'][i]+grid['spacing']*(indices[i]+.5) for i in range(3)]
    margin = .25*float(np.ptp(corners, axis=0).max())
    mask = base['mask'].copy()
    for a,b,c,d in hull.equations:
        mask &= a*centers[0]+b*centers[1]+c*centers[2]+d <= margin
    final = load_snapshot(next(run.glob('*-final-mesh-world.json')))
    rho = load_snapshot(sorted(run.glob('*-surface-input.json'))[-1])['rho']
    surface_coverage = {}
    for key in ('raw_vertices','vertices'):
        points = final[key]
        outside = np.any(points@hull.equations[:,:3].T+hull.equations[:,3]>margin, axis=1)
        surface_coverage[key] = dict(vertices=len(points), outside=int(outside.sum()))
    labels, count = ndimage.label(mask)
    keep = base['model']['keep']
    condition_labels = np.unique(labels[keep])
    connected = mask & np.isin(labels, condition_labels[condition_labels != 0])
    report = dict(method='Convex hull of preserve/anchor body bounding-box corners; offset each plane; intersect existing box-minus-obstacles mask',
                  source_run=run.name, margin_mm=margin, spacing_mm=grid['spacing'],
                  original_cells=int(base['mask'].sum()), candidate_cells=int(mask.sum()),
                  reduction_fraction=float(1-mask.sum()/base['mask'].sum()),
                  keep_cells_outside=int((keep & ~mask).sum()), components=int(count),
                  keep_component_labels=np.unique(labels[keep]).tolist(),
                  component_sizes=np.bincount(labels.ravel())[1:].tolist(),
                  condition_connected_cells=int(connected.sum()),
                  previous_density_outside_connected_mm3=float(rho[~connected].sum()*grid['spacing']**3),
                  previous_dense_cells_outside=int(((rho>.5)&~mask).sum()),
                  previous_density_integral_outside_mm3=float(rho[~mask].sum()*grid['spacing']**3),
                  previous_surface=surface_coverage,
                  limitation='Geometric coverage only; no FEM or optimality/clearance proof. Anchor body boxes are a conservative proxy for condition faces.')
    out = run/'analysis'/'convex_domain_proposal.json'
    out.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
