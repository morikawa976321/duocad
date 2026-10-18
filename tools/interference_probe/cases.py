"""干渉チェックの試作で使う形状の組(Issue #186)。

各ケースは2つのソリッドと、手で計算した期待値(分類・重なり体積・最小距離)を持つ。
分類は3つ: overlap(体積が重なる) / contact(面・辺・点が触れるが体積は重ならない) / clear(離れている)。
gap は clear のときの最短距離(mm)。contact と overlap では 0。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, List, Optional

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepPrimAPI import (BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder,
                             BRepPrimAPI_MakeSphere, BRepPrimAPI_MakeTorus)
from OCP.TopAbs import TopAbs_EDGE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec


@dataclass
class Case:
    name: str
    a: TopoDS_Shape
    b: TopoDS_Shape
    expect: str                    # overlap / contact / clear
    volume: float = 0.0            # 期待する重なり体積(mm^3)
    gap: float = 0.0               # 期待する最短距離(mm)
    note: str = ""


def box(x0, y0, z0, dx, dy, dz) -> TopoDS_Shape:
    return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), dx, dy, dz).Shape()


def cyl(x, y, z, r, h) -> TopoDS_Shape:
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, z), gp_Dir(0, 0, 1)), r, h).Shape()


def sphere(x, y, z, r) -> TopoDS_Shape:
    return BRepPrimAPI_MakeSphere(gp_Pnt(x, y, z), r).Shape()


def moved(shape, dx=0.0, dy=0.0, dz=0.0) -> TopoDS_Shape:
    t = gp_Trsf()
    t.SetTranslation(gp_Vec(dx, dy, dz))
    return BRepBuilderAPI_Transform(shape, t, True).Shape()


def rotated(shape, axis=(0, 0, 1), deg=0.0, origin=(0, 0, 0)) -> TopoDS_Shape:
    t = gp_Trsf()
    t.SetRotation(gp_Ax1(gp_Pnt(*origin), gp_Dir(*axis)), math.radians(deg))
    return BRepBuilderAPI_Transform(shape, t, True).Shape()


def cut(a, b) -> TopoDS_Shape:
    op = BRepAlgoAPI_Cut(a, b)
    op.Build()
    return op.Shape()


def filleted_box(x0, y0, z0, dx, dy, dz, r) -> TopoDS_Shape:
    shape = box(x0, y0, z0, dx, dy, dz)
    mk = BRepFilletAPI_MakeFillet(shape)
    exp = TopExp_Explorer(shape, TopAbs_EDGE)
    while exp.More():
        mk.Add(r, TopoDS.Edge_s(exp.Current()))
        exp.Next()
    mk.Build()
    return mk.Shape()


def block_with_hole(hole_r, size=20.0, h=10.0) -> TopoDS_Shape:
    """中心(0,0)に半径 hole_r の貫通穴を持つ size 角・高さ h の板。"""
    return cut(box(-size / 2, -size / 2, 0, size, size, h), cyl(0, 0, -1, hole_r, h + 2))


def build_cases() -> List[Case]:
    cases: List[Case] = []
    add = cases.append
    A = box(0, 0, 0, 10, 10, 10)

    # --- 箱どうし ---
    add(Case("box_overlap_half", A, box(5, 0, 0, 10, 10, 10), "overlap", volume=500.0))
    add(Case("box_contained", A, box(2, 2, 2, 3, 3, 3), "overlap", volume=27.0, note="内包"))
    add(Case("box_face_touch", A, box(10, 0, 0, 10, 10, 10), "contact"))
    add(Case("box_face_touch_partial", A, box(10, 3, 4, 10, 10, 10), "contact", note="面の一部だけ接する"))
    add(Case("box_edge_touch", A, box(10, 10, 0, 10, 10, 10), "contact"))
    add(Case("box_vertex_touch", A, box(10, 10, 10, 10, 10, 10), "contact"))
    add(Case("box_gap_0.1", A, box(10.1, 0, 0, 10, 10, 10), "clear", gap=0.1))
    add(Case("box_gap_0.01", A, box(10.01, 0, 0, 10, 10, 10), "clear", gap=0.01))
    add(Case("box_gap_1e-5", A, box(10.00001, 0, 0, 10, 10, 10), "clear", gap=1e-5,
             note="OCCT の Confusion(1e-7)より大きく、線形許容差(1e-3 程度の慣習)より小さい"))
    add(Case("box_gap_1e-8", A, box(10.00000001, 0, 0, 10, 10, 10), "contact",
             note="Confusion(1e-7)未満のずれ。接触とみなすのが期待"))
    add(Case("box_overlap_1e-3", A, box(9.999, 0, 0, 10, 10, 10), "overlap", volume=0.1,
             note="0.001mm の食い込み"))
    add(Case("box_overlap_1e-5", A, box(9.99999, 0, 0, 10, 10, 10), "overlap", volume=1e-3,
             note="0.00001mm の食い込み(許容差の境目)"))
    add(Case("box_gap_far", A, box(50, 50, 50, 10, 10, 10), "clear", gap=math.sqrt(3) * 40))
    # 回転した箱(境界箱が緩む)。対角 45° 回転で、箱 A の角から離して置く。
    rot = rotated(box(-5, -5, 0, 10, 10, 10), deg=45.0)
    add(Case("rot_box_clear", A, moved(rot, 10 + 5 * math.sqrt(2) + 0.5, 5, 0), "clear", gap=0.5,
             note="45°回転の箱の頂点が A の面から 0.5"))
    add(Case("rot_box_overlap", A, moved(rot, 10 + 5 * math.sqrt(2) - 0.5, 5, 0), "overlap",
             volume=0.5 * 0.5 * 2 / 2 * 10 * 1.0, note="頂点が 0.5 食い込む三角柱(体積 0.25*10=2.5)"))

    # --- ピンと穴(はめ合い。半径方向の隙間) ---
    for r_pin, r_hole, label in [(5.0, 5.2, "gap_0.2"), (5.0, 5.1, "gap_0.1"), (5.0, 5.05, "gap_0.05"),
                                 (5.0, 5.0, "coincident"), (5.05, 5.0, "press_0.05"), (5.2, 5.0, "press_0.2")]:
        plate = block_with_hole(r_hole)
        pin = cyl(0, 0, -5, r_pin, 20)
        if r_pin < r_hole:
            add(Case(f"pin_{label}", plate, pin, "clear", gap=r_hole - r_pin))
        elif r_pin == r_hole:
            add(Case(f"pin_{label}", plate, pin, "contact", note="円筒面が一致"))
        else:
            add(Case(f"pin_{label}", plate, pin, "overlap",
                     volume=math.pi * (r_pin ** 2 - r_hole ** 2) * 10.0))
    # 偏心したピン(片側だけ接する/食い込む)
    add(Case("pin_offcenter_gap_0.05", block_with_hole(5.2), cyl(0.15, 0, -5, 5.0, 20), "clear", gap=0.05))
    add(Case("pin_offcenter_touch", block_with_hole(5.2), cyl(0.2, 0, -5, 5.0, 20), "contact",
             note="内接(線接触)"))

    # 三角形分割の位相をずらす: ピンを軸まわりに回すと縫い目の位置が変わり、穴の多角形と頂点の角度がずれる。
    # 位相が揃っていると多角形どうしの隙間は (R-r)cos(θ/2) になり、分割の誤差が打ち消されて見える。
    for r_pin, gap, deg in [(5.0, 0.2, 7.3), (30.0, 0.2, 3.1), (30.0, 0.1, 3.1), (30.0, -0.05, 3.1)]:
        r_hole = r_pin + gap if gap > 0 else r_pin
        r_pin_eff = r_pin if gap > 0 else r_pin - gap
        size = 2 * r_hole + 10
        plate = block_with_hole(r_hole, size=size)
        pin = rotated(cyl(0, 0, -5, r_pin_eff, 20), deg=deg)
        if gap > 0:
            add(Case(f"pin_r{r_pin:g}_gap_{gap:g}_phase", plate, pin, "clear", gap=gap))
        else:
            add(Case(f"pin_r{r_pin:g}_press_{-gap:g}_phase", plate, pin, "overlap",
                     volume=math.pi * (r_pin_eff ** 2 - r_hole ** 2) * 10.0))

    # --- 曲面どうし ---
    add(Case("sphere_on_plane", A, sphere(5, 5, 15, 5), "contact", note="点接触"))
    add(Case("sphere_gap_0.2", A, sphere(5, 5, 15.2, 5), "clear", gap=0.2))
    add(Case("sphere_overlap", A, sphere(5, 5, 14, 5), "overlap",
             volume=math.pi * 1.0 ** 2 * (3 * 5 - 1.0) / 3, note="高さ1の球冠"))
    add(Case("spheres_gap_0.2", sphere(0, 0, 0, 5), sphere(10.2, 0, 0, 5), "clear", gap=0.2))
    add(Case("cyl_side_touch", cyl(0, 0, 0, 5, 10), cyl(10, 0, 0, 5, 10), "contact", note="円筒の側面どうしの線接触"))

    # --- フィレット付き ---
    fb = filleted_box(0, 0, 0, 10, 10, 10, 2.0)
    add(Case("fillet_box_face_touch", fb, filleted_box(10, 0, 0, 10, 10, 10, 2.0), "contact"))
    add(Case("fillet_box_overlap", fb, filleted_box(9, 0, 0, 10, 10, 10, 2.0), "overlap",
             volume=None, note="体積は OCCT 基準で比べる(手計算しない)"))
    add(Case("fillet_box_corner_gap", fb, filleted_box(10, 10, 0, 10, 10, 10, 2.0), "clear",
             gap=2 * (2.0 * math.sqrt(2) - 2.0), note="丸めた辺どうしが向かい合う"))
    return cases
