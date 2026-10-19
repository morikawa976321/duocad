"""厳しい版の関門と D(astra の1回目の指摘を反映。docs/FACE_EDIT.md「相談役の意見と食い違い」)。

worker.py の `gate` との違い:
- **面ごとの対応を先に確定し、対応先の曲面の定義と比べる**(「許された曲面のどれかに1点が乗る」ではない)。
  解析曲面(平面・円筒・円錐・球・トーラス)は定義そのもの(法線と位置、軸と半径…)を比べる。自由曲面は内部の複数点を射影する。
- 移動(B)は旧面と新面が**1対1で全部覆う**こと(面の数が同じでも、別の面が消えて別の面ができる形を通さない)。
- 結果全体がちょうど1ソリッドで、ソリッドの外に面が無いこと。
- D は丸めを**構造で**認識する(半径の大小では決めない)。丸め直した面だけを曲面の検査から外し、代わりに半径と接線を確かめる。
  由来は、旧丸め=deleted、丸め直した面=new_faces(旧丸めと新丸めを対応させる根拠を持たないため)。

    .venv/Scripts/python.exe -m tools.face_edit_probe.strict <case> <SA|SB|SD>   # 1件(run.py --strict が呼ぶ)
"""
from __future__ import annotations

import json
import math
import sys
import time

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepTools import BRepTools
from OCP.ChFi3d import ChFi3d
from OCP.GeomAbs import GeomAbs_CurveType, GeomAbs_Shape, GeomAbs_SurfaceType
from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
from OCP.gp import gp_Pnt, gp_Pnt2d
from OCP.TopAbs import TopAbs_ShapeEnum, TopAbs_State
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape

from . import geometry as g
from . import methods, shapes
from .methods import FaceEditError
from .worker import MIN_FACE_AREA

#: 曲面の定義を比べる許容差[mm]と角度の許容差(cos の 1 からのずれ)。置いた値
LINEAR_TOL = 1e-6
ANGULAR_TOL = 1e-9


# --------------------------------------------------------------------------------------
# 曲面の定義の比較
# --------------------------------------------------------------------------------------
def _vec(d):
    return (d.X(), d.Y(), d.Z())


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _parallel(a, b):
    return abs(abs(_dot(a, b)) - 1.0) < ANGULAR_TOL


def _point_line_distance(p, origin, direction):
    v = (p[0] - origin[0], p[1] - origin[1], p[2] - origin[2])
    along = _dot(v, direction)
    return math.sqrt(max(0.0, _dot(v, v) - along * along))


def _axis(ax):
    return _vec(ax.Location()), _vec(ax.Direction())


def describe_surface(face):
    """解析曲面の定義(世界座標)。自由曲面は None。"""
    a = BRepAdaptor_Surface(face)
    kind = a.GetType()
    if kind == GeomAbs_SurfaceType.GeomAbs_Plane:
        p = a.Plane()
        return ("plane", _vec(p.Location()), _vec(p.Axis().Direction()))
    if kind == GeomAbs_SurfaceType.GeomAbs_Cylinder:
        c = a.Cylinder()
        return ("cylinder", *_axis(c.Axis()), c.Radius())
    if kind == GeomAbs_SurfaceType.GeomAbs_Cone:
        c = a.Cone()
        return ("cone", _vec(c.Apex()), _vec(c.Axis().Direction()), c.SemiAngle())
    if kind == GeomAbs_SurfaceType.GeomAbs_Sphere:
        s = a.Sphere()
        return ("sphere", _vec(s.Location()), s.Radius())
    if kind == GeomAbs_SurfaceType.GeomAbs_Torus:
        t = a.Torus()
        return ("torus", *_axis(t.Axis()), t.MajorRadius(), t.MinorRadius())
    return None


def same_surface(a, b):
    if a is None or b is None or a[0] != b[0]:
        return False
    kind = a[0]
    if kind == "plane":
        return _parallel(a[2], b[2]) and abs(_dot((b[1][0] - a[1][0], b[1][1] - a[1][1], b[1][2] - a[1][2]), a[2])) < LINEAR_TOL
    if kind == "cylinder":
        return _parallel(a[2], b[2]) and _point_line_distance(b[1], a[1], a[2]) < LINEAR_TOL and abs(a[3] - b[3]) < LINEAR_TOL
    if kind == "cone":
        return _parallel(a[2], b[2]) and math.dist(a[1], b[1]) < LINEAR_TOL and abs(abs(a[3]) - abs(b[3])) < ANGULAR_TOL
    if kind == "sphere":
        return math.dist(a[1], b[1]) < LINEAR_TOL and abs(a[2] - b[2]) < LINEAR_TOL
    if kind == "torus":
        return (
            _parallel(a[2], b[2]) and math.dist(a[1], b[1]) < LINEAR_TOL
            and abs(a[3] - b[3]) < LINEAR_TOL and abs(a[4] - b[4]) < LINEAR_TOL
        )
    return False


def offset_definition(face, distance):
    """外向き法線へ `distance` ずらした曲面の定義(解析曲面だけ。自由曲面は None)。"""
    d = describe_surface(face)
    if d is None:
        return None
    n = g.face_normal(face)
    kind = d[0]
    if kind == "plane":
        return ("plane", tuple(d[1][i] + n[i] * distance for i in range(3)), d[2])
    if kind == "cylinder":
        return ("cylinder", d[1], d[2], d[3] + _convexity(face, d) * distance)
    if kind == "sphere":
        return ("sphere", d[1], d[2] + _convexity(face, d) * distance)
    if kind == "torus":
        return ("torus", d[1], d[2], d[3], d[4] + _convexity(face, d) * distance)
    return None  # 円錐はずらすと頂点が動く。ここでは比べない


def _convexity(face, definition):
    """外向き法線が中心(軸)から離れる向きなら +1(凸、太ると半径が増える)、近づく向きなら −1(穴)。"""
    point = _interior_points(face, 1)[0]
    n = g.face_normal(face)
    kind = definition[0]
    if kind == "sphere":
        centre = definition[1]
        radial = tuple(point[i] - centre[i] for i in range(3))
    else:
        origin, direction = definition[1], definition[2]
        v = tuple(point[i] - origin[i] for i in range(3))
        along = _dot(v, direction)
        radial = tuple(v[i] - along * direction[i] for i in range(3))
        if kind == "torus":
            length = math.sqrt(_dot(radial, radial)) or 1.0
            ring = tuple(origin[i] + radial[i] / length * definition[3] for i in range(3))
            radial = tuple(point[i] - ring[i] for i in range(3))
    return 1.0 if _dot(radial, n) > 0 else -1.0


def _interior_points(face, count):
    """面の内側(境界の外は除く)の点を最大 `count` 個、**面全体から均等に間引いて**返す
    (格子の先頭から取ると UV の片側に寄り、段の削除で面の上半分が消えたのを見逃した)。"""
    u1, u2, v1, v2 = BRepTools.UVBounds_s(face)
    a = BRepAdaptor_Surface(face)
    found = []
    steps = max(3, int(math.ceil(math.sqrt(count * 4))))
    for i in range(1, steps):
        for j in range(1, steps):
            u = u1 + (u2 - u1) * i / steps
            v = v1 + (v2 - v1) * j / steps
            if BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-7).State() == TopAbs_State.TopAbs_IN:
                p = a.Value(u, v)
                found.append((p.X(), p.Y(), p.Z()))
    if not found:
        raise FaceEditError("could not find an interior point of a face")
    if len(found) <= count:
        return found
    return [found[round(k * (len(found) - 1) / max(1, count - 1))] for k in range(count)]


def lies_on(face, source_face, distance=0.0):
    """`face` が `source_face`(を `distance` ずらした曲面)に乗っているか。解析曲面は定義、自由曲面は内部の点で見る。"""
    expected = offset_definition(source_face, distance) if distance else describe_surface(source_face)
    actual = describe_surface(face)
    if expected is not None:
        return same_surface(expected, actual)
    if distance:
        return False  # 自由曲面のずらしは比べられない(断る側に倒す)
    surface = BRep_Tool.Surface_s(source_face)
    for point in _interior_points(face, 9):
        projector = GeomAPI_ProjectPointOnSurf(gp_Pnt(*point), surface)
        if projector.NbPoints() == 0 or projector.LowerDistance() > LINEAR_TOL:
            return False
    return True


# --------------------------------------------------------------------------------------
# 構造の検査
# --------------------------------------------------------------------------------------
def _face_map(shape):
    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_FACE, m)
    return m


def structural_gate(result):
    """結果全体がちょうど1ソリッドで、外に面が無く、壊れていない。"""
    solids = g.solids_of(result)
    if len(solids) != 1:
        raise FaceEditError(f"result has {len(solids)} solids")
    if len(g.faces_of(result)) != len(g.faces_of(solids[0])):
        raise FaceEditError("result has faces outside its solid")
    if not g.is_valid(result):
        raise FaceEditError("BRepCheck invalid")
    if g.free_edge_count(result):
        raise FaceEditError("result has free edges")
    if not g.volume(result) > 0:
        raise FaceEditError("result has no volume")
    tiny = [f for f in g.faces_of(result) if g.face_area(f) < MIN_FACE_AREA]
    if tiny:
        raise FaceEditError(f"{len(tiny)} face(s) with area < {MIN_FACE_AREA:g}")


def wrap_checked(raw):
    """MakeOffset の生の結果を1ソリッドにする。**包む前に**、ソリッド1つか閉じたシェル1つだけで、その外に面が無いことを見る
    (シェルだけを包むと、外に付いた独立の面が後の検査から見えなくなる。astra の2回目の指摘)。"""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
    from OCP.BRepCheck import BRepCheck_Shell, BRepCheck_Status
    from OCP.BRepLib import BRepLib

    if raw is None or raw.IsNull():
        raise FaceEditError("empty result")
    solids = g.solids_of(raw)
    if solids:
        if len(solids) != 1 or len(g.faces_of(raw)) != len(g.faces_of(solids[0])):
            raise FaceEditError("raw result is not exactly one solid")
        return raw
    shells = _subshapes(raw, TopAbs_ShapeEnum.TopAbs_SHELL)
    if len(shells) != 1 or len(g.faces_of(raw)) != len(g.faces_of(shells[0])):
        raise FaceEditError("raw result is not exactly one shell")
    shell = TopoDS.Shell_s(shells[0])
    if BRepCheck_Shell(shell).Closed() != BRepCheck_Status.BRepCheck_NoError:
        raise FaceEditError("raw shell is not closed")
    solid = BRepBuilderAPI_MakeSolid(shell).Solid()
    if not BRepLib.OrientClosedSolid_s(solid):
        raise FaceEditError("could not orient the closed shell")
    return solid


def _subshapes(shape, kind):
    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, m)
    return [m.FindKey(i) for i in range(1, m.Extent() + 1)]


def _positions(final_map, shapes_):
    return sorted({final_map.FindIndex(s) for s in shapes_} - {0})


# --------------------------------------------------------------------------------------
# 削除(A)
# --------------------------------------------------------------------------------------
def strict_delete(shape, faces):
    result, algo = methods.defeature(shape, faces)
    structural_gate(result)
    history = algo.History()
    before, after = _face_map(shape), _face_map(result)
    table = {}
    for i in range(1, before.Extent() + 1):
        old = TopoDS.Face_s(before.FindKey(i))
        selected = any(old.IsSame(f) for f in faces)
        if selected:
            if not history.IsRemoved(old):
                raise FaceEditError(f"face {i} was selected but is not removed")
            table[i] = []
            continue
        images = [TopoDS.Face_s(x) for x in history.Modified(old)] or ([] if history.IsRemoved(old) else [old])
        positions = _positions(after, images)
        if not positions:
            raise FaceEditError(f"kept face {i} is lost")
        for p in positions:
            image = TopoDS.Face_s(after.FindKey(p))
            if describe_surface(old) is not None and not lies_on(image, old):
                raise FaceEditError(f"kept face {i} left its own surface")
        if not _region_kept(old, [TopoDS.Face_s(after.FindKey(p)) for p in positions]):
            raise FaceEditError(f"kept face {i} lost part of its original region (the body collapsed to another closure)")
        table[i] = positions
    covered = {p for v in table.values() for p in v}
    new = [j for j in range(1, after.Extent() + 1) if j not in covered]
    if new:
        raise FaceEditError(f"faces {new} do not come from any kept face")
    return result, table, new


def _region_kept(old, images):
    """残した面の元の範囲(内部の点)が、どれかの像の面の上に残っているか。

    面の削除は残す面を延ばすだけで削らないはずなので、元の範囲は結果の表面に残る。残らないのは、残した面の反対側が
    つぶれて別の閉じ方になったとき(フィレット1辺の箱で下面と奥面を消すと 23785 → 214.6 mm³ の破片になり、
    ほかの検査は全部通った。viz.py の全組み合わせで実測)。"""
    from OCP.BRepClass import BRepClass_FaceClassifier

    # 点は面全体に散らす(`_interior_points` の先頭の数点は UV の片側に寄り、段の削除で上半分が消えたのを見逃した)
    for point in _interior_points(old, 49):
        on_some = False
        for image in images:
            surface = BRep_Tool.Surface_s(image)
            projector = GeomAPI_ProjectPointOnSurf(gp_Pnt(*point), surface)
            if projector.NbPoints() == 0 or projector.LowerDistance() > LINEAR_TOL:
                continue
            u, v = _into_face_period(image, *projector.LowerDistanceParameters())
            state = BRepClass_FaceClassifier(image, gp_Pnt2d(u, v), 1e-7).State()
            if state in (TopAbs_State.TopAbs_IN, TopAbs_State.TopAbs_ON):
                on_some = True
                break
        if not on_some:
            return False
    return True


def _into_face_period(face, u, v):
    """周期面(円筒・円錐・球・トーラス)では、射影の角度が面の持つ範囲から一周ずれて返ることがある。面の UV の範囲へ寄せる
    (寄せないと面の内側の点を「外」と判定し、実物の STEP の穴・丸めの正しい削除を断った。fan_120mm_stand・hanger_bar で実測)。"""
    u1, u2, v1, v2 = BRepTools.UVBounds_s(face)
    adaptor = BRepAdaptor_Surface(face)
    if adaptor.IsUPeriodic():
        period = adaptor.UPeriod()
        u = u1 + math.fmod(math.fmod(u - u1, period) + period, period)
    if adaptor.IsVPeriodic():
        period = adaptor.VPeriod()
        v = v1 + math.fmod(math.fmod(v - v1, period) + period, period)
    return u, v


# --------------------------------------------------------------------------------------
# 移動(B)
# --------------------------------------------------------------------------------------
def strict_offset(shape, faces, distance):
    raw, algo = methods.offset_faces(shape, faces, distance)
    result = wrap_checked(raw)
    structural_gate(result)
    return result, check_offset_correspondence(shape, faces, distance, result, lambda old: list(algo.Generated(old)))


def check_offset_correspondence(shape, faces, distance, result, images_of, exempt=()):
    """旧面 → 新面が1対1で全部を覆い、各新面が対応元の曲面(選んだ面ならずらした曲面)に乗る。"""
    before, after = _face_map(shape), _face_map(result)
    table = {}
    for i in range(1, before.Extent() + 1):
        old = TopoDS.Face_s(before.FindKey(i))
        positions = _positions(after, images_of(old))
        if len(positions) != 1:
            raise FaceEditError(f"face {i} maps to {len(positions)} faces (topology changed)")
        new = TopoDS.Face_s(after.FindKey(positions[0]))
        selected = any(old.IsSame(f) for f in faces)
        if not lies_on(new, old, distance if selected else 0.0):
            raise FaceEditError(f"face {i} is not where it should be ({'moved' if selected else 'kept'} face)")
        table[i] = positions
    covered = sorted(p for v in table.values() for p in v)
    rest = [j for j in range(1, after.Extent() + 1) if j not in covered and j not in exempt]
    if len(set(covered)) != len(covered) or rest:
        raise FaceEditError("old and new faces are not one-to-one")
    change = g.volume(result) - g.volume(shape)
    if change * distance <= 0:
        raise FaceEditError(f"volume moved the wrong way ({change:+.6g} for distance {distance:+g})")
    return table


# --------------------------------------------------------------------------------------
# D(構造で認識した丸めだけ追従させる)
# --------------------------------------------------------------------------------------
def _edge_faces(shape):
    m = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_ShapeEnum.TopAbs_EDGE, TopAbs_ShapeEnum.TopAbs_FACE, m)
    return m


def _other_face(edge_faces, edge, face):
    index = edge_faces.FindIndex(edge)
    if index == 0:
        return None
    others = [TopoDS.Face_s(f) for f in edge_faces.FindFromIndex(index) if not f.IsSame(face)]
    return others[0] if len(others) == 1 else None


def _tangent(edge, a, b):
    return ChFi3d.IsTangentFaces_s(edge, a, b, GeomAbs_Shape.GeomAbs_G1)


def _is_spring_edge(edge, definition):
    """丸めの長手の辺(円筒なら軸に平行な直線、トーラスなら軸と同軸の円)か。"""
    curve = BRepAdaptor_Curve(edge)
    kind = definition[0]
    if kind == "cylinder" and curve.GetType() == GeomAbs_CurveType.GeomAbs_Line:
        return _parallel(_vec(curve.Line().Direction()), definition[2])
    if kind == "torus" and curve.GetType() == GeomAbs_CurveType.GeomAbs_Circle:
        circle = curve.Circle()
        return _parallel(_vec(circle.Axis().Direction()), definition[2]) and _point_line_distance(
            _vec(circle.Location()), definition[1], definition[2]
        ) < LINEAR_TOL
    return False


def blend_signature(shape, face, edge_faces):
    """`face` が一定半径の丸めなら (半径, 支えの面2枚)。丸めでなければ None。

    丸め = 円筒またはトーラスで、**長手の辺**(円筒は軸に平行な直線、トーラスは同軸の円)がちょうど2本の別の面に
    接線(G1)で接し、その2枚(支えの面)が互いに別の面であること。半径の大小は見ない(意匠の大きな丸めも丸め)。
    """
    definition = describe_surface(face)
    if definition is None or definition[0] not in ("cylinder", "torus"):
        return None
    radius = definition[3] if definition[0] == "cylinder" else definition[4]
    supports = []
    for edge in g.edges_of(face):
        if BRep_Tool.Degenerated_s(edge) or not _is_spring_edge(edge, definition):
            continue
        other = _other_face(edge_faces, edge, face)
        if other is None or other.IsSame(face) or not _tangent(edge, face, other):
            return None
        if not any(other.IsSame(s) for s in supports):
            supports.append(other)
    if len(supports) != 2:
        return None
    return radius, supports[0], supports[1]


def find_structural_blends(shape, faces):
    """選んだ面に接線でつながる丸めの連なり(角の球を含む)。丸めと認めない接線の隣があれば断る。"""
    edge_faces = _edge_faces(shape)
    blends, supports_of = [], {}
    frontier = list(faces)
    seen = list(faces)
    own_supports, radius_of = {}, {}
    while frontier:
        current = frontier.pop(0)
        for edge in g.edges_of(current):
            other = _other_face(edge_faces, edge, current)
            if other is None or any(other.IsSame(s) for s in seen) or not _tangent(edge, current, other):
                continue
            if any(other.IsSame(s) for s in own_supports.get(id(current), ())):
                continue  # 丸めの支えの面(たどらない)
            kind = g.surface_type(other)
            if kind == "sphere" and any(current.IsSame(b) for b in blends):
                current_radius = radius_of.get(id(current))
                if current_radius is None or abs(describe_surface(other)[2] - current_radius) > LINEAR_TOL:
                    raise FaceEditError("a sphere next to a blend has a different radius (not a corner of the blend)")
                radius_of[id(other)] = current_radius
                seen.append(other)
                blends.append(other)
                frontier.append(other)
                continue
            signature = blend_signature(shape, other, edge_faces)
            if signature is None:
                if any(current.IsSame(f) for f in faces) or kind != "plane":
                    raise FaceEditError(f"a tangent neighbour ({kind}) is not a constant-radius blend")
                continue  # 丸めの支えの平面(選んでいない)。たどらない
            seen.append(other)
            blends.append(other)
            supports_of[len(blends) - 1] = signature
            own_supports[id(other)] = signature[1:]
            radius_of[id(other)] = signature[0]
            frontier.append(other)
    return blends, [s for s in supports_of.values()]


def strict_follow(shape, faces, distance):
    blends, supports = find_structural_blends(shape, faces)
    if not blends:
        result, table = strict_offset(shape, faces, distance)
        return result, table, []
    for radius, a, b in supports:
        if any(a.IsSame(f) or b.IsSame(f) for f in blends):
            raise FaceEditError("a blend rests on another blend")
    sharp, removal = methods.defeature(shape, blends)
    structural_gate(sharp)
    history = removal.History()

    def through_removal(face):
        images = [TopoDS.Face_s(x) for x in history.Modified(face)] or ([] if history.IsRemoved(face) else [face])
        if len(images) != 1:
            raise FaceEditError("a face split while removing the blends")
        return _as_in(sharp, images[0])

    moved_faces = [through_removal(f) for f in faces]
    raw, offset = methods.offset_faces(sharp, moved_faces, distance)
    moved = wrap_checked(raw)
    structural_gate(moved)
    sharp_table = check_offset_correspondence(sharp, moved_faces, distance, moved, lambda old: list(offset.Generated(old)))
    sharp_map, moved_map = _face_map(sharp), _face_map(moved)

    def to_moved(face_in_shape):
        position = sharp_map.FindIndex(through_removal(face_in_shape))
        return TopoDS.Face_s(moved_map.FindKey(sharp_table[position][0]))

    edge_faces = _edge_faces(moved)
    maker = BRepFilletAPI_MakeFillet(moved)
    rebuilt_edges, rebuilt_edges_with_supports = [], []
    for radius, a, b in supports:
        fa, fb = to_moved(a), to_moved(b)
        common = []
        for edge in g.edges_of(fa):
            other = _other_face(edge_faces, edge, fa)
            if other is not None and other.IsSame(fb) and not any(edge.IsSame(e) for e, _ in rebuilt_edges):
                common.append(edge)
        if len(common) != 1:
            raise FaceEditError(f"supports of a blend share {len(common)} edges after the move")
        rebuilt_edges.append((common[0], radius))
        rebuilt_edges_with_supports.append((common[0], radius, fa, fb))
    for edge, radius in rebuilt_edges:
        maker.Add(radius, edge)
    maker.Build()
    if not maker.IsDone():
        raise FaceEditError("re-blending failed (IsDone is false)")
    final = maker.Shape()
    structural_gate(final)

    final_map = _face_map(final)
    final_edge_faces = _edge_faces(final)
    regenerated = []
    for edge, radius, fa, fb in rebuilt_edges_with_supports:
        made = sorted({final_map.FindIndex(x) for x in maker.Generated(edge)} - {0})
        if not made:
            raise FaceEditError("re-blending produced no face for an edge")
        supports_after = [x for f in (fa, fb) for x in (list(maker.Modified(f)) or [f])]
        for position in made:
            face = TopoDS.Face_s(final_map.FindKey(position))
            definition = describe_surface(face)
            if definition is None or definition[0] not in ("cylinder", "torus"):
                raise FaceEditError("a re-blended face is not a cylinder or torus")
            r = definition[3] if definition[0] == "cylinder" else definition[4]
            if abs(r - radius) > LINEAR_TOL:
                raise FaceEditError("a re-blended face has the radius of another blend")
            touching = 0
            for spring in g.edges_of(face):
                if not _is_spring_edge(spring, definition):
                    continue
                neighbour = _other_face(final_edge_faces, spring, face)
                if neighbour is None or not any(neighbour.IsSame(x) for x in supports_after) or not _tangent(spring, face, neighbour):
                    raise FaceEditError("a re-blended face is not tangent to its supports")
                touching += 1
            if touching < 2:
                raise FaceEditError("a re-blended face does not rest on both supports")
        regenerated += made
    # 角の球は辺でなく頂点から生まれる(全辺丸めの上面で実測)。頂点に集まる丸めの半径がそろっているときだけ認める
    for edge, radius, _, _ in rebuilt_edges_with_supports:
        for vertex in _vertices(edge):
            made = sorted({final_map.FindIndex(x) for x in maker.Generated(vertex)} - {0})
            for position in made:
                definition = describe_surface(TopoDS.Face_s(final_map.FindKey(position)))
                if definition is None or definition[0] != "sphere" or abs(definition[2] - radius) > LINEAR_TOL:
                    raise FaceEditError("a corner made by re-blending is not a sphere of the blend radius")
            regenerated += made
    regenerated = sorted(set(regenerated))
    # 由来: 元の面 → 丸めを外した形 → 動かした形 → 丸め直した形。旧丸めは deleted、丸め直した面は new_faces
    before = _face_map(shape)
    table = {}
    for i in range(1, before.Extent() + 1):
        old = TopoDS.Face_s(before.FindKey(i))
        if any(old.IsSame(b) for b in blends):
            table[i] = []
            continue
        in_moved = to_moved(old)
        images = [TopoDS.Face_s(x) for x in maker.Modified(in_moved)] or [in_moved]
        positions = _positions(final_map, images)
        if len(positions) != 1:
            raise FaceEditError(f"face {i} does not map to exactly one final face")
        selected = any(old.IsSame(f) for f in faces)
        if not lies_on(TopoDS.Face_s(final_map.FindKey(positions[0])), old, distance if selected else 0.0):
            raise FaceEditError(f"face {i} is not where it should be after re-blending")
        table[i] = positions
    covered = [p for v in table.values() for p in v]
    if len(set(covered)) != len(covered):
        raise FaceEditError("two old faces map to the same final face")
    if set(covered) & set(regenerated):
        raise FaceEditError("a kept face is also counted as re-blended")
    stray = [j for j in range(1, final_map.Extent() + 1) if j not in covered and j not in regenerated]
    if stray:
        raise FaceEditError(f"faces {stray} come from nowhere")
    change = g.volume(final) - g.volume(shape)
    if change * distance <= 0:
        raise FaceEditError(f"volume moved the wrong way ({change:+.6g})")
    return final, table, regenerated


def _vertices(edge):
    from OCP.TopExp import TopExp_Explorer

    out = []
    explorer = TopExp_Explorer(edge, TopAbs_ShapeEnum.TopAbs_VERTEX)
    while explorer.More():
        out.append(explorer.Current())
        explorer.Next()
    return out


def _as_in(shape, face):
    for occurrence in g.faces_of_oriented(shape):
        if occurrence.IsSame(face):
            return occurrence
    raise FaceEditError("face is not in the shape")


# --------------------------------------------------------------------------------------
def run(case, method):
    if method == "SA":
        result, table, new = strict_delete(case.shape, case.faces)
        return result, {"deleted": [k for k, v in table.items() if not v], "new_faces": new}
    if method == "SB":
        result, table = strict_offset(case.shape, case.faces, case.distance)
        return result, {"deleted": [], "new_faces": []}
    if method == "SD":
        result, table, regenerated = strict_follow(case.shape, case.faces, case.distance)
        return result, {"deleted": [k for k, v in table.items() if not v], "new_faces": regenerated}
    raise SystemExit(f"unknown method {method}")


def main(name, method):
    case = next(c for c in shapes.all_cases() if c.name == name)
    record = {"case": name, "method": method, "note": case.note, "tags": case.tags, "faces_before": len(g.faces_of(case.shape))}
    started = time.perf_counter()
    try:
        result, provenance = run(case, method)
    except Exception as error:  # noqa: BLE001
        record.update(verdict="refused", error=f"{type(error).__name__}: {error}", ms=1000 * (time.perf_counter() - started))
        return record
    record.update(ms=1000 * (time.perf_counter() - started), volume=g.volume(result), faces=len(g.faces_of(result)), **provenance)
    if case.expected is None:
        record.update(verdict="wrong", error="passed although it should fail")
    elif case.expected is shapes.UNKNOWN:
        record.update(verdict="recorded")
    else:
        diff = g.symmetric_difference(result, case.expected)
        record.update(symdiff=diff, expected_faces=len(g.faces_of(case.expected)))
        record.update(verdict="match" if diff <= 1e-6 * max(1.0, g.volume(case.expected)) else "wrong")
    return record


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1], sys.argv[2]), ensure_ascii=False))
