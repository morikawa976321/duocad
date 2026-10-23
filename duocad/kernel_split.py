"""ボディを平面で割る幾何(OCPを触るカーネル層)。

平面を道具にした `BRepAlgoAPI_Splitter` で一度に割り、できた Solid を平面のどちら側にあるかで分ける。
位相・幾何量・形状検査は `brep_topology` / `brep_geometry` / `brep_checks`、ビルダ履歴の由来処理は `kernel` を参照する。

戻り値の規律は kernel と同じ(JSON化できる素の値だけ。例外は `"shape"`)。
"""
from __future__ import annotations

import math
from typing import Any, List, Sequence

from OCP.BRepAlgoAPI import BRepAlgoAPI_Splitter
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
from OCP.TopTools import TopTools_ListOfShape
from OCP.TopoDS import TopoDS_Shape  # noqa: F401  型注釈だけ
from OCP.gp import gp_Dir, gp_Pln, gp_Pnt

from . import kernel

from . import (
    brep_checks as _brep_checks,
    brep_geometry as _brep_geometry,
    brep_history as _brep_history,
    brep_topology as _brep_topology,
)


#: 平面の両側に体積が残ったとみなす、元の体積に対する比。平面が面に接しているだけ(片側が空)の判定に使う。
_SIDE_RTOL = 1.0e-9

#: 切った前後の体積の合計が合うとみなす相対差。
_SUM_RTOL = 1.0e-6

#: 境界箱の外側にある平面を「交わらない」と見切る余白[mm]。`kernel.bounding_box` は内部許容差ぶん膨らむ(実測 1e-7mm)。
_BOX_MARGIN = 1.0e-6

#: 体積が同じとみなす相対差(引き継ぎ先を決める比較)。
VOLUME_TIE_RTOL = 1.0e-9

SIDES = ("positive", "negative")


def split_by_plane(shape: "TopoDS_Shape", point: Sequence[float], normal: Sequence[float]) -> dict:
    """`shape` を、点 `point` を通り法線 `normal` の平面で割る(`_split_by_plane`。結果は `kernel.remembered`)。"""
    origin, axis = _brep_geometry._point_and_direction(
        point, normal, names=("plane_point", "plane_normal"), zero_reason="切る平面の向きが決まらない"
    )
    return kernel.remembered(
        "split_body",
        (shape,),
        (tuple(origin), tuple(axis)),
        lambda: _split_by_plane(shape, origin, axis),
    )


def _split_by_plane(shape: "TopoDS_Shape", origin: List[float], axis: List[float]) -> dict:
    """平面で割った塊の一覧を返す。**割れなければ(平面が交わらない・面に接しているだけ・片側が空)ValueError**。

    戻り値(`"shape"` 以外はJSON化できる素の値):
        {"volume_before": float,
         "pieces": [{"side": "positive" | "negative", "shape", "volume", "centroid": [x, y, z],
                     "provenance": {"face_map": {"<元の面の添字>": [この塊の面の添字, ...]}, "deleted": [...], "new_faces": [...]}}]}
    `pieces` の並びは決定的(正側が先、同じ側では重心の x, y, z の昇順)。`new_faces` が切り口の面。
    """
    if shape is None or _brep_topology.is_null(shape):
        raise ValueError("cannot split an empty shape")
    if not _brep_checks.is_solid(shape):
        raise ValueError("split_body: 対象はソリッドでなければならない")
    length = math.sqrt(sum(value * value for value in axis))
    unit = [value / length for value in axis]
    box = kernel.bounding_box(shape, cache=None)
    reach = sum(abs(unit[i]) * box["size"][i] / 2.0 for i in range(3))
    centre = box["center"]
    height = sum((centre[i] - origin[i]) * unit[i] for i in range(3))
    if abs(height) > reach + _BOX_MARGIN:
        raise _no_cut("平面がボディの外にある")
    if abs(height) >= reach - _BOX_MARGIN:
        raise _no_cut("平面が面・辺・頂点で接しているだけ")

    volume_before = _brep_geometry.volume(shape)
    # 工具の面はボディの境界箱の外接球より大きくする(平面の上で centre の真下に置いて余らせる)。
    foot = [centre[i] - height * unit[i] for i in range(3)]
    half = box["diagonal"] + abs(height) + 1.0
    tool = BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(*foot), gp_Dir(*unit)), -half, half, -half, half).Face()
    arguments, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
    arguments.Append(shape)
    tools.Append(tool)
    splitter = BRepAlgoAPI_Splitter()
    splitter.SetArguments(arguments)
    splitter.SetTools(tools)
    splitter.Build()
    if not splitter.IsDone() or splitter.Shape() is None or _brep_topology.is_null(splitter.Shape()):
        raise ValueError("split_body: 平面で割れなかった(OCCTの Splitter が失敗した)")

    pieces = [_piece(solid, splitter, shape, origin, unit) for solid in _brep_topology.split_solids(splitter.Shape())]
    for side in SIDES:
        if not any(piece["side"] == side for piece in pieces):
            raise _no_cut("平面の片側にボディが残らない(面に接しているだけ)")
    total = sum(piece["volume"] for piece in pieces)
    if abs(total - volume_before) > _SUM_RTOL * max(1.0, volume_before):
        raise ValueError(f"split_body: 割った塊の体積の合計が元と合わない({volume_before} → {total})")
    if any(piece["volume"] <= _SIDE_RTOL * volume_before for piece in pieces):
        raise _no_cut("平面の片側に体積がほとんど残らない")
    # 離れた塊を持つボディ(和を取った2つの箱など)では、塊の間を通る平面でも両側に塊が残る。切り口の面ができたかで交わりを見る
    if not any(piece["provenance"]["new_faces"] for piece in pieces):
        raise _no_cut("平面が塊の間を通るだけで、切り口ができない")
    pieces.sort(key=_piece_order)
    return {"volume_before": volume_before, "pieces": pieces}


#: 塊の並びで重心の座標を丸める桁(1e-9 mm の格子)。差の許容で比べると順序にならず(推移律が破れ、3つの塊で比較が循環する)、
#: 6桁の丸めでは 1e-6 mm より小さい x の差が消えて y が先に効く。
_CENTROID_DIGITS = 9


def _piece_order(piece: dict) -> tuple:
    """塊の並びの鍵: 正側が先、同じ側では重心の x, y, z の小さい方が先。"""
    return (piece["side"] != "positive", *[round(value, _CENTROID_DIGITS) for value in piece["centroid"]])


def _no_cut(reason: str) -> ValueError:
    return ValueError(f"split_body: 平面でボディを割れない({reason})。ボディの内側を通る平面を指すこと")


def _piece(solid: Any, splitter: Any, source: Any, origin: List[float], unit: List[float]) -> dict:
    _brep_checks._require_valid(solid, "split_body")
    volume = _brep_geometry.volume(solid)
    if not volume > 0.0:
        raise ValueError(f"split_body: 割った塊の体積が正でない(volume={volume})")
    centroid = _brep_geometry.centroid(solid)
    offset = sum((centroid[i] - origin[i]) * unit[i] for i in range(3))
    final_map = _brep_topology._face_map(solid)
    return {
        "side": "positive" if offset > 0.0 else "negative",
        "shape": solid,
        "volume": volume,
        "centroid": centroid,
        "provenance": _brep_topology._face_map_projection(_brep_history.target_successors(splitter, source, None, final_map), final_map.Extent()),
    }
