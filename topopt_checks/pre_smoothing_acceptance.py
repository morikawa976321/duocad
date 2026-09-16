"""島除去後・平滑化前の6条件。製品の最適化処理や入力配列は変更しない。"""
from dataclasses import asdict, is_dataclass
import hashlib
import math

import numpy as np

from duocad.topopt import anchor, safety_loop, simp, stress, voxelize
from topopt_checks.pre_smoothing_geometry import (
    active_nodes, boundary_check, connectivity_check, region_check,
)


def _positive(value, name):
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} must be finite and positive')


def condition_check(material, table, fixed_faces, load_faces, forces):
    live = active_nodes(material).ravel()
    rows = []
    fixed_faces, load_faces = tuple(fixed_faces), tuple(load_faces)
    forces = np.asarray(forces, dtype=float)
    if not fixed_faces or not load_faces or forces.shape != (len(load_faces), 3):
        raise ValueError('拘束・荷重面と荷重ベクトルの対応が不正')
    if not np.isfinite(forces).all() or not np.any(forces):
        raise ValueError('荷重が非有限または全てゼロ')
    load = np.zeros(3 * live.size)
    fixed = []
    for kind, faces in (('fixed', fixed_faces), ('load', load_faces)):
        for index, face in enumerate(faces):
            ids = table.node_ids([face])
            lost = ids[~live[ids]]
            row = dict(condition=f'{kind}_face_{face}', nodes=int(ids.size),
                       lost_nodes=int(lost.size), status='pass' if ids.size and not lost.size else 'fail')
            if kind == 'fixed':
                if ids.size:
                    fixed.extend(table.fixed_dofs([face]).tolist())
            elif ids.size:
                vector = table.load_vector([face], forces[index])
                row['requested_force_n'] = forces[index].tolist()
                row['lost_force_l1_n'] = float(np.abs(vector.reshape(-1, 3)[~live]).sum())
                row['resultant_n'] = vector.reshape(-1, 3).sum(axis=0).tolist()
                if not np.allclose(row['resultant_n'], forces[index], atol=1e-10, rtol=1e-10):
                    row['status'] = 'fail'
                load += vector
            rows.append(row)
    fixed = np.unique(fixed).astype(np.int64)
    # 各荷重面を個別に点検してから加算する。相反する荷重の脱落を相殺で隠さない。
    supported = bool(rows) and all(row['status'] == 'pass' for row in rows)
    return dict(status='pass' if supported else 'fail', faces=rows), fixed, load


def safety_check(material, grid, fixed, load, *, target, allowable_mpa, youngs_modulus_mpa,
                 percentile, solver):
    try:
        live = active_nodes(material).ravel()
        active_dofs = np.repeat(live, 3)
        physical_fixed = np.intersect1d(fixed, np.flatnonzero(active_dofs))
        node_indices = np.array(np.unravel_index(physical_fixed // 3, tuple(n + 1 for n in material.shape))).T
        positions = node_indices - node_indices.mean(axis=0)
        rigid = np.zeros((len(physical_fixed), 6))
        for i, (position, dof) in enumerate(zip(positions, physical_fixed)):
            axis = dof % 3
            rigid[i, axis] = 1
            for rotation in range(3):
                rigid[i, 3 + rotation] = np.cross(np.eye(3)[rotation], position)[axis]
        if np.linalg.matrix_rank(rigid) < 6:
            return dict(status='fail', reason='rigid_body_modes', message='剛体運動を拘束できない')
        model = simp.Model(*material.shape, fixed_dofs=fixed, load=load,
                           extra_fixed_dofs=np.flatnonzero(~active_dofs), active_mask=material)
        density = material.astype(float)
        displacement = stress.solve_displacement(model, density, solver=solver)
        residual = model.stiffness_apply(density, 3.0, displacement) - model.f
        relative = float(np.linalg.norm(residual[model.free]) / np.linalg.norm(model.f[model.free]))
        if not np.isfinite(displacement).all() or not math.isfinite(relative) or relative > simp.SOLVE_RESIDUAL_TOL:
            raise RuntimeError(f'残差検算に失敗: {relative}')
        # load は既に N。相殺後の節点力で再正規化せず、単位格子の長さだけ換算する。
        scale = stress.UnitScale(grid.spacing, 1.0, youngs_modulus_mpa, solved_load=1.0)
        report = stress.evaluate(model, density, scale=scale, allowable_mpa=allowable_mpa,
                                 u=displacement, percentile=percentile)
        sf = float(report.safety_factor)
        if not math.isfinite(sf):
            raise RuntimeError('安全率が非有限')
        return dict(status='pass' if sf >= target else 'fail', safety_factor=sf,
                    target_safety_factor=target, representative_mpa=report.representative_mpa,
                    allowable_mpa=allowable_mpa, youngs_modulus_mpa=youngs_modulus_mpa,
                    raw_max_mpa=report.raw_max_mpa, percentile=percentile,
                    max_displacement_mm=report.max_displacement_mm, relative_residual=relative,
                    residual_tolerance=simp.SOLVE_RESIDUAL_TOL, solver=solver,
                    evaluated_material_cells=int(material.sum()), density_rule='binary, active elements only')
    except Exception as error:
        return dict(status='unknown', reason='evaluation_error', error=f'{type(error).__name__}: {error}')


def search_diagnostics(history=None, *, converged=None, max_evaluations=None, target=None):
    if history is None:
        return dict(status='unknown', termination='unknown', minimum_proven=False,
                    note='探索履歴なし。形状の合否と探索完了は別判定。')
    rows = [asdict(row) if is_dataclass(row) else dict(row) for row in history]
    valid, errors = [], []
    for i, row in enumerate(rows):
        volume, sf = row.get('material_mm3'), row.get('safety_factor')
        if (row.get('outcome') == 'ok' and isinstance(volume, (int, float)) and
                math.isfinite(volume) and volume > 0 and isinstance(sf, (int, float)) and math.isfinite(sf) and sf > 0):
            valid.append((volume, sf, i))
        else:
            errors.append(i)
    valid.sort()
    inversions = []
    for a, b in zip(valid, valid[1:]):
        if a[0] < b[0] and a[1] > b[1] + 1e-9 * max(a[1], b[1]):
            inversions.append(dict(smaller_evaluation=a[2], larger_evaluation=b[2]))
    termination = ('reported_converged' if converged is True else
                   'evaluation_limit' if max_evaluations is not None and len(rows) >= max_evaluations else 'unknown')
    bracket = None
    if target is not None:
        safe = [v for v, sf, _ in valid if sf >= target]
        if safe:
            upper = min(safe)
            unsafe = [v for v, sf, _ in valid if sf < target and v < upper]
            if unsafe:
                lower = max(unsafe)
                bracket = dict(lower_mm3=lower, upper_mm3=upper, relative_gap=(upper - lower) / upper)
            elif valid and upper == min(v for v, _, _ in valid) and converged is True:
                termination = 'lowest_evaluated_already_safe'
    return dict(status='warning' if errors or inversions else 'recorded', termination=termination,
                minimum_proven=False, history=rows, failed_or_unevaluated=errors,
                monotonicity_inversions=inversions, volume_basis='requested density integral mm3',
                observed_bracket=bracket,
                note='収束フラグは元の探索の申告。最終二値体積の最小性は保証しない。')


def inspect(rho_clean, rho_before, grid, *, table, fixed_faces, load_faces, forces,
            preserves, excludes, target_safety_factor, allowable_mpa=safety_loop.DEFAULT_ALLOWABLE_MPA,
            youngs_modulus_mpa=safety_loop.DEFAULT_YOUNGS_MODULUS_MPA, level=0.5, percentile=stress.DEFAULT_PERCENTILE,
            solver='cg', history=None, converged=None, max_evaluations=None,
            volume_atol=1e-9, volume_rtol=1e-9):
    for name, value in [('spacing', grid.spacing), ('target', target_safety_factor),
                        ('allowable_mpa', allowable_mpa), ('youngs_modulus_mpa', youngs_modulus_mpa)]:
        _positive(value, name)
    if not np.isfinite(grid.origin).all() or not 0 <= level < 1 or not 0 < percentile <= 100:
        raise ValueError('原点・閾値・応力パーセンタイルが不正')
    if any(not math.isfinite(v) or v < 0 for v in (volume_atol, volume_rtol)):
        raise ValueError('体積許容差が不正')
    before, clean = np.asarray(rho_before, dtype=float), np.asarray(rho_clean, dtype=float)
    for field in (before, clean):
        if field.shape != tuple(grid.shape) or not np.isfinite(field).all() or np.any((field < 0) | (field > 1)):
            raise ValueError('密度場の形・値が不正')
    if tuple(table.node_grid.grid.shape) != tuple(grid.shape) or table.node_grid.grid != grid:
        raise ValueError('アンカーと検査格子が一致しない')
    material, original = clean > level, before > level
    if np.any(material & ~original):
        raise ValueError('島除去後に材料セルが増えている')
    checks = dict(watertight=boundary_check(material), single_solid=connectivity_check(material),
                  preserve=region_check(material, grid, preserves, preserve=True, volume_atol=volume_atol, volume_rtol=volume_rtol),
                  exclude=region_check(material, grid, excludes, preserve=False, volume_atol=volume_atol, volume_rtol=volume_rtol))
    checks['conditions'], fixed, load = condition_check(material, table, fixed_faces, load_faces, forces)
    if all(checks[key]['status'] == 'pass' for key in ('watertight', 'single_solid', 'conditions')):
        checks['safety'] = safety_check(material, grid, fixed, load, target=target_safety_factor,
                                       allowable_mpa=allowable_mpa, youngs_modulus_mpa=youngs_modulus_mpa,
                                       percentile=percentile, solver=solver)
    else:
        checks['safety'] = dict(status='unknown', reason='prerequisites_failed')
    statuses = [check['status'] for check in checks.values()]
    status = 'fail' if 'fail' in statuses else 'unknown' if 'unknown' in statuses else 'pass'
    return dict(schema_version=1, status=status, exit_code={'pass': 0, 'fail': 1, 'unknown': 2}[status],
                stage='post-island-removal, pre-smoothing', level=level, threshold_rule='rho > level',
                material_sha256=hashlib.sha256(material.tobytes()).hexdigest(),
                grid=grid.as_dict(), checks=checks,
                volumes=dict(material_mm3=float(material.sum() * grid.element_volume),
                             before_mm3=float(original.sum() * grid.element_volume),
                             removed_islands_mm3=float((original & ~material).sum() * grid.element_volume),
                             density_integral_before_mm3=float(before.sum() * grid.element_volume),
                             density_integral_after_mm3=float(clean.sum() * grid.element_volume)),
                search=search_diagnostics(history, converged=converged, max_evaluations=max_evaluations,
                                          target=target_safety_factor))


def inspect_document(document, rho_clean, rho_before, grid, **kwargs):
    from duocad.optimize_run import build_conditions
    bundle = build_conditions(document)
    soup = voxelize.triangles_from_shape(voxelize._unwrap(bundle.conditions.anchor),
                                         deviation=grid.spacing * voxelize.DEFAULT_DEVIATION_RATIO)
    table = anchor.build_anchors(soup, grid)
    setup = document.optimize
    return inspect(rho_clean, rho_before, grid, table=table,
                   fixed_faces=bundle.conditions.fixed_faces, load_faces=bundle.conditions.load_faces,
                   forces=bundle.forces,
                   preserves=[(name, document.get_body(name).shape) for name in setup.preserve_bodies],
                   excludes=[(name, document.get_body(name).shape) for name in setup.exclude_bodies], **kwargs)
