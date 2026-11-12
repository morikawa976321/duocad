"""面のオフセットのうち「端ごと引き伸ばす」場合の幾何(OCPを触るカーネル層)。決定と実測は docs/FACE_EDIT.md「端ごと引き伸ばす」。

選んだ面がどれも平面で法線が ±n にそろい、n に沿って一番手前が内向き(−n)・一番先が外向き(+n)の面(端の壁の内面と外面)のとき、
`offset_faces` はこれで作る(溝の両壁は手前が外向きなので当たらない)。
n に垂直な切り口 P を、選んだ面に一番近い**柱の区間**(そこを横切る面と辺が、どれも n 方向へ並進しても変わらない区間)に置き、
P より先を d·n 並進して、間を P での断面の柱(区間の中から切り出した板の写し)で埋める。区間の中なら P をどこに置いても同じ形になる。
縮める(d < 0)ときは、同じ区間の中から厚さ |d| の板を抜いて詰める。

`plans` は位相と境界箱だけを見る(親でも子でも呼べる)。`stretch` は子プロセスの中で形を作り、関門(`kernel_face_edit` の
構造の関門と、旧面と新面の1対1・各面が元の曲面かそれを並進した曲面に乗る・体積が断面積 × d だけ変わる)を通す。
依存は kernel_face_stretch → kernel_face_edit(`kernel_face_edit` の側は呼ぶときにだけこれを読む)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

from OCP.BOPAlgo import BOPAlgo_GlueEnum
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse, BRepAlgoAPI_Splitter
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_Transform
from OCP.Bnd import Bnd_Box
from OCP.GeomAbs import GeomAbs_CurveType, GeomAbs_SurfaceType
from OCP.GProp import GProp_GProps
from OCP.BRepGProp import BRepGProp
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp
from OCP.TopTools import TopTools_ListOfShape, TopTools_MapOfShape
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Dir, gp_Pln, gp_Pnt, gp_Quaternion, gp_Trsf, gp_Vec

from . import kernel_face_edit as _edit

from . import (
    brep_geometry as _brep_geometry,
    brep_history as _brep_history,
    brep_topology as _brep_topology,
)


#: 柱の区間がこれより短ければ切り口を置かない[mm]。置いた値(切り口から一番近い形まで半分の 0.005 mm を残す)
MIN_SPAN = 0.01
#: 向きの許容差(法線が n に垂直・平行とみなす |cos| のずれ)。置いた値で、`kernel_face_edit.ANGULAR_TOL` と同じ
_ANGULAR_TOL = 1e-9
#: 座標軸に沿うとみなす、ほかの2成分の大きさの上限。0.001° 傾けた箱で、箱の x 成分と n への内積が 5e-4 mm ずれ、
#: 継ぎ目の辺を見落として断った(実測)。これ以下なら 1 m 先でも 1e-9 mm
_ALIGNED_TOL = 1e-12
#: 体積の増減が断面積 × d と合うとみなす相対差。置いた値(試作の再現ケースで差は 1e-9 未満)
_VOLUME_RTOL = 1e-6
#: 伸ばすときに間を埋める板の写しの数の上限(伸ばす量 ÷ 区間の長さ)。置いた値(数十 mm の区間なら 1〜2 枚)
MAX_COPIES = 64

_HINT = "端ごと引き伸ばせない"


@dataclass(frozen=True)
class Plan:
    """引き伸ばしの向きと切り口の区間。座標 s は `direction` に沿った位置(s = 点 · direction)。"""

    #: 動かない側から選んだ面の側へ向かう単位ベクトル(正の距離で端がこの向きへ出る)
    direction: Tuple[float, float, float]
    #: 柱の区間 (low, high)。切り口はこの中に置く
    low: float
    high: float
    #: 切り口より先にある、n に垂直な平面のうち選んでいないもの(1始まり)
    missing: Tuple[int, ...]
    #: 選んだ面のうち n に沿って一番手前が内向き(−n)、一番先が外向き(+n)(端の壁の内面と外面)
    is_end: bool
    #: `missing` も選べば `is_end` になる
    is_end_with_missing: bool
    #: 各面(添字順)の s の範囲
    spans: Tuple[Tuple[float, float], ...]

    @property
    def applies(self) -> bool:
        return self.is_end and not self.missing


def plans(shape: Any, face_indices: Sequence[int]) -> List[Plan]:
    """選んだ面(1始まり)を端として引き伸ばせる向きごとの計画(0〜2件)。選んだ面が平面でない・法線が平行でないなら空。"""
    face_map = _brep_topology._face_map(shape)
    selected = [int(index) for index in face_indices]
    normals = _parallel_plane_normals(face_map, selected)
    if normals is None:
        return []
    axis = _unit(normals[0])
    spans, invariant, facing, edge_events = _measure(shape, face_map, axis)
    free = _free_intervals(spans, invariant, edge_events)
    found = []
    for sign in (1.0, -1.0):
        direction = tuple(sign * value for value in axis)
        along = [(min(sign * a, sign * b), max(sign * a, sign * b)) for a, b in spans]
        nearest = min(along[index - 1][0] for index in selected)
        candidates = [
            interval
            for interval in ((min(sign * a, sign * b), max(sign * a, sign * b)) for a, b in free)
            if interval[1] <= nearest
        ]
        if not candidates:
            continue
        low, high = max(candidates, key=lambda interval: interval[1])
        beyond = [index for index, (a, _) in enumerate(along, 1) if a >= high]
        missing = tuple(index for index in beyond if facing[index - 1] and index not in selected)
        outward = {index: _edit._dot(normal, direction) > 0 for index, normal in zip(selected, normals)}
        with_missing = dict(outward)
        for index in missing:
            normal = _brep_geometry._face_edit_face_normal(_brep_topology._face_at(face_map, index))
            with_missing[index] = _edit._dot(normal, direction) > 0
        found.append(
            Plan(
                direction=direction,
                low=low,
                high=high,
                missing=missing,
                is_end=_closes_an_end(outward, along),
                is_end_with_missing=_closes_an_end(with_missing, along),
                spans=tuple(along),
            )
        )
    return found


def _closes_an_end(outward: dict, along: list) -> bool:
    """面(添字 → 外向き法線が n を向くか)を n に沿って並べたとき、一番手前が内向きで一番先が外向きか(間に肉がある端の壁)。"""
    order = sorted(outward, key=lambda index: along[index - 1][0])
    return not outward[order[0]] and outward[order[-1]]


def applicable(shape: Any, face_indices: Sequence[int]) -> Optional[Plan]:
    """`offset_faces` が引き伸ばしで作る計画。ちょうど1つの向きで、端の壁の内面と外面を選び(`Plan.is_end`)、切り口より先の
    n に垂直な平面をすべて選んでいるときだけ。それ以外は None(今のオフセットで作る)。"""
    normals = _parallel_plane_normals(_brep_topology._face_map(shape), [int(index) for index in face_indices])
    if normals is None or len({_edit._dot(normal, normals[0]) > 0 for normal in normals}) != 2:
        return None
    found = [plan for plan in plans(shape, face_indices) if plan.applies]
    return found[0] if len(found) == 1 else None


def _parallel_plane_normals(face_map: Any, selected: List[int]) -> Optional[List[tuple]]:
    """選んだ面の外向き法線。どれも平面で法線が平行なときだけ(それ以外は None)。"""
    normals = []
    for index in selected:
        face = _brep_topology._face_at(face_map, index)
        if _edit._surface_type(face) != "plane":
            return None
        normals.append(_brep_geometry._face_edit_face_normal(face))
    axis = _unit(normals[0])
    if any(abs(abs(_edit._dot(normal, axis)) - 1.0) > _ANGULAR_TOL for normal in normals):
        return None
    return normals


def suggestion(shape: Any, face_indices: Sequence[int]) -> Optional[str]:
    """今のオフセットで断られた選択に、足せば引き伸ばせる面があれば、それを名指しする言い換え。"""
    found = [plan for plan in plans(shape, face_indices) if plan.missing and plan.is_end_with_missing]
    if len(found) != 1:
        return None
    faces = "・".join(str(index) for index in found[0].missing)
    return f"面 {faces} も一緒に選ぶと、端ごと引き伸ばせる(切り口より先を平行移動し、間を柱で埋める)"


def stretch(shape: Any, face_indices: Sequence[int], distance: float):
    """引き伸ばした形と、旧面(1始まり)→ 新面の位置の表。断ったら `kernel_face_edit.FaceEditError`。"""
    selected = [int(index) for index in face_indices]
    plan = _the_plan(shape, selected)
    direction, d = plan.direction, float(distance)
    span = plan.high - plan.low
    room = span - MIN_SPAN
    if d < 0 and -d > room:
        raise _edit.FaceEditError(
            f"shrinking by {-d:g} does not fit in the prismatic span {span:g}",
            f"{_HINT}: 縮める量が、切り口を置ける区間の長さ {span:.4g} mm を超える",
        )
    copies = math.ceil(d / room) if d > 0 else 0
    if copies > MAX_COPIES:
        raise _edit.FaceEditError(
            f"stretching by {d:g} needs {copies} copies of the prismatic slab (span {span:g})",
            f"{_HINT}: 伸ばす量が、切り口を置ける区間の長さ {span:.4g} mm に比べて大きすぎる",
        )
    width = d / copies if d > 0 else -d
    cuts = [plan.low + MIN_SPAN / 2, plan.low + MIN_SPAN / 2 + width]

    splitter = _split(shape, direction, cuts)
    lower, middle, beyond = [], [], []
    for solid in _brep_topology._face_edit_solids(splitter.Shape()):
        position = _along(_centre(solid), direction)
        (lower if position < cuts[0] else beyond if position > cuts[1] else middle).append(solid)
    if not lower or not middle or not beyond:
        raise _edit.FaceEditError("the cut did not leave material on both sides", _HINT)
    section = [face for solid in lower for face in _faces_on(solid, direction, cuts[0])]
    if not section:
        raise _edit.FaceEditError("the cut has no section", _HINT)
    area = sum(_brep_geometry._face_edit_face_area(face) for face in section)

    # 間は中の板(元の形の切れ端)の写しで埋める。柱(`BRepPrimAPI_MakePrism`)で埋めると、全周の円筒の側面が軸の向きと閉じ目の角度の
    # 違う円筒になり、統合で元の円筒の切れ端と1枚に戻らなかった(実測)。写しなら面はどれも元の面を並進したもの
    def moved(solids: List[Any], offset: float) -> List[Any]:
        if offset == 0.0:
            return [None] * len(solids)
        shift = gp_Trsf()
        shift.SetTranslation(gp_Vec(*(offset * value for value in direction)))
        return [BRepBuilderAPI_Transform(solid, shift, True) for solid in solids]

    shifts =[(beyond, d)] + ([(middle, k * width) for k in range(copies + 1)] if d > 0 else [])
    placed = [(solids, moved(solids, offset)) for solids, offset in shifts]
    fuse = BRepAlgoAPI_Fuse()
    arguments, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
    for solid in lower:
        arguments.Append(solid)
    for solids, makers in placed:
        for solid, maker in zip(solids, makers):
            tools.Append(solid if maker is None else maker.Shape())
    fuse.SetArguments(arguments)
    fuse.SetTools(tools)
    fuse.SetGlue(BOPAlgo_GlueEnum.BOPAlgo_GlueShift)
    fuse.Build()
    if not fuse.IsDone():
        raise _edit.FaceEditError("fusing the stretched pieces failed", _HINT)
    seams = [cuts[0] + k * width for k in range(copies + 2)] if d > 0 else [cuts[0]]
    unifier = ShapeUpgrade_UnifySameDomain(fuse.Shape(), True, True, False)
    unifier.KeepShapes(_edges_off(fuse.Shape(), direction, seams))
    unifier.Build()
    final = unifier.Shape()
    _edit._structural_gate(final, hint=_HINT)

    final_map = _brep_topology._face_map(final)
    lower_faces = _brep_topology.indexed_map(_brep_topology.compound(lower), TopAbs_ShapeEnum.TopAbs_FACE)
    owners = [
        (_brep_topology.indexed_map(solid, TopAbs_ShapeEnum.TopAbs_FACE), maker)
        for solids, makers in placed
        for solid, maker in zip(solids, makers)
    ]

    def images(old: Any) -> List[Any]:
        pieces = _brep_history.shape_list(splitter, "Modified", old)
        if not pieces and not _brep_history.is_removed(splitter, old):
            pieces = [old]
        found = []
        for piece in pieces:
            places = [piece] if lower_faces.Contains(piece) else [
                piece if maker is None else maker.ModifiedShape(piece) for faces, maker in owners if faces.Contains(piece)
            ]
            for place in places:
                fused = _brep_history.shape_list(fuse, "Modified", place)
                if not fused and not _brep_history.is_removed(fuse, place):
                    fused = [place]
                found += [x for y in fused for x in _brep_history.through_history(unifier.History(), y)]
        return found

    before = _brep_topology._face_map(shape)
    translation = tuple(d * value for value in direction)
    table = {}
    for i in range(1, before.Extent() + 1):
        old = TopoDS.Face_s(before.FindKey(i))
        positions = _brep_history.positions_in(final_map, images(old))
        if len(positions) != 1:
            raise _edit.FaceEditError(f"face {i} maps to {len(positions)} faces after stretching", _HINT)
        new = TopoDS.Face_s(final_map.FindKey(positions[0]))
        beyond = plan.spans[i - 1][0] >= plan.high
        if not (_lies_on_translated(new, old, translation) if beyond else _edit._lies_on(new, old)):
            raise _edit.FaceEditError(
                f"face {i} is not where it should be ({'moved' if beyond else 'kept'} face)", _HINT
            )
        table[i] = positions
    covered = sorted(p for positions in table.values() for p in positions)
    if len(set(covered)) != len(covered):
        raise _edit.FaceEditError("two old faces map to the same new face", _HINT)
    stray = sorted(set(range(1, final_map.Extent() + 1)) - set(covered))
    if stray:
        raise _edit.FaceEditError(f"faces {stray} come from nowhere (the seams did not merge)", _HINT)
    expected = _brep_geometry.volume(shape) + area * d
    if abs(_brep_geometry.volume(final) - expected) > _VOLUME_RTOL * max(1.0, abs(expected)):
        raise _edit.FaceEditError(
            f"volume {_brep_geometry.volume(final):.6g} is not the section area times the distance ({expected:.6g})", _HINT
        )
    return final, table


def _the_plan(shape: Any, selected: List[int]) -> Plan:
    found = plans(shape, selected)
    if not found:
        raise _edit.FaceEditError(
            "no prismatic span to cut the body across",
            f"{_HINT}: 選んだ面がどれも平行な平面で、手前に切り口を置ける区間(そこを横切る面が動かす向きに伸びるだけの区間)があるときだけ",
        )
    usable = [plan for plan in found if plan.applies]
    if len(usable) == 1:
        return usable[0]
    if len(usable) > 1:
        raise _edit.FaceEditError("the body can be stretched toward both sides", f"{_HINT}: 両側のどちらを伸ばすか決まらない")
    plan = found[0]
    if plan.missing:
        faces = "・".join(str(index) for index in plan.missing)
        raise _edit.FaceEditError(
            f"faces {list(plan.missing)} beyond the cut face the move direction but are not selected",
            f"{_HINT}: 切り口より先にある面 {faces} も選ぶ",
        )
    raise _edit.FaceEditError("the selection does not close an end wall", f"{_HINT}: 端の壁の外面と内面を一緒に選ぶ")


def _measure(shape: Any, face_map: Any, axis: tuple):
    """各面の `axis` に沿った範囲・`axis` 方向の並進で変わらない面か・`axis` に垂直な平面か(どれも添字順)と、
    切り口を置けない範囲のうち辺から来るもの(`axis` に平行な直線は両端の点、それ以外は辺の範囲全体)。

    面の範囲は、平面と並進で変わらない面なら辺から取る(`axis` に沿った最も遠い点が境界にある)。ほかの曲面も、緩い境界箱が
    辺の範囲に収まれば辺から取る(辺も同じく両端の頂点から)。最適の境界箱は 86 面・211 辺のボディで1つ 0.9 ms、全部で 0.26 秒
    かかり、計画全体の 97 % だった(実測)。"""
    ruler = _Ruler(axis)
    edge_map = _brep_topology.indexed_map(shape, TopAbs_ShapeEnum.TopAbs_EDGE)
    edge_spans: List[Optional[Tuple[float, float]]] = [None] * (edge_map.Extent() + 1)
    events = []
    for k in range(1, edge_map.Extent() + 1):
        edge = TopoDS.Edge_s(edge_map.FindKey(k))
        if BRep_Tool.Degenerated_s(edge):
            continue
        low, high = edge_spans[k] = ruler.edge_span(edge)
        curve = BRepAdaptor_Curve(edge)
        if curve.GetType() == GeomAbs_CurveType.GeomAbs_Line and _parallel(_edit._vec(curve.Line().Direction()), axis):
            events += [(low, low), (high, high)]
        else:
            events.append((low, high))
    spans, invariant, facing = [], [], []
    for i in range(1, face_map.Extent() + 1):
        face = TopoDS.Face_s(face_map.FindKey(i))
        adaptor = BRepAdaptor_Surface(face)
        kind = adaptor.GetType()
        keeps, faces_axis = False, False
        if kind == GeomAbs_SurfaceType.GeomAbs_Plane:
            normal = _edit._vec(adaptor.Plane().Axis().Direction())
            keeps = abs(_edit._dot(normal, axis)) < _ANGULAR_TOL
            faces_axis = abs(abs(_edit._dot(normal, axis)) - 1.0) < _ANGULAR_TOL
        elif kind == GeomAbs_SurfaceType.GeomAbs_Cylinder:
            keeps = _parallel(_edit._vec(adaptor.Cylinder().Axis().Direction()), axis)
        # n 方向の押し出し面も並進で変わらないが、横切らせない: 割って並進した切れ端を `ShapeUpgrade_UnifySameDomain` が1枚に戻さず
        # (同じ曲面の写しでも、位置だけを動かしても)、楕円の底のある管を断った(sol の指摘で実測)。横切る形は今のオフセットで作る
        bounded = [
            span
            for span in (edge_spans[edge_map.FindIndex(edge)] for edge in _brep_topology._face_edit_edges(face))
            if span is not None
        ]
        span = (min(low for low, _ in bounded), max(high for _, high in bounded)) if bounded else None
        if span is None or not (kind == GeomAbs_SurfaceType.GeomAbs_Plane or keeps):
            loose = ruler.span(face, tight=False)
            if span is None or loose[0] < span[0] - _edit.LINEAR_TOL or loose[1] > span[1] + _edit.LINEAR_TOL:
                span = ruler.span(face)
        spans.append(span)
        invariant.append(keeps)
        facing.append(faces_axis)
    return spans, invariant, facing, events


def _free_intervals(spans: list, invariant: list, edge_events: list) -> List[Tuple[float, float]]:
    """柱の区間: どの面・辺の「置けない範囲」とも重ならない開区間のうち、`MIN_SPAN` より長いもの(`axis` に沿った座標)。
    並進で変わらない面は両端だけを置けない点にする(区間を横切るのは構わない)。"""
    events = list(edge_events)
    for (low, high), keeps in zip(spans, invariant):
        events += [(low, low), (high, high)] if keeps else [(low, high)]
    events.sort()
    found, cursor = [], None
    for low, high in events:
        if cursor is not None and low - cursor > MIN_SPAN:
            found.append((cursor, low))
        cursor = high if cursor is None else max(cursor, high)
    return found


class _Ruler:
    """形の `axis` に沿った範囲を測る。座標軸に沿う向きは箱の成分を読み、斜めの向きは `axis` を x へ回してから読む。"""

    def __init__(self, axis: tuple):
        self.axis = axis
        self.component = next(
            (k for k in range(3) if all(abs(axis[j]) < _ALIGNED_TOL for j in range(3) if j != k)), None
        )
        self.sign = 1.0 if self.component is None or axis[self.component] > 0 else -1.0
        self.rotation = None
        if self.component is None:
            self.rotation = gp_Trsf()
            self.rotation.SetRotation(gp_Quaternion(gp_Vec(*axis), gp_Vec(1.0, 0.0, 0.0)))

    def edge_span(self, edge: Any) -> Tuple[float, float]:
        """辺の範囲。緩い箱が両端の頂点の範囲に収まれば頂点から(直線と、`axis` に沿って膨らまない弧)。"""
        ends = [self.coordinate(BRep_Tool.Pnt_s(vertex)) for vertex in (TopExp.FirstVertex_s(edge), TopExp.LastVertex_s(edge))]
        low, high = min(ends), max(ends)
        loose = self.span(edge, tight=False)
        if loose[0] >= low - _edit.LINEAR_TOL and loose[1] <= high + _edit.LINEAR_TOL:
            return low, high
        return self.span(edge)

    def coordinate(self, point: Any) -> float:
        return _edit._dot((point.X(), point.Y(), point.Z()), self.axis)

    def span(self, shape: Any, *, tight: bool = True) -> Tuple[float, float]:
        """`tight` は許容差で膨らませない最適の箱、そうでなければ曲面の定義から取る緩い箱(形を必ず含む)。"""
        located = shape if self.rotation is None else BRepBuilderAPI_Transform(shape, self.rotation, False).Shape()
        box = Bnd_Box()
        if tight:
            BRepBndLib.AddOptimal_s(located, box, False, False)
        else:
            BRepBndLib.Add_s(located, box, False)
        values = box.Get()
        k = 0 if self.component is None else self.component
        low, high = values[k], values[k + 3]
        return (low, high) if self.sign > 0 else (-high, -low)


def _split(shape: Any, direction: tuple, cuts: List[float]) -> Any:
    """`direction` に垂直で s = `cuts` の平面で `shape` を割る(道具の平面は形の境界箱を覆う大きさ)。"""
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    centre = ((xmin + xmax) / 2, (ymin + ymax) / 2, (zmin + zmax) / 2)
    reach = 2.0 * max(xmax - xmin, ymax - ymin, zmax - zmin, 1.0)
    arguments, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
    arguments.Append(shape)
    for cut in cuts:
        offset = cut - _along(centre, direction)
        origin = gp_Pnt(*(centre[k] + offset * direction[k] for k in range(3)))
        tools.Append(BRepBuilderAPI_MakeFace(gp_Pln(origin, gp_Dir(*direction)), -reach, reach, -reach, reach).Face())
    splitter = BRepAlgoAPI_Splitter()
    splitter.SetArguments(arguments)
    splitter.SetTools(tools)
    splitter.Build()
    if not splitter.IsDone():
        raise _edit.FaceEditError("cutting the body across failed", _HINT)
    return splitter


def _faces_on(solid: Any, direction: tuple, position: float) -> List[Any]:
    """`solid` の面のうち、`direction` に垂直で s = `position` の平面に乗るもの(切り口)。"""
    found = []
    for face in _brep_topology._face_edit_faces(solid):
        definition = _edit._describe(face)
        if (
            definition is not None
            and definition[0] == "plane"
            and _parallel(definition[2], direction)
            and abs(_along(definition[1], direction) - position) < _edit.LINEAR_TOL
        ):
            found.append(face)
    return found


def _edges_off(shape: Any, direction: tuple, seams: List[float]) -> TopTools_MapOfShape:
    """継ぎ目(s = `seams` の平面の中にある辺)以外の、2枚の面を分ける辺。統合でこれらを残し、元から分かれていた同じ曲面の面どうしを
    1枚にしない。周期面の閉じ目の辺は1枚の面にしか属さないので残さない(残すと、切り口で割れて閉じ目の角度がずれた円筒の切れ端どうしを
    1枚に戻せず、全周の円筒が横切る形を断った。sol の指摘で再現)。"""
    ruler = _Ruler(direction)
    closing = TopTools_MapOfShape()
    for face in _brep_topology._face_edit_faces(shape):
        for edge in _brep_topology._face_edit_edges(face):
            if BRep_Tool.IsClosed_s(edge, face):
                closing.Add(edge)
    kept = TopTools_MapOfShape()
    for edge in _brep_topology._face_edit_edges(shape):
        if closing.Contains(edge):
            continue
        low, high = ruler.span(edge)
        if not any(abs(low - seam) < _edit.LINEAR_TOL and abs(high - seam) < _edit.LINEAR_TOL for seam in seams):
            kept.Add(edge)
    return kept


def _lies_on_translated(face: Any, source: Any, translation: tuple) -> bool:
    """`face` が `source` の曲面を `translation` だけ並進した曲面に乗っているか。解析曲面は定義の照合、自由曲面は内部の
    最大9点の射影(`kernel_face_edit._lies_on` と同じ限られた確認)。"""
    definition = _edit._describe(source)
    if definition is not None:
        return _edit._same_surface(_translated(definition, translation), _edit._describe(face))
    surface = BRep_Tool.Surface_s(source).Translated(gp_Vec(*translation))
    return _edit._points_lie_on(face, surface)


def _translated(definition: tuple, translation: tuple) -> tuple:
    """解析曲面の定義(`kernel_face_edit._describe` の形)を並進したもの。位置は2番目の要素だけ。"""
    point = definition[1]
    return (definition[0], tuple(point[k] + translation[k] for k in range(3)), *definition[2:])


def _centre(solid: Any) -> tuple:
    properties = GProp_GProps()
    BRepGProp.VolumeProperties_s(solid, properties)
    centre = properties.CentreOfMass()
    return (centre.X(), centre.Y(), centre.Z())


def _along(point: tuple, direction: tuple) -> float:
    return _edit._dot(point, direction)


def _unit(vector: tuple) -> tuple:
    length = _edit._dot(vector, vector) ** 0.5
    return tuple(value / length for value in vector)


def _parallel(a: tuple, b: tuple) -> bool:
    return abs(abs(_edit._dot(_unit(a), _unit(b))) - 1.0) < _ANGULAR_TOL
