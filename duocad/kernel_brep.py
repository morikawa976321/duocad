"""生成CADの文書内保存・ソリッドの幾何検査(`inspect_solid`。受入検査の判定器 `check_final_shape.py` が使う)と、
箱・格子との共通体積。

共通体積の側(`require_valid_solid` から `boxes_common_volume` まで)は `topopt/acceptance.py` の
受入検査だけが使う。モジュールの状態を持たず、引数の形状と数値だけで閉じている。
"""
from __future__ import annotations

import io
import math
from typing import Dict, Sequence, Tuple

from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Builder
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.BRepTools import BRepTools
from OCP.BOPAlgo import BOPAlgo_BOP, BOPAlgo_Operation, BOPAlgo_Splitter
from OCP.TopAbs import TopAbs_SOLID
from OCP.TopoDS import TopoDS_Shape
from OCP.TopTools import TopTools_ListOfShape
from OCP.gp import gp_Dir, gp_Pln, gp_Pnt

from . import brep_geometry
from .brep_topology import compound, subshapes, is_null as _is_null
from .brep_checks import _count_face_interferences, inspect_solid, require_valid_solid, is_valid as _is_valid
from .brep_geometry import _mass_and_center


def encode_brep(shape):
    stream = io.BytesIO()
    BRepTools.Write_s(shape, stream)
    return stream.getvalue().decode('ascii')


def decode_brep(text):
    shape = TopoDS_Shape()
    BRepTools.Read_s(shape, io.BytesIO(text.encode('ascii')), BRep_Builder())
    if _is_null(shape) or not _is_valid(shape):
        raise ValueError('保存されたCADソリッドが不正')
    return shape


# --------------------------------------------------------------------------------------
# 箱・格子との共通体積(形状最適化の受入検査)
# --------------------------------------------------------------------------------------


def _common_with_box(shape: "TopoDS_Shape", lo: Sequence[float], hi: Sequence[float]):
    """`shape` と軸整列直方体1つの共通部分と、その体積。`BOPAlgo_COMMON`(fuzzy 0.0)で取り、
    エラー・警告・不正な結果は例外にする(黙って0を積まない)。"""
    box = BRepPrimAPI_MakeBox(gp_Pnt(*lo), gp_Pnt(*hi)).Shape()
    operation = BOPAlgo_BOP()
    operation.AddArgument(shape)
    operation.AddTool(box)
    operation.SetOperation(BOPAlgo_Operation.BOPAlgo_COMMON)
    operation.SetFuzzyValue(0.0)
    operation.Perform()
    if operation.HasErrors() or operation.HasWarnings():
        raise RuntimeError("common-volume boolean did not finish cleanly")
    result = operation.Shape()
    if result is None or _is_null(result) or not _is_valid(result):
        raise RuntimeError("common-volume boolean produced an invalid shape")
    piece = brep_geometry.volume(result)
    if not math.isfinite(piece) or piece < 0:
        raise RuntimeError("common volume is not finite/non-negative")
    return result, piece


def box_common_volume(
    shape: "TopoDS_Shape", lo: Sequence[float], hi: Sequence[float]
) -> float:
    """`shape` と軸整列直方体1つ(`(min, max)` の mm 座標)の共通体積。重ならなければ 0.0。
    **`shape` の妥当性は見ない**(呼ぶ側が `require_valid_solid` を1回だけ通す)。
    """
    return _common_with_box(shape, lo, hi)[1]


#: `grid_cell_common_volumes` が「塊の中身が全部ソリッド」とみなす、塊の中の体積 / 塊の体積 の下限。
#: 届かなければ切り分けに進むだけで、値は変わらない。
_FULL_BLOCK_RATIO = 1.0 - 1e-12

#: 同じ体積を2つの方法で求めた値(破片の合計と、切る前の塊の中の体積。塊の中の体積を切る方法とブーリアンで
#: 取ったもの)が、この相対差を超えて食い違ったら失敗にする。**実測**(hanger、要素4.8mm、8セル角の塊):
#: 相対差は最大 1.1e-6(曲面が斜めに切れた大きな円筒の塊)。既定の積分は曲面の破片で 1e-6 ほど
#: ずれる(適応積分 `Eps=1e-12` との差)ので、これより厳しくすると正しい切り分けを落とす。一方、
#: 破片を1つ落とせばこの桁より遥かに大きくずれる。
_SPLIT_VOLUME_RTOL = 1e-5

#: 切り分けた破片は1セルに収まるはずで、セルの体積を(既定の積分の誤差を見込んで)これ以上の割合で超える破片は、
#: 平面で切れていない。重心でセルを決めているので、切れていない破片は別のセルへ振り分けられてしまう。
_FULL_CELL_LIMIT = 1.0 + 1e-6


def _axis_planes(lo: Sequence[float], hi: Sequence[float], positions, reach: float) -> "TopTools_ListOfShape":
    """軸に垂直な平面の面。`positions[axis]` がその軸の平面の座標。面は直方体 `[lo, hi]` の中心から
    `reach` の半幅で広がる(切りたい形状の端まで届く大きさにすること)。"""
    center = [(lo[a] + hi[a]) / 2.0 for a in range(3)]
    half = reach
    planes = TopTools_ListOfShape()
    for axis in range(3):
        normal = [0.0, 0.0, 0.0]
        normal[axis] = 1.0
        for coordinate in positions[axis]:
            origin = list(center)
            origin[axis] = coordinate
            plane = gp_Pln(gp_Pnt(*origin), gp_Dir(*normal))
            planes.Append(BRepBuilderAPI_MakeFace(plane, -half, half, -half, half).Face())
    return planes


def _split_by_planes(argument: "TopoDS_Shape", planes: "TopTools_ListOfShape") -> "TopoDS_Shape":
    splitter = BOPAlgo_Splitter()
    splitter.AddArgument(argument)
    splitter.SetTools(planes)
    splitter.SetFuzzyValue(0.0)
    splitter.Perform()
    # 警告は見ない: 曲面を斜めに切ると `BOPAlgo_AlertFaceBuilderUnusedEdges` が出るが、体積は合っている
    # (実測: 警告の出た塊で破片の合計は切る前の体積に 1.6e-8 で一致)。取りこぼしは呼び出し側の合計の検算が拾う。
    if splitter.HasErrors():
        raise RuntimeError("cell split boolean failed")
    return splitter.Shape()


def grid_cell_common_volumes(
    shape: "TopoDS_Shape", lo: Sequence[float], spacing: float, counts: Sequence[int]
) -> Tuple[str, Dict[Tuple[int, int, int], float]]:
    """`shape` と、`lo` を最小角とする `counts`(x, y, z)個の立方体セル(一辺 `spacing`)の塊との
    共通体積を、**セルごとに**求める。

    戻り値は `(kind, volumes)`。`kind` は `'empty'`(塊と重ならない。`volumes` は空)/ `'full'`
    (塊が全部ソリッドの中。全セルが `spacing³`。`volumes` は空)/ `'partial'`(`volumes` に
    塊内の添字 `(i, j, k)` → 共通体積。重ならないセルは入れない)。

    やり方は「`shape` を平面で切って、破片を重心のあるセルへ振り分ける」。まず塊の6面で切って塊の中の
    破片を取り(空・全部の判定はここ)、境界にかかる塊だけをセルの境界面で切り分ける。
    **セルごとにブーリアン(`box_common_volume` をセル数だけ)を取らない**。理由は2つ。
    (1) セルの面が形状の継ぎ目(円筒のシームなど)のすぐそばに来ると、共通部分が空という誤った答えを
    **エラーも警告も出さずに**返す(実測: hanger の円板ソリッド〈体積1048mm³〉でセル単位の合計が
    1030.58mm³、17.4mm³ を落とした)。平面で切る方法は同じ形状で体積を落とさなかった。
    (2) 曲面との交線をセルごとに計算し直さないので桁違いに速い(実測: 曲面のソリッドの364セルの塊が、
    セルごとのブーリアンで約30秒、平面で切って約1秒)。
    **`shape` の妥当性は見ない**(`require_valid_solid` は呼び出し側が1回だけ通す)。

    (1) の罠(継ぎ目の近くで黙って体積を落とす)を**値で検知するテストは無い** —— 再現に私物の hanger が要り、
    合成の題材では再現できなかった(2026-09-24)。**「切る方法」と「ブーリアン」の2通りの突き合わせ
    (下の `_SPLIT_VOLUME_RTOL` の検査)が唯一の防具**で、食い違えば RuntimeError にして呼び出し側が
    直接ブーリアンへ戻す(`topopt.acceptance._CellOverlaps`)。
    """
    counts = tuple(int(n) for n in counts)
    lo = tuple(float(v) for v in lo)
    hi = tuple(lo[a] + counts[a] * spacing for a in range(3))
    # 面は形状の端まで届く大きさにする。届かないと、切り口の回り込みで塊の外の残り全部が1つの破片に
    # つながり、その重心が塊の中に落ちることがある(実測: 853×50×695mm の箱を8セル角の塊で切ったとき、
    # 残りの破片の重心が塊の中に落ちて、体積を2960万mm³ 余計に数えた)。全部届けば、破片は6面で分けた
    # 27個の直方体領域のどれか1つに収まり(直方体は凸)、重心はその領域の中にある。
    span = Bnd_Box()
    BRepBndLib.Add_s(shape, span, False)
    extent = span.Get()
    reach = max(abs(extent[a + side] - (lo[a] + hi[a]) / 2.0) for a in range(3) for side in (0, 3))
    reach += math.dist(lo, hi)
    inside, inside_volume = [], 0.0
    for solid in subshapes(_split_by_planes(shape, _axis_planes(lo, hi, [(lo[a], hi[a]) for a in range(3)], reach)),
                           TopAbs_SOLID):
        mass, center = _mass_and_center(solid)
        if all(lo[a] < center[a] < hi[a] for a in range(3)):
            inside.append(solid)
            inside_volume += mass
    block = math.prod(counts) * spacing ** 3
    # 切る方法だけに頼らない: 塊の中の体積をブーリアンでも取り、食い違えば失敗にする。塊の面が継ぎ目の
    # すぐそばに来ると、切る方法もブーリアンも黙って体積を落とすことがある(実測: 2セル角の塊で、円板の
    # 体積が切る方法で1.4%、ブーリアンで20%落ちた)が、落とし方が違うので食い違いで見つかる。
    common = box_common_volume(shape, lo, hi)
    if abs(common - inside_volume) > _SPLIT_VOLUME_RTOL * max(common, inside_volume) + 1e-12 * block:
        raise RuntimeError("block volume inside the shape disagrees between cutting and boolean common")
    if not inside or inside_volume == 0.0:
        return "empty", {}
    if inside_volume >= _FULL_BLOCK_RATIO * block:
        return "full", {}
    cells = _split_by_planes(compound(inside), _axis_planes(
        lo, hi, [[lo[a] + m * spacing for m in range(1, counts[a])] for a in range(3)], math.dist(lo, hi)))
    volumes: Dict[Tuple[int, int, int], float] = {}
    for solid in subshapes(cells, TopAbs_SOLID):
        mass, center = _mass_and_center(solid)
        if mass > _FULL_CELL_LIMIT * spacing ** 3:
            raise RuntimeError("cell piece is larger than a cell: a cutting plane did not split it")
        cell = tuple(min(counts[a] - 1, max(0, math.floor((center[a] - lo[a]) / spacing))) for a in range(3))
        volumes[cell] = volumes.get(cell, 0.0) + mass
    if abs(sum(volumes.values()) - inside_volume) > _SPLIT_VOLUME_RTOL * inside_volume:
        raise RuntimeError("cell pieces do not add up to the block's volume inside the shape")
    return "partial", volumes


def boxes_common_volume(
    shape: "TopoDS_Shape",
    boxes: Sequence[Tuple[Sequence[float], Sequence[float]]],
) -> float:
    """`shape` と、軸整列直方体群(各要素は `(min, max)` の mm 座標)の共通体積の合計。

    `boxes` は**互いに体積を共有しない**ことを呼び出し側が保証する(重なると二重に数える)。
    """
    require_valid_solid(shape)
    overlap = 0.0
    for lo, hi in boxes:
        overlap += box_common_volume(shape, lo, hi)
    return overlap
