"""方式 C(自前): 面を外す/ずらした面に差し替え、隣の面を延ばし、`BOPAlgo_MakerVolume` で空間を区切って組み直す。

削除も移動も同じ手順で、違うのは「外した面の代わりに何を入れるか」だけ:

1. 選んだ面 R を外す。R に接する面 N を、それぞれ自分の曲面のまま大きく延ばす(`_big_face`)。
2. 移動なら、R をずらした曲面の大きな面を足す(`_offset_big_face`)。
3. 残りの面(R とも N とも接しない「遠い面」)はそのまま。全部を `BOPAlgo_MakerVolume` に渡して閉じた区画を作る。
4. 遠い面(無ければ N の元の範囲)の内側にある区画を残し、和を取り、同一平面を統合する。
"""
from __future__ import annotations

import math

from OCP.Bnd import Bnd_Box
from OCP.BOPAlgo import BOPAlgo_MakerVolume
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.BRepOffset import BRepOffset_Tool
from OCP.BRepTools import BRepTools
from OCP.ElSLib import ElSLib
from OCP.Geom import Geom_OffsetSurface
from OCP.GeomAbs import GeomAbs_SurfaceType
from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
from OCP.GeomLProp import GeomLProp_SLProps
from OCP.gp import gp_Pnt, gp_Pnt2d, gp_Vec
from OCP.TopAbs import TopAbs_Orientation, TopAbs_ShapeEnum, TopAbs_State
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Face
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_ListOfShape

from . import geometry as g
from .methods import FaceEditError
from .shapes import unify


def rebuild_delete(shape, faces):
    neighbours = g.neighbours(shape, faces)
    far = [f for f in g.faces_of(shape) if not _in(f, faces) and not _in(f, neighbours)]
    size = _size(shape)
    big = [(n, _big_face(n, shape, size)) for n in neighbours]
    return _assemble(shape, far, big, [])


def rebuild_move(shape, faces, distance):
    neighbours = g.neighbours(shape, faces)
    far = [f for f in g.faces_of(shape) if not _in(f, faces) and not _in(f, neighbours)]
    size = _size(shape)
    big = [(n, _big_face(n, shape, size)) for n in neighbours]
    moved = [_offset_big_face(f, distance, shape, size) for f in faces]
    return _assemble(shape, far, big, moved)


# --------------------------------------------------------------------------------------
def _assemble(shape, far, big, extra):
    arguments = TopTools_ListOfShape()
    for f in far:
        arguments.Append(f)
    for _, b in big:
        arguments.Append(b)
    for e in extra:
        arguments.Append(e)
    maker = BOPAlgo_MakerVolume()
    maker.SetArguments(arguments)
    maker.SetIntersect(True)
    maker.SetAvoidInternalShapes(True)
    maker.SetRunParallel(False)
    maker.Perform()
    if maker.HasErrors():
        raise FaceEditError("MakerVolume failed")
    cells = g.solids_of(maker.Shape())
    if not cells:
        raise FaceEditError("MakerVolume built no closed cell")

    owners = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(maker.Shape(), TopAbs_ShapeEnum.TopAbs_FACE, TopAbs_ShapeEnum.TopAbs_SOLID, owners)

    keep = []
    # 遠い面と、隣の面の「元の範囲」に乗る断片の、内側の区画を残す
    seeds = [(f, f, False) for f in far] + [(original, b, True) for original, b in big]
    for original, argument, restricted in seeds:
        images = list(maker.Modified(argument)) or [argument]
        for image in images:
            image = TopoDS.Face_s(image)
            point, _ = _sample(image)
            if restricted and not _on_face(original, point):
                continue
            outward = _outward_normal_at(original, point)
            index = owners.FindIndex(image)
            if index == 0:
                continue
            for cell in owners.FindFromIndex(index):
                if _in(cell, keep):
                    continue
                if _dot(_normal_in_cell(cell, image, point), outward) > 0:
                    keep.append(cell)
    if not keep:
        raise FaceEditError("no cell lies inside the kept faces")
    result = keep[0]
    for cell in keep[1:]:
        result = BRepAlgoAPI_Fuse(result, cell).Shape()
    return unify(result)


def _size(shape):
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    centre = gp_Pnt((xmin + xmax) / 2, (ymin + ymax) / 2, (zmin + zmax) / 2)
    return centre, 3.0 * math.dist((xmin, ymin, zmin), (xmax, ymax, zmax))


def _big_face(face, shape, size):
    centre, length = size
    adaptor = BRepAdaptor_Surface(face)
    kind = adaptor.GetType()
    if kind == GeomAbs_SurfaceType.GeomAbs_Plane:
        plane = adaptor.Plane()
        u, v = ElSLib.Parameters_s(plane, centre)
        made = BRepBuilderAPI_MakeFace(plane, u - length, u + length, v - length, v + length).Face()
    elif kind == GeomAbs_SurfaceType.GeomAbs_Cylinder:
        cylinder = adaptor.Cylinder()
        _, v = ElSLib.Parameters_s(cylinder, centre)
        made = BRepBuilderAPI_MakeFace(cylinder, 0.0, 2 * math.pi, v - length, v + length).Face()
    elif kind == GeomAbs_SurfaceType.GeomAbs_Sphere:
        made = BRepBuilderAPI_MakeFace(adaptor.Sphere()).Face()
    elif kind == GeomAbs_SurfaceType.GeomAbs_Torus:
        made = BRepBuilderAPI_MakeFace(adaptor.Torus()).Face()
    elif kind == GeomAbs_SurfaceType.GeomAbs_Cone:
        cone = adaptor.Cone()
        apex_v = -cone.RefRadius() / math.sin(cone.SemiAngle())
        v1, v2 = adaptor.FirstVParameter(), adaptor.LastVParameter()
        low, high = (max(apex_v + 1e-3, v1 - length), v2 + length) if apex_v < v1 else (v1 - length, min(apex_v - 1e-3, v2 + length))
        made = BRepBuilderAPI_MakeFace(cone, 0.0, 2 * math.pi, low, high).Face()
    else:
        made = TopoDS_Face()
        BRepOffset_Tool.EnLargeFace_s(face, made, True, False, True, True, True, 2)
    return made


def _offset_big_face(face, distance, shape, size):
    """面を外向き法線へ `distance` だけずらした曲面の大きな面。"""
    adaptor = BRepAdaptor_Surface(face)
    kind = adaptor.GetType()
    if kind == GeomAbs_SurfaceType.GeomAbs_Plane:
        n = g.face_normal(face)
        plane = adaptor.Plane().Translated(gp_Vec(n[0] * distance, n[1] * distance, n[2] * distance))
        proxy = BRepBuilderAPI_MakeFace(plane).Face()
        return _big_face(proxy, shape, size)
    if kind == GeomAbs_SurfaceType.GeomAbs_Cylinder:
        cylinder = adaptor.Cylinder()
        point, _ = _sample(face)
        axis = cylinder.Axis()
        a = axis.Location()
        d = axis.Direction()
        ap = gp_Vec(a, point)
        along = ap.Dot(gp_Vec(d))
        radial = ap - gp_Vec(d) * along
        sign = 1.0 if _dot((radial.X(), radial.Y(), radial.Z()), g.face_normal(face)) > 0 else -1.0
        radius = cylinder.Radius() + sign * distance
        if radius <= 1e-6:
            raise FaceEditError(f"cylinder radius would become {radius:g}")
        cylinder.SetRadius(radius)
        proxy = BRepBuilderAPI_MakeFace(cylinder, 0.0, 2 * math.pi, -1.0, 1.0).Face()
        return _big_face(proxy, shape, size)
    sign = -1.0 if face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED else 1.0
    surface = Geom_OffsetSurface(BRep_Tool.Surface_s(face), sign * distance)
    u1, u2, v1, v2 = BRepTools.UVBounds_s(face)
    proxy = BRepBuilderAPI_MakeFace(surface, u1, u2, v1, v2, 1e-7).Face()
    made = TopoDS_Face()
    BRepOffset_Tool.EnLargeFace_s(proxy, made, True, False, True, True, True, 2)
    return made


def _sample(face):
    u1, u2, v1, v2 = BRepTools.UVBounds_s(face)
    adaptor = BRepAdaptor_Surface(face)
    for fu, fv in ((0.5, 0.5), (0.37, 0.61), (0.23, 0.29), (0.71, 0.83)):
        u, v = u1 + fu * (u2 - u1), v1 + fv * (v2 - v1)
        classifier = BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-7)
        if classifier.State() == TopAbs_State.TopAbs_IN:
            return adaptor.Value(u, v), (u, v)
    u, v = 0.5 * (u1 + u2), 0.5 * (v1 + v2)
    return adaptor.Value(u, v), (u, v)


def _on_face(face, point):
    surface = BRep_Tool.Surface_s(face)
    projector = GeomAPI_ProjectPointOnSurf(point, surface)
    if projector.NbPoints() == 0 or projector.LowerDistance() > 1e-6:
        return False
    u, v = projector.LowerDistanceParameters()
    return BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-7).State() == TopAbs_State.TopAbs_IN


def _outward_normal_at(face, point):
    surface = BRep_Tool.Surface_s(face)
    projector = GeomAPI_ProjectPointOnSurf(point, surface)
    u, v = projector.LowerDistanceParameters()
    props = GeomLProp_SLProps(surface, u, v, 1, 1e-7)
    n = props.Normal()
    sign = -1.0 if face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED else 1.0
    return (sign * n.X(), sign * n.Y(), sign * n.Z())


def _normal_in_cell(cell, image, point):
    explorer = TopExp_Explorer(cell, TopAbs_ShapeEnum.TopAbs_FACE)
    while explorer.More():
        occurrence = TopoDS.Face_s(explorer.Current())
        if occurrence.IsSame(image):
            return _outward_normal_at(occurrence, point)
        explorer.Next()
    return (0.0, 0.0, 0.0)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _in(shape, shapes):
    return any(shape.IsSame(s) for s in shapes)
