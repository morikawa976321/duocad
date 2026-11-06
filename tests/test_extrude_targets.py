"""押し出し・回転体・ロフトの相手(`duocad.verbs.extrude._place`): `body_id` を省けば、押し出す立体に触れる・重なる、
見えている編集できるソリッドの全部。join は1つに融合し、cut は全部を削る。決まった相手はログに残る。

寸法: 20×20×20 の箱 A(x 0..20)と B(x 30..50)。体積は各 8000。
"""
from __future__ import annotations

import math

import pytest

from duocad import kernel, query
from duocad.document import Document
from duocad.region import region_ref
from duocad.server import Session
from duocad.sketch import add_line, add_rectangle, sketch_create
from duocad.verbs import add_mesh_body, delete, extrude, extrude_faces, revolve
from document_support import assert_matches_replay, replay_through_json, shape_ref
from session_support import select

BOX = 20.0
BOX_VOLUME = BOX**3
TOL = 1e-9


def _box_at(document: Document, x: float) -> str:
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (x, 0.0), BOX, BOX)
    return extrude(document, [region_ref(sketch_id, 1)], BOX, mode="new").args["body_id"]


def _two_boxes() -> tuple[Document, str, str]:
    document = Document()
    return document, _box_at(document, 0.0), _box_at(document, 30.0)


def _rectangle(document: Document, corner: tuple, width: float, height: float) -> str:
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, corner, width, height)
    return sketch_id


def _volume(document: Document, body_id: str) -> float:
    return query.volume(document.get_body(body_id).shape)


def _bridge_cut(document: Document, **options):
    """A と B の両方を貫く 30×5 の溝(各ボディから 10×5×20 = 1000 を削る)。"""
    sketch_id = _rectangle(document, (10.0, 5.0), 30.0, 5.0)
    return extrude(document, [region_ref(sketch_id, 1)], 30.0, mode="cut", **options)


def test_cut_still_finds_the_visible_body_after_the_last_made_body_was_deleted():
    """落ちたら、最後に作ったボディを消した後の切り取りが、見えている箱を前にして
    「cut requires a target body」で断られる(STEP の部品を消した後に実物で起きた)。"""
    document, a, b = _two_boxes()
    delete(document, bodies=[b])
    sketch_id = _rectangle(document, (5.0, 5.0), 5.0, 5.0)

    operation = extrude(document, [region_ref(sketch_id, 1)], 30.0, mode="cut")

    assert operation.args["body_id"] == a and "other_bodies" not in operation.args
    assert math.isclose(_volume(document, a), BOX_VOLUME - 5.0 * 5.0 * BOX, rel_tol=TOL)


def test_cut_carves_every_body_it_reaches_and_records_them():
    """落ちたら、2つの箱を貫く溝が片方しか削らない、または削った相手がログに残らず再生で変わる。"""
    document, a, b = _two_boxes()

    operation = _bridge_cut(document)

    assert operation.args["body_id"] == a and operation.args["other_bodies"] == [b]
    for body_id in (a, b):
        assert math.isclose(_volume(document, body_id), BOX_VOLUME - 1000.0, rel_tol=TOL)
    replayed = replay_through_json(document)
    for body_id in (a, b):
        assert math.isclose(_volume(replayed, body_id), BOX_VOLUME - 1000.0, rel_tol=TOL)

    document.log.undo(document)
    for body_id in (a, b):
        assert math.isclose(_volume(document, body_id), BOX_VOLUME, rel_tol=TOL)
    assert_matches_replay(document)
    document.log.redo(document)
    assert math.isclose(_volume(document, b), BOX_VOLUME - 1000.0, rel_tol=TOL)


def test_join_merges_every_body_it_touches_into_the_first():
    """落ちたら、2つの箱をつなぐ板が片方にしか足されない(もう片方と重なったまま別ボディで残る)、
    または undo で消えたボディが元の並びに戻らない。"""
    document, a, b = _two_boxes()
    sketch_id = _rectangle(document, (15.0, 5.0), 20.0, 5.0)

    operation = extrude(document, [region_ref(sketch_id, 1)], 10.0, mode="join")

    assert operation.args["body_id"] == a and operation.args["other_bodies"] == [b]
    assert [body.id for body in document.bodies] == [a]
    # 板 20×5×10 のうち各箱と重なる 5×5×10 を引く
    assert math.isclose(_volume(document, a), 2 * BOX_VOLUME + 1000.0 - 2 * 250.0, rel_tol=TOL)
    assert [body.id for body in replay_through_json(document).bodies] == [a]

    document.log.undo(document)
    assert [body.id for body in document.bodies] == [a, b]
    assert math.isclose(_volume(document, a), BOX_VOLUME, rel_tol=TOL)
    assert_matches_replay(document)


def test_join_that_touches_nothing_makes_a_new_body():
    """落ちたら、離れた場所の結合が既存のボディへ飛び地として足される(1つのボディが2つの塊になる)。"""
    document, a, b = _two_boxes()
    sketch_id = _rectangle(document, (100.0, 0.0), 5.0, 5.0)

    operation = extrude(document, [region_ref(sketch_id, 1)], 5.0, mode="join")

    created = operation.args["body_id"]
    assert created not in (a, b) and len(document.bodies) == 3
    assert math.isclose(_volume(document, b), BOX_VOLUME, rel_tol=TOL)


def test_join_that_only_touches_a_face_still_joins():
    """落ちたら、面に貼ったスケッチから外へ足す(箱の上面に接するだけの)板が、箱に足されず別のボディになる。"""
    document, a, _b = _two_boxes()
    sketch_id = sketch_create(document, {"kind": "offset", "base": {"kind": "base", "name": "XY"}, "distance": BOX})
    add_rectangle(document, sketch_id, (5.0, 5.0), 5.0, 5.0)

    operation = extrude(document, [region_ref(sketch_id, 1)], 5.0, mode="join")

    assert operation.args["body_id"] == a and "other_bodies" not in operation.args
    assert math.isclose(_volume(document, a), BOX_VOLUME + 125.0, rel_tol=TOL)


def test_cut_buried_inside_a_compound_body_still_finds_it():
    """落ちたら、ブーリアンの結果(1つのソリッドを COMPOUND で包んだ形)の内側に埋まった切り取りが、表面どうしの距離だけで
    「どのボディにも食い込まない」と断られる。"""
    document, a, _b = _two_boxes()
    boss = _rectangle(document, (5.0, 5.0), 3.0, 3.0)
    extrude(document, [region_ref(boss, 1)], 25.0, mode="join")
    assert document.get_body(a).shape.ShapeType().name == "TopAbs_COMPOUND"
    volume = _volume(document, a)
    sketch_id = sketch_create(document, {"kind": "offset", "base": {"kind": "base", "name": "XY"}, "distance": 5.0})
    add_rectangle(document, sketch_id, (12.0, 12.0), 2.0, 2.0)

    operation = extrude(document, [region_ref(sketch_id, 1)], 2.0, mode="cut")

    assert operation.args["body_id"] == a
    assert math.isclose(_volume(document, a), volume - 8.0, rel_tol=TOL)


@pytest.mark.parametrize("reverse", [False, True], ids=["inner_first", "outer_first"])
def test_cut_with_one_column_buried_and_one_outside_finds_the_body(reverse):
    """落ちたら、箱の内側に埋まった柱と箱の外の柱を一度に切ると、選んだ順によって箱が相手から落ちて断られる
    (内外を形全体の先頭の頂点1つだけで見ている)。"""
    document, a, _b = _two_boxes()
    boss = _rectangle(document, (5.0, 5.0), 3.0, 3.0)
    extrude(document, [region_ref(boss, 1)], 25.0, mode="join")
    volume = _volume(document, a)
    sketch_id = sketch_create(document, {"kind": "offset", "base": {"kind": "base", "name": "XY"}, "distance": 5.0})
    add_rectangle(document, sketch_id, (12.0, 12.0), 2.0, 2.0)
    add_rectangle(document, sketch_id, (100.0, 5.0), 2.0, 2.0)
    refs = [region_ref(sketch_id, 1), region_ref(sketch_id, 2)]

    operation = extrude(document, refs[::-1] if reverse else refs, 2.0, mode="cut")

    assert operation.args["body_id"] == a
    assert math.isclose(_volume(document, a), volume - 8.0, rel_tol=TOL)


def test_join_merges_a_body_even_when_the_first_does_not_grow():
    """落ちたら、A の中に埋まった別ボディ B と A の両方の内側だけに足す結合が、A の体積が増えないことを理由に断られる
    (B を A へ融合して消すので文書は変わる)。"""
    document, a, b = _two_boxes()
    inner = _rectangle(document, (5.0, 5.0), 5.0, 5.0)
    nested = extrude(document, [region_ref(inner, 1)], 5.0, mode="new").args["body_id"]
    sketch_id = sketch_create(document, {"kind": "offset", "base": {"kind": "base", "name": "XY"}, "distance": 1.0})
    add_rectangle(document, sketch_id, (6.0, 6.0), 2.0, 2.0)

    operation = extrude(document, [region_ref(sketch_id, 1)], 2.0, mode="join")

    assert operation.args["body_id"] == a and operation.args["other_bodies"] == [nested]
    assert [body.id for body in document.bodies] == [a, b]
    assert math.isclose(_volume(document, a), BOX_VOLUME, rel_tol=TOL)


def test_a_hidden_body_is_left_alone():
    """落ちたら、隠したボディが見えないところで削られる(見えないものは変えない)。"""
    document, a, b = _two_boxes()
    document.get_body(b).visible = False

    operation = _bridge_cut(document)

    assert operation.args["body_id"] == a and "other_bodies" not in operation.args
    assert math.isclose(_volume(document, b), BOX_VOLUME, rel_tol=TOL)


def test_cut_that_reaches_no_visible_body_fails_and_changes_nothing():
    """落ちたら、隠したボディだけを通る・面に触れるだけの切り取りが、何も削らずに成功として通る。"""
    document, a, b = _two_boxes()
    document.get_body(a).visible = False
    sketch_id = _rectangle(document, (5.0, 5.0), 5.0, 5.0)
    entries = len(document.log.entries)

    with pytest.raises(ValueError, match="cut reaches no visible body"):
        extrude(document, [region_ref(sketch_id, 1)], 30.0, mode="cut")
    with pytest.raises(ValueError, match="正の距離は面の外向き"):
        extrude_faces(document, [shape_ref(document, b, "face", 1)], 5.0, mode="cut")

    assert len(document.log.entries) == entries
    assert math.isclose(_volume(document, a), BOX_VOLUME, rel_tol=TOL)


def test_a_given_body_id_limits_the_target_to_that_body():
    """落ちたら、MCP が行き先を指したのに、触れている他のボディまで削られる。"""
    document, a, b = _two_boxes()

    operation = _bridge_cut(document, body_id=b)

    assert operation.args["body_id"] == b and "other_bodies" not in operation.args
    assert math.isclose(_volume(document, a), BOX_VOLUME, rel_tol=TOL)
    assert math.isclose(_volume(document, b), BOX_VOLUME - 1000.0, rel_tol=TOL)


def test_a_face_extrusion_joins_the_other_body_it_runs_into():
    """落ちたら、面を伸ばして隣の箱にめり込ませても、めり込んだ箱が別ボディのまま重なって残る。"""
    document, a, b = _two_boxes()
    right = next(face["index"] for face in kernel.tessellate(document.get_body(a).shape)["faces"] if face["normal"][0] > 0.999)

    operation = extrude_faces(document, [shape_ref(document, a, "face", right)], 15.0, mode="join")

    assert operation.args["body_id"] == a and operation.args["other_bodies"] == [b]
    assert [body.id for body in document.bodies] == [a]
    # A は x 0..35、B は x 30..50 で、重なり 5×20×20 を1回だけ数える
    assert math.isclose(_volume(document, a), 50.0 * BOX * BOX, rel_tol=TOL)


def test_a_body_without_a_solid_is_not_a_target():
    """落ちたら、面だけの部品(STEP の平面などが kind='solid' で入る)が箱の中を通る切り取りの相手に入り、
    「cut left nothing solid」で切り取り全体が止まる。"""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Pln, gp_Pnt, gp_Dir

    from duocad.document import Body

    document, a, _b = _two_boxes()
    sheet = BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(0.0, 0.0, 10.0), gp_Dir(0.0, 0.0, 1.0)), 0.0, 20.0, 0.0, 20.0).Face()
    document.add_body(Body(id="sheet", name="sheet", kind="solid", shape=sheet))
    sketch_id = _rectangle(document, (5.0, 5.0), 5.0, 5.0)

    operation = extrude(document, [region_ref(sketch_id, 1)], 30.0, mode="cut")

    assert operation.args["body_id"] == a and "other_bodies" not in operation.args
    assert document.get_body("sheet").shape is sheet


def test_joining_a_face_of_a_sheet_makes_a_new_body():
    """落ちたら、面だけの部品(相手にならない)の面を結合で押し出すと、新しいボディにならず「join requires a target shape」で断られる。"""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Pln, gp_Pnt, gp_Dir

    from duocad.document import Body

    document = Document()
    sheet = BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(0.0, 0.0, 1.0)), 0.0, 20.0, 0.0, 20.0).Face()
    document.add_body(Body(id="sheet", name="sheet", kind="solid", shape=sheet))

    operation = extrude_faces(document, [shape_ref(document, "sheet", "face", 1)], 5.0, mode="join")

    created = operation.args["body_id"]
    assert created != "sheet" and document.get_body("sheet").shape is sheet
    assert math.isclose(_volume(document, created), 2000.0, rel_tol=TOL)
    document.log.undo(document)
    assert [body.id for body in document.bodies] == ["sheet"]
    document.log.redo(document)
    assert math.isclose(_volume(document, created), 2000.0, rel_tol=TOL)


def test_a_coarse_vertex_far_away_does_not_make_a_body_touch():
    """落ちたら、0.005 mm 離れた箱が、接していない側の頂点1つの許容差(0.01 mm)だけで「触れている」とされ、
    結合で融合されて文書から消える(融合しても2つの塊のまま)。"""
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.TopAbs import TopAbs_VERTEX
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.gp import gp_Pnt
    from OCP.BRep import BRep_Tool

    from duocad.document import Body

    document = Document()
    near = BRepPrimAPI_MakeBox(gp_Pnt(10.005, 0.0, 0.0), 10.0, 10.0, 10.0).Shape()
    explorer = TopExp_Explorer(near, TopAbs_VERTEX)
    while explorer.More():
        vertex = TopoDS.Vertex_s(explorer.Current())
        if BRep_Tool.Pnt_s(vertex).X() > 15.0:
            BRep_Builder().UpdateVertex(vertex, 0.01)
            break
        explorer.Next()
    document.add_body(Body(id="a", name="a", kind="solid", shape=BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()))
    document.add_body(Body(id="near", name="near", kind="solid", shape=near))
    sketch_id = _rectangle(document, (9.0, 2.0), 1.0, 5.0)

    operation = extrude(document, [region_ref(sketch_id, 1)], 15.0, mode="join")  # A(高さ10)の上へ突き出す

    assert operation.args["body_id"] == "a" and "other_bodies" not in operation.args
    assert [body.id for body in document.bodies] == ["a", "near"]


def test_a_mesh_body_is_not_a_target():
    """落ちたら、重なっているメッシュボディを相手にしようとして断られ、ソリッドへの切り取りまで止まる。"""
    document, a, b = _two_boxes()
    add_mesh_body(document, [5.0, 5.0, 5.0, 15.0, 5.0, 5.0, 5.0, 15.0, 5.0, 5.0, 5.0, 15.0], [0, 2, 1, 0, 1, 3, 0, 3, 2, 1, 2, 3])
    sketch_id = _rectangle(document, (5.0, 5.0), 5.0, 5.0)

    operation = extrude(document, [region_ref(sketch_id, 1)], 30.0, mode="cut")

    assert operation.args["body_id"] == a and "other_bodies" not in operation.args


def test_revolve_cuts_every_body_it_reaches():
    """落ちたら、回転体の切り取りが押し出しと違う相手の決め方をしている。"""
    document, a, b = _two_boxes()
    sketch_id = sketch_create(document, "XZ")
    # x 15..24・z 5..10 の長方形を x=25 の縦の線のまわりに回す(半径 1..10 の輪が両方の箱をかすめる)
    add_rectangle(document, sketch_id, (15.0, 5.0), 9.0, 5.0)
    line = add_line(document, sketch_id, (25.0, -10.0), (25.0, 30.0))

    operation = revolve(document, [region_ref(sketch_id, 1)], {"sketch": sketch_id, "entity": line}, 360.0, mode="cut")

    assert sorted([operation.args["body_id"], *operation.args["other_bodies"]]) == sorted([a, b])
    for body_id in (a, b):
        assert _volume(document, body_id) < BOX_VOLUME


def test_the_session_reports_and_previews_every_body():
    """落ちたら、画面のプレビューが削れる2つのボディの片方しか見せない・融合で消えるボディを知らせない(本物が幽霊に
    重なって残る)、または応答の changed_bodies / removed_bodies が実際に変わった・消えたボディと食い違う。"""
    session = Session(0.2)
    document = session.document
    a, b = _box_at(document, 0.0), _box_at(document, 30.0)
    sketch_id = _rectangle(document, (10.0, 5.0), 30.0, 5.0)

    select(session, [{"sketch": sketch_id, "index": 1}])
    preview = session.preview_job("extrude", {"distance": 30.0, "mode": "cut"}).run()
    assert sorted(body["body_id"] for body in preview["bodies"]) == sorted([a, b])
    result = session.apply("extrude", {"distance": 30.0, "mode": "cut"})
    assert sorted(result["changed_bodies"]) == sorted([a, b]) and result["removed_bodies"] == []

    joined = _rectangle(document, (15.0, 12.0), 20.0, 5.0)
    select(session, [{"sketch": joined, "index": 1}])
    preview = session.preview_job("extrude", {"distance": 10.0, "mode": "join"}).run()
    assert [body["body_id"] for body in preview["bodies"]] == [a] and preview["removed_bodies"] == [b]
    assert session.preview("extrude", {"distance": 10.0, "mode": "join"})["removed_bodies"] == [b]
    result = session.apply("extrude", {"distance": 10.0, "mode": "join"})
    assert result["changed_bodies"] == [a] and result["removed_bodies"] == [b]


def test_confirming_a_previewed_cut_reuses_every_body_result(monkeypatch):
    """落ちたら、2つの箱を削るプレビューの後の確定が、2つ目の箱の切り取りをもう一度計算する(大きなボディで確定が遅れる)。"""
    from duocad import kernel_extrude

    session = Session(0.2)
    document = session.document
    _box_at(document, 0.0), _box_at(document, 30.0)
    sketch_id = _rectangle(document, (10.0, 5.0), 30.0, 5.0)
    select(session, [{"sketch": sketch_id, "index": 1}])
    calls = []
    original = kernel_extrude.extrude_regions

    def counted(*args, **kwargs):
        if kwargs.get("target_shape") is not None:
            calls.append(args[2])
        return original(*args, **kwargs)

    monkeypatch.setattr(kernel_extrude, "extrude_regions", counted)
    job = session.preview_job("extrude", {"distance": 30.0, "mode": "cut"})
    job.run()
    job.adopt()
    assert len(calls) == 2
    session.apply("extrude", {"distance": 30.0, "mode": "cut"})
    assert len(calls) == 2


def test_a_positive_cut_from_a_sketch_on_the_top_of_a_body_tells_to_flip_the_sign():
    """落ちたら、領域の切り取りが相手に届かないとき、距離の符号を反対にするという次の一手が文に無い。"""
    document = Document()
    body_id = _box_at(document, 0.0)
    sketch_id = sketch_create(document, {"kind": "offset", "base": {"kind": "base", "name": "XY"}, "distance": BOX})
    add_rectangle(document, sketch_id, (5.0, 5.0), 5.0, 5.0)

    with pytest.raises(ValueError, match="cut reaches no visible body.*負の距離"):
        extrude(document, [region_ref(sketch_id, 1)], 5.0, mode="cut")
    extrude(document, [region_ref(sketch_id, 1)], -5.0, mode="cut")

    assert math.isclose(_volume(document, body_id), BOX_VOLUME - 5.0**3, rel_tol=TOL)
