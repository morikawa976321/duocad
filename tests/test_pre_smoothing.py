import importlib.util
import json
from pathlib import Path
import sys

import numpy as np
import pytest
from build123d import Box, Pos

from duocad import kernel
from duocad.topopt import voxelize, fem, stress
from duocad.topopt.acceptance import (
    active_nodes, boundary_check, closed_voids, condition_check, connectivity_check, void_components,
    inspect, inspect_document, intersection_volume, region_check, safety_check, search_diagnostics,
)
from topopt_support import Table

CHECKS = Path(__file__).resolve().parents[1] / 'topopt_checks'
sys.path.insert(0, str(CHECKS))
spec = importlib.util.spec_from_file_location('check_pre_smoothing', CHECKS / 'check_pre_smoothing.py')
check_pre_smoothing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_pre_smoothing)


def fixture():
    shape = (4, 2, 2)
    grid = voxelize.Grid((0., 0., 0.), 1., shape)
    table = Table(grid, {1: list(range(9)), 2: list(range(36, 45))})
    material = np.ones(shape, dtype=bool)
    keep = Pos(.5, .5, .5) * Box(.4, .4, .4)
    excluded = Pos(4., 1., 1.) * Box(1., 1., 1.)
    return material, grid, table, dict(preserves=[('keep', keep.wrapped)], excludes=[('hole', excluded.wrapped)],
                                    fixed_faces=(1,), load_faces=(2,), forces=[(0, 0, -1)],
                                    target_safety_factor=.001, solver='direct')


@pytest.mark.parametrize('kind', ['full', 'tunnel'])
def test_regular_boundaries(kind):
    m = np.ones((5, 5, 5), bool)
    if kind == 'tunnel': m[:, 2, 2] = False
    assert boundary_check(m)['status'] == 'pass'
    assert boundary_check(m)['void_components'] == 1  # 貫通穴は外へつながるので空洞ではない
    assert connectivity_check(m)['status'] == 'pass'


def test_a_closed_void_fails_watertight_and_is_counted_as_a_second_empty_component():
    """閉じた空洞は境界の頂点が全て正則でも水密で落ちる(中空構造を許容しない製品制約)。"""
    m = np.ones((5, 5, 5), bool)
    m[2, 2, 2] = False
    report = boundary_check(m)
    assert (report['status'], report['irregular_vertices'], report['void_components']) == ('fail', 0, 2)
    assert connectivity_check(m)['status'] == 'pass'  # 材料は1成分のまま。落とすのは水密だけ
    assert closed_voids(m) == [dict(cells=1, representative_cell=[2, 2, 2])]
    m[2, 2, 2:] = False  # 空洞を外へつなぐと通る
    assert boundary_check(m)['status'] == 'pass' and closed_voids(m) == []


def test_closed_voids_are_reported_largest_first_with_a_representative_cell():
    m = np.ones((9, 5, 5), bool)
    m[1, 2, 2] = False  # 1セル
    m[4:7, 1:4, 1:4] = False  # 27セル
    assert void_components(m) == 3
    assert closed_voids(m) == [dict(cells=27, representative_cell=[4, 1, 1]),
                               dict(cells=1, representative_cell=[1, 2, 2])]
    assert closed_voids(m, limit=1) == [dict(cells=27, representative_cell=[4, 1, 1])]


def test_a_void_touching_the_grid_boundary_is_not_closed():
    m = np.ones((4, 4, 4), bool)
    m[0, 1:3, 1:3] = False  # 格子の面に開いた凹み
    assert void_components(m) == 1 and boundary_check(m)['status'] == 'pass'


def test_a_hollow_material_is_not_solved_and_reports_the_prerequisite_failure():
    """空洞のあるマスクは6条件の水密で落ち、FEMへ進まない。"""
    kwargs = fixture()[3]
    hollow = np.ones((5, 5, 5), bool)
    hollow[2, 2, 2] = False
    grid = voxelize.Grid((0., 0., 0.), 1., hollow.shape)
    table = Table(grid, {1: list(range(36)), 2: list(range(180, 216))})
    kwargs = dict(kwargs, excludes=[])
    report = inspect(hollow, hollow, grid, table=table, **kwargs)
    assert report['checks']['watertight']['status'] == 'fail'
    assert report['checks']['watertight']['void_components'] == 2
    assert report['checks']['safety'] == dict(status='unknown', reason='prerequisites_failed')


@pytest.mark.parametrize('position', [(1, 1, 1), (1, 1, 0)])
def test_corner_and_edge_contacts_fail(position):
    m = np.zeros((2, 2, 2), bool)
    m[0, 0, 0] = True
    m[position] = True
    assert boundary_check(m)['status'] == 'fail'
    assert connectivity_check(m)['status'] == 'fail'


def test_local_singularity_is_detected_even_if_globally_connected():
    m = np.zeros((4, 4, 3), bool)
    for cell in [(1, 1, 1), (2, 2, 1), (0, 1, 1), (0, 2, 1), (0, 3, 1), (1, 3, 1), (2, 3, 1)]:
        m[cell] = True
    assert connectivity_check(m)['status'] == 'pass'
    assert boundary_check(m)['status'] == 'fail'


def test_empty_and_separate_closed_boxes():
    m = np.zeros((3, 1, 1), bool)
    assert boundary_check(m)['status'] == 'fail'
    assert connectivity_check(m)['status'] == 'fail'
    m[0] = m[2] = True
    assert boundary_check(m)['status'] == 'pass'
    assert connectivity_check(m)['components'] == 2


def test_exact_cad_overlap_including_subcell_region_and_contact():
    grid = voxelize.Grid((10., 20., 30.), 2., (1, 1, 1))
    m = np.ones(grid.shape, bool)
    tiny = Pos(10.1, 20.1, 30.1) * Box(.1, .1, .1)
    tangent = Pos(12.5, 21, 31) * Box(1, 1, 1)
    partial = Pos(12, 21, 31) * Box(1, 1, 1)
    bodies = [('tiny', tiny.wrapped), ('contact', tangent.wrapped), ('partial', partial.wrapped)]
    result = region_check(m, grid, bodies, preserve=True)
    assert [b['status'] for b in result['bodies']] == ['pass', 'fail', 'pass']
    assert result['bodies'][0]['intersection_mm3'] == pytest.approx(.001)
    result = region_check(m, grid, bodies, preserve=False)
    assert [b['status'] for b in result['bodies']] == ['fail', 'pass', 'pass']
    assert result['bodies'][2]['empty_volume_mm3'] == pytest.approx(.5)
    assert region_check(m, grid, [], preserve=False)['status'] == 'pass'
    assert region_check(m, grid, [], preserve=True)['status'] == 'fail'


def test_intersection_volume_matches_with_and_without_bbox_cache():
    grid = voxelize.Grid((10., 20., 30.), 2., (1, 1, 1))
    m = np.ones(grid.shape, bool)
    shape = (Pos(12, 21, 31) * Box(1, 1, 1)).wrapped
    uncached = intersection_volume(m, grid, shape, cache=None)
    cache = kernel.BoundingBoxCache()
    cold = intersection_volume(m, grid, shape, cache=cache)  # このキャッシュには未測定
    warm = intersection_volume(m, grid, shape, cache=cache)  # 同じキャッシュを読む
    assert uncached == cold == warm
    assert len(cache) == 1


def test_inspect_result_is_identical_with_and_without_bbox_cache():
    rho, grid, table, options = fixture()
    without = inspect(rho, rho, grid, table=table, **options)
    cache = kernel.BoundingBoxCache()
    with_cache = inspect(rho, rho, grid, table=table, bbox_cache=cache, **options)
    assert without['checks']['preserve'] == with_cache['checks']['preserve']
    assert without['checks']['exclude'] == with_cache['checks']['exclude']
    assert without['status'] == with_cache['status'] == 'pass'
    assert len(cache) == 2  # preserve(keep) + exclude(hole) の2CADをこのキャッシュで測った


def test_six_checks_pass_and_recompute_safety(monkeypatch):
    rho, grid, table, options = fixture()
    original = rho.copy()
    seen = []
    solve = stress.solve_displacement
    def spy(model, density, *args, **kwargs):
        seen.append(density.copy())
        return solve(model, density, *args, **kwargs)
    monkeypatch.setattr(stress, 'solve_displacement', spy)
    result = inspect(rho, rho, grid, table=table, **options)
    assert result['status'] == 'pass', result
    assert len(result['checks']) == 6
    assert len(seen) == 1 and np.array_equal(seen[0], np.ones(rho.shape))
    assert np.array_equal(rho, original)
    assert result['volumes']['material_mm3'] == 16
    assert result['search']['status'] == 'unknown'
    assert result['search']['minimum_proven'] is False
    options['target_safety_factor'] = 1e12
    assert inspect(rho, rho, grid, table=table, **options)['checks']['safety']['status'] == 'fail'


def test_island_removal_cannot_hide_a_lost_load_or_keep():
    rho, grid, table, options = fixture()
    clean = rho.copy()
    clean[3] = 0
    options['preserves'] = [('removed_keep', (Pos(3.5, .5, .5) * Box(.4, .4, .4)).wrapped)]
    result = inspect(clean, rho, grid, table=table, **options)
    assert result['status'] == 'fail'
    assert result['volumes']['removed_islands_mm3'] == 4
    assert result['checks']['preserve']['status'] == 'fail'
    assert result['checks']['conditions']['status'] == 'fail'
    assert result['checks']['safety']['status'] == 'unknown'


def test_cancelled_loads_are_checked_per_face():
    rho, grid, table, _ = fixture()
    table.faces[3] = table.faces[2]
    rho[3] = 0
    result, _, _ = condition_check(rho.astype(bool), table, (1,), (2, 3), [(0, 0, -1), (0, 0, 1)])
    assert result['status'] == 'fail'
    assert all(row['lost_force_l1_n'] == pytest.approx(1) for row in result['faces'][1:])


def test_unused_elements_are_not_weak_material(monkeypatch):
    rho, grid, table, _ = fixture()
    rho[:, 1, 1] = 0
    live = active_nodes(rho.astype(bool)).ravel()
    table.faces[1] = [n for n in table.faces[1] if live[n]]
    table.faces[2] = [n for n in table.faces[2] if live[n]]
    _, fixed, load = condition_check(rho.astype(bool), table, (1,), (2,), [(0, 0, -1)])
    solve = stress.solve_displacement
    def spy(model, density, **kwargs):
        assert len(model.active) == int(rho.sum())
        assert np.all(np.isin(np.flatnonzero(~np.repeat(live, 3)), model.fixed))
        return solve(model, density, **kwargs)
    monkeypatch.setattr(stress, 'solve_displacement', spy)
    result = safety_check(rho.astype(bool), grid, fixed, load, target=.001, allowable_mpa=40,
                          youngs_modulus_mpa=2000, percentile=99.5, solver='direct')
    assert result['status'] == 'pass', result


def test_solver_failure_is_unknown_not_unsafe(monkeypatch):
    rho, grid, table, options = fixture()
    def broken(*args, **kwargs):
        raise fem.SolveResidualError('test residual failure')
    monkeypatch.setattr(stress, 'solve_displacement', broken)
    report = inspect(rho, rho, grid, table=table, **options)
    assert report['status'] == 'unknown'
    assert report['checks']['safety']['reason'] == 'evaluation_error'


@pytest.mark.parametrize('dtype', [float, np.int64])
def test_inspect_rejects_non_bool_masks(dtype):
    rho, grid, table, options = fixture()
    with pytest.raises(ValueError):
        inspect(rho.astype(dtype), rho, grid, table=table, **options)


def test_history_does_not_turn_failed_evaluations_into_unsafe_candidates():
    history = [dict(material_mm3=1, safety_factor=3, outcome='ok'),
               dict(material_mm3=2, safety_factor=2, outcome='ok'),
               dict(material_mm3=3, safety_factor=None, outcome='too_little', error='solver failed')]
    report = search_diagnostics(history, converged=False, max_evaluations=3)
    assert report['termination'] == 'evaluation_limit'
    assert report['failed_or_unevaluated'] == [2]
    assert report['monotonicity_inversions'] == [dict(smaller_evaluation=0, larger_evaluation=1)]
    assert report['minimum_proven'] is False


def test_force_scale_and_underconstrained_solid():
    rho, grid, table, options = fixture()
    first = inspect(rho, rho, grid, table=table, **options)['checks']['safety']
    options['forces'] = [(0, 0, -2)]
    second = inspect(rho, rho, grid, table=table, **options)['checks']['safety']
    assert second['safety_factor'] == pytest.approx(first['safety_factor'] / 2)
    table.faces[1] = [0]
    result = inspect(rho, rho, grid, table=table, **options)
    assert result['checks']['safety']['reason'] == 'rigid_body_modes'
    assert result['status'] == 'fail'


def _beam_document(tmp_path):
    """4x2x2mm格子を占める1本の梁。-X面を拘束・+X面に荷重(全保持・無除外)。
    `inspect_document` の入口(CADの拘束・荷重面)を作るための共通fixture。"""
    from duocad.session import Session
    from duocad import query
    session = Session(None, .2, models_dir=tmp_path)
    sketch = session.sketch_create('XY')['sketch_id']
    session.add_rectangle(sketch, [0., 0.], 4., 2.)
    session.modify_selection([{'sketch': sketch, 'index': 1}], [])
    session.extrude(2., mode='new')
    session.clear_selection()
    body = session.document.bodies[-1]
    session.modify_selection([{'body': body.id, 'kind': 'body'}], [])
    session.set_preserve_bodies()
    faces = query.describe_faces(body.shape)
    for normal, method in [(-1, session.set_fixtures), (1, lambda: session.set_loads(1., [0., 0., -1.]))]:
        face = next(f for f in faces if f['normal'][0] * normal > .9)
        session.clear_selection()
        session.modify_selection([{'body': body.id, 'kind': 'face', 'index': face['index']}], [])
        method()
    session.save_document('beam.json')
    return session.document


def test_inspect_document_thresholds_density_and_reports_integral(tmp_path):
    document = _beam_document(tmp_path)
    grid = voxelize.Grid((0., 0., 0.), 1., (4, 2, 2))
    report = inspect_document(document, np.full((4, 2, 2), .7), np.ones((4, 2, 2)), grid,
                              target_safety_factor=.001, solver='direct')
    assert report['status'] == 'pass', report
    assert report['level'] == 0.5
    assert report['threshold_rule'] == 'rho > level'
    # 二値領域の体積(bool: 0.7 > 0.5 で全材料)と、密度積算量(SIMP時代の指標)は別々に報告される。
    assert report['volumes']['material_mm3'] == 16
    assert report['volumes']['density_integral_before_mm3'] == pytest.approx(16.0)
    assert report['volumes']['density_integral_after_mm3'] == pytest.approx(11.2)


@pytest.mark.parametrize('bad', [np.nan, -1, 1.1])
def test_inspect_document_rejects_invalid_density(tmp_path, bad):
    document = _beam_document(tmp_path)
    grid = voxelize.Grid((0., 0., 0.), 1., (4, 2, 2))
    clean = np.ones((4, 2, 2))
    clean[0, 0, 0] = bad
    with pytest.raises(ValueError):
        inspect_document(document, clean, np.ones((4, 2, 2)), grid,
                         target_safety_factor=.001, solver='direct')


def test_cli_npz_and_document_without_surface_generation(tmp_path):
    _beam_document(tmp_path)
    np.savez_compressed(tmp_path / 'fields.npz', rho_clean=np.ones((4, 2, 2)),
                        rho_before=np.ones((4, 2, 2)), origin=[0., 0., 0.], spacing=1.)
    output = tmp_path / 'report.json'
    code = check_pre_smoothing.main(['--fields', str(tmp_path / 'fields.npz'), '--document', str(tmp_path / 'beam.json'),
                                     '--target', '.001', '--solver', 'direct', '--out', str(output)])
    report = json.loads(output.read_text(encoding='utf-8'))
    assert code == 0, report
    assert report['status'] == 'pass'
    assert report['volumes']['material_mm3'] == 16
    assert report['search']['status'] == 'unknown'


def test_cli_invalid_input_is_machine_readable(tmp_path):
    output = tmp_path / 'error.json'
    assert check_pre_smoothing.main(['--fields', str(tmp_path / 'missing.npz'), '--out', str(output)]) == 2
    assert json.loads(output.read_text(encoding='utf-8'))['status'] == 'unknown'
