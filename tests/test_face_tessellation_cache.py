"""面単位のテセレーションのキャッシュ(`view._FaceTessellations`)。カメラが動いて細かく測る面が変わっても、測り直すのは
測り方の変わった面だけ・同じ測り方へ戻れば測らない。issue #166。"""
from __future__ import annotations

import pytest

from duocad import kernel
from duocad.server import Session

from duocad import (
    brep_topology as _brep_topology,
)

DEVIATION = 0.2
SIDE = 20.0
RADIUS = 10.0


@pytest.fixture()
def counted(monkeypatch):
    """`kernel.tessellate_faces` が測った面の添字(呼び出しごと)。"""
    calls: list = []
    real = kernel.tessellate_faces

    def counting(shape, groups, **options):
        calls.append(sorted(index for indices in groups.values() for index in indices))
        return real(shape, groups, **options)

    monkeypatch.setattr(kernel, "tessellate_faces", counting)
    return calls


@pytest.fixture()
def boxed() -> tuple:
    """円柱(曲面1・平面2)と直方体(平面6)を持つセッション。"""
    session = Session(DEVIATION)
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


def test_the_first_measurement_matches_the_grouped_tessellation(boxed) -> None:
    """面のキャッシュから組んだ配信が、同じ組を直接測った形・粗さ・印と一致する。"""
    session, body = boxed
    shape = session.document.get_body(body).shape
    total = _brep_topology.face_count(shape)
    for world_per_pixel, focus in ((0.02, None), (0.02, {body: [1]}), (0.02, {body: []}), (50.0, {body: [1]}), (50.0, {body: []})):
        fresh = Session(DEVIATION)
        fresh.document = session.document
        fresh.view.reset(session.document)
        fresh.set_view_resolution(world_per_pixel, 400.0, focus)
        ours = fresh.geometry(body)
        fine, dev, angle, coarse_dev, coarse_angle = fresh.view._tessellation_args(body)
        fine_set = set(range(1, total + 1)) if fine is None else set(fine)
        coarse = (coarse_dev or dev, coarse_angle or angle)
        groups = {}
        for index in range(1, total + 1):
            groups.setdefault((dev, angle) if index in fine_set else coarse, []).append(index)
        measured = kernel.tessellate_faces(shape, groups)
        assert ours["deviation"] == (dev if fine_set else coarse[0])
        assert ours["coarse_deviation"] == coarse[0]
        assert ours["fine_face_count"] == len(fine_set)
        assert ours["face_count"] == total
        assert ours["closed"] == measured["closed"]
        assert ours["untriangulated"] == measured["untriangulated"]
        assert ours["bbox"] == kernel.bounding_box_of_faces(list(measured["faces"].values()))
        expected = [dict(measured["faces"][index], detail="fine" if index in fine_set else "coarse") for index in range(1, total + 1)]
        assert [{key: value for key, value in face.items() if key != "serial"} for face in ours["faces"]] == expected


def test_a_new_shape_does_not_take_triangles_by_index(boxed, counted) -> None:
    """落ちたら、フィレットで形が変わったとき、同じ添字の別の面に前の形の三角形を配るか、そのまま残った面まで測り直す
    (フィレットは子プロセスで計算して形が往復するので、残った面は子が返す対で前の形の面につなぐ)。"""
    session, body = boxed
    session.geometry(body)
    counted.clear()
    session.modify_selection([{"body": body, "kind": "edge", "index": 1}], [])
    session.fillet(2.0)
    measured = session.geometry(body)
    fresh = kernel.tessellate(session.document.get_body(body).shape, session.view.view_deviation, session.view.view_angular_deflection)
    assert [face["positions"] for face in measured["faces"]] == [face["positions"] for face in fresh["faces"]]
    assert counted and len(counted[-1]) < measured["face_count"], "残った面まで測り直した"


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
    session = Session(DEVIATION)
    session.document.add_body(Body(id="odd", name="odd", kind="solid", shape=shape))
    measured = kernel.tessellate(shape, session.view.view_deviation, session.view.view_angular_deflection)
    assert measured["untriangulated"], "題材の前提(面が取れない)が崩れた"
    session.view.seed_faces("odd", shape, session.view.view_deviation, session.view.view_angular_deflection, measured)
    session.take_warnings()
    session.geometry("odd")
    assert any("表示できない面があります" in line for line in session.take_warnings())
    session.view._geometry_cache.clear()
    session.geometry("odd")
    assert not any("表示できない面があります" in line for line in session.take_warnings()), "警告は1回だけ"


def test_boundary_diagnostics_name_the_faces_of_the_body() -> None:
    """落ちたら、一部の面だけを測ったときの境界の交差の警告が、測った面を集めた compound の中の
    番号(別の面)を出す。"""
    shape = _bowtie_beside_a_box(bowtie_first=False)
    recipe = (DEVIATION, kernel.DEFAULT_ANGULAR_DEFLECTION)
    whole = kernel.tessellate_faces(shape, {recipe: range(1, 8)})
    assert [entry["face"] for entry in whole["boundary"]["unresolved"]] == [7], "題材の前提(7番目の面が交差する)が崩れた"
    part = kernel.tessellate_faces(shape, {recipe: [2, 7]})
    assert [entry["face"] for entry in part["boundary"]["unresolved"]] == [7]
    split = kernel.tessellate_faces(shape, {(1.0, recipe[1]): [1, 3, 4, 5, 6], recipe: [2, 7]})
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
    recipe = (session.view.view_deviation, session.view.view_angular_deflection)
    theirs = kernel.tessellate_faces(session.document.get_body(body).shape, {recipe: [1, 2]})
    assert [{key: value for key, value in face.items() if key != "serial"} for face in ours["faces"] if face["detail"] == "fine"] == [
        dict(theirs["faces"][index], detail="fine") for index in (1, 2)
    ]
    counted.clear()
    session.set_view_resolution(0.02, 400.0, {body: [1]})
    session.geometry(body)
    session.set_view_resolution(0.02, 400.0, {body: [1, 2]})
    session.geometry(body)
    assert counted[-1] == [2], "画素から偏差が決まっている間は、足りない面だけを測る"


def _top_face(session, body) -> int:
    return max((face for face in session.geometry(body)["faces"] if face["kind"] == "plane"), key=lambda face: face["centroid"][2])["index"]


def test_an_operation_measures_only_the_faces_it_changed(boxed, counted) -> None:
    """落ちたら、ボディに足す押し出しのたびに、そのまま残った面までボディを丸ごと測り直す(733 面のボディで 1 回 0.6 秒)。"""
    session, body = boxed
    before = {face["serial"]: face for face in session.geometry(body)["faces"]}
    session.modify_selection([{"body": body, "kind": "face", "index": _top_face(session, body)}], [])
    counted.clear()
    session.extrude(2.0, mode="join")
    after = session.geometry(body)
    kept = [face for face in after["faces"] if face["serial"] in before]
    assert kept, "題材の前提(足しても残る面がある)が崩れた"
    assert sorted(counted[-1]) == sorted(face["index"] for face in after["faces"] if face["serial"] not in before)
    for face in kept:
        assert face["positions"] == before[face["serial"]]["positions"], "同じ serial の面が別の三角形を持っている"
    fresh = kernel.tessellate(session.document.get_body(body).shape, session.view.view_deviation, session.view.view_angular_deflection)
    assert [face["positions"] for face in after["faces"]] == [face["positions"] for face in fresh["faces"]], (
        "借りた面の三角形が、新しい形を測った三角形と違う(添字の取り違え)"
    )


def test_bringing_a_deleted_body_back_measures_nothing(boxed, counted) -> None:
    """落ちたら、ボディを消して undo で戻すたびに、そのボディを丸ごと測り直す(733 面のボディで 1.4 秒)。"""
    session, body = boxed
    session.geometry(body)
    session.modify_selection([{"body": body, "kind": "body", "index": None}], [])
    session.delete()
    session.state_meta()
    counted.clear()
    session.undo()
    session.state_meta()
    session.geometry(body)
    assert counted == []


def test_the_preview_measures_only_the_faces_the_operation_changed(boxed, counted, monkeypatch) -> None:
    """落ちたら、ゴーストを出すたびにボディを丸ごと測る(733 面のボディで 0.4〜0.6 秒、半径を打ち直すたびに)。"""
    session, body = boxed
    session.geometry(body)
    whole: list = []
    real = kernel.tessellate
    monkeypatch.setattr(kernel, "tessellate", lambda *args, **kwargs: whole.append(1) or real(*args, **kwargs))
    session.modify_selection([{"body": body, "kind": "edge", "index": 1}], [])
    counted.clear()
    job = session.preview_job("fillet", {"radius": 2.0})
    preview = job.run()
    geometry = preview["bodies"][0]["geometry"]
    assert not whole, "ボディを丸ごと測った"
    assert counted and len(counted[-1]) < geometry["face_count"]
    job.adopt()
    session.fillet(2.0)
    fresh = real(session.document.get_body(body).shape, session.view.view_deviation, session.view.view_angular_deflection)
    assert [face["positions"] for face in geometry["faces"]] == [face["positions"] for face in fresh["faces"]]


@pytest.mark.parametrize("verb, args", [
    ("move_body", {"dx": 5.0, "dy": -3.0, "dz": 1.0}),
    ("move_body", {"mode": "rotate", "axis_point": [0.0, 0.0, 0.0], "axis_dir": [0.0, 0.0, 1.0], "angle": 30.0}),
    ("move_body", {"dx": 100.0, "dy": 0.0, "dz": 0.0, "copy": True}),
    ("mirror_body", {"base_plane": "YZ"}),
])
def test_moving_copying_or_mirroring_a_body_measures_nothing(boxed, counted, verb, args) -> None:
    """落ちたら、ボディを動かす・複製する・鏡映するたびに丸ごと測り直す(733 面のボディで 0.9 秒)。写した三角形は元の三角形を
    同じ変換で動かしたもの(偏差も面の境目も元のまま)で、鏡映では外向きの巻きを保つ。"""
    import numpy as np

    session, body = boxed
    session.set_view_resolution(0.05, 400.0)  # 見え方が届く前の粗さは外接箱で動く(動かすと外接箱が変わる)ので、固定してから動かす
    before = session.geometry(body)
    session.modify_selection([{"body": body, "kind": "body", "index": None}], [])
    counted.clear()
    session.apply(verb, args)
    moved = session.document.bodies[-1] if verb == "mirror_body" or args.get("copy") else session.document.get_body(body)
    after = session.geometry(moved.id)
    assert counted == []
    assert len(after["faces"]) == len(before["faces"])
    for old, new in zip(before["faces"], after["faces"]):
        assert len(old["positions"]) == len(new["positions"]) and len(old["indices"]) == len(new["indices"])
        assert new["area"] == pytest.approx(old["area"])
        assert new["serial"] != old["serial"], "三角形が変わったのに同じ serial(画面が古い位置の三角形を使う)"
        if old["kind"] == "plane":
            points = np.asarray(new["positions"]).reshape(-1, 3)
            triangles = np.asarray(new["indices"]).reshape(-1, 3)
            normals = np.cross(points[triangles[:, 1]] - points[triangles[:, 0]], points[triangles[:, 2]] - points[triangles[:, 0]])
            assert (normals @ np.asarray(new["normal"]) > 0).all(), "三角形の巻きが面の外向きと逆"
