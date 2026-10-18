"""試す形の一式。各ケースは「元の形・選ぶ面・(移動なら)距離・期待する形」を持つ。

期待する形は**特徴を足す前の形を直接組んだもの**で、結果との一致は体積の差ではなく
対称差の体積(`geometry.symmetric_difference`)で見る(体積だけだと別の形でも一致しうる)。
`expected=None` は「失敗するのが正しい」、`expected=UNKNOWN` は「正解を決めずに結果を記録する」。
"""
from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeFace
from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer, BRepFilletAPI_MakeFillet
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCone,
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakePrism,
)
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Vec
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Reader, STEPControl_Writer
from OCP.TopAbs import TopAbs_ShapeEnum

from .geometry import edge_midpoint, edges_of, face_centroid, face_normal, faces_of, solids_of, surface_type

UNKNOWN = object()


@dataclass
class Case:
    name: str
    kind: str  # "delete" | "move"
    shape: object
    faces: List[object]
    expected: object  # TopoDS_Shape / None(失敗が正) / UNKNOWN
    distance: float = 0.0
    note: str = ""
    tags: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# 組み立ての部品
# --------------------------------------------------------------------------------------
def box(dx, dy, dz, x=0.0, y=0.0, z=0.0):
    return BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), dx, dy, dz).Shape()


def cylinder(r, h, p=(0, 0, 0), d=(0, 0, 1)):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*p), gp_Dir(*d)), r, h).Shape()


def cone(r1, r2, h, p=(0, 0, 0), d=(0, 0, 1)):
    return BRepPrimAPI_MakeCone(gp_Ax2(gp_Pnt(*p), gp_Dir(*d)), r1, r2, h).Shape()


def unify(shape):
    unifier = ShapeUpgrade_UnifySameDomain(shape, True, True, False)
    unifier.Build()
    return unifier.Shape()


def cut(a, *tools):
    for tool in tools:
        a = BRepAlgoAPI_Cut(a, tool).Shape()
    return unify(a)


def fuse(a, *tools):
    for tool in tools:
        a = BRepAlgoAPI_Fuse(a, tool).Shape()
    return unify(a)


def prism_yz(points, length):
    """yz 平面の多角形を x 方向へ `length` だけ押し出す(斜面を持つ形の正解を組む)。"""
    polygon = BRepBuilderAPI_MakePolygon()
    for y, z in points:
        polygon.Add(gp_Pnt(0, y, z))
    polygon.Close()
    face = BRepBuilderAPI_MakeFace(polygon.Wire(), True).Face()
    return BRepPrimAPI_MakePrism(face, gp_Vec(length, 0, 0)).Shape()


def fillet(shape, predicate: Callable, r):
    maker = BRepFilletAPI_MakeFillet(shape)
    count = 0
    for edge in edges_of(shape):
        if predicate(edge_midpoint(edge)):
            maker.Add(r, edge)
            count += 1
    assert count, "no edge matched"
    maker.Build()
    assert maker.IsDone(), "fillet failed while building a case"
    return maker.Shape()


def chamfer(shape, predicate: Callable, d):
    maker = BRepFilletAPI_MakeChamfer(shape)
    count = 0
    for edge in edges_of(shape):
        if predicate(edge_midpoint(edge)):
            maker.Add(d, edge)
            count += 1
    assert count, "no edge matched"
    maker.Build()
    assert maker.IsDone(), "chamfer failed while building a case"
    return maker.Shape()


def _quiet_occt():
    from OCP.Message import Message

    messenger = Message.DefaultMessenger_s()
    for printer in list(messenger.Printers()):
        messenger.RemovePrinter(printer)


def step_roundtrip(shape):
    """STEP へ書いて読み直す(取り込んだボディには構築の履歴も同じ TShape も無い)。"""
    _quiet_occt()
    handle, path = tempfile.mkstemp(suffix=".step")
    os.close(handle)
    try:
        writer = STEPControl_Writer()
        writer.Transfer(shape, STEPControl_AsIs)
        writer.Write(path)
        reader = STEPControl_Reader()
        assert reader.ReadFile(path) == IFSelect_ReturnStatus.IFSelect_RetDone
        reader.TransferRoots()
        result = reader.OneShape()
    finally:
        os.unlink(path)
    solids = solids_of(result)
    assert len(solids) == 1
    return solids[0]


def pick(shape, predicate: Callable):
    """`predicate(face)` を満たす面。"""
    faces = [f for f in faces_of(shape) if predicate(f)]
    assert faces, "no face matched"
    return faces


def curved(face):
    return surface_type(face) != "plane"


def plane_at(axis, value, normal_sign=None, tol=1e-6):
    """軸 `axis`(0/1/2)の座標が `value` の平面(法線の向きで絞るなら `normal_sign`)。"""

    def predicate(face):
        if surface_type(face) != "plane":
            return False
        c = face_centroid(face)
        if abs(c[axis] - value) > tol:
            return False
        n = face_normal(face)
        if abs(abs(n[axis]) - 1.0) > 1e-6:
            return False
        return normal_sign is None or n[axis] * normal_sign > 0

    return predicate


def slanted_plane(face):
    if surface_type(face) != "plane":
        return False
    n = face_normal(face)
    return max(abs(v) for v in n) < 1 - 1e-6


def of_type(kind, **conditions):
    def predicate(face):
        if surface_type(face) != kind:
            return False
        c = face_centroid(face)
        return all(cond(c) for cond in conditions.values())

    return predicate


# --------------------------------------------------------------------------------------
# ケース
# --------------------------------------------------------------------------------------
W, D, H = 40.0, 30.0, 20.0  # 基準の直方体


def _top_front(m):
    return abs(m[1]) < 1e-6 and abs(m[2] - H) < 1e-6


def _top(m):
    return abs(m[2] - H) < 1e-6


def delete_cases() -> List[Case]:
    base = box(W, D, H)
    cases: List[Case] = []

    s = fillet(base, _top_front, 5)
    cases.append(Case("d_fillet_1edge", "delete", s, pick(s, curved), base, tags=["fillet"]))

    s = fillet(base, _top, 4)
    cases.append(Case("d_fillet_top4", "delete", s, pick(s, curved), base, note="上面4辺の連なり", tags=["fillet"]))
    front = pick(s, lambda f: curved(f) and face_centroid(f)[1] < 2.0)
    cases.append(
        Case("d_fillet_top4_one", "delete", s, front, UNKNOWN, note="連なりの1枚だけ", tags=["fillet", "partial"])
    )

    s = fillet(base, lambda m: True, 3)
    cases.append(Case("d_fillet_all12", "delete", s, pick(s, curved), base, note="頂点の球面を含む", tags=["fillet"]))
    vertical = [f for f in faces_of(s) if surface_type(f) == "cylinder" and _axis_of(f) == 2]
    cases.append(
        Case(
            "d_fillet_all12_vertical4", "delete", s, vertical, UNKNOWN,
            note="縦の4辺のフィレットだけ(球面の角は残す)", tags=["fillet", "partial"],
        )
    )

    s = chamfer(base, _top_front, 5)
    cases.append(Case("d_chamfer_1edge", "delete", s, pick(s, slanted_plane), base, tags=["chamfer"]))
    s = chamfer(base, _top, 3)
    cases.append(Case("d_chamfer_top4", "delete", s, pick(s, slanted_plane), base, tags=["chamfer"]))

    hole = cylinder(5, H + 2, (W / 2, D / 2, -1))
    s = cut(base, hole)
    cases.append(Case("d_hole_through", "delete", s, pick(s, curved), base, tags=["hole"]))

    s = cut(base, cylinder(5, 11, (W / 2, D / 2, 10)))
    blind = pick(s, curved) + pick(s, plane_at(2, 10.0))
    cases.append(Case("d_hole_blind", "delete", s, blind, base, tags=["hole"]))

    small = cylinder(3, H + 2, (W / 2, D / 2, -1))
    s = cut(base, small, cylinder(5.5, 6, (W / 2, D / 2, 15)))
    through_small = cut(base, small)
    cbore = pick(s, of_type("cylinder", z=lambda c: c[2] > 15)) + pick(s, plane_at(2, 15.0))
    cases.append(Case("d_counterbore_all", "delete", s, pick(s, curved) + pick(s, plane_at(2, 15.0)), base, tags=["hole"]))
    cases.append(Case("d_counterbore_cbore_only", "delete", s, cbore, through_small, note="座ぐりだけ", tags=["hole"]))

    s = cut(base, small, cone(3, 6.0 + 1.0, 4.0, (W / 2, D / 2, 17)))
    cases.append(Case("d_countersink_all", "delete", s, pick(s, curved), base, tags=["hole"]))
    cases.append(
        Case("d_countersink_cone_only", "delete", s, pick(s, of_type("cone")), through_small, note="皿だけ", tags=["hole"])
    )

    boss = cylinder(5, 8, (W / 2, D / 2, H))
    s = fuse(base, boss)
    boss_faces = pick(s, curved) + pick(s, plane_at(2, H + 8))
    cases.append(Case("d_boss", "delete", s, boss_faces, base, tags=["boss"]))
    sf = fillet(s, lambda m: abs(m[2] - H) < 1e-6 and math.hypot(m[0] - W / 2, m[1] - D / 2) < 5.5, 2)
    cases.append(
        Case("d_boss_fillet", "delete", sf, pick(sf, curved) + pick(sf, plane_at(2, H + 8)), base, tags=["boss", "fillet"])
    )

    s = cut(base, box(10, 10, 6, 15, 10, 15))
    pocket = [f for f in faces_of(s) if _inside_xy(face_centroid(f), 15, 25, 10, 20) and 1.0 < face_centroid(f)[2] < H - 1e-6]
    cases.append(Case("d_pocket", "delete", s, pocket, base, tags=["pocket"]))

    s = cut(base, box(W + 2, 10, 6, -1, 10, 15))
    slot = [f for f in faces_of(s) if 10 - 1e-6 <= face_centroid(f)[1] <= 20 + 1e-6 and 15 - 1e-6 <= face_centroid(f)[2] < H - 1e-6]
    cases.append(Case("d_slot_through", "delete", s, slot, base, tags=["pocket"]))

    hx = cylinder(4, W + 2, (-1, D / 2, 10), (1, 0, 0))
    hy = cylinder(4, D + 2, (W / 2, -1, 10), (0, 1, 0))
    s = cut(base, hx, hy)
    x_hole = [f for f in faces_of(s) if surface_type(f) == "cylinder" and _axis_of(f) == 0]
    cases.append(
        Case("d_cross_holes_x", "delete", s, x_hole, cut(base, hy), note="交差する穴の片方。残る穴の面が交差部を埋め直す", tags=["hole", "topology"])
    )

    s = fillet(cut(base, hole), lambda m: abs(m[2] - H) < 1e-6 and math.hypot(m[0] - W / 2, m[1] - D / 2) < 5.5, 1.5)
    cases.append(Case("d_hole_rim_fillet", "delete", s, pick(s, of_type("torus")), cut(base, hole), note="穴の縁の丸めだけ", tags=["hole", "fillet"]))

    plate = box(40, 10, 20)
    s = fillet(plate, lambda m: abs(m[2] - 20) < 1e-6 and abs(m[0] - 20) < 1e-6, 4.9)
    cases.append(Case("d_almost_full_round", "delete", s, pick(s, curved), plate, note="上面が 0.2 mm 幅まで食われた板", tags=["fillet", "topology"]))
    round_top = fuse(box(40, 10, 15), cylinder(5, 40, (0, 5, 15), (1, 0, 0)))
    cases.append(
        Case("d_full_round", "delete", round_top, pick(round_top, curved), None, note="上面が無い。消すと面が「現れる」必要がある", tags=["topology", "fail"])
    )

    cases.append(Case("d_box_top", "delete", base, pick(base, plane_at(2, H)), None, note="周りが平行で閉じない", tags=["fail"]))

    lblock = fuse(box(40, 30, 10), box(20, 30, 10, 0, 0, 10))
    cases.append(
        Case("d_step_tread", "delete", lblock, pick(lblock, plane_at(2, 10.0)), UNKNOWN, note="段の踏み面", tags=["topology"])
    )
    cases.append(
        Case("d_step_riser", "delete", lblock, pick(lblock, plane_at(0, 20.0)), UNKNOWN, note="段の蹴上げ面。閉じ方が2通りある", tags=["topology"])
    )

    holed = cut(base, hole)
    s = fillet(holed, _top_front, 5)
    mixed = pick(s, of_type("cylinder", y=lambda c: c[1] < 5)) + pick(s, of_type("cylinder", y=lambda c: abs(c[1] - D / 2) < 1e-6))
    cases.append(Case("d_fillet_and_hole", "delete", s, mixed, base, note="別の特徴の面を一度に", tags=["fillet", "hole", "multi"]))

    plate = box(200, 200, 10)
    holes = [cylinder(3, 12, (10 + 20 * i, 10 + 20 * j, -1)) for i in range(10) for j in range(10)]
    many = cut(plate, *[_compound(holes)])
    cases.append(Case("d_many_holes", "delete", many, pick(many, curved), plate, note="穴100個を一度に", tags=["hole", "scale"]))
    many_f = fillet(many, lambda m: abs(m[2] - 10) < 1e-6 and _near_hole(m), 1.0)
    cases.append(
        Case("d_many_holes_rim_fillets", "delete", many_f, pick(many_f, of_type("torus")), many, note="穴100個の縁の丸め100枚", tags=["fillet", "scale"])
    )

    for name in ("d_fillet_1edge", "d_fillet_all12", "d_counterbore_all", "d_boss_fillet", "d_hole_through"):
        original = next(c for c in cases if c.name == name)
        cases.append(_via_step(original))
    return cases


def move_cases() -> List[Case]:
    base = box(W, D, H)
    cases: List[Case] = []
    top = pick(base, plane_at(2, H))
    cases.append(Case("m_box_top_up5", "move", base, top, box(W, D, H + 5), 5.0))
    cases.append(Case("m_box_top_down5", "move", base, top, box(W, D, H - 5), -5.0))
    cases.append(Case("m_box_top_too_far", "move", base, top, None, -25.0, note="高さを越えて沈める", tags=["fail"]))
    cases.append(Case("m_box_top_exact", "move", base, top, None, -20.0, note="ちょうど底まで(厚み0)", tags=["fail"]))

    hole = cylinder(5, H + 2, (W / 2, D / 2, -1))
    s = cut(base, hole)
    hf = pick(s, curved)
    cases.append(Case("m_hole_shrink1", "move", s, hf, cut(base, cylinder(4, H + 2, (W / 2, D / 2, -1))), 1.0, tags=["hole"]))
    cases.append(Case("m_hole_grow2", "move", s, hf, cut(base, cylinder(7, H + 2, (W / 2, D / 2, -1))), -2.0, tags=["hole"]))
    cases.append(Case("m_hole_break_wall", "move", s, hf, None, -11.0, note="半径16で壁(15)を破る", tags=["hole", "fail"]))
    cases.append(Case("m_hole_vanish", "move", s, hf, None, 5.0, note="半径0", tags=["hole", "fail"]))

    c = cylinder(10, 20)
    cases.append(Case("m_cyl_outer_grow", "move", c, pick(c, curved), cylinder(12, 20), 2.0))

    profile = [(0, 0), (30, 0), (30, 20), (10, 20), (0, 8)]
    wedge = prism_yz(profile, W)
    wtop = pick(wedge, plane_at(2, 20.0))
    up = prism_yz([(0, 0), (30, 0), (30, 24), (16 * 10 / 12, 24), (0, 8)], W)
    down = prism_yz([(0, 0), (30, 0), (30, 16), (8 * 10 / 12, 16), (0, 8)], W)
    cases.append(Case("m_wedge_top_up4", "move", wedge, wtop, up, 4.0, note="斜面が延びる", tags=["slant"]))
    cases.append(Case("m_wedge_top_down4", "move", wedge, wtop, down, -4.0, note="斜面が縮む", tags=["slant"]))

    s = chamfer(base, _top_front, 5)
    expect = prism_yz([(0, 0), (D, 0), (D, H + 3), (8, H + 3), (0, 15)], W)
    cases.append(Case("m_chamfer_top_up3", "move", s, pick(s, plane_at(2, H)), expect, 3.0, note="面取りの斜面が延びる", tags=["slant", "chamfer"]))

    s = fillet(base, _top_front, 5)
    follow_up = fillet(box(W, D, H + 3), lambda m: abs(m[1]) < 1e-6 and abs(m[2] - H - 3) < 1e-6, 5)
    cases.append(
        Case("m_fillet_adj_top_up3", "move", s, pick(s, plane_at(2, H)), follow_up, 3.0,
             note="丸めの隣の面。正解は「丸めが付いてくる」形", tags=["fillet", "blend"])
    )
    follow_down = fillet(box(W, D, H - 3), lambda m: abs(m[1]) < 1e-6 and abs(m[2] - H + 3) < 1e-6, 5)
    cases.append(
        Case("m_fillet_adj_top_down3", "move", s, pick(s, plane_at(2, H)), follow_down, -3.0, tags=["fillet", "blend"])
    )
    follow_front = fillet(box(W, D + 3, H, 0, -3, 0), lambda m: abs(m[1] + 3) < 1e-6 and abs(m[2] - H) < 1e-6, 5)
    cases.append(
        Case("m_fillet_adj_front_out3", "move", s, pick(s, plane_at(1, 0.0)), follow_front, 3.0, tags=["fillet", "blend"])
    )
    bottom_down = fillet(box(W, D, H + 3, 0, 0, -3), _top_front, 5)
    cases.append(
        Case("m_fillet_nonadj_bottom3", "move", s, pick(s, plane_at(2, 0.0)), bottom_down, 3.0, note="丸めに接しない面", tags=["fillet"])
    )

    s = fillet(base, lambda m: True, 3)
    follow_all = fillet(box(W, D, H + 2), lambda m: True, 3)
    cases.append(
        Case("m_fillet_all12_top_up2", "move", s, pick(s, plane_at(2, H)), follow_all, 2.0, note="全辺丸めの上面", tags=["fillet", "blend"])
    )

    lblock = fuse(box(40, 30, 10), box(20, 30, 10, 0, 0, 10))
    riser = pick(lblock, plane_at(0, 20.0))
    cases.append(
        Case("m_step_riser_out10", "move", lblock, riser, fuse(box(40, 30, 10), box(30, 30, 10, 0, 0, 10)), 10.0, tags=["topology"])
    )
    cases.append(
        Case("m_step_riser_to_end", "move", lblock, riser, None, 20.0, note="踏み面が消える", tags=["topology", "fail"])
    )
    cases.append(
        Case("m_step_riser_beyond", "move", lblock, riser, None, 25.0, note="端を越える", tags=["topology", "fail"])
    )

    s = fuse(base, cylinder(5, 8, (W / 2, D / 2, H)))
    cases.append(
        Case("m_boss_grow1", "move", s, pick(s, curved), fuse(base, cylinder(6, 8, (W / 2, D / 2, H))), 1.0, tags=["boss"])
    )

    two = pick(base, plane_at(2, H)) + pick(base, plane_at(1, 0.0))
    cases.append(Case("m_box_two_faces", "move", base, two, box(W, D + 3, H + 3, 0, -3, 0), 3.0, note="2面を同時に", tags=["multi"]))

    for name in ("m_hole_shrink1", "m_wedge_top_up4", "m_box_top_up5", "m_fillet_adj_top_up3"):
        original = next(c for c in cases if c.name == name)
        cases.append(_via_step(original))
    return cases


def _compound(shapes_):
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    for item in shapes_:
        builder.Add(compound, item)
    return compound


def _near_hole(m):
    i = round((m[0] - 10) / 20)
    j = round((m[1] - 10) / 20)
    return math.hypot(m[0] - (10 + 20 * i), m[1] - (10 + 20 * j)) < 3.5


def _inside_xy(c, x0, x1, y0, y1):
    return x0 - 1e-6 <= c[0] <= x1 + 1e-6 and y0 - 1e-6 <= c[1] <= y1 + 1e-6


def _axis_of(face):
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    d = BRepAdaptor_Surface(face).Cylinder().Axis().Direction()
    v = [abs(d.X()), abs(d.Y()), abs(d.Z())]
    return v.index(max(v))


def _via_step(case: Case) -> Case:
    """同じケースを STEP 経由の形で作り直す。面は重心と面の種類で選び直す。"""
    shape = step_roundtrip(case.shape)
    wanted = [(surface_type(f), face_centroid(f)) for f in case.faces]
    chosen = []
    for kind, c in wanted:
        match = [
            f for f in faces_of(shape)
            if surface_type(f) == kind and max(abs(a - b) for a, b in zip(face_centroid(f), c)) < 1e-4
        ]
        assert len(match) == 1, (case.name, kind, c, len(match))
        chosen.append(match[0])
    return Case(
        case.name.replace("_", "_step_", 1),
        case.kind,
        shape,
        chosen,
        case.expected,
        case.distance,
        note=("STEP経由 " + case.note).strip(),
        tags=case.tags + ["step"],
    )


def all_cases() -> List[Case]:
    return delete_cases() + move_cases()
