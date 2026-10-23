"""形の問い合わせと、結果の判定(有効か・期待する形と一致するか)。"""
from __future__ import annotations

from typing import List

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.GeomAbs import GeomAbs_SurfaceType
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_Orientation, TopAbs_ShapeEnum
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape

_SURFACE_NAMES = {
    GeomAbs_SurfaceType.GeomAbs_Plane: "plane",
    GeomAbs_SurfaceType.GeomAbs_Cylinder: "cylinder",
    GeomAbs_SurfaceType.GeomAbs_Cone: "cone",
    GeomAbs_SurfaceType.GeomAbs_Sphere: "sphere",
    GeomAbs_SurfaceType.GeomAbs_Torus: "torus",
    GeomAbs_SurfaceType.GeomAbs_BSplineSurface: "bspline",
    GeomAbs_SurfaceType.GeomAbs_BezierSurface: "bezier",
    GeomAbs_SurfaceType.GeomAbs_SurfaceOfRevolution: "revolution",
    GeomAbs_SurfaceType.GeomAbs_SurfaceOfExtrusion: "extrusion",
    GeomAbs_SurfaceType.GeomAbs_OffsetSurface: "offset",
}


def _map(shape, kind) -> TopTools_IndexedMapOfShape:
    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, m)
    return m


def faces_of(shape) -> List:
    m = _map(shape, TopAbs_ShapeEnum.TopAbs_FACE)
    return [TopoDS.Face_s(m.FindKey(i)) for i in range(1, m.Extent() + 1)]


def faces_of_oriented(shape) -> List:
    """シェルの中での向きのまま(`faces_of` は向きを最初に出会った出現のものにする)。"""
    from OCP.TopExp import TopExp_Explorer

    out = []
    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_FACE)
    while explorer.More():
        out.append(TopoDS.Face_s(explorer.Current()))
        explorer.Next()
    return out


def edges_of(shape) -> List:
    m = _map(shape, TopAbs_ShapeEnum.TopAbs_EDGE)
    return [TopoDS.Edge_s(m.FindKey(i)) for i in range(1, m.Extent() + 1)]


def solids_of(shape) -> List:
    m = _map(shape, TopAbs_ShapeEnum.TopAbs_SOLID)
    return [TopoDS.Solid_s(m.FindKey(i)) for i in range(1, m.Extent() + 1)]


def face_index(shape, face) -> int:
    return _map(shape, TopAbs_ShapeEnum.TopAbs_FACE).FindIndex(face)


def surface_type(face) -> str:
    return _SURFACE_NAMES.get(BRepAdaptor_Surface(face).GetType(), "other")


def face_centroid(face):
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    c = props.CentreOfMass()
    return (c.X(), c.Y(), c.Z())


def face_area(face) -> float:
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    return props.Mass()


def face_normal(face, u=None, v=None):
    """面の中央(UV の中点)での**外向き**法線(面の向きを反映)。"""
    adaptor = BRepAdaptor_Surface(face)
    if u is None:
        u = 0.5 * (adaptor.FirstUParameter() + adaptor.LastUParameter())
        v = 0.5 * (adaptor.FirstVParameter() + adaptor.LastVParameter())
    props = BRepLProp_SLProps(adaptor, u, v, 1, 1e-7)
    n = props.Normal()
    sign = -1.0 if face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED else 1.0
    return (sign * n.X(), sign * n.Y(), sign * n.Z())


def edge_midpoint(edge):
    curve = BRepAdaptor_Curve(edge)
    p = curve.Value(0.5 * (curve.FirstParameter() + curve.LastParameter()))
    return (p.X(), p.Y(), p.Z())


def volume(shape) -> float:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return props.Mass()


def is_valid(shape) -> bool:
    return BRepCheck_Analyzer(shape).IsValid()


def free_edge_count(shape) -> int:
    """閉じていない辺(面が1枚しか付かない辺。継ぎ目の辺は同じ面が2回付くので数えない)。"""
    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_ShapeEnum.TopAbs_EDGE, TopAbs_ShapeEnum.TopAbs_FACE, ancestors)
    free = 0
    for i in range(1, ancestors.Extent() + 1):
        edge = TopoDS.Edge_s(ancestors.FindKey(i))
        if BRep_Tool.Degenerated_s(edge):
            continue
        if ancestors.FindFromIndex(i).Size() < 2:
            free += 1
    return free


def symmetric_difference(a, b) -> float:
    """vol(a−b) + vol(b−a)。一致すれば 0 に近い。"""
    total = 0.0
    for x, y in ((a, b), (b, a)):
        op = BRepAlgoAPI_Cut(x, y)
        if not op.IsDone():
            return float("nan")
        total += abs(volume(op.Shape()))
    return total


def neighbours(shape, faces) -> List:
    """`faces` と辺を共有する面(`faces` 自身は除く)。"""
    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_ShapeEnum.TopAbs_EDGE, TopAbs_ShapeEnum.TopAbs_FACE, ancestors)
    result = []
    for face in faces:
        for edge in edges_of(face):
            index = ancestors.FindIndex(edge)
            if index == 0:
                continue
            for other in ancestors.FindFromIndex(index):
                if any(other.IsSame(f) for f in faces) or any(other.IsSame(r) for r in result):
                    continue
                result.append(TopoDS.Face_s(other))
    return result
