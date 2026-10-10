"""face単位の選択的高精度化(視錐台+バックフェイスカリング) — 契約。"""
from __future__ import annotations

import pytest

from duocad import kernel, resolution
from duocad.server import Session
from session_support import browser

DEVIATION = 0.2
SIDE = 20.0
RADIUS = 10.0


def test_the_coarse_side_does_not_move_when_the_camera_zooms() -> None:
    """**ズームに依らない**。これが版とキャッシュの据え置きを成立させている。"""
    far = resolution.coarse_view_resolution(1.11, 600.0)
    near = resolution.coarse_view_resolution(0.028, 600.0)
    deeper = resolution.coarse_view_resolution(0.0001, 600.0)
    assert far == near == deeper
    # 大きさが変われば変わってよい(モデルの寸法だけで決まる値である)。
    assert resolution.coarse_view_resolution(0.028, 60.0) != near


def test_the_coarse_side_is_never_finer_than_the_fine_side() -> None:
    """逆転すると「画面に出ていない面の方が細かい」という無意味な状態になる。"""
    for world_per_pixel in (10.0, 1.11, 0.1, 0.028, 1.0e-4):
        fine = resolution.view_resolution(world_per_pixel, 600.0)
        coarse = resolution.coarse_view_resolution(world_per_pixel, 600.0)
        assert coarse[0] >= fine[0]
        assert coarse[1] >= fine[1]


def test_without_a_size_hint_there_is_nothing_to_split() -> None:
    """モデルの大きさが分からなければ粗い側を作れない —— 細かい側と同じ値へ倒す。"""
    for size_hint in (0.0, -1.0, float("nan")):
        assert resolution.coarse_view_resolution(0.01, size_hint) == resolution.view_resolution(
            0.01, size_hint
        )


def test_zoomed_all_the_way_out_the_two_sides_meet() -> None:
    """引ききれば分ける意味が無くなる(細かい側が粗い側に追いつく)。"""
    assert resolution.coarse_view_resolution(50.0, 600.0) == resolution.view_resolution(50.0, 600.0)


@pytest.fixture()
def cylinder_shape():
    """円柱(曲面1・平面2)。**曲面と平面が混ざっている**ので粗さの効きが見える。"""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder

    return BRepPrimAPI_MakeCylinder(RADIUS, SIDE).Shape()


def _triangles(payload: dict, index: int) -> int:
    return len(payload["faces"][index - 1]["indices"]) // 3


def test_asking_for_every_face_is_the_same_as_the_plain_tessellation(cylinder_shape) -> None:
    """`fine_faces=None` は `tessellate()` と同じ測り方。"""
    plain = kernel.tessellate(cylinder_shape, 0.002, 0.05)
    selective = kernel.tessellate_selective(cylinder_shape, None, 0.002, 0.05, 0.4, 0.5)
    assert selective["face_count"] == plain["face_count"]
    for index in range(1, plain["face_count"] + 1):
        assert _triangles(selective, index) == _triangles(plain, index)
    assert selective["fine_face_count"] == plain["face_count"]
    assert all(face["detail"] == "fine" for face in selective["faces"])


def test_a_face_measured_finely_is_identical_whether_or_not_its_neighbours_are(
    cylinder_shape,
) -> None:
    """**面が互いの粗さに引きずられない**。この仕掛けが成立する前提そのもの。"""
    everything = kernel.tessellate_selective(cylinder_shape, None, 0.002, 0.05, 0.4, 0.5)
    nothing = kernel.tessellate_selective(cylinder_shape, [], 0.002, 0.05, 0.4, 0.5)
    mixed = kernel.tessellate_selective(cylinder_shape, [1], 0.002, 0.05, 0.4, 0.5)

    assert _triangles(mixed, 1) == _triangles(everything, 1), "細かい面が粗い隣に引きずられた"
    assert _triangles(mixed, 2) == _triangles(nothing, 2), "粗い面が細かい隣に引きずられた"
    assert _triangles(mixed, 3) == _triangles(nothing, 3)
    assert [face["detail"] for face in mixed["faces"]] == ["fine", "coarse", "coarse"]


def test_leaving_the_curved_face_coarse_actually_saves_triangles(cylinder_shape) -> None:
    """**節約になっていること**を数で見る(分ける仕掛けを入れた目的)。"""
    everything = kernel.tessellate_selective(cylinder_shape, None, 0.002, 0.05, 0.4, 0.5)
    nothing = kernel.tessellate_selective(cylinder_shape, [], 0.002, 0.05, 0.4, 0.5)
    total_fine = sum(len(face["indices"]) // 3 for face in everything["faces"])
    total_coarse = sum(len(face["indices"]) // 3 for face in nothing["faces"])
    assert total_coarse * 4 < total_fine, f"粗くしても減らない: {total_coarse} / {total_fine}"


def test_the_reported_deviation_is_the_finest_one_in_the_payload(cylinder_shape) -> None:
    """`deviation` は「この配りものの中でいちばん細かい値」(画面側の待ち合わせが見る)。"""
    mixed = kernel.tessellate_selective(cylinder_shape, [1], 0.002, 0.05, 0.4, 0.5)
    assert mixed["deviation"] == pytest.approx(0.002)
    assert mixed["coarse_deviation"] == pytest.approx(0.4)
    # 1面も細かくしないなら、いちばん細かい値は粗い側である(嘘をつかない)。
    nothing = kernel.tessellate_selective(cylinder_shape, [], 0.002, 0.05, 0.4, 0.5)
    assert nothing["deviation"] == pytest.approx(0.4)


def test_a_coarse_side_finer_than_the_fine_side_is_pulled_back(cylinder_shape) -> None:
    """粗い側に細かい値を渡しても**細かい側を下回らない**(呼び出し側の取り違えを吸う)。"""
    payload = kernel.tessellate_selective(cylinder_shape, [1], 0.1, 0.3, 0.001, 0.01)
    assert payload["coarse_deviation"] == pytest.approx(0.1)
    plain = kernel.tessellate(cylinder_shape, 0.1, 0.3)
    for index in (1, 2, 3):
        assert _triangles(payload, index) == _triangles(plain, index)


def test_out_of_range_face_indices_are_ignored(cylinder_shape) -> None:
    """存在しない面を指されても落ちない(画面の添字は形状変更で古くなりうる)。"""
    payload = kernel.tessellate_selective(cylinder_shape, [0, 99, 2], 0.002, 0.05, 0.4, 0.5)
    assert payload["fine_face_count"] == 1
    assert [face["detail"] for face in payload["faces"]] == ["coarse", "fine", "coarse"]


def test_a_solid_is_closed_and_a_lone_face_is_not(cylinder_shape) -> None:
    """**開いたシェルでは裏を向いた面が本当に見える** —— 画面側はこの旗で判定を止める。"""
    solid = kernel.tessellate_selective(cylinder_shape, None, 0.1, 0.35)
    assert solid["closed"] is True

    lone_face = kernel._face_map(cylinder_shape).FindKey(1)
    opened = kernel.tessellate_selective(lone_face, None, 0.1, 0.35)
    assert opened["closed"] is False


@pytest.fixture()
def two_body_session() -> tuple:
    """円柱を2つ持つセッション(片方を「画面外」に見立てる)。"""
    session = Session(None, DEVIATION)
    ids = []
    for offset in (0.0, 100.0):
        sketch_id = session.sketch_create("XY")["sketch_id"]
        session.add_circle(sketch_id, [offset, 0.0], RADIUS)
        session.clear_selection()
        session.modify_selection([{"sketch": sketch_id, "index": 1}], [])
        session.extrude(SIDE, mode="new")
        session.clear_selection()
        ids.append(session.document.bodies[-1].id)
    return session, ids[0], ids[1]


def test_a_body_left_entirely_coarse_keeps_its_revision_across_zoom(two_body_session) -> None:
    """**効き所**: 画面外のBodyは寄っても引いても版が上がらない = 何も送られない。"""
    session, visible, hidden = two_body_session
    focus = {visible: None, hidden: []}

    session.set_view_resolution(0.05, 400.0, focus)
    hidden_revision = session.body_revision(hidden)
    visible_revision = session.body_revision(visible)

    session.set_view_resolution(0.005, 400.0, focus)
    assert session.body_revision(hidden) == hidden_revision, "画面外のBodyまで配り直している"
    assert session.body_revision(visible) > visible_revision, "画面内のBodyが追随していない"


def test_the_cached_mesh_of_an_untouched_body_survives(two_body_session) -> None:
    """測り直しそのものが起きない(版だけでなくキャッシュも据え置く)。"""
    session, visible, hidden = two_body_session
    focus = {visible: None, hidden: []}
    session.set_view_resolution(0.05, 400.0, focus)
    hidden_first = session.geometry(hidden)
    visible_first = session.geometry(visible)

    session.set_view_resolution(0.005, 400.0, focus)
    assert session.geometry(hidden) is hidden_first, "画面外のBodyを測り直している"
    assert session.geometry(visible) is not visible_first


def test_the_focus_actually_changes_what_is_measured(two_body_session) -> None:
    """粗く指した面の三角形が実際に減る(旗を持っているだけ、では意味が無い)。"""
    session, visible, _ = two_body_session
    session.set_view_resolution(0.005, 400.0, {visible: None})
    everything = session.geometry(visible)
    session.set_view_resolution(0.005, 400.0, {visible: [1]})
    partial = session.geometry(visible)

    assert partial["fine_face_count"] == 1
    assert sum(len(f["indices"]) // 3 for f in partial["faces"]) < sum(
        len(f["indices"]) // 3 for f in everything["faces"]
    )
    assert _triangles(partial, 1) == _triangles(everything, 1), "細かい面が巻き添えで粗くなった"


def test_omitting_the_focus_keeps_the_previous_behaviour(two_body_session) -> None:
    """**旧クライアント互換**: `focus` を送らなければ全Body全面が細かい側。"""
    session, visible, hidden = two_body_session
    session.set_view_resolution(0.005, 400.0)
    for body_id in (visible, hidden):
        payload = session.geometry(body_id)
        assert payload["fine_face_count"] == payload["face_count"]
        assert payload["deviation"] == pytest.approx(session.view.view_deviation)


def test_junk_in_the_focus_is_dropped_instead_of_crashing(two_body_session) -> None:
    """画面から届く値を信じない。落としても**形が壊れない**(粗く測るだけ)。"""
    session, visible, _ = two_body_session
    assert session.set_view_resolution(0.005, 400.0, "not a dict") is True
    assert session.view._view_focus is None
    session.set_view_resolution(0.004, 400.0, {visible: ["1", -3, 2.5, True, 1], 7: [1]})
    assert session.view._view_focus == {visible: frozenset({1})}


def test_replacing_the_document_forgets_which_faces_were_visible(two_body_session) -> None:
    """面の添字は文書が変われば別のものを指す —— 引き継ぐと**別の面を粗く測る**。"""
    session, visible, _ = two_body_session
    session.set_view_resolution(0.005, 400.0, {visible: []})
    assert session.view._view_focus is not None
    session.new_document()
    assert session.view._view_focus is None


def test_the_edges_of_a_culled_body_stay_coarse(two_body_session) -> None:
    """辺は面ごとに分けられないので**Body単位**で倒す(細かい面が1つも無ければ粗い側)。"""
    session, visible, hidden = two_body_session
    session.set_view_resolution(0.005, 400.0, {visible: None, hidden: []})
    fine_points = max(
        len(item["points"]) // 3
        for item in session.edges(visible)["edges"]
        if item["kind"] == "circle"
    )
    coarse_points = max(
        len(item["points"]) // 3
        for item in session.edges(hidden)["edges"]
        if item["kind"] == "circle"
    )
    assert coarse_points < fine_points


def test_the_websocket_carries_the_focus_and_only_redelivers_what_changed(
    two_body_session,
) -> None:
    """**プロトコルの契約**: `focus` が届き、2度目のズームで画面外のBodyは飛ばない。"""
    session, visible, hidden = two_body_session

    async def script(send):
        base = {"cmd": "set_view_resolution", "size_hint": 400.0, "focus": {visible: None, hidden: []}}
        await send({**base, "world_per_pixel": 0.05})
        await send({**base, "world_per_pixel": 0.005})

    client = browser(session, script, connections=1)[1][0]
    states = [payload for payload in client.payloads if payload.get("type") == "state"]
    assert len(states) == 2, f"2回とも配信されるはず: {len(states)}件"
    # 線に乗るのは `delta.bodies` だけ(変わっていないBodyは**現れない**のが「送らない」)。
    second = [body["body_id"] for body in states[-1]["delta"]["bodies"]]
    assert second == [visible], f"画面外のBodyまで線に乗せている: {second}"
    assert hidden not in second


def test_the_websocket_still_works_without_a_focus(two_body_session) -> None:
    """`focus` は省略できる(旧クライアントがそのまま動く)。"""
    session, visible, _ = two_body_session

    async def script(send):
        await send({"cmd": "set_view_resolution", "world_per_pixel": 0.005, "size_hint": 400.0})

    client = browser(session, script, connections=1)[1][0]
    assert not [p for p in client.payloads if p.get("type") == "error"]
    assert session.geometry(visible)["fine_face_count"] == session.geometry(visible)["face_count"]
