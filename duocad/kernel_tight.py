"""辺の曲率半径を超える半径のフィレット(issue #207): 平面の上面を囲む輪を、Fusion 360 と同じ形に丸める。

OCCT の `MakeFillet` は、転がる球が上面に触れる線(輪郭を内へ R ずらした曲線)が自己交差する半径(辺の曲率半径を超える)で
断る。Fusion 360 は、球を両側から転がして自己交差の点 X(球が壁の2点と上面の1点に同時に触れる所)で止め、その先を隣の面と
G1 でつながる帽子の面で埋める(本人が示した画面。docs/FILLET_OVERFLOW.md「曲率半径を超える半径」)。ここはその形を作る:

1. 上面から深さ R の平面で輪郭を内へ R ずらし、自己交差の輪(燕尾)を切り落とす。これが球の中心の道。
2. 道の滑らかな区間ごとに、四分円の断面(壁の接点 → 上面の接点)を掃いて丸めの面にする。
3. 道の角(自己交差を切った所)ごとに、両側の区間の端の四分円と、壁の縁の上の弧の3辺を、隣の面と G1 で埋めて帽子にする。
4. 区間の上の縁で囲む上面(レンズ形)と深さ R の底面を合わせて冠の立体にし、本体の深さ R より下と結合する。

**引き受けるのは**(それ以外は `Unsupported`): 選んだ辺が1つの平面の上面の外周を成し、接線でつながる1つの輪で、上面に内側の輪
(穴)が無い。本体の上面から深さ R までが、上面を押し出した形そのもの(壁が上面に垂直で、その範囲に他の形が無い)。
"""
from __future__ import annotations

import math
from typing import List, NamedTuple, Tuple

import numpy as np

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_CompCurve, BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakeSolid,
    BRepBuilderAPI_MakeWire,
    BRepBuilderAPI_Sewing,
    BRepBuilderAPI_Transform,
)
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeFilling, BRepOffsetAPI_MakePipeShell
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakePrism
from OCP.BRepTools import BRepTools, BRepTools_WireExplorer
from OCP.Bnd import Bnd_Box
from OCP.GC import GC_MakeArcOfCircle
from OCP.GCPnts import GCPnts_AbscissaPoint
from OCP.GProp import GProp_GProps
from OCP.GeomAPI import GeomAPI_Interpolate
from OCP.GeomAbs import GeomAbs_Shape, GeomAbs_SurfaceType
from OCP.ShapeFix import ShapeFix_Solid
from OCP.TColgp import TColgp_HArray1OfPnt
from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED, TopAbs_SHELL, TopAbs_WIRE
from OCP.TopExp import TopExp
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec

from .kernel_overflow import Unsupported, _boolean, _outward_normal, _solids, _volume
from .mesh_geometry import indexed_map


class Tight(NamedTuple):
    """丸めた形と、由来をたどる材料。上面(`top`)の後継は、丸めた形の上面と同じ平面の面(`lens`)。`builders` は本体の深さ R
    より下を取る引き算と、冠との縫い合わせ(この順。どちらも `Modified`・`IsDeleted` に答える)で、上面以外の元の面の後継は
    この2つをたどる。"""

    shape: TopoDS_Shape
    top: TopoDS_Shape
    lens: TopoDS_Shape
    builders: list


#: 球の中心の道を求める輪郭の点の数(少なくとも)と、輪郭の辺1本あたりの数。
_PATH_SAMPLES = 512
_PATH_SAMPLES_PER_EDGE = 64
#: 帽子と隣の面の接平面のずれの上限(ラジアン)。試作では 4e-4〜7e-4。
_CAP_G1_ERROR = 1.0e-2
#: ずらした曲線の交点を詰める繰り返しの上限。
_MEETING_STEPS = 60
#: 道の継ぎ目・輪の継ぎ目を接線とみなす角度(ラジアン)。
_SMOOTH_ANGLE = 1.0e-3


class Declined(Unsupported):
    """選んだ辺は上面を囲む輪(この作り方の対象)だが、丸められなかった。理由は利用者への失敗の文面に添える。"""


def round_tight_loop(shape: TopoDS_Shape, edge_map, radii: dict) -> Tight:
    """`radii`(`{辺の添字: 半径}`)の辺が上面を囲む輪なら、自己交差を切り落とした形で丸める。輪でなければ `Unsupported`、
    輪だが丸められなければ `Declined`。"""
    values = set(radii.values())
    if len(values) != 1:
        raise Unsupported("the loop must be rounded with one radius")
    radius = float(values.pop())
    edges = [edge_map.FindKey(int(index)) for index in radii]
    top = _top_face(shape, edges)
    try:
        return _round(shape, top, radius)
    except Unsupported as error:
        raise Declined(str(error)) from error


def _round(shape: TopoDS_Shape, top: TopoDS_Shape, radius: float) -> Tight:
    if len(_solids(shape)) != 1:
        raise Unsupported("the body must be one solid")
    normal = _outward_normal(top, _a_point_on(top))
    normal.Normalize()
    size = _diagonal(shape)
    below = _split_at_depth(shape, top, normal, radius, size)
    expected = _precise_volume(BRepPrimAPI_MakePrism(top, gp_Vec(normal.XYZ()).Multiplied(-radius)).Shape())

    shift = gp_Trsf()
    shift.SetTranslation(gp_Vec(normal.XYZ()).Multiplied(-radius))
    section = TopoDS.Face_s(BRepBuilderAPI_Transform(top, shift, True).Shape())
    contour = _Contour(BRepTools.OuterWire_s(section), normal)
    segments, loops = _center_path(contour, radius)
    sides = [_side(segment, contour, radius) for segment in segments]
    # 区間 i の終わりと区間 i+1 の始めの間が、切り落とした輪 i+1(1つ目の区間は輪 0 の終わりから始まる)
    caps = [_cap(this, following, loop, contour, radius)
            for this, following, loop in zip(sides, sides[1:] + sides[:1], loops[1:] + loops[:1])]
    lens = BRepBuilderAPI_MakeFace(_wire([side.top for side in sides]), True).Face()
    crown = _crown([lens, *(side.face for side in sides), *caps], section, size)

    # 本体の深さ R の断面と冠の底面を外して縫い合わせる。ブーリアンの結合は、帽子が壁に G1 で接する縁の交わりを解くのに
    # 楕円 6×2・R=0.67 で 8 秒かかった(R=1 で 1 秒)
    cut, floor = _face_on(below.Shape(), section, normal), _face_on(crown, section, normal)
    result, sewing = _closed_solid(
        [face for face in _faces(below.Shape()) if not face.IsSame(cut)] + [face for face in _faces(crown) if not face.IsSame(floor)],
        1.0e-6 * size,
        "joining the rounded crown to the body",
    )
    # 既定の積分は、帽子(充填の自由曲面)のある冠で 8% ずれた(楕円 6×2・R=0.67)
    removed = _precise_volume(shape) - _precise_volume(result)
    if not 0.0 < removed < expected:
        raise Unsupported(f"the rounding removed an implausible volume ({removed:.6g} of at most {expected:.6g})")
    return Tight(result, top, _face_on(result, top, normal), [below, _Sewn(sewing)])


class _Sewn:
    """縫い合わせの由来を、ブーリアンのビルダと同じ問い(`Modified` は面の列、`IsDeleted`)で答える。"""

    def __init__(self, sewing) -> None:
        self.sewing = sewing

    def Modified(self, face):  # noqa: N802  ビルダと同じ名前
        return [self.sewing.Modified(face)] if self.sewing.IsModified(face) else []

    def IsDeleted(self, face) -> bool:  # noqa: N802
        return False


# --------------------------------------------------------------------------------------
# 引き受けるかの確かめ
# --------------------------------------------------------------------------------------
def _top_face(shape: TopoDS_Shape, edges: list) -> TopoDS_Shape:
    """選んだ辺が全部接する平面の面(上面)。その面の輪が1つだけで、その輪の辺が選んだ辺とちょうど一致し、継ぎ目が接線。"""
    edge_faces = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, edge_faces)
    def planes(edge):
        return [
            face for face in edge_faces.FindFromKey(edge)
            if BRepAdaptor_Surface(TopoDS.Face_s(face)).GetType() == GeomAbs_SurfaceType.GeomAbs_Plane
        ]

    shared = [face for face in planes(edges[0]) if all(any(face.IsSame(other) for other in planes(edge)) for edge in edges[1:])]
    if len(shared) != 1:
        raise Unsupported("the edges do not all bound one planar face")
    common = shared[0]
    wires = indexed_map(common, TopAbs_WIRE)
    if wires.Extent() != 1:
        raise Unsupported("the top face has holes or other inner loops")
    loop = indexed_map(wires.FindKey(1), TopAbs_EDGE)
    if loop.Extent() != len(edges) or not all(loop.Contains(edge) for edge in edges):
        raise Unsupported("the edges must be the whole outline of the top face")
    explorer = BRepTools_WireExplorer(TopoDS.Wire_s(wires.FindKey(1)))
    ordered = []
    while explorer.More():
        ordered.append(explorer.Current())
        explorer.Next()
    for this, following in zip(ordered, ordered[1:] + ordered[:1]):
        if _end_direction(this, at_end=True).Angle(_end_direction(following, at_end=False)) > _SMOOTH_ANGLE:
            raise Unsupported("the outline has a sharp corner (corners are not rounded this way)")
    return common


def _split_at_depth(shape, top, normal: gp_Vec, radius: float, size: float):
    """本体を上面から深さ `radius` の平面で切った下の部分(引き算のビルダ)。上の部分が、上面をそのまま深さ `radius` まで
    押し出した形(帯)でなければ引き受けない(壁が上面に垂直で、その範囲に穴・突起・別の面が無い)。"""
    base = _a_point_on(top).Translated(gp_Vec(normal.XYZ()).Multiplied(-radius))
    axis = gp_Dir(normal)
    side = gp_Dir(gp_Vec(axis.X(), axis.Y(), axis.Z()).Crossed(_any_perpendicular(axis)))
    corner = base.Translated(gp_Vec(side.XYZ()).Multiplied(-size)).Translated(gp_Vec(axis.Crossed(side).XYZ()).Multiplied(-size))
    box = BRepPrimAPI_MakeBox(gp_Ax2(corner, axis, side), 2.0 * size, 2.0 * size, size).Shape()
    above = _boolean("common", shape, box).Shape()
    band = BRepPrimAPI_MakePrism(top, gp_Vec(normal.XYZ()).Multiplied(-radius)).Shape()
    # 外部の罠: 既定の積分(`BRepGProp.VolumeProperties_s` に精度を渡さない)は、ブーリアンで切った楕円の壁で相対 8.5e-7 ずれ、
    # 帯との比較が通らなかった。面ごとに積分を選ぶ `kernel.volume` なら 1e-10 で一致する
    expected = _precise_volume(band)
    tolerance = 1.0e-8 * max(1.0, expected)
    if (abs(_precise_volume(above) - expected) > tolerance
            or abs(_precise_volume(_boolean("common", above, band).Shape()) - expected) > tolerance):
        raise Unsupported("the body is not the top face pushed straight down over the fillet depth")
    below = _boolean("cut", shape, box)
    if len(_solids(below.Shape())) != len(_solids(shape)):
        raise Unsupported("cutting the body at the fillet depth split it")
    return below


# --------------------------------------------------------------------------------------
# 球の中心の道
# --------------------------------------------------------------------------------------
class _Contour:
    """深さ R の輪郭を、辺の境目を弧長に置いた媒介変数(1周をまたいでよい)で辿る。`at(s)` は点・向き・面の内側への法線
    (どれも長さ 1)。"""

    def __init__(self, wire: TopoDS_Shape, up: gp_Vec) -> None:
        self.wire = wire
        self.up = up
        self.curve = BRepAdaptor_CompCurve(TopoDS.Wire_s(wire), True)
        self.first = self.curve.FirstParameter()
        self.length = self.curve.LastParameter() - self.first
        self.edges = indexed_map(wire, TopAbs_EDGE).Extent()
        # 上から見て反時計回りなら、左(上 × 向き)が面の内側
        points = [self.curve.Value(self.first + self.length * i / 256) for i in range(256)]
        area = gp_Vec(0.0, 0.0, 0.0)
        for a, b in zip(points, points[1:] + points[:1]):
            area.Add(gp_Vec(points[0], a).Crossed(gp_Vec(points[0], b)))
        self.side = 1.0 if area.Dot(up) > 0.0 else -1.0
        self.origin = points[0]
        self.x_axis = up.Crossed(_any_perpendicular(gp_Dir(up))).Normalized()
        self.y_axis = up.Crossed(self.x_axis)

    def _parameter(self, s: float) -> float:
        return self.first + math.fmod(s, self.length) + (self.length if s < 0.0 else 0.0)

    def at(self, s: float) -> Tuple[gp_Pnt, gp_Vec, gp_Vec]:
        point, tangent = gp_Pnt(), gp_Vec()
        self.curve.D1(self._parameter(s), point, tangent)
        tangent.Normalize()
        return point, tangent, self.up.Crossed(tangent).Multiplied(self.side)

    def bend(self, s: float) -> float:
        """曲率(面の内側へ曲がる向きが正)。"""
        point, first, second = gp_Pnt(), gp_Vec(), gp_Vec()
        self.curve.D2(self._parameter(s), point, first, second)
        return self.side * first.Crossed(second).Dot(self.up) / first.Magnitude() ** 3

    def flat(self, points) -> "np.ndarray":
        """点を上面の平面の座標(N×2)にする。"""
        return np.array([[gp_Vec(self.origin, point).Dot(self.x_axis), gp_Vec(self.origin, point).Dot(self.y_axis)]
                         for point in points])

    def center(self, s: float, radius: float) -> gp_Pnt:
        point, _, inward = self.at(s)
        return point.Translated(inward.Multiplied(radius))


class _Segment(NamedTuple):
    """球の中心の道の滑らかな1区間と、それに当たる輪郭の媒介変数の範囲。"""

    path: object
    low: float
    high: float


def _center_path(contour: _Contour, radius: float):
    """球の中心の道(深さ R の輪郭を内へ R ずらした曲線から、自己交差の輪を切り落としたもの)の区間と、切り落とした輪の
    輪郭の弧長の範囲(帽子が壁に触れる範囲)を返す。

    ずらした点は輪郭から直接求める: `BRepOffsetAPI_MakeOffset` の道は近似の継ぎ目で最大 6° 折れ(楕円 6×2 を R=1)、
    半径 1 の円を 3 ずらす(領域が消える)とプロセスごと落ちた。
    """
    count = max(_PATH_SAMPLES, _PATH_SAMPLES_PER_EDGE * contour.edges)
    step = contour.length / count
    loops = []

    def cut(first, second, reach):
        low, high = sorted(_meeting(contour, radius, first, second, reach))
        if high - low > 0.5 * contour.length:  # 切り落とす輪は短い方(燕尾は局所)
            low, high = high, low + contour.length
        loops.append((low, high))

    for first, second in _self_crossings(contour.flat([contour.center(i * step, radius) for i in range(count)])):
        cut(first * step, second * step, step)
    for first, second in _small_swallowtails(contour, radius, step, loops):
        cut(first, second, second - first)
    loops.sort()
    for (low, high), (next_low, _) in zip(loops, loops[1:] + [(loops[0][0] + contour.length, 0.0)] if loops else []):
        if high >= next_low:
            raise Unsupported("the swallowtails of the offset outline overlap")
    if not loops:
        # 道が自己交差しない(半径が辺の曲率半径を超えない)なら OCCT の `MakeFillet` の領分。ここでは作らない
        raise Unsupported("the radius does not exceed the curvature of the outline anywhere")
    _clear_of_walls(contour, radius, loops, count)
    segments = []
    for (_, high), (next_low, _) in zip(loops, loops[1:] + [(loops[0][0] + contour.length, 0.0)]):
        span = next_low - high
        pieces = max(_PATH_SAMPLES_PER_EDGE, int(math.ceil(span / step)))
        points = [contour.center(high + span * i / pieces, radius) for i in range(pieces + 1)]
        # ずらした曲線の向きは、切り落とした輪の外では輪郭の向きと同じ
        path = _interpolate(points, contour.at(high)[1], contour.at(next_low)[1])
        segments.append(_Segment(path, high, next_low))
    return segments, loops


def _clear_of_walls(contour: _Contour, radius: float, loops: list, count: int) -> None:
    """切り落とした輪の外の球の中心が、輪郭のどこにも R より近づかないこと(半径が上面の幅を超える・首がくびれた形では、
    球が反対側の壁に食い込む)。折れ線の距離で候補を絞り、候補だけ輪郭そのものとの距離を詰める。"""
    step = contour.length / count
    flat = contour.flat
    start = flat([contour.at(i * step)[0] for i in range(count)])
    middles = [contour.at((i + 0.5) * step)[0] for i in range(count)]
    direction = np.roll(start, -1, axis=0) - start
    # 折れ線と輪郭のずれ(弦の中点と弧の中点の距離)。折れ線の距離はこの分だけ輪郭の距離より小さく出うる
    deviation = float(np.max(np.linalg.norm(flat(middles) - (start + 0.5 * direction), axis=1)))
    kept = [i for i in range(count)
            if not any(low < i * step < high or low < i * step + contour.length < high for low, high in loops)]
    centers = flat([contour.center(i * step, radius) for i in kept])
    lengths = np.maximum(np.einsum("ij,ij->i", direction, direction), 1.0e-300)
    relative = centers[:, None, :] - start[None, :, :]
    along = np.clip(np.einsum("mnk,nk->mn", relative, direction) / lengths[None, :], 0.0, 1.0)
    distance = np.linalg.norm(relative - along[..., None] * direction[None, :, :], axis=2)
    own = np.array(kept)[:, None]
    apart = np.abs(np.arange(count)[None, :] - own)
    distance[np.minimum(apart, count - apart) <= 2] = np.inf  # 自分の接点のまわりは R ちょうど
    allowance = 1.0e-7 * max(1.0, radius)
    for row, column in zip(*np.nonzero(distance < radius + 2.0 * deviation + allowance)):
        center = contour.center(kept[row] * step, radius)
        nearest = _golden_minimum(lambda s: contour.at(s)[0].Distance(center), (column - 1) * step, (column + 2) * step)
        if contour.at(nearest)[0].Distance(center) < radius - allowance:
            raise Unsupported("the ball would cut into the opposite wall (the top is too narrow for the radius)")


def _small_swallowtails(contour: _Contour, radius: float, step: float, loops: list) -> List[Tuple[float, float]]:
    """標本の折れ線では交差が見えないほど小さい燕尾(半径が辺の曲率半径をわずかに超える所)の、交点の目安(両枝の輪郭の弧長)。
    曲率の山で R × 曲率 が 1 を超え、見つけた輪に含まれないものを、山のまわりだけ細かく標本して交差を探す。"""
    count = int(round(contour.length / step))
    excess = [radius * contour.bend(i * step) - 1.0 for i in range(count)]
    found = []
    for i in range(count):
        if not excess[i - 1] <= excess[i] >= excess[(i + 1) % count]:
            continue
        peak = _golden_minimum(lambda s: -contour.bend(s), (i - 1) * step, (i + 1) * step)
        if radius * contour.bend(peak) <= 1.0:
            continue
        if any(low < peak < high or low < peak + contour.length < high for low, high in loops):
            continue
        # 裏返る範囲(R × 曲率 > 1)が標本の間隔より広いのに折れ線が交わらないのは、燕尾ではなく道全体の裏返り
        if any(radius * contour.bend(peak + direction * step) > 1.0 for direction in (1.0, -1.0)):
            raise Unsupported("the offset outline turns over without crossing itself (the radius is wider than the top)")
        # 交点は、燕尾の2つの尖り(R × 曲率 = 1)の間の半分の幅の √3 倍ほど山から離れる(放物線の先端で厳密)。その 3 倍まで見る
        half = min(abs(_crossover(contour, radius, peak, peak + direction * step) - peak) for direction in (1.0, -1.0))
        window = np.linspace(peak - 3.0 * half, peak + 3.0 * half, 257)
        crossings = _self_crossings(contour.flat([contour.center(s, radius) for s in window]), closed=False)
        if len(crossings) != 1:
            raise Unsupported("a small swallowtail of the offset outline could not be located")
        unit = window[1] - window[0]
        found.append((window[0] + unit * crossings[0][0], window[0] + unit * crossings[0][1]))
    return found


def _crossover(contour: _Contour, radius: float, inside: float, outside: float) -> float:
    """`inside`(R × 曲率 > 1)と `outside`(≤ 1)の間で R × 曲率 = 1 になる所(二分法)。"""
    for _ in range(60):
        middle = 0.5 * (inside + outside)
        if radius * contour.bend(middle) > 1.0:
            inside = middle
        else:
            outside = middle
    return 0.5 * (inside + outside)


def _meeting(contour: _Contour, radius: float, first: float, second: float, reach: float) -> Tuple[float, float]:
    """ずらした曲線の2本の枝が交わる所の、両枝の輪郭の弧長。ニュートン法で詰める(片方の枝に他方を交互に寄せるのは、
    枝の交わる角が小さいと 60 回で収束しなかった。楕円 6×2 を R=0.67)。1回に動かすのは `reach` まで。"""
    def gap(a, b):
        ends = contour.flat([contour.center(a, radius), contour.center(b, radius)])
        return ends[0] - ends[1]

    def slope(s):
        h = 1.0e-5 * reach
        ends = contour.flat([contour.center(s + h, radius), contour.center(s - h, radius)])
        return (ends[0] - ends[1]) / (2.0 * h)

    tolerance = 1.0e-7 * max(1.0, radius)
    for _ in range(_MEETING_STEPS):
        difference = gap(first, second)
        if np.linalg.norm(difference) < 1.0e-3 * tolerance:
            break
        try:
            move = np.linalg.solve(np.column_stack([slope(first), -slope(second)]), -difference)
        except np.linalg.LinAlgError:
            break
        move *= min(1.0, reach / max(float(np.max(np.abs(move))), 1.0e-300))
        first, second = first + float(move[0]), second + float(move[1])
    if contour.center(first, radius).Distance(contour.center(second, radius)) > tolerance:
        raise Unsupported("the crossing of the offset outline could not be located")
    return first % contour.length, second % contour.length


def _self_crossings(points: "np.ndarray", closed: bool = True) -> List[Tuple[float, float]]:
    """折れ線(`points` は N×2。`closed` なら最後の点から最初の点へも結ぶ)が自分と交わる所を、2本の線分の位置
    (線分の番号 + 割合)の組で返す。隣り合う線分は見ない。"""
    start, finish = points, np.roll(points, -1, axis=0)
    if not closed:
        start, finish = start[:-1], finish[:-1]
    count = len(start)
    direction = finish - start
    crossings = []
    for i in range(count):
        others = np.arange(i + 2, count)
        if i == 0 and closed:
            others = others[others != count - 1]
        if others.size == 0:
            continue
        a, d = start[i], direction[i]
        b, e = start[others], direction[others]
        denominator = d[0] * e[:, 1] - d[1] * e[:, 0]
        offset = b - a
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (offset[:, 0] * e[:, 1] - offset[:, 1] * e[:, 0]) / denominator
            u = (offset[:, 0] * d[1] - offset[:, 1] * d[0]) / denominator
        lengths = np.hypot(d[0], d[1]) * np.hypot(e[:, 0], e[:, 1])
        hit = (np.abs(denominator) > 1.0e-12 * lengths) & (t >= 0.0) & (t < 1.0) & (u >= 0.0) & (u < 1.0)
        for j, tj, uj in zip(others[hit], t[hit], u[hit]):
            crossings.append((i + float(tj), int(j) + float(uj)))
    return crossings


def _interpolate(points: List[gp_Pnt], start: object, end: object):
    """点を通る B スプライン。`start`・`end`(長さ 1 の向き)が None なら1周(周期)。"""
    array = TColgp_HArray1OfPnt(1, len(points))
    for number, point in enumerate(points, 1):
        array.SetValue(number, point)
    interpolation = GeomAPI_Interpolate(array, start is None, 1.0e-9)
    if start is not None:
        # 補間の媒介変数は弦の長さなので、向きは長さ 1 で渡す(`kernel_overflow._interpolated` と同じ外部の罠)
        interpolation.Load(start, end, False)
    interpolation.Perform()
    if not interpolation.IsDone():
        raise Unsupported("the ball path could not be interpolated")
    return interpolation.Curve()


# --------------------------------------------------------------------------------------
# 丸めの面・帽子
# --------------------------------------------------------------------------------------
class _Side(NamedTuple):
    face: TopoDS_Shape
    top: TopoDS_Shape      # 上面に触れる縁
    start: TopoDS_Shape    # 区間の始めの四分円
    end: TopoDS_Shape      # 区間の終わりの四分円


def _side(segment: _Segment, contour: _Contour, radius: float) -> _Side:
    """道に沿って四分円(壁の接点 → 上面の接点)を掃いた丸めの面。断面の平面は常に上面の法線を含む。"""
    up = contour.up
    foot, _, inward = contour.at(segment.low)
    center = foot.Translated(inward.Multiplied(radius))
    middle = center.Translated(inward.Multiplied(-radius * math.sqrt(0.5))).Translated(up.Multiplied(radius * math.sqrt(0.5)))
    crest = center.Translated(up.Multiplied(radius))
    profile = BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(foot, middle, crest).Value()).Edge()).Wire()
    sweep = BRepOffsetAPI_MakePipeShell(BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(segment.path).Edge()).Wire())
    sweep.SetMode(gp_Dir(up))  # 二法線を上面の法線に固定(断面の平面が常に法線を含む)
    # 実測: 既定の許容(1e-4)では、下の縁が輪郭から最大 3.8e-5 離れ(楕円 6×2・R=1.95)、壁と縫い合わせられなかった。
    # 1e-6 なら 1.5e-8 で、時間は変わらない
    sweep.SetTolerance(1.0e-6 * max(1.0, radius), 1.0e-6 * max(1.0, radius), 1.0e-4)
    sweep.Add(profile)
    sweep.Build()
    if not sweep.IsDone():
        raise Unsupported("the rounding face could not be swept along the ball path")
    faces = indexed_map(sweep.Shape(), TopAbs_FACE)
    if faces.Extent() != 1:
        raise Unsupported("sweeping the rounding gave more than one face")
    face = faces.FindKey(1)
    top = None
    ends = []
    for edge in _edges(face):
        heights = [gp_Vec(center, point).Dot(up) for point in _samples(edge, 5)]
        if all(abs(height - radius) < 1.0e-6 * max(1.0, radius) for height in heights):
            top = edge
        elif not all(abs(height) < 1.0e-6 * max(1.0, radius) for height in heights):
            ends.append(edge)
    if top is None or len(ends) != 2:
        raise Unsupported("the swept rounding face has unexpected edges")
    ends.sort(key=lambda edge: min(point.Distance(foot) for point in _samples(edge, 3)))
    return _Side(face, top, ends[0], ends[1])


def _cap(this: _Side, following: _Side, loop: Tuple[float, float], contour: _Contour, radius: float) -> TopoDS_Shape:
    """道の角で、前の区間の終わりの四分円・次の区間の始めの四分円・壁の縁の弧(切り落とした輪に当たる輪郭)を、隣の面と G1
    で埋めた帽子。

    外部の罠: `BRepOffsetAPI_MakeFilling` に G1 の境界として、支える面の上の座標(pcurve)を持たない辺を渡すと、`Build()` が
    アクセス違反でプロセスごと落ちた。壁の縁の弧は、その弧を押し出した面を支えにし、押し出しの元の辺を渡す。
    """
    # 実測: 曲線上の点 15・繰り返し 2・3D の許容 1e-4 では、楕円 6×2 で R=0.667(G1 0.012)と R≥1.7(G0 1.9e-4)が上限を超えた。
    # 30・3・1e-5 なら R=0.6667〜1.9 で G0 ≤ 1.3e-5・G1 ≤ 5.5e-3、帽子 1 枚 0.1 秒ほど
    filling = BRepOffsetAPI_MakeFilling(3, 30, 3, False, 1.0e-5, 1.0e-5, 1.0e-2, 0.1, 8, 9)
    filling.Add(TopoDS.Edge_s(this.end), TopoDS.Face_s(this.face), GeomAbs_Shape.GeomAbs_G1, True)
    filling.Add(TopoDS.Edge_s(following.start), TopoDS.Face_s(following.face), GeomAbs_Shape.GeomAbs_G1, True)
    for rim in _contour_pieces(contour, *loop):
        support = BRepPrimAPI_MakePrism(rim, gp_Vec(contour.up.XYZ()).Multiplied(radius)).Shape()
        filling.Add(rim, TopoDS.Face_s(indexed_map(support, TopAbs_FACE).FindKey(1)), GeomAbs_Shape.GeomAbs_G1, True)
    try:
        filling.Build()
    except Exception as error:  # noqa: BLE001
        raise Unsupported(f"the cap at the tip could not be filled: {error}") from error
    if not filling.IsDone() or filling.G0Error() > 1.0e-4 * max(1.0, radius) or filling.G1Error() > _CAP_G1_ERROR:
        raise Unsupported("the cap at the tip could not be filled smoothly")
    return filling.Shape()


def _contour_pieces(contour: _Contour, low: float, high: float) -> list:
    """輪郭の媒介変数 [low, high](1周をまたいでよい)を、輪郭の辺ごとに切った辺の列(輪郭の向きに並ぶ)にする。

    外部の罠: `BRepAdaptor_CompCurve` は弧長の媒介変数を指定しても、辺の境目を弧長の位置に置くだけで、辺の中では辺の媒介変数
    (楕円なら角度)を一次で伸ばしたものになる。辺の中の位置も同じ一次の換算で戻す(弧長として換算すると、楕円で帽子の縁の端が
    0.5 ずれた)。"""
    explorer = BRepTools_WireExplorer(TopoDS.Wire_s(contour.wire))
    spans = []
    position = 0.0
    while explorer.More():
        edge = TopoDS.Edge_s(explorer.Current())
        edge_length = GCPnts_AbscissaPoint.Length_s(BRepAdaptor_Curve(edge))
        spans.append((position, position + edge_length, edge))
        position += edge_length
        explorer.Next()
    pieces = []
    for shift in (0.0, contour.length):
        for begin, finish, edge in spans:
            lo, hi = max(low, begin + shift), min(high, finish + shift)
            if hi - lo <= 1.0e-9 * contour.length:
                continue
            adaptor = BRepAdaptor_Curve(edge)
            first, last = adaptor.FirstParameter(), adaptor.LastParameter()
            if edge.Orientation() == TopAbs_REVERSED:
                first, last = last, first

            def parameter(along):  # 輪郭の媒介変数 → 辺の媒介変数
                return first + (along - begin - shift) / (finish - begin) * (last - first)

            u0, u1 = parameter(lo), parameter(hi)
            # 位置(Location)を含めた曲線で作る(含めないと、写した輪郭の辺は元の高さのまま作られた)
            pieces.append(BRepBuilderAPI_MakeEdge(BRep_Tool.Curve_s(edge, 0.0, 0.0), min(u0, u1), max(u0, u1)).Edge())
    return pieces


def _golden_minimum(f, low: float, high: float, steps: int = 60) -> float:
    """[low, high] で f を最小にする点(黄金分割)。"""
    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    a, b = low, high
    c, d = b - ratio * (b - a), a + ratio * (b - a)
    fc, fd = f(c), f(d)
    for _ in range(steps):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - ratio * (b - a)
            fc = f(c)
        else:
            a, c, fc = c, d, fd
            d = a + ratio * (b - a)
            fd = f(d)
    return 0.5 * (a + b)


# --------------------------------------------------------------------------------------
# 小さな道具
# --------------------------------------------------------------------------------------
def _crown(faces: list, section: TopoDS_Shape, size: float) -> TopoDS_Shape:
    """上面・丸めの面・帽子を縫い、残った縁(深さ R の輪郭の上)で底面を張って閉じた立体にする。底面を辺から組むと、
    道を補間した誤差で丸めの面の下の縁と帽子の縁の端が 1e-6 ほどずれてつながらないので、縫い目の許容でまとめる。"""
    tolerance = 1.0e-6 * size
    sewing = BRepBuilderAPI_Sewing(tolerance)
    for face in faces:
        sewing.Add(face)
    sewing.Perform()
    free = ShapeAnalysis_FreeBounds(sewing.SewedShape(), tolerance, False, False)
    loops = indexed_map(free.GetClosedWires(), TopAbs_WIRE)
    if loops.Extent() != 1 or indexed_map(free.GetOpenWires(), TopAbs_EDGE).Extent() != 0:
        raise Unsupported("the rounding faces do not leave one loop on the wall")
    plane = BRepAdaptor_Surface(TopoDS.Face_s(section)).Plane()
    floor = BRepBuilderAPI_MakeFace(plane, TopoDS.Wire_s(loops.FindKey(1)), True)
    if not floor.IsDone():
        raise Unsupported("the floor of the rounded crown could not be made")
    return _closed_solid([*faces, floor.Face()], tolerance, "the rounded crown")[0]


def _closed_solid(faces: list, tolerance: float, what: str):
    """面を縫って閉じた1つの立体にする。立体と縫い合わせ(由来をたどる)を返す。"""
    sewing = BRepBuilderAPI_Sewing(tolerance)
    for face in faces:
        sewing.Add(face)
    sewing.Perform()
    shells = indexed_map(sewing.SewedShape(), TopAbs_SHELL)
    if sewing.NbFreeEdges() != 0 or shells.Extent() != 1:
        raise Unsupported(f"{what} did not close ({sewing.NbFreeEdges()} free edges, {shells.Extent()} shells)")
    solid = BRepBuilderAPI_MakeSolid(TopoDS.Shell_s(shells.FindKey(1))).Solid()
    fix = ShapeFix_Solid(solid)
    fix.Perform()
    solid = fix.Solid()
    if _volume(solid) <= 0.0 or not BRepCheck_Analyzer(solid).IsValid():
        raise Unsupported(f"{what} is not a valid solid")
    return solid, sewing


def _precise_volume(shape: TopoDS_Shape) -> float:
    from .kernel import volume  # kernel がこのモジュールを読むので、呼ぶときに読む

    return volume(shape)


def _faces(shape: TopoDS_Shape) -> list:
    faces = indexed_map(shape, TopAbs_FACE)
    return [faces.FindKey(i) for i in range(1, faces.Extent() + 1)]


def _face_on(shape: TopoDS_Shape, top: TopoDS_Shape, normal: gp_Vec) -> TopoDS_Shape:
    """`shape` の面のうち、上面と同じ平面にある面(丸めた後の上面)。"""
    level = _a_point_on(top)
    found = []
    for face in (indexed_map(shape, TopAbs_FACE).FindKey(i) for i in range(1, indexed_map(shape, TopAbs_FACE).Extent() + 1)):
        surface = BRepAdaptor_Surface(TopoDS.Face_s(face))
        if surface.GetType() != GeomAbs_SurfaceType.GeomAbs_Plane:
            continue
        plane = surface.Plane()
        if plane.Axis().Direction().IsParallel(gp_Dir(normal), 1.0e-9) and plane.Distance(level) < 1.0e-7 * max(1.0, _diagonal(shape)):
            found.append(face)
    if len(found) != 1:
        raise Unsupported("the rounded top face could not be found")
    return found[0]


def _wire(edges: list) -> TopoDS_Shape:
    maker = BRepBuilderAPI_MakeWire()
    for edge in edges:
        maker.Add(TopoDS.Edge_s(edge))
    if not maker.IsDone():
        raise Unsupported("the rounding edges do not join into a loop")
    return maker.Wire()


def _edges(shape: TopoDS_Shape) -> list:
    found = indexed_map(shape, TopAbs_EDGE)
    return [TopoDS.Edge_s(found.FindKey(i)) for i in range(1, found.Extent() + 1)]


def _samples(edge, count: int) -> list:
    curve = BRepAdaptor_Curve(TopoDS.Edge_s(edge))
    first, last = curve.FirstParameter(), curve.LastParameter()
    return [curve.Value(first + (last - first) * i / (count - 1)) for i in range(count)]


def _end_direction(edge, at_end: bool) -> gp_Vec:
    """辺を輪の向きに辿ったときの、始め(`at_end=False`)または終わりの向き(長さ 1)。"""
    curve = BRepAdaptor_Curve(TopoDS.Edge_s(edge))
    forward = edge.Orientation() != TopAbs_REVERSED
    parameter = curve.LastParameter() if forward == at_end else curve.FirstParameter()
    point, tangent = gp_Pnt(), gp_Vec()
    curve.D1(parameter, point, tangent)
    if tangent.Magnitude() < 1.0e-12:
        raise Unsupported("an edge has no direction at its end")
    tangent.Normalize()
    return tangent if forward else tangent.Reversed()


def _a_point_on(face: TopoDS_Shape) -> gp_Pnt:
    surface = BRepAdaptor_Surface(TopoDS.Face_s(face))
    return surface.Value(0.5 * (surface.FirstUParameter() + surface.LastUParameter()),
                         0.5 * (surface.FirstVParameter() + surface.LastVParameter()))


def _any_perpendicular(direction: gp_Dir) -> gp_Vec:
    helper = gp_Vec(1.0, 0.0, 0.0) if abs(direction.X()) < 0.9 else gp_Vec(0.0, 1.0, 0.0)
    return gp_Vec(direction).Crossed(helper)


def _area(face: TopoDS_Shape) -> float:
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    return props.Mass()


def _diagonal(shape: TopoDS_Shape) -> float:
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return max(1.0, math.dist((x0, y0, z0), (x1, y1, z1)))
