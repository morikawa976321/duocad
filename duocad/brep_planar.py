"""平面の座標系・補助面・面集合の演算。スケッチと投影が共有する幾何機構。"""
from __future__ import annotations

import math
from typing import Any, List, Sequence, Tuple

from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
from OCP.GeomAbs import GeomAbs_Shape
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
from OCP.TColStd import TColStd_Array1OfReal
from OCP.TopTools import TopTools_ListOfShape
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Ax3, gp_Dir, gp_Pnt

from . import (
    brep_topology as _brep_topology,
)


_BASE_PLANE_AXES = {
    "XY": ((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)),  # v -> +Y
    "XZ": ((0.0, 0.0, 0.0), (0.0, -1.0, 0.0), (1.0, 0.0, 0.0)),  # v -> +Z
    "YZ": ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),  # v -> +Z
}


#: 包み込みの面の結合・境界判定に使う許容差[mm]。OCCTの既定(1e-7)では、R と d の円弧が
#: 同じ点で接するオフセット結果の結合を取りこぼしうるので、その10倍にする。
_ENVELOPE_TOLERANCE = 1.0e-6


def _shape_faces(shape: Any) -> List[Any]:
    shape_map = _brep_topology._face_map(shape)
    return [TopoDS.Face_s(shape_map.FindKey(i)) for i in range(1, shape_map.Extent() + 1)]


def _unified(shape: Any) -> Any:
    """同一平面・同一曲線上で隣り合う面・辺を1つにまとめる(共線の線分や同心の円弧の分割を消す)。"""
    unify = ShapeUpgrade_UnifySameDomain(shape, True, True, False)
    unify.Build()
    return unify.Shape()


def _union_faces(faces: Sequence[Any]) -> List[Any]:
    """面を和集合にして、つながった成分ごとの面の並びにする(辺で接するだけの面も1枚に結合する)。"""
    if len(faces) <= 1:
        return list(faces)
    return _shape_faces(_boolean(BRepAlgoAPI_Fuse(), faces[:1], faces[1:]))


def _boolean(operation: Any, arguments: Sequence[Any], tools: Sequence[Any]) -> Any:
    """`arguments` と `tools` で2Dのブーリアンを取る(許容差つき)。"""
    argument_list = TopTools_ListOfShape()
    for shape in arguments:
        argument_list.Append(shape)
    tool_list = TopTools_ListOfShape()
    for shape in tools:
        tool_list.Append(shape)
    operation.SetArguments(argument_list)
    operation.SetTools(tool_list)
    operation.SetFuzzyValue(_ENVELOPE_TOLERANCE)
    operation.Build()
    if not operation.IsDone():
        raise ValueError("領域のブーリアン演算に失敗した")
    return operation.Shape()


def _smooth_breaks(curve: Any, first: float, last: float) -> List[float]:
    """`[first, last]` を曲線の滑らかな区間(B-スプラインならノット、オフセット曲線なら元の曲線のノット)の境目で区切った点の列。"""
    count = curve.NbIntervals(GeomAbs_Shape.GeomAbs_CN)
    bounds = TColStd_Array1OfReal(1, count + 1)
    curve.Intervals(bounds, GeomAbs_Shape.GeomAbs_CN)
    inner = sorted({bounds.Value(i) for i in range(1, count + 2) if first < bounds.Value(i) < last})
    return [first, *inner, last]


def _ax3_from_plane(plane: dict) -> "gp_Ax3":
    """PlaneRef から gp_Ax3 を作る。

    **`kind` を問わず `plane["frame"]`(作成時に焼き付けた姿勢。`face_frame` のdocstring)が最優先**。
    `sketch.resolve_plane` が必ず `frame` を焼き付けて渡すので、新しい `kind` が増えても無改修で通る。
    """
    kind = plane.get("kind")
    if kind == "base":
        name = plane.get("name")
        if name not in _BASE_PLANE_AXES:
            raise ValueError(f"unknown base plane: {name!r}")
        origin, normal, xdir = _BASE_PLANE_AXES[name]
        return gp_Ax3(gp_Pnt(*origin), gp_Dir(*normal), gp_Dir(*xdir))
    frame = plane.get("frame")
    if frame is not None:
        return _ax3_from_frame(frame)
    raise ValueError(f"invalid sketch plane: {plane!r}")


def _frame_from_ax3(ax3: "gp_Ax3") -> dict:
    """gp_Ax3 を素の数値の枠へ(`_ax3_from_frame` の逆)。"""
    origin = ax3.Location()
    xdir = ax3.XDirection()
    ydir = ax3.YDirection()
    normal = ax3.Direction()
    return {
        "origin": [origin.X(), origin.Y(), origin.Z()],
        "u": [xdir.X(), xdir.Y(), xdir.Z()],
        "v": [ydir.X(), ydir.Y(), ydir.Z()],
        "normal": [normal.X(), normal.Y(), normal.Z()],
    }


def _ax3_from_frame(frame: Any) -> "gp_Ax3":
    """素の数値の枠から gp_Ax3 を復元する(`_frame_from_ax3` の逆)。

    **`v` を捨ててはいけない**。`gp_Ax3(location, direction, xdirection)` は必ず右手系を作る
    (`YDirection = Direction ^ XDirection`)のに対し、`_ax3_from_face` が反転面へ当てる
    `ZReverse()` は Direction だけを裏返して X/Y を据え置く(＝左手系)。origin/normal/u だけで
    組み直すと反転面のスケッチは v 軸が符号反転し、図形が鏡像になる(実測)。
    そこで `v` も保存し、必要なら `YReverse()` で左手系へ戻す。

    復元した軸が保存値と一致しなければ ValueError(黙って鏡像の平面を配らない)。
    """
    if not isinstance(frame, dict):
        raise ValueError(f"invalid sketch plane frame: {frame!r}")
    try:
        given = {
            name: [float(value) for value in frame[name]]
            for name in ("origin", "u", "v", "normal")
        }
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"invalid sketch plane frame: {frame!r}") from error
    for name, vector in given.items():
        if len(vector) != 3 or not all(math.isfinite(value) for value in vector):
            raise ValueError(f"invalid sketch plane frame ({name}): {frame!r}")
    origin, u, v, normal = given["origin"], given["u"], given["v"], given["normal"]

    try:
        # 長さ0や normal と u が平行な枠は OCCT が Standard_ConstructionError で落とす。
        # 呼び出し側が「平面が不正」を1種類の例外で捕まえられるよう ValueError へ揃える。
        ax3 = gp_Ax3(gp_Pnt(*origin), gp_Dir(*normal), gp_Dir(*u))
    except Exception as error:
        raise ValueError(f"invalid sketch plane frame: {frame!r}") from error
    if _dot3(ax3.YDirection(), v) < 0.0:
        ax3.YReverse()

    restored = _frame_from_ax3(ax3)
    for name, vector in given.items():
        for expected, actual in zip(vector, restored[name]):
            if abs(expected - actual) > 1.0e-9 * max(1.0, abs(expected)):
                raise ValueError(
                    f"sketch plane frame is not orthonormal ({name}): {frame!r}"
                )
    return ax3


def _dot3(direction: "gp_Dir", vector: Sequence[float]) -> float:
    """gp_Dir と素の3成分の内積。"""
    return (
        direction.X() * float(vector[0])
        + direction.Y() * float(vector[1])
        + direction.Z() * float(vector[2])
    )


def _to_pnt(ax3: "gp_Ax3", u: float, v: float) -> "gp_Pnt":
    """スケッチ平面上の (u, v) を 3D 点へ。"""
    origin = ax3.Location()
    xdir = ax3.XDirection()
    ydir = ax3.YDirection()
    return gp_Pnt(
        origin.X() + u * xdir.X() + v * ydir.X(),
        origin.Y() + u * xdir.Y() + v * ydir.Y(),
        origin.Z() + u * xdir.Z() + v * ydir.Z(),
    )


def _to_uv(ax3: "gp_Ax3", x: float, y: float, z: float) -> Tuple[float, float]:
    """3D 点をスケッチ平面の (u, v) へ。"""
    origin = ax3.Location()
    xdir = ax3.XDirection()
    ydir = ax3.YDirection()
    dx = x - origin.X()
    dy = y - origin.Y()
    dz = z - origin.Z()
    u = dx * xdir.X() + dy * xdir.Y() + dz * xdir.Z()
    v = dx * ydir.X() + dy * ydir.Y() + dz * ydir.Z()
    return u, v


def _plane_face(
    ax3: "gp_Ax3", u0: float, u1: float, v0: float, v1: float
) -> Any:
    """スケッチ平面上の軸平行矩形の TopoDS_Face(補助面)。"""
    corners = [
        _to_pnt(ax3, u0, v0),
        _to_pnt(ax3, u1, v0),
        _to_pnt(ax3, u1, v1),
        _to_pnt(ax3, u0, v1),
    ]
    wire_maker = BRepBuilderAPI_MakeWire()
    for index in range(4):
        edge_maker = BRepBuilderAPI_MakeEdge(corners[index], corners[(index + 1) % 4])
        if not edge_maker.IsDone():
            raise ValueError("failed to build an auxiliary edge")
        wire_maker.Add(edge_maker.Edge())
    if not wire_maker.IsDone():
        raise ValueError("failed to build an auxiliary wire")
    face_maker = BRepBuilderAPI_MakeFace(wire_maker.Wire(), True)
    if not face_maker.IsDone():
        raise ValueError("failed to build an auxiliary face")
    face = face_maker.Face()
    if face is None or _brep_topology.is_null(face):
        raise ValueError("auxiliary face is empty")
    return face
