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
    # 結果がメッシュボディで入るようになってからは、ブーリアン後の B-rep は記録の final-solid で検査する
    snapshot = tmp_path/'000009-final-solid.json'
    snapshot.write_text(json.dumps(dict(brep=encode_brep(box((0, 0, 0), (2, 3, 4))))), encoding='utf-8')
    out2 = tmp_path/'acceptance2.json'
    assert check.main(['--model-dir', str(recorder.path), '--model', 'document_after.json',
                       '--solid-snapshot', str(snapshot), '--out', str(out2)]) == 0
    report2 = json.loads(out2.read_text(encoding='utf-8'))
    assert report2['status'] == 'pass' and report2['source']['path'] == str(snapshot.resolve())


# ---------------------------------------------------------------- 最終メッシュの成果物検査
def _fixture(name):
    from pathlib import Path
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape
    shape = TopoDS_Shape()
    BRepTools.Read_s(shape, str(Path(__file__).resolve().parent / 'fixtures' / name), BRep_Builder())
    return shape


def test_solid_mesh_repairs_the_gap_reproduction_and_passes_every_check():
    """docs/TOPOPT_GAP_CAUSALITY.md の最小3面ソリッド: 以前は境界辺6・非多様体頂点7で黙って通っていた。"""
    from duocad.topopt.finalize import final_mesh_checks
    vertices, faces = solid_mesh(_fixture('gap_minimal_solid.brep'))
    checks = final_mesh_checks(vertices, faces)
    assert checks['failed'] == []
    assert checks['topology']['boundary_edges'] == 0 and checks['topology']['nonmanifold_vertices'] == 0
    assert checks['self_intersections']['pairs'] == []


def test_solid_mesh_refuses_a_shape_whose_boundary_cannot_be_repaired():
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    polygon = BRepBuilderAPI_MakePolygon()
    for point in ((0, 0, 0), (1, 1, 0), (1, 0, 0), (0, 1, 0)):
        polygon.Add(gp_Pnt(*point))
    polygon.Close()
    bowtie = BRepBuilderAPI_MakeFace(polygon.Wire(), True).Face()
    with pytest.raises(ValueError, match='三角形分割できない面'):
        solid_mesh(kernel.make_compound([bowtie, box((3, 3, 3), (4, 4, 4))]))


def test_final_mesh_checks_catch_self_intersection_and_float32_collapse():
    from duocad.topopt.finalize import final_mesh_checks
    v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float)
    f = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
                  [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    assert final_mesh_checks(v, f)['failed'] == []
    # 立方体を貫く三角形は接続6項目には現れない(境界辺は増えるので、貫く三角形は自己交差の項目でも落ちる)
    pierced = final_mesh_checks(np.vstack([v, [[0.5, 0.5, -1], [0.5, 0.5, 2], [2, 2, 0.5]]]), np.vstack([f, [[8, 9, 10]]]))
    assert 'self_intersections' in pierced['failed']
    # float64 では別の頂点だが float32 では同じ座標になる
    shifted = v.copy()
    shifted[1] = [1 + 1e-9, 0, 0]
    collapsed = final_mesh_checks(np.vstack([shifted, [[1, 0, 0]]]), f)
    assert 'float32_merged_vertices' in collapsed['failed']
