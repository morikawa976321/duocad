"""フィレットの契約。半径によって成功/失敗/「成功だが歪」に分岐する境目が壊れていないか。

期待値の出どころ(20mm立方体・全辺フィレットの実測):

**球が `other` に落ちること自体が契約**(`brep_geometry.classify_face` は球を独立の種別に持たない)。
"""
from __future__ import annotations

import subprocess
import sys

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import kernel, kernel_chains, kernel_shell, query
from duocad.paths import REPO_ROOT
from duocad.document import Document
from duocad.verbs import fillet
from document_support import edges_of_kind, kinds_of, new_box, new_box_with_hole

from duocad import (
    brep_topology as _brep_topology,
)

#: 20mm立方体・全辺フィレットの実測（上のdocstring参照）
CUBE_MM = 20.0
RADIUS_OK = 3.0
RADIUS_FAILS_LOW = 10.0
RADIUS_ODD_BUT_OK = 10.5

FILLET_R3_KINDS = {"plane": 6, "cylinder_partial": 12, "other": 8}
#: 半径を 1.0 + 0.25*i でバラバラにしたときの実測
VARIED_RADII_KINDS = {"plane": 6, "cylinder_partial": 12, "bspline": 8}
HOLE_FILLET_KINDS = {"plane": 6, "cylinder_full": 1, "torus": 2}


def test_fillet_on_a_box_makes_cylinders_spheres_and_planes():
    """全辺・同一半径なら**円筒 + 球 + 平面**になり、B-splineは出ない(球は `other`)。"""
    doc = Document()
    body_id = new_box(doc)
    all_edges = [edge["index"] for edge in query.describe_edges(doc.get_body(body_id).shape)]
    assert len(all_edges) == 12, "立方体の辺は12本（重複除去済み）"

    fillet(doc, all_edges, RADIUS_OK, body_id=body_id)

    assert kinds_of(doc, body_id) == FILLET_R3_KINDS
    assert "bspline" not in kinds_of(doc, body_id), "同一半径でsplineが出たら副産物の理解が違う"


def test_fillet_on_the_vertical_edges_only():
    """縦辺だけのフィレットでは角の球ができない(実測: 部分円筒4枚+平面6枚)。"""
    doc = Document()
    body_id = new_box(doc)
    # **端点で選ぶ**（添字の決め打ちにしない）。`describe_edges` が端点を返すのはこのため。
    vertical = [
        edge["index"]
        for edge in query.describe_edges(doc.get_body(body_id).shape)
        if edge["kind"] == "line" and edge["start"][2] != edge["end"][2]
    ]
    assert len(vertical) == 4, "立方体の縦辺は4本"

    fillet(doc, vertical, RADIUS_OK, body_id=body_id)

    kinds = kinds_of(doc, body_id)
    assert kinds.get("cylinder_partial") == 4
    assert kinds.get("plane") == 6
    assert kinds.get("other", 0) == 0, "縦辺だけなら角の球は出ない"


def test_varied_radii_produce_bspline_faces():
    """**半径をバラバラにすると B-spline が出る**(splineはフィレットの副産物であり異常ではない)。"""
    doc = Document()
    body_id = new_box(doc)
    all_edges = [edge["index"] for edge in query.describe_edges(doc.get_body(body_id).shape)]
    radii = [1.0 + index * 0.25 for index in range(len(all_edges))]

    fillet(doc, all_edges, radii, body_id=body_id)

    # **実測どおり厳密に**（`bspline > 0` だけだと面構成を取り違えても緑になる）
    assert kinds_of(doc, body_id) == VARIED_RADII_KINDS


def test_radius_list_must_match_the_edges():
    """半径のリストが辺の数と合わなければ ValueError（黙って足りない分を補わない）。"""
    doc = Document()
    body_id = new_box(doc)
    with pytest.raises(ValueError):
        fillet(doc, [1, 2, 3], [1.0, 2.0], body_id=body_id)


def test_fillet_on_a_circular_edge_produces_torus():
    """曲がった辺(穴の縁)を丸めると**トーラス**になる —— **辺の種別が結果の面の種別を決める**。"""
    doc = Document()
    body_id = new_box_with_hole(doc)
    assert kinds_of(doc, body_id) == {"plane": 6, "cylinder_full": 1}

    circular = edges_of_kind(doc, body_id, "circle")
    assert len(circular) == 2, "貫通穴の円い辺は上下の2本"

    fillet(doc, circular, 1.5, body_id=body_id)

    assert kinds_of(doc, body_id) == HOLE_FILLET_KINDS


@pytest.mark.parametrize("radius", [RADIUS_FAILS_LOW])
def test_a_failing_fillet_raises_and_leaves_nothing_behind(radius: float):
    """`IsDone()=False` を例外にし、文書もログも変えない。"""
    doc = Document()
    body_id = new_box(doc)
    shape_before = doc.get_body(body_id).shape
    kinds_before = kinds_of(doc, body_id)
    log_before = len(doc.log.entries)
    all_edges = [edge["index"] for edge in query.describe_edges(shape_before)]

    # 例外の型まで縛る: `Exception` で受けると、`IsDone()` を一度も見ない実装が
    # 引数検査の TypeError で落ちても緑になり、この検査が空になる。
    with pytest.raises(ValueError):
        fillet(doc, all_edges, radius, body_id=body_id)

    assert doc.get_body(body_id).shape is shape_before, "失敗したのに形状が置き換わった"
    assert kinds_of(doc, body_id) == kinds_before
    assert len(doc.log.entries) == log_before, "失敗したのにログへ積まれた"


def test_radius_10_5_is_reported_as_done_but_is_actually_broken():
    """r=10.5は`IsDone()`は真だが`BRepCheck.IsValid=False`(実測) —— 立方体の半辺(10mm)を
    わずかに超えてフィレット同士が干渉し始める境界値。duocadはこの形状を文書に入れない。
    """
    doc = Document()
    body_id = new_box(doc)
    shape_before = doc.get_body(body_id).shape
    log_before = len(doc.log.entries)
    all_edges = [edge["index"] for edge in query.describe_edges(shape_before)]

    with pytest.raises(ValueError) as error:
        fillet(doc, all_edges, RADIUS_ODD_BUT_OK, body_id=body_id)

    assert "BRepCheck" in str(error.value), (
        "r=10.5 は IsDone では止まらない。止めているのが BRepCheck であること自体が契約"
    )
    assert doc.get_body(body_id).shape is shape_before
    assert len(doc.log.entries) == log_before


def test_fillet_is_logged_and_undoable():
    """フィレットは操作ログに1行積まれ、undoで元の形状へ戻る。"""
    doc = Document()
    body_id = new_box(doc)
    kinds_before = kinds_of(doc, body_id)
    volume_before = query.volume(doc.get_body(body_id).shape)
    all_edges = [edge["index"] for edge in query.describe_edges(doc.get_body(body_id).shape)]

    operation = fillet(doc, all_edges, RADIUS_OK, body_id=body_id)

    assert operation.verb == "fillet"
    assert operation.args["edges"] == all_edges
    assert operation.args["radius"] == RADIUS_OK
    assert operation.args["body_id"] == body_id
    assert doc.log.entries[-1] is operation

    doc.log.undo(doc)
    assert kinds_of(doc, body_id) == kinds_before
    assert query.volume(doc.get_body(body_id).shape) == pytest.approx(volume_before)

    doc.log.redo(doc)
    assert kinds_of(doc, body_id) == FILLET_R3_KINDS


def test_fillet_shifts_face_indices():
    """同じ面でも添字は形状変更で変わりうる(実測: 20mm立方体の1辺にr=2で2→7/3→6/6→2)。
    穴の径変更・追加・ポケット・面取りでは動かなかった —— 散発的に静かに間違う。
    """
    doc = Document()
    body_id = new_box(doc)
    before = query.describe_shape(doc.get_body(body_id).shape)
    assert before["face_count"] == 6
    all_edges = [edge["index"] for edge in query.describe_edges(doc.get_body(body_id).shape)]

    operation = fillet(doc, all_edges[:1], 2.0, body_id=body_id)

    face_map = operation.provenance["face_map"]
    moved = [
        (int(old), new)
        for old, new in face_map.items()
        if new and int(old) != new[0]
    ]
    assert moved, "1面も添字が動かないなら、この実測（および§7の前提）と食い違う"


def test_fillet_rejects_an_unknown_edge_index():
    """範囲外の辺の添字は ValueError（黙って無視して「成功」にしない）。"""
    doc = Document()
    body_id = new_box(doc)
    log_before = len(doc.log.entries)
    with pytest.raises(ValueError):
        fillet(doc, [999], 1.0, body_id=body_id)
    assert len(doc.log.entries) == log_before


def test_fillet_needs_at_least_one_edge():
    """辺を1本も選ばずに呼んだら ValueError（何も起きないのを成功にしない）。"""
    doc = Document()
    body_id = new_box(doc)
    with pytest.raises(ValueError):
        fillet(doc, [], 1.0, body_id=body_id)


def test_chamfer_and_fillet_compose_on_the_same_body():
    """同じボディに面取りとフィレットを順にかけられること。"""
    from duocad.verbs import chamfer

    doc = Document()
    body_id = new_box_with_hole(doc)
    circular = edges_of_kind(doc, body_id, "circle")
    assert len(circular) == 2

    chamfer(doc, circular[:1], 1.0, body_id=body_id)
    assert kinds_of(doc, body_id) == {"plane": 6, "cylinder_full": 1, "cone": 1}

    # 面取りで辺の添字は変わりうるので、**選び直す**（生の添字を持ち回らない。§7）
    remaining = edges_of_kind(doc, body_id, "circle")
    fillet(doc, remaining[-1:], 1.0, body_id=body_id)

    assert kinds_of(doc, body_id) == {
        "plane": 6,
        "cylinder_full": 1,
        "cone": 1,
        "torus": 1,
    }
    doc.log.undo(doc)
    assert kinds_of(doc, body_id) == {"plane": 6, "cylinder_full": 1, "cone": 1}


#: 壁厚。`_open_box` の縁(厚さ `WALL` の環状の面)を、半径 `WALL` のフィレットが過不足なく食い尽くす。
WALL = 2.0


def _open_box():
    """`BRepPrimAPI_MakeBox` の箱の上面を厚さ `WALL` で抜いた形(押し出しで作った箱では `IsDone()=False` で済み、落ちない)。"""
    shape = BRepPrimAPI_MakeBox(30.0, 30.0, 40.0).Shape()
    top = [face["index"] for face in query.describe_faces(shape) if face["normal"][2] > 0.9]
    return kernel_shell.shell_solid(shape, top, WALL)["shape"]


def _edges_where(shape, keep):
    return [edge for edge in query.describe_edges(shape) if keep(edge)]


def _is_rim(edge) -> bool:
    return abs(edge["start"][2] - 40.0) < 1e-6 and abs(edge["end"][2] - 40.0) < 1e-6


def test_the_guard_finds_a_fillet_radius_equal_to_the_rim_width():
    """縁(幅 `WALL`)の辺を半径 `WALL` で丸めると面を食い尽くす。落ちたら、その半径で OCCT の `Build()` がプロセスごと落ちる
    (`test_a_fillet_that_consumes_a_face_exactly_is_refused_without_killing_the_process`)。"""
    shape = _open_box()
    edge_map = _brep_topology._edge_map(shape)
    rim = [edge["index"] for edge in _edges_where(shape, _is_rim)]
    assert len(rim) == 8

    found = kernel_chains.fillet_consumes_face(shape, edge_map, {index: WALL for index in rim})

    assert found is not None and found[0] in rim and found[1] in rim


@pytest.mark.parametrize("radius", [0.25 * WALL, 0.999 * WALL, 1.001 * WALL, 1.5 * WALL])
def test_the_guard_leaves_every_other_radius_to_occt(radius):
    """半径が縁の幅と 1e-7 mm より離れていれば止めない(止めるのは落ちる半径だけ。ほかは従来どおり OCCT の判定)。"""
    shape = _open_box()
    edge_map = _brep_topology._edge_map(shape)
    rim = [edge["index"] for edge in _edges_where(shape, _is_rim)]

    assert kernel_chains.fillet_consumes_face(shape, edge_map, {index: radius for index in rim}) is None


def test_the_guard_does_not_stop_edges_whose_faces_are_wider_than_the_radius():
    """縦辺(隣の面は幅 30)・底の辺は半径 `WALL` で止めない。落ちたら、実測で通る丸めまで断っている。"""
    shape = _open_box()
    edge_map = _brep_topology._edge_map(shape)
    vertical = [edge["index"] for edge in _edges_where(shape, lambda e: e["kind"] == "line" and e["start"][2] != e["end"][2])]
    bottom = [edge["index"] for edge in _edges_where(shape, lambda e: abs(e["start"][2]) < 1e-6 and abs(e["end"][2]) < 1e-6)]
    assert vertical and bottom

    assert kernel_chains.fillet_consumes_face(shape, edge_map, {index: WALL for index in vertical + bottom}) is None
    kernel.fillet_edges(shape, {index: WALL for index in vertical})


def _parallelogram_prism():
    """XY 平面の平行四辺形 (0,0)(10,0)(13,4)(3,4) を高さ 30 で押し出した柱(押し出しで作るので落ちない側の作り方)。"""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Pnt, gp_Vec

    polygon = BRepBuilderAPI_MakePolygon()
    for x, y in [(0.0, 0.0), (10.0, 0.0), (13.0, 4.0), (3.0, 4.0)]:
        polygon.Add(gp_Pnt(x, y, 0.0))
    polygon.Close()
    face = BRepBuilderAPI_MakeFace(polygon.Wire()).Face()
    return BRepPrimAPI_MakePrism(face, gp_Vec(0.0, 0.0, 30.0)).Shape()


def _vertical_edge_at(shape, x: float, y: float) -> int:
    (found,) = [
        edge["index"]
        for edge in query.describe_edges(shape)
        if edge["kind"] == "line"
        and edge["start"][2] != edge["end"][2]
        and abs(edge["start"][0] - x) < 1e-6
        and abs(edge["start"][1] - y) < 1e-6
    ]
    return found


def test_the_guard_does_not_stop_a_fillet_on_an_obtuse_edge_whose_radius_equals_the_face_width():
    """鈍角の辺(平行四辺形の(10,0)の縦辺、内角 126.9 度)は、半径 5 が隣の斜めの面の幅 5 と一致しても、面の中へ使う幅は
    `5·tan(φ/2)` = 2.5 で、面は食い尽くされない(OCCT は体積 1172.7 の正常な形を作る)。落ちたら、角度を見ずに半径だけで
    比べていて、正しく丸められる形を断っている。"""
    shape = _parallelogram_prism()
    edge_map = _brep_topology._edge_map(shape)
    index = _vertical_edge_at(shape, 10.0, 0.0)

    assert kernel_chains.fillet_consumes_face(shape, edge_map, {index: 5.0}) is None
    assert kernel.fillet_edges(shape, {index: 5.0})["shape"] is not None


def _circle_edge(shape, radius: float, z: float) -> int:
    import math

    (found,) = [
        edge["index"]
        for edge in query.describe_edges(shape)
        if edge["kind"] == "circle" and abs(edge["length"] - 2 * math.pi * radius) < 1e-6 and abs(edge["start"][2] - z) < 1e-6
    ]
    return found


def test_the_guard_keeps_the_radius_as_the_width_next_to_a_curved_face():
    """隣が曲面の辺は、面の中へ使う幅を半径とみなす(安全側)。円錐台に穴を開けた上面の外周(環の幅 5)を R=5 で丸めるのは
    OCCT が通すが、幅が半径とちょうど一致するので断る。落ちたら、曲面の隣にも平面どうしの角度補正を当てはめている
    (球や斜めの切断面では補正が曲率を含まず、正当な丸めまで断る: 下の2つのテスト)。"""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCone, BRepPrimAPI_MakeCylinder

    shape = BRepAlgoAPI_Cut(
        BRepPrimAPI_MakeCone(20.0, 10.0, 20.0).Shape(), BRepPrimAPI_MakeCylinder(5.0, 20.0).Shape()
    ).Shape()
    edge_map = _brep_topology._edge_map(shape)
    outer = _circle_edge(shape, 10.0, 20.0)

    assert kernel_chains.fillet_consumes_face(shape, edge_map, {outer: 5.0}) is not None
    assert kernel_chains.fillet_consumes_face(shape, edge_map, {outer: 4.0}) is None


def test_the_guard_does_not_stop_a_fillet_on_the_rim_of_a_spherical_dent():
    """球を抜いた円柱の上面の環(幅 4)の内周を R=2 で丸める。隣は球面で、辺の1点の法線角から `R·tan(φ/2)` を求めると
    幅 4 と偶然一致するが、OCCT は `IsDone()=True` で正常な形(体積 925.09)を作る。落ちたら、曲面の隣に角度補正を当てている。"""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakeSphere

    shape = BRepAlgoAPI_Cut(BRepPrimAPI_MakeCylinder(12.0, 6.0).Shape(), BRepPrimAPI_MakeSphere(10.0).Shape()).Shape()
    edge_map = _brep_topology._edge_map(shape)
    rim = _circle_edge(shape, 8.0, 6.0)

    assert kernel_chains.fillet_consumes_face(shape, edge_map, {rim: 2.0}) is None
    assert kernel.fillet_edges(shape, {rim: 2.0})["shape"] is not None


def test_the_guard_does_not_stop_a_fillet_on_a_tube_cut_at_a_slant():
    """外半径 10・内半径 8・高さ 30 の円管を、斜め 25 度の平面で切った切断面の外周を R=1.2741405216149864 で丸める。中点の
    面幅は約 2.2 だが、中点の法線で補正した幅が内周までの最短距離 2 と偶然一致する。OCCT は `IsDone()=True`。
    落ちたら、辺に沿って角度が変わる曲面の隣に、中点1点の補正を当てている。"""
    import math

    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
    from OCP.gp import gp_Dir, gp_Pln, gp_Pnt, gp_Vec

    tube = BRepAlgoAPI_Cut(BRepPrimAPI_MakeCylinder(10.0, 30.0).Shape(), BRepPrimAPI_MakeCylinder(8.0, 30.0).Shape()).Shape()
    # 平面 z = 15 - x·tan(25°) より上を落とす(平面の上側に十分大きな柱を作って引く)
    tilt = math.radians(25.0)
    normal = gp_Dir(math.sin(tilt), 0.0, math.cos(tilt))
    cutter = BRepPrimAPI_MakePrism(
        BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(0.0, 0.0, 15.0), normal), -40.0, 40.0, -40.0, 40.0).Face(),
        gp_Vec(normal.X() * 60.0, normal.Y() * 60.0, normal.Z() * 60.0),
    ).Shape()
    shape = BRepAlgoAPI_Cut(tube, cutter).Shape()
    edge_map = _brep_topology._edge_map(shape)
    (outer,) = [
        edge["index"]
        for edge in query.describe_edges(shape)
        if edge["kind"] == "other" and abs(math.hypot(edge["start"][0], edge["start"][1]) - 10.0) < 1e-6
    ]
    radius = 2.0 / math.tan(math.radians(57.5))

    assert kernel_chains.fillet_consumes_face(shape, edge_map, {outer: radius}) is None
    assert kernel.fillet_edges(shape, {outer: radius})["shape"] is not None


def test_the_guard_still_finds_the_radius_that_consumes_the_slanted_face():
    """同じ辺は、使う幅 `R·tan(φ/2)` が斜めの面の幅 5 と一致する R = 10 で止める(OCCT は `IsDone()=False`)。"""
    shape = _parallelogram_prism()
    edge_map = _brep_topology._edge_map(shape)
    index = _vertical_edge_at(shape, 10.0, 0.0)

    found = kernel_chains.fillet_consumes_face(shape, edge_map, {index: 10.0})

    assert found is not None and found[0] == index


_CONSUME_CHILD = """
import sys
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from duocad import kernel, kernel_shell, query

shape = BRepPrimAPI_MakeBox(30.0, 30.0, 40.0).Shape()
top = [face["index"] for face in query.describe_faces(shape) if face["normal"][2] > 0.9]
shaped = kernel_shell.shell_solid(shape, top, 2.0)["shape"]
edges = query.describe_edges(shaped)
selections = {
    "all": edges,
    "rim": [e for e in edges if abs(e["start"][2] - 40.0) < 1e-6 and abs(e["end"][2] - 40.0) < 1e-6],
    "x": [e for e in edges if abs(e["end"][0] - e["start"][0]) > 1e-6 and abs(e["end"][1] - e["start"][1]) < 1e-6 and abs(e["end"][2] - e["start"][2]) < 1e-6],
}
for name, chosen in selections.items():
    try:
        kernel.fillet_edges(shaped, {e["index"]: 2.0 for e in chosen})
    except ValueError as error:
        print(name, "refused", str(error)[:40])
    else:
        print(name, "built")
print("survived")
"""


def test_a_fillet_that_consumes_a_face_exactly_does_not_kill_the_process():
    """薄肉の箱で半径 = 壁厚のフィレットは、元の形の `MakeFillet` を呼ばずにはみ出しの吸収へ回る(以前は access violation で
    プロセスごと死んだ。別プロセスで試す)。角で連なりが出会う選び方(全辺・縁)は吸収できずに断り、X に平行な辺は丸まる。"""
    done = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", _CONSUME_CHILD],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0, (done.returncode, done.stderr[-800:])
    lines = done.stdout.splitlines()
    assert lines[-1] == "survived"
    assert [line.split()[:2] for line in lines[:-1]] == [["all", "refused"], ["rim", "refused"], ["x", "built"]], done.stdout
