import numpy as np
import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Pnt

from duocad import kernel
from duocad.document import Document
from duocad.kernel_brep import inspect_solid, encode_brep
from duocad.topopt.finalize import finalize, solid_mesh
from duocad.verbs import add_solid_body


def box(lo, hi):
    return BRepPrimAPI_MakeBox(gp_Pnt(*lo), gp_Pnt(*hi)).Shape()


def test_union_then_cut_preserves_cad_and_inputs():
    source = box((0, 0, 0), (2, 2, 2))
    vertices, faces = solid_mesh(source)
    keep = box((1, 0, 0), (3, 2, 2))
    cut = BRepPrimAPI_MakeCylinder(.3, 2).Shape()
    before = [kernel.volume(s) for s in (source, keep, cut)]
    result = finalize(vertices, faces, [keep], [cut])
    assert inspect_solid(result)['watertight']
    assert inspect_solid(result)['single_solid']
    assert kernel.volume(result) == pytest.approx(12-np.pi*.3**2*2/4)
    assert [kernel.volume(s) for s in (source, keep, cut)] == before
    assert 'CylindricalSurface' in encode_brep(result) or kernel.face_count(result) > 6


def test_exclusion_wins_overlap():
    v, f = solid_mesh(box((0, 0, 0), (2, 2, 2)))
    result = finalize(v, f, [box((1, 0, 0), (3, 2, 2))], [box((2, 0, 0), (4, 2, 2))])
    assert kernel.volume(result) == pytest.approx(8)


def test_preserve_fully_covered_by_exclude_contributes_nothing():
    v, f = solid_mesh(box((0, 0, 0), (4, 4, 4)))
    preserve = box((0.5, 0.5, 0.5), (1.5, 1.5, 1.5))
    exclude = box((0, 0, 0), (2, 2, 2))
    result = finalize(v, f, [preserve], [exclude])
    assert kernel.volume(result) == pytest.approx(64-8)


def test_cut_cannot_silently_leave_two_solids():
    v, f = solid_mesh(box((0, 0, 0), (3, 2, 2)))
    with pytest.raises(ValueError, match='単一ソリッド'):
        finalize(v, f, [], [box((1, -1, -1), (2, 3, 3))])


def test_disconnected_preserve_cannot_be_dropped():
    v, f = solid_mesh(box((0, 0, 0), (1, 1, 1)))
    with pytest.raises(ValueError, match='単一ソリッド'):
        finalize(v, f, [box((3, 0, 0), (4, 1, 1))], [])


def test_open_surface_rejected():
    v, f = solid_mesh(box((0, 0, 0), (1, 1, 1)))
    with pytest.raises(ValueError, match='開口'):
        finalize(v, f[:-1], [], [])


def test_solid_operation_undo_redo_and_replay():
    doc = Document()
    shape = box((0, 0, 0), (2, 3, 4))
    body_id = add_solid_body(doc, shape, 'optimized')
    assert doc.get_body(body_id).kind == 'solid'
    doc.log.undo(doc)
    assert not doc.bodies
    doc.log.redo(doc)
    assert kernel.volume(doc.get_body(body_id).shape) == pytest.approx(24)
    replay = Document()
    doc.log.replay(replay)
    assert replay.get_body(body_id).name == 'optimized'
    assert kernel.volume(replay.get_body(body_id).shape) == pytest.approx(24)


def test_generated_document_and_two_condition_cli(tmp_path):
    from duocad.topopt.trace import Recorder
    import importlib.util
    import json
    from pathlib import Path
    import sys

    check_dir = Path(__file__).resolve().parents[1]/'topopt_checks'
    sys.path.insert(0, str(check_dir))
    spec = importlib.util.spec_from_file_location('final_shape_cli', check_dir/'check_final_shape.py')
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    doc = Document()
    body_id = add_solid_body(doc, box((0, 0, 0), (2, 3, 4)), 'optimized')
    recorder = Recorder(tmp_path)
    recorder.document(doc, 'document_after')
    out = tmp_path/'acceptance.json'
    assert check.main(['--model-dir', str(recorder.path), '--model', 'document_after.json',
                       '--solid-body', body_id, '--out', str(out)]) == 0
    report = json.loads(out.read_text(encoding='utf-8'))
    assert set(report['checks']) == {'watertight', 'single_solid'}
    assert report['solid']['solids'] == 1
