import numpy as np
import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Pnt

from duocad import kernel
from duocad.document import Document
from duocad.io_step import STL_DEVIATION
from duocad.mesh_geometry import topology
from duocad.topopt.finalize import CAD_DEVIATION_MM, final_mesh_checks, finalize, tessellate_body
from duocad.topopt.mesh import mesh_volume
from duocad.verbs import add_solid_body


def box(lo, hi):
    return BRepPrimAPI_MakeBox(gp_Pnt(*lo), gp_Pnt(*hi)).Shape()


def mesh_of(shape):
    vertices, faces, _ = tessellate_body(shape)
    return vertices, faces


def test_cad_deviation_equals_stl_export():
    """保持・除外の三角形分割は STL 書き出しと同じ粗さ(issue #95)。食い違うと穴の内面の寸法が STL と変わる。"""
    assert CAD_DEVIATION_MM == STL_DEVIATION == 0.01


def test_union_then_cut_preserves_cad_and_inputs():
    source = box((0, 0, 0), (2, 2, 2))
    vertices, faces = mesh_of(source)
    keep = box((1, 0, 0), (3, 2, 2))
    cut = BRepPrimAPI_MakeCylinder(.3, 2).Shape()
    before = [kernel.volume(s) for s in (source, keep, cut)]
    result_vertices, result_faces = finalize(vertices, faces, [keep], [cut])
    checks = final_mesh_checks(result_vertices, result_faces)
    assert checks['failed'] == [] and checks['solids'] == 1
    # 円柱は内接する三角形で引くので、引ける体積は真値よりわずかに小さい(docs/TOPOPT_ACCEPTANCE.md「メッシュ集合演算」)
    assert mesh_volume(result_vertices, result_faces) == pytest.approx(12-np.pi*.3**2*2/4, rel=1e-3)
    assert [kernel.volume(s) for s in (source, keep, cut)] == before


def test_exclusion_wins_overlap():
    v, f = mesh_of(box((0, 0, 0), (2, 2, 2)))
    rv, rf = finalize(v, f, [box((1, 0, 0), (3, 2, 2))], [box((2, 0, 0), (4, 2, 2))])
    assert mesh_volume(rv, rf) == pytest.approx(8)


def test_preserve_fully_covered_by_exclude_contributes_nothing():
    v, f = mesh_of(box((0, 0, 0), (4, 4, 4)))
    preserve = box((0.5, 0.5, 0.5), (1.5, 1.5, 1.5))
    exclude = box((0, 0, 0), (2, 2, 2))
    rv, rf = finalize(v, f, [preserve], [exclude])
    assert mesh_volume(rv, rf) == pytest.approx(64-8)


def test_cavity_is_one_solid_and_counted():
    """除外が材料の中に完全に入ると空洞になる。空洞は分断ではなく、単一ソリッドのまま数だけ残す。"""
    v, f = mesh_of(box((0, 0, 0), (4, 4, 4)))
    rv, rf = finalize(v, f, [], [box((1, 1, 1), (2, 2, 2))])
    checks = final_mesh_checks(rv, rf)
    assert checks['failed'] == [] and checks['solids'] == 1 and checks['cavities'] == 1
    assert mesh_volume(rv, rf) == pytest.approx(63)


def test_cut_cannot_silently_leave_two_solids():
    v, f = mesh_of(box((0, 0, 0), (3, 2, 2)))
    with pytest.raises(ValueError, match='単一ソリッド'):
        finalize(v, f, [], [box((1, -1, -1), (2, 3, 3))])


@pytest.mark.parametrize('offset', [0, 10000])
def test_detached_inward_shell_is_not_a_cavity(offset):
    from duocad.topopt.finalize import input_mesh_checks
    v, f = mesh_of(box((0, 0, 0), (4, 4, 4)))
    u, g = mesh_of(box((6, 0, 0), (7, 1, 1)))
    vertices = np.vstack([v, u]) + offset
    faces = np.vstack([f, g[:, ::-1] + len(v)])
    assert input_mesh_checks(vertices, faces)['failed'] == ['invalid_shell_orientation']
    assert final_mesh_checks(vertices, faces)['failed'] == ['invalid_shell_orientation']
    with pytest.raises(ValueError, match='invalid_shell_orientation'):
        finalize(vertices, faces, [], [])


@pytest.mark.parametrize('inner_outward', [False, True])
def test_nested_shell_orientation_matches_containment(inner_outward):
    from duocad.topopt.finalize import input_mesh_checks
    v, f = mesh_of(box((0, 0, 0), (4, 4, 4)))
    u, g = mesh_of(box((1, 1, 1), (3, 3, 3)))
    vertices = np.vstack([v, u])
    faces = np.vstack([f, (g if inner_outward else g[:, ::-1]) + len(v)])
    checks = input_mesh_checks(vertices, faces)
    if inner_outward:
        assert checks['failed'] == ['invalid_shell_orientation']
    else:
        assert checks['failed'] == []
        rv, rf = finalize(vertices, faces, [], [])
        assert final_mesh_checks(rv, rf)['cavities'] == 1


def test_material_inside_a_cavity_is_a_second_solid():
    from duocad.topopt.finalize import input_mesh_checks
    vertices, faces = [], []
    for lo, hi, outward in [(0, 6, True), (1, 5, False), (2, 4, True)]:
        v, f = mesh_of(box((lo, lo, lo), (hi, hi, hi)))
        faces.append((f if outward else f[:, ::-1]) + sum(len(x) for x in vertices))
        vertices.append(v)
    vertices, faces = np.vstack(vertices), np.vstack(faces)
    assert input_mesh_checks(vertices, faces)['failed'] == []
    checks = final_mesh_checks(vertices, faces)
    assert checks['failed'] == ['single_solid'] and checks['solids'] == 2


def test_shell_inside_bounding_box_but_outside_material_is_not_a_cavity():
    v, f = mesh_of(BRepPrimAPI_MakeCylinder(3, 4).Shape())
    u, g = mesh_of(box((2.5, 2.5, 1), (2.6, 2.6, 2)))
    checks = final_mesh_checks(np.vstack([v, u]), np.vstack([f, g[:, ::-1] + len(v)]))
    assert checks['failed'] == ['invalid_shell_orientation']


def test_disconnected_preserve_cannot_be_dropped():
    v, f = mesh_of(box((0, 0, 0), (1, 1, 1)))
    with pytest.raises(ValueError, match='単一ソリッド'):
        finalize(v, f, [box((3, 0, 0), (4, 1, 1))], [])


def test_exclude_covering_everything_is_an_error():
    v, f = mesh_of(box((0, 0, 0), (1, 1, 1)))
    with pytest.raises(ValueError, match='空'):
        finalize(v, f, [], [box((-1, -1, -1), (2, 2, 2))])


def test_open_surface_rejected():
    v, f = mesh_of(box((0, 0, 0), (1, 1, 1)))
    with pytest.raises(ValueError, match='開口'):
        finalize(v, f[:-1], [], [])


def test_inward_surface_rejected():
    """向きが内向きの入力は manifold3d が通してしまう(体積が負の形として演算する)ので、手前で止める。"""
    v, f = mesh_of(box((0, 0, 0), (1, 1, 1)))
    with pytest.raises(ValueError, match='inward_orientation'):
        finalize(v, f[:, ::-1], [], [])


def test_self_intersecting_smoothed_surface_rejected():
    """平滑化後の表面の自己交差は、manifold3d が結果に残すので入力で止める(小さいから通す判定は無い)。"""
    v, f = mesh_of(box((0, 0, 0), (2, 2, 2)))
    spike = box((0.5, 0.5, 1.5), (1.5, 1.5, 2.5))
    sv, sf = mesh_of(spike)
    # 2つの閉殻を1つの入力として渡す(接続6項目は合格、幾何では貫通している)
    merged_v = np.vstack([v, sv])
    merged_f = np.vstack([f, sf + len(v)])
    stats, _ = topology(merged_v, merged_f)
    assert all(stats[k] == 0 for k in ('boundary_edges', 'nonmanifold_edges'))
    with pytest.raises(ValueError, match='自己交差'):
        finalize(merged_v, merged_f, [], [])


def test_result_survives_float32_rounding():
    """文書・画面・STL は float32 で消費する。簡略化後の結果に float32 で潰れる三角形や一致する頂点が無い。"""
    v, f = mesh_of(box((0, 0, 0), (10, 10, 10)))
    rv, rf = finalize(v, f, [BRepPrimAPI_MakeCylinder(2.5, 12).Shape()], [BRepPrimAPI_MakeCylinder(1.0, 12).Shape()])
    checks = final_mesh_checks(rv, rf)
    assert checks['failed'] == []
    assert checks['float32'] == dict(merged_vertices=0, degenerate_triangles=0)
    # 円柱は原点が軸なので 3/4 が立方体の外へ出る。保持は外に出た分だけ足され、除外は保持に全部含まれる
    assert mesh_volume(rv, rf) == pytest.approx(1000 + np.pi*2.5**2*(12 - 10/4) - np.pi*1.0**2*12, rel=1e-3)


def test_collapse_float32_slivers_removes_only_unrepresentable_edges():
    """float32 で潰れる三角形は、許容値未満の最短辺を潰して消す。それより長い辺の三角形と健全な三角形は触らない。"""
    from duocad.topopt.finalize import SIMPLIFY_TOLERANCE_MM, collapse_float32_slivers
    v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float) * 100
    f = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
                  [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    same_v, same_f, collapsed = collapse_float32_slivers(v, f)
    assert collapsed == 0 and np.array_equal(same_v, v) and np.array_equal(same_f, f)
    # 上面の三角形 (4,5,6) の中に、頂点6=(100,100,100) から 3.6e-6 mm(座標 100 での float32 の分解能 7.6e-6 mm 未満)の
    # 頂点を挿す。float32 では頂点6と同じ座標になり、それを含む2枚が潰れる
    p = v[6] - np.array([2e-6, 3e-6, 0])
    split_v = np.vstack([v, p])
    split_f = np.vstack([f[:2], f[3:], [[4, 5, 8], [5, 6, 8], [6, 4, 8]]])
    assert {'float32_merged_vertices', 'float32_degenerate_triangles'} <= set(final_mesh_checks(split_v, split_f)['failed'])
    rv, rf, collapsed = collapse_float32_slivers(split_v, split_f)
    assert collapsed == 1 and len(rf) == 12 and len(rv) == 8
    assert final_mesh_checks(rv, rf)['failed'] == []
    assert mesh_volume(rv, rf) == pytest.approx(1e6)
    # 最短辺が許容値以上の三角形は潰さない(座標 ~1e6 mm では辺 0.01 mm でも float32 で潰れる)
    far_v = v + 1e6
    far_p = far_v[6] - np.array([1e-2, 2e-2, 0])
    far_f = np.vstack([f[:2], f[3:], [[4, 5, 8], [5, 6, 8], [6, 4, 8]]])
    _, _, collapsed = collapse_float32_slivers(np.vstack([far_v, far_p]), far_f, max_edge_mm=SIMPLIFY_TOLERANCE_MM)
    assert collapsed == 0


def test_collapse_float32_crossings_removes_the_needle_that_flips_when_rounded():
    """F2000_h2.0_w2 (#133) の交差5組を作る5枚。落ちたら、float32 で裏返る針状の三角形を潰せていない。"""
    from duocad.mesh_intersections import triangle_intersections
    from duocad.topopt.finalize import collapse_float32_crossings
    v = np.array([[17.47172086510047, -186.4580733292355, 58.52722761479261],
                  [17.492879514968028, -186.37686499573488, 58.51839567628473],
                  [17.49918044285612, -186.32033256484826, 58.35884796767712],
                  [17.499348273106545, -186.32799719505292, 58.287291130940716],
                  [17.499988021321386, -186.2435964631998, 59.07493259940401],
                  [17.499989320350313, -186.24356399536146, 59.07523545827184],
                  [19.179058114101224, -186.15252540887144, 59.70752185933259]])
    f = np.array([[3, 5, 2], [0, 1, 4], [1, 2, 4], [4, 2, 5], [3, 6, 5]])
    rounded = lambda vertices: np.asarray(vertices).astype(np.float32).astype(np.float64)
    assert triangle_intersections(v, f)['pairs'] == []
    pairs = triangle_intersections(rounded(v), f)['pairs']
    assert len(pairs) == 5
    rv, rf, collapsed = collapse_float32_crossings(v, f, pairs)
    # 潰れるのは頂点4・5の間の 3.05e-4 mm の辺だけで、針(4, 2, 5)が消える
    assert collapsed == 1 and len(rf) == 4
    assert triangle_intersections(rounded(rv), rf)['pairs'] == []
    same_v, same_f, collapsed = collapse_float32_crossings(v, f, pairs, max_edge_mm=1e-4)
    assert collapsed == 0 and np.array_equal(same_v, v) and np.array_equal(same_f, f)
    assert collapse_float32_crossings(v, f, [])[2] == 0


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


# ---------------------------------------------------------------- 最終メッシュの成果物検査
def _fixture(name):
    from pathlib import Path
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape
    shape = TopoDS_Shape()
    BRepTools.Read_s(shape, str(Path(__file__).resolve().parent / 'fixtures' / name), BRep_Builder())
    return shape


def test_tessellate_body_repairs_the_gap_reproduction_and_passes_every_check():
    """docs/TOPOPT_GAP_CAUSALITY.md の最小3面ソリッド: 以前は境界辺6・非多様体頂点7で黙って通っていた。"""
    vertices, faces, boundary = tessellate_body(_fixture('gap_minimal_solid.brep'))
    checks = final_mesh_checks(vertices, faces)
    assert checks['failed'] == []
    assert checks['topology']['boundary_edges'] == 0 and checks['topology']['nonmanifold_vertices'] == 0
    assert checks['self_intersections']['pairs'] == []
    assert set(boundary) == {'termination', 'rounds', 'amplified_edges', 'reinstated'}


def test_tessellate_body_refuses_a_shape_whose_boundary_cannot_be_repaired():
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    polygon = BRepBuilderAPI_MakePolygon()
    for point in ((0, 0, 0), (1, 1, 0), (1, 0, 0), (0, 1, 0)):
        polygon.Add(gp_Pnt(*point))
    polygon.Close()
    bowtie = BRepBuilderAPI_MakeFace(polygon.Wire(), True).Face()
    with pytest.raises(ValueError, match='三角形分割できない面'):
        tessellate_body(kernel.make_compound([bowtie, box((3, 3, 3), (4, 4, 4))]))


def test_final_mesh_checks_catch_self_intersection_and_float32_collapse():
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
    # 2つの閉殻は単一ソリッドでない(空洞ではなく、どちらも外向き)
    two = final_mesh_checks(np.vstack([v, v + [3, 0, 0]]), np.vstack([f, f + 8]))
    assert 'single_solid' in two['failed'] and two['solids'] == 2 and two['cavities'] == 0
