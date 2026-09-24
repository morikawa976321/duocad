import importlib.util
from pathlib import Path
import sys
import json
import struct

import numpy as np
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt
import pytest

CHECKS = Path(__file__).resolve().parents[1] / 'topopt_checks'
sys.path.insert(0, str(CHECKS))
spec = importlib.util.spec_from_file_location('check_final_shape', CHECKS / 'check_final_shape.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def cube(low=0., high=1.):
    vertices = np.array([[0,0,0],[1,0,0],[1,1,0],[0,1,0],
                         [0,0,1],[1,0,1],[1,1,1],[0,1,1]], dtype=float)
    faces = np.array([[0,2,1],[0,3,2],[4,5,6],[4,6,7],[0,1,5],[0,5,4],
                      [1,2,6],[1,6,5],[2,3,7],[2,7,6],[3,0,4],[3,4,7]])
    return vertices * (high-low)+low, faces


def box(low, high):
    return BRepPrimAPI_MakeBox(gp_Pnt(*low), *np.subtract(high, low)).Shape()


def run(vertices=None, faces=None, preserve=None, exclude=()):
    v, f = cube()
    return check.evaluate(v if vertices is None else vertices, f if faces is None else faces,
                          [('keep', box((.1,.1,.1),(.2,.2,.2)))] if preserve is None else preserve, exclude,
                          check_constraints=True)


def test_identical_preserve_and_boundary_contact_pass():
    result = run(preserve=[('keep', box((0,0,0),(1,1,1)))],
                 exclude=[('cut', box((1,0,0),(2,1,1)))])
    assert result['status'] == 'pass', result


@pytest.mark.parametrize('key', ['preserve','exclude'])
def test_small_violation_is_not_allowed(key):
    shape = box((0,0,0),(1.0001,1,1)) if key == 'preserve' else box((.9999,0,0),(2,1,1))
    result = run(**{key: [('violation', shape)]})
    assert result['checks'][key]['status'] == 'fail', result
    assert result['checks'][key]['bodies'][0]['residual_volume_mm3'] > 0


@pytest.mark.parametrize('kind', ['hole','duplicate','flip','inward'])
def test_bad_boundary_fails(kind):
    v, f = cube()
    if kind == 'hole': f = f[:-1]
    elif kind == 'duplicate': f = np.vstack([f, f[0]])
    elif kind == 'flip': f[0] = f[0, ::-1]
    else: f = f[:, ::-1]
    result = run(v, f)
    assert result['checks']['watertight']['status'] == 'fail', result


def test_two_islands_fail():
    v, f = cube()
    result = run(np.vstack([v,v+2]), np.vstack([f,f+8]))
    assert result['checks']['watertight']['status'] == 'pass', result
    assert result['checks']['single_solid']['status'] == 'fail', result


def test_hollow_single_solid_passes():
    v, f = cube()
    inner, inner_f = cube(.3,.7)
    result = run(np.vstack([v,inner]), np.vstack([f,inner_f[:,::-1]+8]),
                 exclude=[('cavity',box((.4,.4,.4),(.6,.6,.6)))])
    assert result['status'] == 'pass', result
    assert result['mesh']['cavities'] == 1


def test_intersecting_shells_fail():
    v, f = cube()
    result = run(np.vstack([v,v+.5]),np.vstack([f,f+8]))
    assert result['checks']['watertight']['status'] == 'fail', result


def test_missing_preserve_is_unknown():
    assert run(preserve=[])['status'] == 'unknown'


def test_boolean_error_is_unknown(monkeypatch):
    def broken(*args): raise RuntimeError('deliberate failure')
    monkeypatch.setattr(check, 'boolean_remainder', broken)
    result = run()
    assert result['checks']['preserve']['status'] == 'unknown', result
    assert result['status'] == 'unknown'


@pytest.mark.parametrize('binary', [False, True])
def test_stl_roundtrip(tmp_path, binary):
    v, f = cube()
    path = tmp_path / 'cube.stl'
    if binary:
        path.write_bytes(b'\0'*80 + struct.pack('<I',len(f)) + b''.join(
            struct.pack('<12fH',0,0,0,*v[face].flatten(),0) for face in f))
    else:
        path.write_text('solid cube\n' + ''.join('facet normal 0 0 0\nouter loop\n' +
            ''.join('vertex ' + ' '.join(map(str,p)) + '\n' for p in v[face]) +
            'endloop\nendfacet\n' for face in f) + 'endsolid cube\n')
    vertices, faces = check.read_stl(path)
    assert run(vertices,faces)['status'] == 'pass'


def test_point_touch_is_nonmanifold():
    v, f = cube()
    triangles = np.vstack([v[f],(v+1)[f]])
    vertices, inverse = np.unique(triangles.reshape(-1,3), axis=0, return_inverse=True)
    result = run(vertices,inverse.reshape(-1,3))
    assert result['checks']['watertight']['status'] == 'fail', result
    assert result['mesh']['nonmanifold_vertices'] == 1


def test_output_cannot_overwrite_input(tmp_path):
    path = tmp_path / 'input.stl'
    path.write_text('original')
    with pytest.raises(SystemExit):
        check.main(['--mesh',str(path),'--out',str(path)])
    assert path.read_text() == 'original'


def test_invalid_input_reports_two_unknown(tmp_path):
    out = tmp_path / 'report.json'
    assert check.main(['--model','missing.json','--mesh',str(tmp_path/'missing.stl'),
                       '--out',str(out)]) == 2
    report = json.loads(out.read_text(encoding='utf-8'))
    assert len(report['checks']) == 2
    assert all(row['status'] == 'unknown' for row in report['checks'].values())


def test_progress_reports_current_boolean_and_outcome():
    messages = []
    v, f = cube()
    result = check.evaluate(v, f, [('keep',box((.1,.1,.1),(.2,.2,.2)))], [],
                            progress=messages.append, check_constraints=True)
    assert result['status'] == 'pass'
    assert 'preserve 1/1: keep' in messages
    assert 'keep: PASS' in messages


def test_default_acceptance_has_only_two_conditions():
    v, f = cube()
    result = check.evaluate(v, f)
    assert result['status'] == 'pass'
    assert set(result['checks']) == {'watertight', 'single_solid'}
