import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest
from build123d import Box, Pos

from duocad.topopt import anchor, voxelize, simp, stress
from topopt_checks.pre_smoothing_geometry import boundary_check, connectivity_check, region_check
from topopt_checks.pre_smoothing_acceptance import inspect, condition_check, safety_check, search_diagnostics


class Table:
    def __init__(self, grid, faces):
        self.node_grid = anchor.NodeGrid(grid)
        self.faces = faces

    def node_ids(self, faces):
        return np.array(self.faces[faces[0]], dtype=int)

    def fixed_dofs(self, faces):
        ids = self.node_ids(faces)
        return (ids[:, None] * 3 + np.arange(3)).ravel()

    def load_vector(self, faces, force):
        out = np.zeros(self.node_grid.ndof)
        ids = self.node_ids(faces)
        out.reshape(-1, 3)[ids] = np.asarray(force) / len(ids)
        return out


def fixture():
    shape = (4, 2, 2)
    grid = voxelize.Grid((0., 0., 0.), 1., shape)
    table = Table(grid, {1: list(range(9)), 2: list(range(36, 45))})
    material = np.ones(shape)
    keep = Pos(.5, .5, .5) * Box(.4, .4, .4)
    excluded = Pos(4., 1., 1.) * Box(1., 1., 1.)
    return material, grid, table, dict(preserves=[('keep', keep.wrapped)], excludes=[('hole', excluded.wrapped)],
                                    fixed_faces=(1,), load_faces=(2,), forces=[(0, 0, -1)],
                                    target_safety_factor=.001, solver='direct')


@pytest.mark.parametrize('kind', ['full', 'cavity', 'tunnel'])
def test_regular_boundaries(kind):
    m = np.ones((5, 5, 5), bool)
    if kind == 'cavity': m[2, 2, 2] = False
    if kind == 'tunnel': m[:, 2, 2] = False
    assert boundary_check(m)['status'] == 'pass'
    assert connectivity_check(m)['status'] == 'pass'


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


def test_six_checks_pass_and_recompute_safety(monkeypatch):
    rho, grid, table, options = fixture()
    original = rho.copy()
    seen = []
    solve = stress.solve_displacement
    def spy(model, density, *args, **kwargs):
        seen.append(density.copy())
        return solve(model, density, *args, **kwargs)
    monkeypatch.setattr(stress, 'solve_displacement', spy)
    result = inspect(rho * .7, rho, grid, table=table, **options)
    assert result['status'] == 'pass', result
    assert len(result['checks']) == 6
    assert len(seen) == 1 and np.array_equal(seen[0], np.ones(rho.shape))
    assert np.array_equal(rho, original)
    assert result['volumes']['material_mm3'] == 16
    assert result['volumes']['density_integral_after_mm3'] == pytest.approx(11.2)
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
    from topopt_checks.pre_smoothing_geometry import active_nodes
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
        raise simp.SolveResidualError('test residual failure')
    monkeypatch.setattr(stress, 'solve_displacement', broken)
    report = inspect(rho, rho, grid, table=table, **options)
    assert report['status'] == 'unknown'
    assert report['checks']['safety']['reason'] == 'evaluation_error'


@pytest.mark.parametrize('bad', [np.nan, -1, 1.1])
def test_invalid_density_rejected(bad):
    rho, grid, table, options = fixture()
    clean = rho.copy()
    clean[0, 0, 0] = bad
    with pytest.raises(ValueError):
        inspect(clean, rho, grid, table=table, **options)


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


def test_cli_npz_and_document_without_surface_generation(tmp_path):
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
    np.savez_compressed(tmp_path / 'fields.npz', rho_clean=np.ones((4, 2, 2)),
                        rho_before=np.ones((4, 2, 2)), origin=[0., 0., 0.], spacing=1.)
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / 'report.json'
    command = [sys.executable, str(root / 'topopt_checks/check_pre_smoothing.py'),
               '--fields', str(tmp_path / 'fields.npz'), '--document', str(tmp_path / 'beam.json'),
               '--target', '.001', '--solver', 'direct', '--out', str(output)]
    completed = subprocess.run(command, cwd=root, capture_output=True, text=True)
    report = json.loads(output.read_text(encoding='utf-8'))
    assert completed.returncode == 0, (report, completed.stderr)
    assert report['status'] == 'pass'
    assert report['volumes']['material_mm3'] == 16
    assert report['search']['status'] == 'unknown'


def test_cli_invalid_input_is_machine_readable(tmp_path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / 'error.json'
    completed = subprocess.run([sys.executable, str(root / 'topopt_checks/check_pre_smoothing.py'),
                                '--fields', str(tmp_path / 'missing.npz'), '--out', str(output)],
                               cwd=root, capture_output=True, text=True)
    assert completed.returncode == 2
    assert json.loads(output.read_text(encoding='utf-8'))['status'] == 'unknown'
