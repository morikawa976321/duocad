"""OCCT の面分割の段階制御と、境界交差の検出・共有辺の再分割(`duocad/kernel_meshing.py`)。

落ちたら、フィレット付き保持穴と除外円柱の間の細い領域(docs/TOPOPT_GAP_CAUSALITY.md)で
面が欠ける・非多様体になる不具合が戻っているか、正常な形状の分割が標準と食い違っている。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCone, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakeSphere
from OCP.BRepTools import BRepTools
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.TopTools import TopTools_IndexedMapOfShape
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from duocad import kernel, kernel_meshing
from duocad.mesh_geometry import TOPOLOGY_DEFECTS, topology, weld_exact

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
CONNECTIVITY = TOPOLOGY_DEFECTS


def load(name):
    shape = TopoDS_Shape()
    BRepTools.Read_s(shape, str(FIXTURES / name), BRep_Builder())
    assert not shape.IsNull()
    return shape


def welded(shape):
    """形状に付いた三角形を座標一致で溶接して (vertices, faces) にする。"""
    face_map = kernel._face_map(shape)
    chunks = []
    for index in range(1, face_map.Extent() + 1):
        positions, indices = kernel._face_triangles(TopoDS.Face_s(face_map.FindKey(index)))
        if indices:
            chunks.append(np.asarray(positions).reshape(-1, 3)[np.asarray(indices).reshape(-1, 3)])
    return weld_exact(np.concatenate(chunks))


def connectivity(shape):
    stats, _ = topology(*welded(shape))
    return {key: stats[key] for key in CONNECTIVITY}


def standard_mesh(shape, deviation, angle=0.35):
    BRepTools.Clean_s(shape)
    assert BRepMesh_IncrementalMesh(shape, deviation, False, angle, True).IsDone()


def per_face_triangles(shape):
    face_map = kernel._face_map(shape)
    return [kernel._face_triangles(TopoDS.Face_s(face_map.FindKey(i))) for i in range(1, face_map.Extent() + 1)]


# ------------------------------------------------------------------ 最小再現
@pytest.mark.parametrize('name, deviation', [
    ('gap_minimal_solid.brep', 0.2),
    ('gap_minimal_solid.brep', 0.05),
    ('gap_local_shift_minus0.01_radius2.6.brep', 0.2),
    ('gap_local_shift0_radius2.599.brep', 0.2),
    ('gap_local_shift0_radius2.599.brep', 0.01),
])
def test_gap_reproductions_are_detected_and_repaired(name, deviation):
    """標準の分割では境界辺・非多様体頂点が出る形状(docs/TOPOPT_GAP_CAUSALITY.md の表)が、
    交差の検出と共有辺の再分割だけで接続6項目ゼロになる。形状は変えない。"""
    shape = load(name)
    standard_mesh(shape, deviation)
    assert any(connectivity(shape).values()), '標準の分割が壊れていない(再現条件が変わった)'
    diagnostics = kernel_meshing.mesh_shape(shape, deviation, 0.35)
    assert diagnostics['termination'] == 'resolved'
    assert diagnostics['unresolved'] == []
    assert diagnostics['rounds'] >= 1 and diagnostics['amplified_edges'] >= 2
    assert diagnostics['rounds_log'][0]['issues'] >= 1
    assert diagnostics['points_final'] > diagnostics['points_initial']
    assert all(v == 0 for v in connectivity(shape).values()), connectivity(shape)


def test_control_shape_needs_no_repair_and_matches_the_standard_mesher():
    """隙間を広げた対照例は交差が無く、段階制御の結果は一括の BRepMesh_IncrementalMesh と三角形まで同じ。"""
    shape = load('gap_local_shift0_radius2.59_control.brep')
    standard_mesh(shape, 0.2)
    reference = per_face_triangles(shape)
    diagnostics = kernel_meshing.mesh_shape(shape, 0.2, 0.35)
    assert diagnostics['rounds'] == 0 and diagnostics['amplified_edges'] == 0
    assert diagnostics['termination'] == 'resolved'
    assert per_face_triangles(shape) == reference


# ------------------------------------------------------------------ CAD 表現
def holed_box():
    box = BRepPrimAPI_MakeBox(40.0, 30.0, 10.0).Shape()
    hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(20.0, 15.0, -1.0), gp_Dir(0.0, 0.0, 1.0)), 6.0, 12.0).Shape()
    return BRepAlgoAPI_Cut(box, hole).Shape()


@pytest.mark.parametrize('label, make', [
    ('box', lambda: BRepPrimAPI_MakeBox(20.0, 30.0, 10.0).Shape()),
    ('cylinder_seam_and_two_edge_caps', lambda: BRepPrimAPI_MakeCylinder(5.0, 10.0).Shape()),
    ('sphere_poles', lambda: BRepPrimAPI_MakeSphere(4.0).Shape()),
    ('cone_seam', lambda: BRepPrimAPI_MakeCone(5.0, 2.0, 10.0).Shape()),
    ('face_with_hole', holed_box),
    ('open_single_face', lambda: kernel.face_of(BRepPrimAPI_MakeBox(20.0, 30.0, 10.0).Shape(), 1)),
])
def test_ordinary_shapes_are_unchanged_and_identical_to_the_standard_mesher(label, make):
    """seam・極・穴・2辺 wire・開いた面で誤報せず、三角形が一括メッシャーと一致する。"""
    shape = make()
    for deviation in (0.2, 0.05):
        standard_mesh(shape, deviation)
        reference = per_face_triangles(shape)
        diagnostics = kernel_meshing.mesh_shape(shape, deviation, 0.35)
        assert diagnostics['termination'] == 'resolved' and diagnostics['rounds'] == 0, (label, diagnostics['unresolved'])
        assert per_face_triangles(shape) == reference, label


# ------------------------------------------------------------------ 再分割
def test_amplified_edge_updates_every_pcurve_and_the_committed_polygons_agree():
    """再分割した共有辺は両側の面の pcurve が同じ点数になり、面分割後の折れ線もその点数を持つ。"""
    shape = load('gap_minimal_solid.brep')
    diagnostics = kernel_meshing.mesh_shape(shape, 0.2, 0.35)
    assert diagnostics['amplified_edge_ids']
    edge_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_EDGE, edge_map)
    face_map = kernel._face_map(shape)
    for edge_id in diagnostics['amplified_edge_ids']:
        edge = TopoDS.Edge_s(edge_map.FindKey(edge_id))
        counts = []
        for index in range(1, face_map.Extent() + 1):
            face = TopoDS.Face_s(face_map.FindKey(index))
            explorer = TopExp_Explorer(face, TopAbs_ShapeEnum.TopAbs_EDGE)
            uses_edge = False
            while explorer.More():
                uses_edge = uses_edge or explorer.Current().IsSame(edge)
                explorer.Next()
            if not uses_edge:
                continue
            location = TopLoc_Location()
            triangulation = BRep_Tool.Triangulation_s(face, location)
            polygon = BRep_Tool.PolygonOnTriangulation_s(edge, triangulation, location)
            counts.append(polygon.NbNodes())
        assert len(counts) == 2 and counts[0] == counts[1], counts
        assert counts[0] > 2
    assert diagnostics['count_mismatch'] == []


def bowtie_face():
    polygon = BRepBuilderAPI_MakePolygon()
    for point in ((0, 0, 0), (1, 1, 0), (1, 0, 0), (0, 1, 0)):
        polygon.Add(gp_Pnt(*point))
    polygon.Close()
    return BRepBuilderAPI_MakeFace(polygon.Wire(), True).Face()


def test_a_truly_self_intersecting_boundary_fails_bounded_with_diagnostics():
    """直線の辺は再分割しても点列が変わらないので `no_change` で止まり、面は分割されずに報告される。"""
    shape = bowtie_face()
    diagnostics = kernel_meshing.mesh_shape(shape, 0.2, 0.35)
    assert diagnostics['termination'] == 'no_change'
    assert diagnostics['rounds'] == 1
    assert [entry['face'] for entry in diagnostics['unresolved']] == [1]
    assert diagnostics['unresolved'][0]['issues'][0]['kind'] == 'cross'
    assert per_face_triangles(shape) == [([], [])]
    # 1面だけの形状は「1面も取れない」の既存例外になるので、正常な箱と組にして製品経路を通す
    compound = kernel.make_compound([bowtie_face(), BRepPrimAPI_MakeBox(5.0, 5.0, 5.0).Shape()])
    payload = kernel.tessellate(compound, 0.2)
    assert payload['untriangulated'] == [1]
    assert payload['boundary']['termination'] == 'no_change'
    assert payload['boundary']['unresolved'][0]['face'] == 1
    assert len(payload['faces']) == 7 and all(f['indices'] for f in payload['faces'][1:])


def test_round_limit_is_respected():
    shape = load('gap_minimal_solid.brep')
    diagnostics = kernel_meshing.mesh_shape(shape, 0.2, 0.35, max_rounds=0)
    assert diagnostics['termination'] == 'max_rounds' and diagnostics['rounds'] == 0
    assert [entry['face'] for entry in diagnostics['unresolved']] == [1]
    assert per_face_triangles(shape)[0] == ([], [])


# ------------------------------------------------------------------ 製品経路
def test_tessellate_payload_carries_boundary_diagnostics_and_repaired_faces():
    shape = load('gap_minimal_solid.brep')
    payload = kernel.tessellate(shape, 0.2)
    assert payload['untriangulated'] == []
    assert payload['boundary']['termination'] == 'resolved'
    assert payload['boundary']['amplified_edges'] >= 2
    stats, _ = topology(*welded(shape))
    assert all(stats[key] == 0 for key in CONNECTIVITY)


def test_selective_split_path_reports_both_passes():
    shape = holed_box()
    total = kernel.face_count(shape)
    fine = {1, 2}
    payload = kernel.tessellate_selective(shape, fine, 0.05, 0.35, 0.5, 0.7)
    assert payload['fine_face_count'] == 2 and total > 2
    assert len(payload['boundary']['passes']) == 2
    assert payload['boundary']['termination'] == 'resolved'
    assert payload['untriangulated'] == []


def test_sketch_region_with_a_line_ending_inside_a_circle_meshes_like_the_standard_mesher():
    """スケッチの線が円の中で終わる領域は、内側の辺が外周の頂点に 2.4e-16 ずれて着く(healer は揃えない)。
    CAD 頂点の共有で正当な接続と判定し、標準メッシャーと同じ三角形になる(以前は 12 巡回って面が消えた)。"""
    from duocad import region
    from duocad.server import Session
    session = Session(0.2)
    sketch_id = session.sketch_create('XY')['sketch_id']
    session.add_line(sketch_id, [0.0, 0.0], [10.0, 0.0])
    session.add_circle(sketch_id, [10.0, 0.0], 4.0)
    faces = [item['face'] for item in region._regions_detailed(session.document, sketch_id)]
    assert faces
    for face in faces:
        standard_mesh(face, 0.2)
        reference = per_face_triangles(face)
        diagnostics = kernel_meshing.mesh_shape(face, 0.2, 0.35)
        assert diagnostics['termination'] == 'resolved' and diagnostics['rounds'] == 0
        assert diagnostics['endpoint']['mismatches'] >= 1
        assert per_face_triangles(face) == reference
    assert region.regions_with_geometry(session.document, sketch_id, 0.2)[0]['indices']


def _lines(*segments):
    return [{'kind': 'line', 'params': {'x0': a[0], 'y0': a[1], 'x1': b[0], 'y1': b[1]}} for a, b in segments]


_RECTANGLE_20_10 = {'kind': 'rectangle', 'params': {'x': 0.0, 'y': 0.0, 'width': 20.0, 'height': 10.0, 'angle': 0.0}}

#: 長方形(または円)の内側に置いた孤立線のスケッチ。以前は偽の touch / cross で領域が三角形にならなかった。
_ISOLATED_LINES = {
    'L': [_RECTANGLE_20_10, *_lines(((2, 5), (10, 5)), ((10, 5), (10, 2)))],
    'Y': [_RECTANGLE_20_10, *_lines(((10, 5), (5, 5)), ((10, 5), (15, 5)), ((10, 5), (10, 2)))],
    'X': [_RECTANGLE_20_10, *_lines(((5, 3), (15, 7)), ((5, 7), (15, 3)))],
    'circle_cross': [{'kind': 'circle', 'params': {'cx': 10.0, 'cy': 10.0, 'diameter': 16.0}},
                     *_lines(((5, 10), (15, 10)), ((10, 5), (10, 15)))],
    'two_sides_of_a_triangle': [_RECTANGLE_20_10, *_lines(((3, 3), (8, 3)), ((8, 3), (5, 7)))],
    'integer_slant_to_the_top_edge': [_RECTANGLE_20_10, *_lines(((3, 4), (13, 10)))],
    'shallow_slant': [{'kind': 'rectangle', 'params': {'x': 0.0, 'y': 0.0, 'width': 20.0, 'height': 7.3, 'angle': 0.0}},
                      *_lines(((1.1785053404777324, 4.978862320621157), (9.86522085500276, 7.3)))],
}


@pytest.mark.parametrize('name', _ISOLATED_LINES)
def test_a_sketch_region_with_isolated_lines_meshes_like_the_standard_mesher(name):
    """孤立線を含む領域は標準メッシャーが正常に分割する。落ちたら段階制御が偽の touch / cross で面を落とし、
    スケッチの領域が一覧から消える(`kernel_sketch.sketch_regions` の面を `kernel.tessellate` に渡す)。"""
    from duocad import kernel_sketch
    found = kernel_sketch.sketch_regions({'kind': 'base', 'name': 'XY'}, _ISOLATED_LINES[name])
    assert found
    for item in found:
        face = item['face']
        standard_mesh(face, 0.2)
        reference = per_face_triangles(face)
        assert reference, 'この題材は標準メッシャーでも分割できていない(題材の誤り)'
        diagnostics = kernel_meshing.mesh_shape(face, 0.2, 0.35)
        assert diagnostics['termination'] == 'resolved' and diagnostics['unresolved'] == [], diagnostics['unresolved']
        assert per_face_triangles(face) == reference
        assert kernel.tessellate(face, 0.2)['faces'][0]['indices']
