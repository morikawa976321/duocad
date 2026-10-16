"""面単位のテセレーションのキャッシュ(`view._FaceTessellations`)。カメラが動いて細かく測る面が変わっても、測り直すのは
測り方の変わった面だけ・同じ測り方へ戻れば測らない。issue #166。"""
from __future__ import annotations

import pytest

from duocad import kernel
from duocad.server import Session

DEVIATION = 0.2
SIDE = 20.0
RADIUS = 10.0


@pytest.fixture()
def counted(monkeypatch):
    """`kernel.tessellate_faces` が測った面の添字(呼び出しごと)。"""
    calls: list = []
    real = kernel.tessellate_faces

    def counting(shape, groups):
        calls.append(sorted(index for indices in groups.values() for index in indices))
        return real(shape, groups)

    monkeypatch.setattr(kernel, "tessellate_faces", counting)
    return calls


@pytest.fixture()
def boxed() -> tuple:
    """円柱(曲面1・平面2)と直方体(平面6)を持つセッション。"""
    session = Session(None, DEVIATION)
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sketch_id, [0.0, 0.0], 2 * RADIUS)
    session.add_rectangle(sketch_id, [40.0, -10.0], 30.0, 20.0)
    session.modify_selection([{"sketch": sketch_id, "index": 1}, {"sketch": sketch_id, "index": 2}], [])
    session.extrude(SIDE, mode="new")
    session.clear_selection()
    return session, session.document.bodies[-1].id


def test_only_faces_whose_recipe_changed_are_measured(boxed, counted) -> None:
    """落ちたら、カメラを回して細かく測る面が1つ入れ替わっただけでボディを丸ごと測り直す(PC ケースで 1 回 1.3 秒)。"""
    session, body = boxed
    total = session.geometry(body)["face_count"]
    session.set_view_resolution(0.02, 400.0, {body: [1, 2]})
    session.geometry(body)
    counted.clear()
    session.set_view_resolution(0.02, 400.0, {body: [1, 3]})
    after = session.geometry(body)
    assert counted == [[2, 3]], "測り方の変わった面(2: 細→粗、3: 粗→細)だけを測る"
    assert after["fine_face_count"] == 2 and after["face_count"] == total


def test_zooming_back_measures_nothing(boxed, counted) -> None:
    """落ちたら、ズームの行き来(寄って元へ戻る)のたびに全面を測り直す(PC ケースで 1 回 1.4 秒)。"""
    session, body = boxed
    session.set_view_resolution(0.5, 400.0, None)
    first = session.geometry(body)
    session.set_view_resolution(0.01, 400.0, None)
    session.geometry(body)
    counted.clear()
    session.set_view_resolution(0.5, 400.0, None)
    back = session.geometry(body)
    assert counted == [], "同じ測り方へ戻ったのに測り直した"
    assert [face["positions"] for face in back["faces"]] == [face["positions"] for face in first["faces"]]


def test_the_first_measurement_matches_the_selective_tessellation(boxed) -> None:
    """落ちたら、面ごとに組み直した配りものが、ボディを一度に測った従来の配りものと食い違う(形・粗さ・印)。"""
    session, body = boxed
    # 最後の2つは引ききった姿(粗い側が細かい側と同じ。全面を細かく測るが、報告は求められた面のまま)
    for world_per_pixel, focus in ((0.02, None), (0.02, {body: [1]}), (0.02, {body: []}), (50.0, {body: [1]}), (50.0, {body: []})):
        fresh = Session(None, DEVIATION)
        fresh.document = session.document
        fresh.view.reset(session.document)
        fresh.set_view_resolution(world_per_pixel, 400.0, focus)
        ours = fresh.geometry(body)
        args = fresh.view._tessellation_args(body)
        theirs = kernel.tessellate_selective(session.document.get_body(body).shape, *args)
        for key in ("deviation", "coarse_deviation", "fine_face_count", "face_count", "closed", "untriangulated", "bbox"):
            assert ours[key] == theirs[key], key
        assert [(f["index"], f["detail"], f["positions"], f["indices"]) for f in ours["faces"]] == [
            (f["index"], f["detail"], f["positions"], f["indices"]) for f in theirs["faces"]
        ]


def test_a_new_shape_starts_a_new_cache(boxed, counted) -> None:
    """落ちたら、フィレット・undo で形が変わっても前の形の面の三角形を配る(添字が同じ別の面になる)。"""
    session, body = boxed
    session.geometry(body)
    counted.clear()
    session.modify_selection([{"body": body, "kind": "edge", "index": 1}], [])
    session.fillet(2.0)
    measured = session.geometry(body)
    assert counted and len(counted[-1]) == measured["face_count"]


def test_undo_reuses_the_faces_of_the_shape_it_returns_to(boxed, counted) -> None:
    """落ちたら、undo で元の形へ戻るたびに全面を測り直す(PC ケースの最大ボディで 0.14 秒)。"""
    session, body = boxed
    session.geometry(body)
    session.modify_selection([{"body": body, "kind": "edge", "index": 1}], [])
    session.fillet(2.0)
    session.geometry(body)
    counted.clear()
    session.undo()
    session.geometry(body)
    assert counted == []


def test_the_commit_reuses_what_the_preview_measured(boxed, counted) -> None:
    """落ちたら、プレビューで全面を測った確定後の形を、確定の配信でもう一度測る(PC ケースの最大ボディで 0.15 秒)。"""
    session, body = boxed
    session.geometry(body)
    session.modify_selection([{"body": body, "kind": "edge", "index": 1}], [])
    job = session.preview_job("fillet", {"radius": 2.0})
    preview = job.run()
    job.adopt()
    session.fillet(2.0)
    counted.clear()
    committed = session.geometry(body)
    assert counted == []
    assert [face["positions"] for face in committed["faces"]] == [
        face["positions"] for face in preview["bodies"][0]["geometry"]["faces"]
    ]


def test_a_camera_that_differs_in_the_last_digit_measures_nothing(boxed, counted) -> None:
    """落ちたら、カメラを戻しても画素の長さの最後の桁が違うだけで偏差が変わり、全面を測り直す(PC ケースで毎回 1.2 秒)。"""
    session, body = boxed
    session.set_view_resolution(1.1022631871338904, 400.0, {body: [1, 2]})
    session.geometry(body)
    counted.clear()
    session.set_view_resolution(1.102263187133815, 400.0, {body: [1, 2]})
    session.geometry(body)
    assert counted == []


def _bowtie_beside_a_box(bowtie_first: bool):
    """辺が交差する面(三角形分割が取れない)と立方体の compound。`bowtie_first` が偽なら交差する面は7番目。"""
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.TopoDS import TopoDS_Compound
    from OCP.gp import gp_Pnt

    polygon = BRepBuilderAPI_MakePolygon()
    for x, y in ((0, 0), (1, 1), (1, 0), (0, 1)):  # 辺が交差する蝶ネクタイ
        polygon.Add(gp_Pnt(x, y, 0))
    polygon.Close()
    bowtie = BRepBuilderAPI_MakeFace(polygon.Wire(), True).Face()
    box = BRepPrimAPI_MakeBox(gp_Pnt(3, 0, 0), 5, 5, 5).Shape()
    builder, shape = BRep_Builder(), TopoDS_Compound()
    builder.MakeCompound(shape)
    for part in ((bowtie, box) if bowtie_first else (box, bowtie)):
        builder.Add(shape, part)
    return shape


def test_faces_seeded_by_the_preview_still_warn_once_when_delivered() -> None:
    """落ちたら、プレビューで測った形を確定したとき、取れなかった面・解消できなかった境界の警告が一度も出ない
    (プレビューは警告を積まず、確定の配信は覚えた三角形を使うので測らない)。"""
    from duocad.document import Body

    shape = _bowtie_beside_a_box(bowtie_first=True)
    session = Session(None, DEVIATION)
    session.document.add_body(Body(id="odd", name="odd", kind="solid", shape=shape))
    measured = kernel.tessellate(shape, session.view.view_deviation, session.view.view_angular_deflection)
    assert measured["untriangulated"], "題材の前提(面が取れない)が崩れた"
    session.view.seed_faces("odd", shape, session.view.view_deviation, session.view.view_angular_deflection, measured)
    session.take_warnings()
    session.geometry("odd")
    assert any("三角形分割が取れなかった面" in line for line in session.take_warnings())
    session.view._geometry_cache.clear()
    session.geometry("odd")
    assert not any("三角形分割が取れなかった面" in line for line in session.take_warnings()), "警告は1回だけ"


def test_boundary_diagnostics_name_the_faces_of_the_body() -> None:
    """落ちたら、一部の面だけを測ったときの「境界の交差を解消できなかった面」の警告が、測った面を集めた compound の中の
    番号(別の面)を出す。"""
    shape = _bowtie_beside_a_box(bowtie_first=False)
    recipe = (DEVIATION, kernel.DEFAULT_ANGULAR_DEFLECTION)
    whole = kernel.tessellate_faces(shape, {recipe: range(1, 8)})
    assert [entry["face"] for entry in whole["boundary"]["unresolved"]] == [7], "題材の前提(7番目の面が交差する)が崩れた"
    part = kernel.tessellate_faces(shape, {recipe: [2, 7]})
    assert [entry["face"] for entry in part["boundary"]["unresolved"]] == [7]
    split = kernel.tessellate_selective(shape, [2, 7], DEVIATION, recipe[1], coarse_deviation=1.0)
    assert [entry["face"] for entry in split["boundary"]["unresolved"]] == [7]


def test_a_deep_zoom_measures_the_fine_faces_in_one_go(boxed, counted) -> None:
    """落ちたら、偏差が下限に張り付く深いズームで、別々の回に測った細かい面の境目に画素より大きな隙間が残る
    (境目の分割は一緒に測る面に依存し、隙間は偏差の2倍程度。再現: 1画素 1e-6 mm で 40 画素)。"""
    session, body = boxed
    deep = 1.0e-6
    session.set_view_resolution(deep, 400.0, {body: [1]})
    session.geometry(body)
    counted.clear()
    session.set_view_resolution(deep, 400.0, {body: [1, 2]})
    ours = session.geometry(body)
    assert counted == [[1, 2]], "細かい面の組を1回で測り直す"
    args = session.view._tessellation_args(body)
    theirs = kernel.tessellate_selective(session.document.get_body(body).shape, *args)
    fine = lambda result: [(f["index"], f["positions"], f["indices"]) for f in result["faces"] if f["detail"] == "fine"]
    assert fine(ours) == fine(theirs)
    counted.clear()
    session.set_view_resolution(0.02, 400.0, {body: [1]})
    session.geometry(body)
    session.set_view_resolution(0.02, 400.0, {body: [1, 2]})
    session.geometry(body)
    assert counted[-1] == [2], "画素から偏差が決まっている間は、足りない面だけを測る"
