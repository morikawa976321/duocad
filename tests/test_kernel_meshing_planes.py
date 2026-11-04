"""境界の点が多い平面を Delabella で分割し、辺の裏返しで Delaunay に戻す経路(`kernel_meshing._discretize_faces`)。

落ちたら、穴の多い平面の三角形が穴を塞ぐ・領域の外を覆う・境界の線分を欠く・向きが揃わない・既定の分割より細い三角形を
作るか、少ない面の三角形が既定の分割と食い違っている。
"""
from __future__ import annotations

import math

import numpy as np
import pytest
from OCP.BRep import BRep_Tool
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakeWire,
    BRepBuilderAPI_Transform,
)
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.BRepTools import BRepTools
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pln, gp_Pnt, gp_Trsf, gp_Vec

from duocad import boundary_check, kernel, kernel_meshing
from duocad.mesh_geometry import TOPOLOGY_DEFECTS, topology, weld_exact

from duocad import (
    brep_topology as _brep_topology,
)


def perforated_plate(columns=12, rows=8, pitch=4.0, radius=1.0, thickness=3.0):
    face = BRepBuilderAPI_MakeFace(gp_Pln(), 0.0, columns * pitch, 0.0, rows * pitch)
    for i in range(columns):
        for j in range(rows):
            circle = gp_Circ(gp_Ax2(gp_Pnt((i + 0.5) * pitch, (j + 0.5) * pitch, 0.0), gp_Dir(0.0, 0.0, 1.0)), radius)
            wire = BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(circle).Edge()).Wire()
            wire.Reverse()
            face.Add(wire)
    return BRepPrimAPI_MakePrism(face.Face(), gp_Vec(0.0, 0.0, thickness)).Shape()


def mirrored(shape):
    trsf = gp_Trsf()
    trsf.SetMirror(gp_Ax2(gp_Pnt(1.0, 2.0, 3.0), gp_Dir(0.3, -0.5, 0.8)))
    return BRepBuilderAPI_Transform(shape, trsf, True).Shape()


def faces_of(shape):
    face_map = _brep_topology._face_map(shape)
    return [TopoDS.Face_s(face_map.FindKey(index)) for index in range(1, face_map.Extent() + 1)]


def triangulation_arrays(face):
    location = TopLoc_Location()
    triangulation = BRep_Tool.Triangulation_s(face, location)
    nodes = triangulation.NbNodes()
    uv = np.asarray([triangulation.UVNode(i).Coord() for i in range(1, nodes + 1)]).reshape(-1, 2)
    xyz = np.asarray([triangulation.Node(i).Coord() for i in range(1, nodes + 1)]).reshape(-1, 3)
    triangles = np.asarray([triangulation.Triangle(i).Get() for i in range(1, triangulation.NbTriangles() + 1)],
                           dtype=np.int64).reshape(-1, 3) - 1
    return triangulation, location, uv, xyz, triangles


def boundary_segments(face, triangulation, location):
    segments = set()
    explorer = TopExp_Explorer(face, TopAbs_ShapeEnum.TopAbs_EDGE)
    while explorer.More():
        polygon = BRep_Tool.PolygonOnTriangulation_s(TopoDS.Edge_s(explorer.Current()), triangulation, location)
        assert polygon is not None, '辺の折れ線が書かれていない(`IMeshData_Reused` の印が残った)'
        nodes = [polygon.Node(i) - 1 for i in range(1, polygon.NbNodes() + 1)]
        segments |= {(min(a, b), max(a, b)) for a, b in zip(nodes, nodes[1:]) if a != b}
        explorer.Next()
    return segments


def assert_valid_plane_triangulation(face):
    """向きが全部同じで潰れていない・各辺が2枚まで(内側は逆向きの2枚)・1枚だけの辺が境界の線分とちょうど一致・節点を全部使う。
    この4つで、三角形は領域をちょうど1重に覆う(穴を塞がず、外を覆わず、重ならない)。"""
    triangulation, location, uv, _xyz, triangles = triangulation_arrays(face)
    signs = boundary_check.orient2d(uv[triangles[:, 0]], uv[triangles[:, 1]], uv[triangles[:, 2]])
    assert (signs > 0).all() or (signs < 0).all()
    directed = np.concatenate([triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]]])
    assert len({tuple(edge) for edge in directed.tolist()}) == len(directed)
    undirected, counts = np.unique(np.sort(directed, axis=1), axis=0, return_counts=True)
    assert counts.max() <= 2
    once = {tuple(edge) for edge in undirected[counts == 1].tolist()}
    assert once == boundary_segments(face, triangulation, location)
    assert len(np.unique(triangles)) == len(uv)
    return uv, triangles


def illegal_edges(uv, triangles):
    """内側の辺のうち、向かいの頂点が外接円の内に入るもの(厳密)の数。"""
    sense = int(np.sign(boundary_check.orient2d(uv[triangles[:1, 0]], uv[triangles[:1, 1]], uv[triangles[:1, 2]])[0]))
    opposite = {}
    for row in triangles.tolist():
        for k in range(3):
            opposite[(row[k], row[(k + 1) % 3])] = row[(k + 2) % 3]
    rows = [(a, b, c, opposite[(b, a)]) for (a, b), c in opposite.items() if (b, a) in opposite and a < b]
    if not rows:
        return 0
    a, b, c, d = (uv[np.asarray(column)] for column in zip(*rows))
    return int((sense * boundary_check.incircle(a, b, c, d) > 0).sum())


def min_angle(xyz, triangles):
    p, q, r = xyz[triangles[:, 0]], xyz[triangles[:, 1]], xyz[triangles[:, 2]]

    def angle(at, u, v):
        x, y = u - at, v - at
        cosine = (x * y).sum(axis=1) / (np.linalg.norm(x, axis=1) * np.linalg.norm(y, axis=1))
        return np.arccos(np.clip(cosine, -1.0, 1.0))

    return float(np.minimum(np.minimum(angle(p, q, r), angle(q, r, p)), angle(r, p, q)).min())


def standard(shape, deviation, angle):
    BRepTools.Clean_s(shape)
    assert BRepMesh_IncrementalMesh(shape, deviation, False, angle, True).IsDone()
    return [triangulation_arrays(face)[2:] for face in faces_of(shape)]


def welded_defects(shape):
    chunks = []
    for face in faces_of(shape):
        positions, indices = kernel._face_triangles(face)
        chunks.append(np.asarray(positions).reshape(-1, 3)[np.asarray(indices).reshape(-1, 3)])
    stats, _ = topology(*weld_exact(np.concatenate(chunks)))
    return {key: stats[key] for key in TOPOLOGY_DEFECTS}


@pytest.mark.parametrize('label, make, deviation, angle', [
    ('grid', perforated_plate, 0.2, 0.35),
    ('grid_fine', perforated_plate, 0.05, 0.17),
    ('elongated', lambda: perforated_plate(columns=24, rows=3), 0.2, 0.35),
    ('mirrored', lambda: mirrored(perforated_plate()), 0.2, 0.35),
])
def test_heavy_planes_are_delaunay_with_the_standard_nodes(label, make, deviation, angle):
    """重い平面は Delabella で分割され、節点・三角形の数は既定の分割と同じ、境界・向き・1重の被覆を保ち、UV の上で Delaunay で、
    最小角は既定の分割より小さくならない。ほかの面は既定の分割と三角形まで同じ。"""
    shape = make()
    reference = standard(shape, deviation, angle)
    reference_defects = welded_defects(shape)
    BRepTools.Clean_s(shape)
    diagnostics = kernel_meshing.mesh_shape(shape, deviation, angle)
    assert diagnostics['termination'] == 'resolved' and diagnostics['delabella_fallback'] == []
    heavy = set(diagnostics['delabella'])
    assert heavy, label
    faces = faces_of(shape)
    partners = {index for index, face in enumerate(faces, 1) if any(face.IsPartner(faces[h - 1]) for h in heavy)}
    for index, face in enumerate(faces, 1):
        uv, xyz, triangles = triangulation_arrays(face)[2:]
        ref_uv, ref_xyz, ref_triangles = reference[index - 1]
        assert np.array_equal(xyz, ref_xyz) and np.array_equal(uv, ref_uv), (label, index)
        if index not in partners:
            assert np.array_equal(triangles, ref_triangles), (label, index)
            continue
        assert len(triangles) == len(ref_triangles)
        assert_valid_plane_triangulation(face)
        assert illegal_edges(uv, triangles) == 0
        assert min_angle(xyz, triangles) >= min_angle(ref_xyz, ref_triangles) - 1e-12
    assert welded_defects(shape) == reference_defects
    if label != 'mirrored':  # 鏡映の写しは、同じ節点が面ごとの位置の変換で 1 ulp ずれ、座標の一致では溶接しきれない(既定の分割でも同じ)
        assert all(value == 0 for value in reference_defects.values())


def test_mirrored_plate_keeps_outward_triangles():
    """鏡映した形でも、`kernel.tessellate` の三角形の法線は面の外向きの法線と同じ側を向く(既定の分割と同じ)。"""
    payload = kernel.tessellate(mirrored(perforated_plate()), 0.2, 0.35)
    assert payload['boundary']['delabella']
    for face in payload['faces']:
        positions = np.asarray(face['positions']).reshape(-1, 3)
        indices = np.asarray(face['indices']).reshape(-1, 3)
        p, q, r = positions[indices[:, 0]], positions[indices[:, 1]], positions[indices[:, 2]]
        normals = np.cross(q - p, r - p)
        if face['kind'] == 'plane':
            assert (normals @ np.asarray(face['normal']) > 0).all(), face['index']


def test_failed_count_check_falls_back_to_the_standard_mesher(monkeypatch):
    """Delabella の結果が数の検査を通らなければ、三角形を外して既定の分割で測り直し、既定と三角形まで同じになる。"""
    shape = perforated_plate()
    reference = standard(shape, 0.2, 0.35)
    monkeypatch.setattr(kernel_meshing, '_complete', lambda df: False)
    BRepTools.Clean_s(shape)
    diagnostics = kernel_meshing.mesh_shape(shape, 0.2, 0.35)
    assert diagnostics['delabella'] == [] and diagnostics['delabella_fallback']
    for index, face in enumerate(faces_of(shape), 1):
        assert np.array_equal(triangulation_arrays(face)[4], reference[index - 1][2]), index


def test_threshold_keeps_small_planes_on_the_standard_mesher(monkeypatch):
    """境界の点が閾値より少なければ Delabella を使わず、既定の分割と三角形まで同じ。"""
    shape = perforated_plate()
    reference = standard(shape, 0.2, 0.35)
    monkeypatch.setattr(kernel_meshing, 'DELABELLA_PLANE_POINTS', 10 ** 9)
    BRepTools.Clean_s(shape)
    diagnostics = kernel_meshing.mesh_shape(shape, 0.2, 0.35)
    assert diagnostics['delabella'] == []
    for index, face in enumerate(faces_of(shape), 1):
        assert np.array_equal(triangulation_arrays(face)[4], reference[index - 1][2]), index


def test_face_groups_report_delabella_faces_by_their_shape_index():
    """面の組(compound)で測っても、診断の面番号は元の形の番号。"""
    shape = perforated_plate()
    holed = [index for index, face in enumerate(faces_of(shape), 1)
             if _brep_topology.indexed_map(face, TopAbs_ShapeEnum.TopAbs_WIRE).Extent() > 1]
    assert len(holed) == 2
    total = _brep_topology.face_count(shape)
    others = [index for index in range(1, total + 1) if index != holed[0]]
    payload = kernel.tessellate_faces(shape, {(0.2, 0.35): [holed[0]], (0.5, 0.5): others}, closed=False)
    assert payload['boundary']['delabella'] == holed
    assert payload['untriangulated'] == []


# ------------------------------------------------------------------ 辺の裏返し
def fan(points):
    return np.asarray([(0, k, k + 1) for k in range(1, len(points) - 1)], dtype=np.int64)


def test_flips_turn_a_fan_of_a_convex_polygon_into_delaunay():
    """凸多角形の扇(Delaunay でない)を、数と向きを保って Delaunay にする。時計回りの入力も同じ。"""
    rng = np.random.default_rng(3)
    for _ in range(50):
        angles = np.sort(rng.uniform(0.0, 2.0 * math.pi, int(rng.integers(5, 40))))
        hull = np.column_stack((rng.uniform(1.5, 4.0) * np.cos(angles), np.sin(angles)))  # 楕円の上の点は凸で、同じ円に乗らない
        for triangles in (fan(hull), fan(hull)[:, [0, 2, 1]]):
            boundary = np.arange(len(hull))
            fixed = np.minimum(boundary, np.roll(boundary, -1)) * len(hull) + np.maximum(boundary, np.roll(boundary, -1))
            flipped = kernel_meshing._delaunay_flips(hull, triangles, fixed)
            assert len(flipped) == len(triangles)
            before = boundary_check.orient2d(hull[triangles[:, 0]], hull[triangles[:, 1]], hull[triangles[:, 2]])
            after = boundary_check.orient2d(hull[flipped[:, 0]], hull[flipped[:, 1]], hull[flipped[:, 2]])
            assert (after == before[0]).all()
            assert illegal_edges(hull, flipped) == 0


def test_flips_keep_fixed_edges_and_cocircular_quads():
    """固定した辺は裏返さない。4点が同じ円の上にある四角形(どちらの対角線も Delaunay)は裏返さない。"""
    quad = np.asarray([[0.0, 0.0], [2.0, 0.0], [2.0, 1.0], [0.0, 1.0]])
    square = np.asarray([[0, 1, 2], [0, 2, 3]], dtype=np.int64)
    assert np.array_equal(kernel_meshing._delaunay_flips(quad, square, np.zeros(0, dtype=np.int64)), square)
    thin = np.asarray([[0.0, 0.0], [1.0, -0.3], [2.0, 0.0], [1.0, 0.3]])  # 長い対角線 0-2 は Delaunay でない
    triangles = np.asarray([[0, 1, 2], [0, 2, 3]], dtype=np.int64)
    flipped = kernel_meshing._delaunay_flips(thin, triangles, np.zeros(0, dtype=np.int64))
    assert not np.array_equal(flipped, triangles) and illegal_edges(thin, flipped) == 0
    diagonal = np.asarray([0 * 4 + 2], dtype=np.int64)
    assert np.array_equal(kernel_meshing._delaunay_flips(thin, triangles, diagonal), triangles)


def test_incircle_matches_exact_arithmetic_near_degenerate_points():
    """浮動小数点で決まらない組(同じ円の上・その近く)も整数演算と同じ符号。"""
    rng = np.random.default_rng(5)
    rows = []
    for _ in range(400):
        center = rng.normal(size=2)
        angles = rng.uniform(0.0, 2.0 * math.pi, 4)
        points = center + np.column_stack((np.cos(angles), np.sin(angles)))
        points[3] += rng.choice([0.0, 1e-17, -1e-15, 1e-9]) * rng.normal(size=2)
        rows.append(points)
    rows.append(np.asarray([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]))
    rows = np.asarray(rows)
    got = boundary_check.incircle(rows[:, 0], rows[:, 1], rows[:, 2], rows[:, 3])
    expected = [boundary_check.incircle_exact(*row) for row in rows]
    assert got.tolist() == expected
    assert expected[-1] == 0
