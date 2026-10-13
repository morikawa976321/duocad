"""シェル(`shell_body`)の契約。OCCT が成功を装って何もしない形を通さないこと。

期待値の出どころ(40×30×20 の箱、2026-10-13 実測。docs/CAD_LIBRARY_PITFALLS.md):
上面を抜いて厚さ2 → 24000 - 36×26×18 = 7152。厚さ16は元の形のまま返る。
全辺を r3 で丸めた箱の上面を抜くと元の形のまま返る。
"""
from __future__ import annotations

import pytest

from duocad import kernel_shell, query, selection
from duocad.document import Document
from duocad.entry import entry_for
from duocad.verbs import fillet, shell_body
from document_support import new_box, replay_through_json, second_body

WIDTH, DEPTH, HEIGHT = 40.0, 30.0, 20.0
THICKNESS = 2.0
OPEN_TOP_VOLUME = WIDTH * DEPTH * HEIGHT - (WIDTH - 4) * (DEPTH - 4) * (HEIGHT - 2)


def _box(doc: Document) -> str:
    return new_box(doc, WIDTH, DEPTH, HEIGHT)


def _face_where(doc: Document, body_id: str, axis: int, value: float) -> int:
    """中心の座標 `axis` が `value` の平面を1枚選ぶ(添字の決め打ちにしない)。"""
    found = [
        face["index"]
        for face in query.describe_faces(doc.get_body(body_id).shape)
        if face["kind"] == "plane" and face["center"][axis] == pytest.approx(value)
    ]
    assert len(found) == 1, found
    return found[0]


def _bbox(doc: Document, body_id: str) -> list:
    """頂点の座標の最小・最大(平面だけの箱なら外形の寸法になる)。"""
    points = [vertex["point"] for vertex in query.describe_vertices(doc.get_body(body_id).shape)]
    return [min(p[axis] for p in points) for axis in range(3)] + [max(p[axis] for p in points) for axis in range(3)]


def test_opening_the_top_leaves_walls_of_the_given_thickness_inside_the_outline():
    """外形は変わらず、中身だけが抜ける(体積は外 − 内の箱)。"""
    doc = Document()
    body_id = _box(doc)
    bbox_before = _bbox(doc, body_id)

    shell_body(doc, [_face_where(doc, body_id, 2, HEIGHT)], THICKNESS, body_id=body_id)

    shape = doc.get_body(body_id).shape
    assert query.volume(shape) == pytest.approx(OPEN_TOP_VOLUME)
    assert _bbox(doc, body_id) == pytest.approx(bbox_before)


def test_two_openings_remove_both_faces():
    """抜く面を2枚にすると、上面に加えて側面も開く(体積は1枚のときより減る)。"""
    doc = Document()
    body_id = _box(doc)
    top = _face_where(doc, body_id, 2, HEIGHT)
    side = _face_where(doc, body_id, 0, WIDTH)

    shell_body(doc, [top, side], THICKNESS, body_id=body_id)

    expected = WIDTH * DEPTH * HEIGHT - (WIDTH - 2) * (DEPTH - 4) * (HEIGHT - 2)
    assert query.volume(doc.get_body(body_id).shape) == pytest.approx(expected)


def _assert_refused_without_change(doc: Document, body_id: str, faces: list, thickness: float, match: str):
    shape_before = doc.get_body(body_id).shape
    log_before = len(doc.log.entries)
    with pytest.raises(ValueError, match=match):
        shell_body(doc, faces, thickness, body_id=body_id)
    assert doc.get_body(body_id).shape is shape_before
    assert len(doc.log.entries) == log_before


def test_too_thick_is_refused_instead_of_returning_the_body_unchanged():
    """厚すぎると OCCT は IsDone 真・BRepCheck 真のまま元の形を返す。それを成功にしない。"""
    doc = Document()
    body_id = _box(doc)
    _assert_refused_without_change(
        doc, body_id, [_face_where(doc, body_id, 2, HEIGHT)], 16.0, "did not hollow the body"
    )


def test_an_opening_bordered_by_fillets_is_refused():
    """抜く面がフィレット面に接すると OCCT は何もしない。黙って通さない。"""
    doc = Document()
    body_id = _box(doc)
    edges = [edge["index"] for edge in query.describe_edges(doc.get_body(body_id).shape)]
    fillet(doc, edges, 3.0, body_id=body_id)
    _assert_refused_without_change(
        doc, body_id, [_face_where(doc, body_id, 2, HEIGHT)], THICKNESS, "did not hollow the body"
    )


@pytest.mark.parametrize(
    "faces,thickness,match",
    [
        ([], THICKNESS, "at least one face"),
        ([1, 1], THICKNESS, "same face twice"),
        ([1], 0.0, "thickness"),
        ([1], float("nan"), "thickness"),
        ([99], THICKNESS, "out of range"),
    ],
)
def test_bad_arguments_are_refused_without_change(faces, thickness, match):
    doc = Document()
    body_id = _box(doc)
    _assert_refused_without_change(doc, body_id, faces, thickness, match)


def test_shell_is_logged_undoable_and_replays():
    """ログに1行積まれ、undo で元へ戻り、redo と JSON 経由の再生で同じ形になる。"""
    doc = Document()
    body_id = _box(doc)
    top = _face_where(doc, body_id, 2, HEIGHT)
    volume_before = query.volume(doc.get_body(body_id).shape)

    operation = shell_body(doc, [top], THICKNESS, body_id=body_id)

    assert operation.verb == "shell_body"
    assert operation.args == {"faces": [top], "thickness": THICKNESS, "body_id": body_id}
    assert operation.provenance["verb"] == "shell_body"
    assert len(operation.provenance["new_faces"]) == 5, "内側の壁4枚と底1枚"
    doc.log.to_json()  # JSON化できない値が混ざっていればここで落ちる

    doc.log.undo(doc)
    assert query.volume(doc.get_body(body_id).shape) == pytest.approx(volume_before)
    doc.log.redo(doc)
    assert query.volume(doc.get_body(body_id).shape) == pytest.approx(OPEN_TOP_VOLUME)

    replayed = replay_through_json(doc)
    assert query.volume(replayed.get_body(body_id).shape) == pytest.approx(OPEN_TOP_VOLUME)


# ---------------------------------------------------------------- 入口(選択から取る)
def test_the_entry_takes_the_opening_faces_from_the_selection():
    doc = Document()
    body_id = _box(doc)
    top = _face_where(doc, body_id, 2, HEIGHT)
    selection.set_selection(doc, refs=[selection.select_face(doc, body_id, top)])

    result = entry_for("shell_body")(doc, thickness=THICKNESS)

    assert result == {"body_id": body_id, "changed_bodies": [body_id]}
    assert query.volume(doc.get_body(body_id).shape) == pytest.approx(OPEN_TOP_VOLUME)


def test_the_entry_refuses_an_empty_selection():
    """面を選ばない閉じた中空は作らない(本人判断)。空の選択は断る。"""
    doc = Document()
    _box(doc)
    with pytest.raises(selection.SelectionEmpty):
        entry_for("shell_body")(doc, thickness=THICKNESS)


def test_the_entry_refuses_a_body_without_faces():
    """ボディだけを選んでも閉じた中空にはしない(面が無いと言って断る)。"""
    doc = Document()
    body_id = _box(doc)
    selection.set_selection(doc, refs=[selection.select_body(doc, body_id)])
    with pytest.raises(selection.SelectionKindMismatch):
        entry_for("shell_body")(doc, thickness=THICKNESS)


def test_the_entry_refuses_faces_of_two_bodies():
    doc = Document()
    first = _box(doc)
    other = second_body(doc)
    selection.set_selection(
        doc,
        refs=[selection.select_face(doc, first, 1), selection.select_face(doc, other, 1)],
    )
    log_before = len(doc.log.entries)
    with pytest.raises(ValueError, match="multiple bodies"):
        entry_for("shell_body")(doc, thickness=THICKNESS)
    assert len(doc.log.entries) == log_before


# ---------------------------------------------------------------- kernel(壁の厚さの検査)
def _box_with_boss():
    """40×30×20 の箱の上面に、半径3・高さ11の円柱を立てた形(sol の再現例)。"""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    boss = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(20, 15, 19), gp_Dir(0, 0, 1)), 3, 11).Shape()
    fuse = BRepAlgoAPI_Fuse(BRepPrimAPI_MakeBox(WIDTH, DEPTH, HEIGHT).Shape(), boss)
    fuse.Build()
    return fuse.Shape()


def _bottom(shape) -> list:
    return [
        face["index"]
        for face in query.describe_faces(shape)
        if face["kind"] == "plane" and face["center"][2] == pytest.approx(0.0)
    ]


def test_a_wall_thinner_than_requested_is_refused():
    """厚さが出っ張りの半径を超えると OCCT はそこだけ薄い壁で成功を返す(体積も BRepCheck も通る)。"""
    shape = _box_with_boss()
    with pytest.raises(ValueError, match="thinner than requested"):
        kernel_shell.shell_solid(shape, _bottom(shape), 4.0)
    kernel_shell.shell_solid(shape, _bottom(shape), 2.0)  # 半径未満なら通る


def test_a_very_thin_wall_is_not_refused_by_the_tolerance():
    """許容差を厚さに合わせて下げる(固定 1e-3 では厚さ 0.001 が IsDone=False だった)。"""
    doc = Document()
    body_id = _box(doc)
    shell_body(doc, [_face_where(doc, body_id, 2, HEIGHT)], 0.001, body_id=body_id)
    assert query.volume(doc.get_body(body_id).shape) > 0


def test_the_wall_check_measures_through_concentric_fillets():
    """上面以外の辺を丸めた箱は内外の丸み(円筒・角の球)が同心で、面どうしの距離を OCCT が測れない
    (`StdFail_NotDone`。縁の点で測り直す)。"""
    doc = Document()
    body_id = _box(doc)
    below_top = [
        edge["index"]
        for edge in query.describe_edges(doc.get_body(body_id).shape)
        if not (edge["start"][2] == pytest.approx(HEIGHT) and edge["end"][2] == pytest.approx(HEIGHT))
    ]
    fillet(doc, below_top, 3.0, body_id=body_id)
    volume_before = query.volume(doc.get_body(body_id).shape)
    shell_body(doc, [_face_where(doc, body_id, 2, HEIGHT)], THICKNESS, body_id=body_id)
    assert query.volume(doc.get_body(body_id).shape) < volume_before
