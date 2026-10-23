"""方式 D: 動かす面に接線でつながる丸めを外し(A)、尖った形で面を動かし(B か C)、同じ半径で丸め直す。

丸めの見つけ方: 選んだ面から、**接線で(G1)つながる平面以外の面**を幅優先でたどる(丸めの連なりと角の球面)。
丸め直す辺: 円筒・トーラスの丸めごとに、それが接線で接する「支えの面」2枚を覚え、面を動かした後の2枚の像の
共有辺を同じ半径で丸める(角の球面は辺を丸め直せば OCCT が作り直す)。
"""
from __future__ import annotations

from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.ChFi3d import ChFi3d
from OCP.GeomAbs import GeomAbs_Shape, GeomAbs_SurfaceType
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

from . import geometry as g
from .methods import FaceEditError, defeature, offset_faces


def _edge_faces(shape):
    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_ShapeEnum.TopAbs_EDGE, TopAbs_ShapeEnum.TopAbs_FACE, ancestors)
    return ancestors


def _tangent_pairs(shape):
    """(面, 面, 共有辺) のうち G1 で接するもの。"""
    ancestors = _edge_faces(shape)
    pairs = []
    for i in range(1, ancestors.Extent() + 1):
        faces = list(ancestors.FindFromIndex(i))
        if len(faces) != 2 or faces[0].IsSame(faces[1]):
            continue
        edge = TopoDS.Edge_s(ancestors.FindKey(i))
        a, b = TopoDS.Face_s(faces[0]), TopoDS.Face_s(faces[1])
        if ChFi3d.IsTangentFaces_s(edge, a, b, GeomAbs_Shape.GeomAbs_G1):
            pairs.append((a, b))
    return pairs


def find_blends(shape, faces):
    pairs = _tangent_pairs(shape)
    blends = []
    frontier = list(faces)
    while frontier:
        current = frontier.pop()
        for a, b in pairs:
            other = b if a.IsSame(current) else a if b.IsSame(current) else None
            if other is None or g.surface_type(other) == "plane":
                continue
            if any(other.IsSame(x) for x in blends) or any(other.IsSame(x) for x in faces):
                continue
            blends.append(other)
            frontier.append(other)
    supports = []
    for blend in blends:
        kind = BRepAdaptor_Surface(blend).GetType()
        if kind == GeomAbs_SurfaceType.GeomAbs_Cylinder:
            radius = BRepAdaptor_Surface(blend).Cylinder().Radius()
        elif kind == GeomAbs_SurfaceType.GeomAbs_Torus:
            radius = BRepAdaptor_Surface(blend).Torus().MinorRadius()
        elif kind == GeomAbs_SurfaceType.GeomAbs_Sphere:
            continue  # 角の球面は辺の丸めから作り直される
        else:
            raise FaceEditError(f"blend of kind {g.surface_type(blend)} has no single radius")
        touching = [b if a.IsSame(blend) else a for a, b in pairs if a.IsSame(blend) or b.IsSame(blend)]
        touching = [t for t in touching if not any(t.IsSame(x) for x in blends)]
        if len(touching) != 2:
            raise FaceEditError(f"blend has {len(touching)} support faces")
        supports.append((touching[0], touching[1], radius))
    return blends, supports


def _image(history, face):
    modified = list(history.Modified(face))
    if modified:
        return [TopoDS.Face_s(m) for m in modified]
    if history.IsRemoved(face):
        return []
    return [face]


def _as_in(shape, face):
    for occurrence in g.faces_of_oriented(shape):
        if occurrence.IsSame(face):
            return occurrence
    raise FaceEditError("face is not in the shape")


def move_following_blends(shape, faces, distance, *, mover="B"):
    blends, supports = find_blends(shape, faces)
    if not blends:
        if mover == "B":
            result, _ = offset_faces(shape, faces, distance)
            from .worker import as_solid

            return as_solid(result)
        from .custom import rebuild_move

        return rebuild_move(shape, faces, distance)

    sharp, algo = defeature(shape, blends)
    history = algo.History()
    # 履歴の像は結果のシェルの中の向きを持たないことがある(向きを取り違えると逆へ動く。DC の front_out3 で実測)
    moved_faces = [_as_in(sharp, i) for f in faces for i in _image(history, f)]
    support_images = [(_image(history, a), _image(history, b), r) for a, b, r in supports]

    if mover == "B":
        moved, _ = offset_faces(sharp, moved_faces, distance)
        from .worker import as_solid

        moved = as_solid(moved)
    else:
        from .custom import rebuild_move

        moved = rebuild_move(sharp, moved_faces, distance)

    def after(face):
        # MakeOffset は動かさない面も作り直し、Modified が空のことがある。両方とも曲面で対応を取る
        return _match_by_surface(moved, face, moved_faces, distance)

    edge_faces = _edge_faces(moved)
    maker = BRepFilletAPI_MakeFillet(moved)
    added = 0
    for images_a, images_b, radius in support_images:
        targets_a = [x for f in images_a for x in after(f)]
        targets_b = [x for f in images_b for x in after(f)]
        for i in range(1, edge_faces.Extent() + 1):
            owners = list(edge_faces.FindFromIndex(i))
            if len(owners) != 2:
                continue
            hit_a = any(o.IsSame(t) for o in owners for t in targets_a)
            hit_b = any(o.IsSame(t) for o in owners for t in targets_b)
            if hit_a and hit_b:
                maker.Add(radius, TopoDS.Edge_s(edge_faces.FindKey(i)))
                added += 1
    if added == 0:
        raise FaceEditError("no edge to re-blend after the move")
    maker.Build()
    if not maker.IsDone():
        raise FaceEditError("re-blending failed (IsDone is false)")
    return maker.Shape()


def _match_by_surface(moved, face, moved_faces, distance):
    """C は履歴を持たないので、同じ曲面(動かした面ならずらした曲面)に乗る面を探す。"""
    from .custom import _offset_big_face, _sample, _size

    reference = face
    for m in moved_faces:
        if face.IsSame(m):  # 履歴の像でなく、シェルの中の向きを持つ方でずらす
            reference = _offset_big_face(m, distance, moved, _size(moved))
            break
    from OCP.BRep import BRep_Tool
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf

    surface = BRep_Tool.Surface_s(reference)
    found = []
    for candidate in g.faces_of(moved):
        point, _ = _sample(candidate)
        projector = GeomAPI_ProjectPointOnSurf(point, surface)
        if projector.NbPoints() and projector.LowerDistance() < 1e-6:
            found.append(candidate)
    return found
