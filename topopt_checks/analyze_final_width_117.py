"""Issue #117 の段階別形状・小型梁FEM。既存の最適化結果は上書きしない。

.venv/Scripts/python.exe topopt_checks/analyze_final_width_117.py synthetic
.venv/Scripts/python.exe topopt_checks/analyze_final_width_117.py real --shape S2_hanger200_step20
.venv/Scripts/python.exe topopt_checks/analyze_final_width_117.py fem --spacing 0.5
.venv/Scripts/python.exe topopt_checks/analyze_final_width_117.py real-fem --shape S2_hanger200_step20
"""
import argparse
import json
import math
from pathlib import Path
import subprocess
import sys
import time

import _bootstrap  # noqa: F401
import numpy as np
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt

from duocad.topopt import mesh, min_width, thin_voids, voxelize
from duocad.topopt.finalize import finalize, final_mesh_checks
from duocad.topopt.material import DEFAULT_ALLOWABLE_MPA, DEFAULT_YOUNGS_MODULUS_MPA

OUT = Path('output/final_width_117')


def save(name, record):
    OUT.mkdir(parents=True, exist_ok=True)
    record['code_commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    (OUT / f'{name}.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(name, 'saved', flush=True)


def occupancy(vertices, faces, grid):
    soup = voxelize.TriangleSoup(positions=np.asarray(vertices), triangles=np.asarray(faces),
                                face_index=np.zeros(len(faces), dtype=np.int64))
    return voxelize.occupancy(soup, grid, allow_empty=True)[0]


def upsample(mask, factor):
    for axis in range(3):
        mask = np.repeat(mask, factor, axis=axis)
    return mask


def width_rows(mask, exempt, spacing, sides):
    rows = []
    for side in sorted(set(sides)):
        lost = mask & ~min_width.cube_opening(mask, side)
        if exempt is not None:
            lost &= ~exempt
        coords = np.argwhere(lost)
        rows.append(dict(side_cells=side, side_mm=side*spacing, lost_cells=int(lost.sum()),
                         lost_fraction=float(lost.sum()/mask.sum()),
                         lost_centroid_cells=coords.mean(axis=0).tolist() if len(coords) else None))
    return rows


def z_crossings(vertices, faces, x, y):
    tri = vertices[faces]
    a, b = tri[:, 1, :2]-tri[:, 0, :2], tri[:, 2, :2]-tri[:, 0, :2]
    p = np.array([x, y])-tri[:, 0, :2]
    det = a[:, 0]*b[:, 1]-a[:, 1]*b[:, 0]
    valid = np.abs(det) > 1e-14
    u = np.zeros(len(tri)); v = u.copy()
    u[valid] = (p[valid, 0]*b[valid, 1]-p[valid, 1]*b[valid, 0])/det[valid]
    v[valid] = (a[valid, 0]*p[valid, 1]-a[valid, 1]*p[valid, 0])/det[valid]
    hit = valid & (u >= -1e-10) & (v >= -1e-10) & (u+v <= 1+1e-10)
    zs = tri[:, 0, 2]+u*(tri[:, 1, 2]-tri[:, 0, 2])+v*(tri[:, 2, 2]-tri[:, 0, 2])
    return np.unique(np.round(zs[hit], 10)).tolist()


def synthetic():
    rows = []
    for thickness in (2, 4, 6):
        mask = np.ones((32, 12, thickness), bool)
        base = mesh.build_surface_from_mask(mask, n_iter=0)
        for iterations in (0, 1, 5, 15, 30):
            v = mesh.taubin_smooth(base.verts_raw, base.faces, n_iter=iterations)
            crossings = {str(x): z_crossings(v, base.faces, x, 6.125) for x in (0.5, 2, 4, 8, 16)}
            fine = occupancy(v, base.faces, voxelize.Grid((-2., -2., -2.), .25, (144, 64, 4*thickness+16)))
            rows.append(dict(thickness=thickness, iterations=iterations,
                             volume=mesh.mesh_volume(v, base.faces), voxel_volume=int(mask.sum()),
                             z_crossings=crossings,
                             widths=width_rows(fine, None, .25, [4*thickness-2, 4*thickness-1, 4*thickness])))
    save('synthetic', dict(rows=rows))


def real(shape):
    sys.path.insert(0, str(Path(__file__).resolve().parent / 'idle_validation'))
    import final_shapes_09 as fs
    from duocad.topopt import binary_search
    ld = fs.load_shape(shape)
    ctx, original = ld['ctx'], ld['mask']
    grid, cond = ctx.grid, ctx.cond
    filled, fill = thin_voids.fill_tubes(original, ctx.space.mask)
    keep = binary_search._keep_cells(cond, original.shape)
    surface = mesh.build_surface_from_mask(filled)
    raw = grid.origin + surface.verts_raw*grid.spacing
    smooth = grid.origin + surface.verts_smooth*grid.spacing
    doc, bundle = ld['doc'], ld['bundle']
    v, f = finalize(smooth, surface.faces,
                    [doc.get_body(b).shape for b in bundle.keep_body_ids],
                    [doc.get_body(b).shape for b in bundle.exclude_body_ids])
    stages = dict(raw=(raw, surface.faces), smooth=(smooth, surface.faces), final=(v, f))
    record = dict(shape=shape, requested_mm=ld['spec']['width_mm'], grid=grid.as_dict(), fill=fill,
                  original_width_ok=binary_search._mask_satisfies_min_width(original, ctx.min_width_cells, keep),
                  filled_width_ok=binary_search._mask_satisfies_min_width(filled, ctx.min_width_cells, keep),
                  final_checks=final_mesh_checks(v, f), rows=[])
    OUT.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(OUT/f'{shape}_stages.npz', original=original, filled=filled, keep=keep,
                        raw=raw, smooth=smooth, faces=surface.faces, final_vertices=v, final_faces=f)
    for factor in (2, 4):
        fine_grid = voxelize.Grid(tuple(np.asarray(grid.origin)-2*grid.spacing), grid.spacing/factor,
                                  tuple((n+4)*factor for n in grid.shape))
        exempt = upsample(np.pad(keep, 2), factor)
        requested_cells = record['requested_mm']/fine_grid.spacing
        sides = [math.floor(requested_cells), math.ceil(requested_cells), ctx.min_width_cells*factor]
        for stage in ('original', 'filled', 'raw', 'smooth', 'final'):
            fine = upsample(np.pad(original if stage == 'original' else filled, 2), factor) if stage in ('original', 'filled') else occupancy(*stages[stage], fine_grid)
            row = dict(stage=stage, factor=factor, sampled_volume_mm3=float(fine.sum()*fine_grid.element_volume),
                       widths=width_rows(fine, exempt, fine_grid.spacing, sides))
            if stage in stages:
                row['mesh_volume_mm3'] = mesh.mesh_volume(*stages[stage])
            record['rows'].append(row)
            save(shape, record)


def beam_stages():
    mask = np.ones((24, 8, 4), bool)
    surface = mesh.build_surface_from_mask(mask)
    keep = [BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(3, 8, 4)).Shape(),
            BRepPrimAPI_MakeBox(gp_Pnt(21, 0, 0), gp_Pnt(24, 8, 4)).Shape()]
    exclude = [BRepPrimAPI_MakeBox(gp_Pnt(-3, -3, -3), gp_Pnt(0, 11, 7)).Shape(),
               BRepPrimAPI_MakeBox(gp_Pnt(24, -3, -3), gp_Pnt(27, 11, 7)).Shape()]
    return {name: finalize(v, surface.faces, keep, exclude)
            for name, v in [('raw_final', surface.verts_raw), ('smooth_final', surface.verts_smooth)]}


def fem(spacing):
    from duocad.topopt import anchor, fem as solver, stress
    grid = voxelize.Grid((0., -2., -2.), spacing, tuple(round(n/spacing) for n in (24, 12, 8)))
    stages = beam_stages()
    rows = []
    for stage in ('voxel', 'raw_final', 'smooth_final'):
        started = time.perf_counter()
        y0, y1, z0, z1 = [round(n/spacing) for n in (2, 10, 2, 6)]
        if stage == 'voxel':
            material = np.zeros(grid.shape, bool)
            material[:, y0:y1, z0:z1] = True
        else:
            material = occupancy(*stages[stage], grid)
            vertices = stages[stage][0]
            assert np.all(vertices.min(axis=0) >= np.array(grid.origin)-1e-10)
            assert np.all(vertices.max(axis=0) <= np.array(grid.origin)+np.array(grid.shape)*spacing+1e-10)
        ids = np.arange(np.prod(np.array(grid.shape)+1)).reshape(tuple(n+1 for n in grid.shape))
        support = ids[0, y0:y1+1, z0:z1+1].ravel()
        fixed = (support[:, None]*3 + np.arange(3)).ravel()
        loaded = ids[-1, y0:y1+1, z0:z1+1].ravel()
        weights = np.ones((y1-y0+1, z1-z0+1)); weights[[0, -1], :] *= .5; weights[:, [0, -1]] *= .5
        load = np.zeros(ids.size*3)
        load[loaded*3+2] = -10 * weights.ravel()/weights.sum()
        active = anchor.active_nodes(material)
        assert active.ravel()[support].all() and active.ravel()[loaded].all()
        model = solver.Model(*grid.shape, fixed_dofs=fixed, load=load,
                             extra_fixed_dofs=anchor.floating_dofs(material), active_mask=material)
        info = {}
        u = stress.solve_displacement(model, material.astype(float), solver='cg', info=info)
        residual = model.stiffness_apply(material.astype(float), stress.PENAL, u)-model.f
        relative = float(np.linalg.norm(residual[model.free])/np.linalg.norm(model.f[model.free]))
        assert relative <= solver.SOLVE_RESIDUAL_TOL
        report = stress.evaluate(model, material.astype(float),
                                 scale=stress.UnitScale(spacing, 1., DEFAULT_YOUNGS_MODULUS_MPA),
                                 allowable_mpa=DEFAULT_ALLOWABLE_MPA, u=u)
        x = (np.arange(grid.shape[0])+.5)*spacing
        interior = material & ((x >= 4) & (x <= 20))[:, None, None]
        rows.append(dict(stage=stage, spacing=spacing, cells=int(material.sum()), volume_mm3=float(material.sum()*grid.element_volume),
                         safety_factor=report.safety_factor, representative_mpa=report.representative_mpa,
                         material_max_mpa=report.representative.dense_max,
                         interior_max_mpa=float(report.field.von_mises[interior].max()),
                         max_displacement_mm=report.max_displacement_mm, residual=relative,
                         solver=info, seconds=time.perf_counter()-started))
        save(f'beam_fem_h{spacing}', dict(load_n=10, youngs_modulus_mpa=DEFAULT_YOUNGS_MODULUS_MPA,
                                        allowable_mpa=DEFAULT_ALLOWABLE_MPA, rows=rows))


def real_fem(shape):
    import psutil
    from duocad.topopt import acceptance, stress
    if psutil.virtual_memory().available < 3 * 2**30:
        raise RuntimeError('FEM開始に必要な空き3GBを確保できない')
    sys.path.insert(0, str(Path(__file__).resolve().parent / 'idle_validation'))
    import final_shapes_09 as fs
    ld = fs.load_shape(shape)
    ctx = ld['ctx']; cond = ctx.cond
    saved = np.load(OUT/f'{shape}_stages.npz')
    rows = []
    for stage in ('filled', 'final'):
        material = saved['filled'] if stage == 'filled' else occupancy(saved['final_vertices'], saved['final_faces'], ctx.grid)
        conditions, fixed, load = acceptance.condition_check(material, ctx.table, cond.fixed_faces, cond.load_faces, cond.forces)
        row = dict(stage=stage, conditions=conditions, cells=int(material.sum()))
        if conditions['status'] == 'pass':
            row['safety'] = acceptance.safety_check(material, ctx.grid, fixed, load,
                target=cond.target_safety_factor, allowable_mpa=cond.allowable_mpa,
                youngs_modulus_mpa=cond.youngs_modulus_mpa,
                percentile=cond.percentile or stress.DEFAULT_PERCENTILE, solver=cond.solver)
        rows.append(row)
        save(f'{shape}_fem', dict(grid=ctx.grid.as_dict(), rows=rows,
                                 peak_working_set_mb=psutil.Process().memory_info().peak_wset/2**20))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['synthetic', 'real', 'fem', 'real-fem'])
    parser.add_argument('--shape', default='S2_hanger200_step20')
    parser.add_argument('--spacing', type=float, default=.5)
    args = parser.parse_args()
    if args.mode == 'synthetic': synthetic()
    elif args.mode == 'real': real(args.shape)
    elif args.mode == 'real-fem': real_fem(args.shape)
    else: fem(args.spacing)
