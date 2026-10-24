"""選択集合をコアの第一級オブジェクトにする契約(詳細は `duocad/selection.py` のdocstring)。ここが崩れると、AIが提案し人が過不足を直しAIが適用する
往復のどこかが閉じなくなる(選択の正がブラウザ側に漏れる/提案と確定の区別が消える等)。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from duocad import kernel_extrude, selection
from duocad.document import Body, Document
from duocad.oplog import OpLog
from duocad.paths import REPO_ROOT
from duocad.region import region_ref, regions
from duocad.selection import set_visible
from duocad.server import Session
from duocad.sketch import add_circle, sketch_create
from duocad.verbs import extrude_faces
from document_support import box_shape as _box_shape, shape_ref
from session_support import SIDE, pad, select, session_with_box

from duocad import (
    brep_topology as _brep_topology,
)

SESSION_TS = REPO_ROOT / "web" / "src" / "session.ts"
COMMAND_TS = REPO_ROOT / "web" / "src" / "commands" / "command.ts"
#: 押し出し量(mm)。文書を実際に変えるために使う。
EXTRUDE_MM = 5.0


@pytest.fixture()
def two_bodies() -> Document:
    """ソリッドを2つ持つ文書(可視性の契約と、複数Bodyにまたがる選択のため)。"""
    document = Document()
    document.add_body(Body(id="a", name="a", kind="solid", shape=_box_shape()))
    document.add_body(Body(id="b", name="b", kind="solid", shape=_box_shape(10.0, 10.0, 10.0)))
    return document


@pytest.fixture()
def sketch_doc():
    """半径20の円を1つ持つスケッチの文書と、その sketch_id(領域はちょうど1個)。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    add_circle(document, sketch_id, (0.0, 0.0), 40.0)
    return document, sketch_id


@pytest.fixture()
def session() -> Session:
    """STEPを読まないセッション(選択の契約は形状の中身に依存しない)。"""
    return session_with_box()


def _first_extrudable_face(shape) -> int:
    """押し出しに成功する最初の面の1始まり添字(添字の決め打ちを避ける)。"""
    for index in range(1, _brep_topology.face_count(shape) + 1):
        try:
            kernel_extrude.extrude_faces(shape, [index], EXTRUDE_MM, "join", target_shape=shape)
        except Exception:  # noqa: BLE001  曲面・退化など押せない面は飛ばす
            continue
        return index
    raise AssertionError("押し出せる面が1つも無い(テストの前提が崩れている)")


def test_each_kind_of_reference_keeps_its_kind(doc: Document):
    """面・辺・頂点の参照が、それぞれ `kind` を保持したまま選択に入る。"""
    refs = [
        shape_ref(doc, "box", "face", 1),
        shape_ref(doc, "box", "edge", 1),
        shape_ref(doc, "box", "vertex", 1),
    ]
    result = selection.modify_selection(doc, add=refs)
    assert [item["kind"] for item in result.items] == ["face", "edge", "vertex"]


def test_shape_refs_have_the_shape_of_the_spec(doc: Document):
    """ShapeRef の形は §3.5 のとおり(`provenance` は day1 の空スロット)。"""
    ref = shape_ref(doc, "box", "edge", 3)
    assert ref == {"body": "box", "kind": "edge", "index": 3, "provenance": None}


def test_edge_and_vertex_indexes_follow_the_kernel_numbering(doc: Document):
    """辺・頂点の添字は `kernel` の数え方(1始まり)で、範囲外は ValueError。"""
    shape = doc.bodies[0].shape
    assert _brep_topology.edge_count(shape) == 12
    assert _brep_topology.count_subshapes(shape, "vertex") == 8

    shape_ref(doc, "box", "edge", 12)
    shape_ref(doc, "box", "vertex", 8)
    with pytest.raises(ValueError):
        shape_ref(doc, "box", "edge", 13)
    with pytest.raises(ValueError):
        shape_ref(doc, "box", "vertex", 0)


def test_regions_can_be_selected_with_a_region_ref(sketch_doc):
    """領域は RegionRef(`{"sketch":.., "index":.., "provenance": None}`)で選ぶ(§3.5)。"""
    document, sketch_id = sketch_doc
    ref = selection.select_region(document, sketch_id, 1)
    assert ref == region_ref(sketch_id, 1).to_dict()
    assert set(ref) == {"sketch", "index", "provenance"}
    assert ref["provenance"] is None


def test_selecting_a_region_validates_the_sketch_and_the_index(sketch_doc):
    """存在しないスケッチは KeyError、範囲外の領域添字は ValueError(黙って通さない)。"""
    document, sketch_id = sketch_doc
    assert len(regions(document, sketch_id)) == 1

    with pytest.raises(KeyError):
        selection.select_region(document, "no-such-sketch", 1)
    with pytest.raises(ValueError):
        selection.select_region(document, sketch_id, 2)


def test_regions_go_into_the_selection_like_any_other_kind(sketch_doc, doc: Document):
    """**領域が選択集合に実際に入る**こと(参照を作れるだけでは足りない)。"""
    document, sketch_id = sketch_doc
    region = selection.select_region(document, sketch_id, 1)

    result = selection.modify_selection(document, add=[region, dict(region)])
    assert len(result.items) == 1, "同じ領域を2回addしても重複しない"
    assert selection.ref_kind(result.items[0]) == selection.REGION_KIND
    assert selection.selected_items(document, kind=selection.REGION_KIND) == [region]

    assert selection.modify_selection(document, remove=[dict(region)]).items == []


def test_selected_items_rejects_an_unknown_kind(doc: Document):
    """`kind` の綴り間違いは ValueError(0件として返すとverbが黙って何もしない)。"""
    selection.modify_selection(doc, add=[shape_ref(doc, "box", "edge", 1)])
    with pytest.raises(ValueError):
        selection.selected_items(doc, kind="edges")


def test_selecting_from_a_missing_body_raises(doc: Document):
    """存在しない body_id は KeyError(黙って空振りさせない。§6予防6)。"""
    with pytest.raises(KeyError):
        shape_ref(doc, "nope", "edge", 1)


def test_modify_selection_refuses_a_malformed_ref(doc: Document):
    """人が触る主経路でも、壊れた参照は落ちて選択を変更しない(`propose` と同じ契約)。"""
    selection.modify_selection(doc, add=[shape_ref(doc, "box", "edge", 1)])
    with pytest.raises(ValueError):
        selection.modify_selection(doc, add=[{"body": "box", "index": 1}])
    with pytest.raises(KeyError):
        selection.modify_selection(
            doc, add=[{"body": "nope", "kind": "edge", "index": 1, "provenance": None}]
        )
    assert [item["index"] for item in selection.get_selection(doc).items] == [1]


def test_removing_something_that_is_not_selected_is_a_no_op(doc: Document):
    """選択に無いものの remove は空振りでよい(UIのトグルがそのまま乗る)。"""
    selection.modify_selection(doc, add=[shape_ref(doc, "box", "edge", 1)])
    result = selection.modify_selection(doc, remove=[shape_ref(doc, "box", "edge", 9)])
    assert [item["index"] for item in result.items] == [1]


def test_remove_wins_when_the_same_reference_is_added_and_removed(doc: Document):
    """同じ参照が add と remove の両方にあるとき、**remove が勝つ**(引き算を後に適用)。"""
    ref = shape_ref(doc, "box", "edge", 5)
    result = selection.modify_selection(doc, add=[ref], remove=[dict(ref)])
    assert result.items == []


def test_selection_spans_multiple_bodies(two_bodies: Document):
    """選択は**Bodyをまたげる**(§3.2「選択が body_id を持つ」ことの実利)。"""
    result = selection.modify_selection(
        two_bodies,
        add=[
            shape_ref(two_bodies, "a", "face", 1),
            shape_ref(two_bodies, "b", "face", 1),
        ],
    )
    assert [item["body"] for item in result.items] == ["a", "b"]


def test_hidden_bodies_cannot_be_selected(two_bodies: Document):
    """不可視Bodyは指せない(M3.5で決めた「見えないものは選べない」の一貫性)。"""
    set_visible(two_bodies, "b", False)
    assert selection.selectable_bodies(two_bodies) == ["a"]
    with pytest.raises(ValueError):
        shape_ref(two_bodies, "b", "face", 1)


def test_operating_the_raw_verb_function_does_not_touch_selection(doc: Document):
    """**選択集合そのものは永続する**。verb関数(`duocad/verbs/*.py` の生の関数)は形しか触らない。"""
    face_index = _first_extrudable_face(doc.bodies[0].shape)
    before = selection.modify_selection(doc, add=[shape_ref(doc, "box", "face", face_index)])
    kept = [dict(item) for item in before.items]

    extrude_faces(doc, [{"body": "box", "index": face_index}], EXTRUDE_MM)

    assert selection.get_selection(doc).items == kept


def test_changing_the_selection_does_not_touch_the_operation_log(doc: Document):
    """**選択の変更は操作ログに載らない**(選択は文書を変更しないため)。"""
    before = len(doc.log.entries)
    selection.set_selection(doc, refs=[shape_ref(doc, "box", "face", 1)])
    selection.modify_selection(doc, add=[shape_ref(doc, "box", "edge", 1)])
    selection.toggle_selection(doc, [shape_ref(doc, "box", "edge", 2)])
    selection.clear_selection(doc)
    assert len(doc.log.entries) == before


def test_set_visible_does_not_land_in_the_log_either(doc: Document):
    """**可視性もログに載らない**。`Body.visible` という文書の状態を変えるが、
    形は1つも変えないので undo の1手を使わせない —— 奥を覗くために手前を隠して戻す
    「見るための操作」が編集の履歴を埋めると、直前の押し出しへ戻れなくなる。
    """
    before = len(doc.log.entries)
    set_visible(doc, "box", False)
    set_visible(doc, "box", True)
    assert len(doc.log.entries) == before, "可視性が操作ログに積まれている"

    sketch_create(doc, "XY")  # 対照: 本物のverbは載る
    assert len(doc.log.entries) == before + 1


def test_selection_is_not_saved(doc: Document):
    """**選択は保存されない**(セッション状態)。保存・再生の対象は操作ログだけ。"""
    sketch_create(doc, "XY")  # ログに1行積んでおく(空のログを再生しても何も検査できない)
    selected = shape_ref(doc, "box", "face", 1)
    selection.set_selection(doc, refs=[selected])
    saved = doc.log.to_json()

    # **保存されたJSONがそもそも選択を運んでいない**。ここが本体の検査で、
    # 「新しい文書の選択が空」だけを見ると、実装が間違っていても緑になる。
    operations = json.loads(saved)["operations"]
    assert operations, "検査の前提: ログに操作が入っていること"
    assert all("selection" not in operation for operation in operations)
    assert json.dumps(selected, ensure_ascii=False) not in saved

    restored = Document()
    restored.add_body(Body(id="box", name="box", kind="solid", shape=_box_shape()))
    OpLog.from_json(saved).replay(restored)
    assert restored.selection.items == []


def test_the_ai_selects_the_human_fixes_and_the_ai_reads_it_back(doc: Document):
    """**交互操作そのもの。このファイルの核心の1本**。"""
    chosen = selection.set_selection(
        doc, refs=[shape_ref(doc, "box", "edge", index) for index in (1, 2, 3)]
    )
    assert [item["index"] for item in chosen.items] == [1, 2, 3]

    # 人が過不足を直す(1本足して1本引く)
    selection.modify_selection(
        doc,
        add=[shape_ref(doc, "box", "edge", 4)],
        remove=[shape_ref(doc, "box", "edge", 2)],
    )

    # AIが読み直す。**人の編集が見えている**
    read_back = selection.get_selection(doc)
    assert [item["index"] for item in read_back.items] == [1, 3, 4]


def test_toggle_selection_adds_what_is_absent_and_removes_what_is_present(doc: Document):
    """`toggle_selection` は参照ごとにあれば外し無ければ足す(UIの素のクリックと同じ)。"""
    selection.set_selection(doc, refs=[shape_ref(doc, "box", "edge", 1)])
    selection.toggle_selection(
        doc, [shape_ref(doc, "box", "edge", 1), shape_ref(doc, "box", "edge", 2)]
    )
    assert [item["index"] for item in selection.get_selection(doc).items] == [2]


def test_toggle_selection_refuses_a_malformed_ref_without_touching_the_selection(doc: Document):
    keep = [shape_ref(doc, "box", "edge", 1)]
    selection.set_selection(doc, refs=keep)
    with pytest.raises(ValueError):
        selection.toggle_selection(doc, [{"body": "box", "kind": "nope", "index": 1}])
    assert [item["index"] for item in selection.get_selection(doc).items] == [1]


def test_selected_items_can_be_filtered_by_kind(doc: Document):
    """`selected_items(kind=...)` で種別を絞れる(verbは自分が食える種別だけ要る)。"""
    selection.modify_selection(
        doc,
        add=[shape_ref(doc, "box", "face", 1), shape_ref(doc, "box", "edge", 1)],
    )
    assert len(selection.selected_items(doc, kind="edge")) == 1
    assert len(selection.selected_items(doc, kind="face")) == 1


def test_edge_indices_feed_fillet_directly(doc: Document):
    """確定した選択が `fillet(edges=[...])` へそのまま渡せる形で取り出せる。"""
    selection.modify_selection(
        doc, add=[shape_ref(doc, "box", "edge", index) for index in (1, 3)]
    )
    assert selection.edge_indices(doc) == [1, 3]


def test_edge_indices_refuses_to_guess_across_bodies(two_bodies: Document):
    """複数Bodyの辺が混ざったまま body_id を省略したら ValueError(どれのつもりかを黙って決めない)。"""
    selection.modify_selection(
        two_bodies,
        add=[
            shape_ref(two_bodies, "a", "edge", 1),
            shape_ref(two_bodies, "b", "edge", 1),
        ],
    )
    with pytest.raises(ValueError):
        selection.edge_indices(two_bodies)
    assert selection.edge_indices(two_bodies, body_id="a") == [1]


def test_proposing_again_replaces_the_previous_selection(doc: Document):
    """`set_selection` は**置き換え**(差分は `modify_selection` の仕事)。"""
    selection.set_selection(doc, refs=[shape_ref(doc, "box", "edge", 1)])
    result = selection.set_selection(doc, refs=[shape_ref(doc, "box", "edge", 2)])
    assert [item["index"] for item in result.items] == [2]


@pytest.mark.parametrize(
    "bad, expected, why",
    [
        ({"body": "box", "index": 1, "provenance": None}, ValueError, "kindが無い"),
        ({"body": "box", "kind": "solid", "index": 1}, ValueError, "未知のkind"),
        ({"body": "box", "kind": "edge"}, ValueError, "indexが無い"),
        ({"body": "nope", "kind": "edge", "index": 1, "provenance": None}, KeyError,
         "存在しないBody"),
        ({"body": "box", "kind": "edge", "index": 99, "provenance": None}, ValueError,
         "範囲外の添字"),
    ],
)
def test_a_malformed_ref_is_refused_without_touching_the_selection(
    doc: Document, bad: dict, expected: type, why: str
):
    """手書きの壊れた参照も**落ちて、選択を変更しない**。"""
    keep = [shape_ref(doc, "box", "edge", 1)]
    selection.modify_selection(doc, add=keep)

    with pytest.raises(expected):
        selection.set_selection(doc, refs=[bad])

    after = selection.get_selection(doc)
    assert [item["index"] for item in after.items] == [1]


def test_set_selection_needs_exactly_one_source(doc: Document):
    """`refs` と `predicate` は**どちらか一方**。両方・どちらも無しは ValueError。"""
    with pytest.raises(ValueError):
        selection.set_selection(doc)
    with pytest.raises(ValueError):
        selection.set_selection(
            doc,
            refs=[shape_ref(doc, "box", "edge", 1)],
            predicate={"kind": "edge", "where": []},
        )


def test_every_selection_api_fires_exactly_one_event(session: Session):
    """変更する4つのAPIがそれぞれ1回だけ発火し、読み取りは発火しない。"""
    events = []
    session.add_listener(events.append)
    ref = shape_ref(session.document, "box", "edge", 1)

    session.set_selection(refs=[ref])
    assert len(events) == 1
    session.modify_selection(add=[shape_ref(session.document, "box", "edge", 2)])
    assert len(events) == 2
    session.toggle_selection([shape_ref(session.document, "box", "edge", 2)])
    assert len(events) == 3
    session.clear_selection()
    assert len(events) == 4

    session.get_selection()
    assert len(events) == 4


def test_the_event_carries_enough_to_redraw(session: Session):
    """イベントは**UIが描き直すのに足る情報**を持つ。"""
    events = []
    session.add_listener(events.append)
    session.set_selection(refs=[shape_ref(session.document, "box", "edge", 1)])

    payload = events[0]
    assert payload["change"] == "selection"
    assert [item["index"] for item in payload["selection"]["items"]] == [1]
    assert payload["selection"]["count"] == 1


def test_the_event_carries_the_client_op_back(session: Session):
    """`client_op` をそのまま載せて返す(操作を出したクライアントが自分の計測と突き合わせる)。"""
    events = []
    session.add_listener(events.append)
    ref = shape_ref(session.document, "box", "edge", 1)

    assert session.set_selection(refs=[ref], client_op=7)["client_op"] == 7
    assert events[0]["client_op"] == 7
    # 出し手のいない変更(MCP等)では None
    assert session.clear_selection()["client_op"] is None


def test_selection_events_ride_the_same_up_path_as_set_visible(session: Session):
    """**別系統を新設していない**こと。

    `set_visible` の変更がブラウザへ届く経路は `Session._emit` → `add_listener` の
    購読者(`Server._on_core_change`)である。選択も**同じ購読者リストに乗る**ので、
    購読をやめれば両方止まる。専用のイベントバスを新設していたら、この1本が落ちる。
    """
    events = []
    session.add_listener(events.append)

    session.set_visible("box", False)
    session.set_visible("box", True)
    assert len(events) == 2

    session.set_selection(refs=[shape_ref(session.document, "box", "edge", 1)])
    assert len(events) == 3

    session.remove_listener(events.append)
    session.clear_selection()
    assert len(events) == 3, "購読をやめた後も届くなら、選択は別の経路で配られている"


def test_selection_is_part_of_the_pushed_state(session: Session):
    """選択は `state_meta()` に載る —— **これが無いとAIの変更は画面へ届かない**。"""
    session.set_selection(refs=[shape_ref(session.document, "box", "edge", 1)])
    state = session.state_meta()
    assert "origin" not in state["selection"]
    assert state["selection"]["count"] == 1


def test_a_failing_listener_does_not_undo_the_selection(session: Session):
    """購読者が落ちても**選択の変更は成立したまま**にする(M4で決めた `_emit` の契約)。"""
    def broken(_applied):
        raise RuntimeError("購読者が落ちた")

    session.add_listener(broken)
    session.set_selection(refs=[shape_ref(session.document, "box", "edge", 1)])

    assert session.get_selection()["count"] == 1
    assert any("配信に失敗" in warning for warning in session.warnings)


# ---------------------------------------------------------------- 隠したものを選択から外す・参照の形
def test_a_hidden_body_that_is_still_selected_can_be_replaced_by_a_new_pick(two_bodies: Document):
    """落ちたら、選択に残った隠したボディの項目が `remove` を丸ごと拒否させ、画面の「いまの選択を外して新しく選ぶ」が失敗する。"""
    selection.set_selection(two_bodies, refs=[shape_ref(two_bodies, "a", "face", 1)])
    set_visible(two_bodies, "a", False)
    assert selection.get_selection(two_bodies).items, "可視性の切替で選択は飛ばない(前提)"

    result = selection.modify_selection(
        two_bodies,
        add=[shape_ref(two_bodies, "b", "face", 1)],
        remove=[{"body": "a", "kind": "face", "index": 1, "provenance": None}],
    )

    assert [(item["body"], item["index"]) for item in result.items] == [("b", 1)]
    with pytest.raises(ValueError, match="hidden"):
        selection.modify_selection(two_bodies, add=[{"body": "a", "kind": "face", "index": 2}])


def test_a_hidden_sketch_entity_that_is_still_selected_can_be_removed(sketch_doc):
    """落ちたら、隠したスケッチの図形が選択に残ると外せない(ボディ側と同じ門が掛かっている)。"""
    document, sketch_id = sketch_doc
    ref = {"sketch": sketch_id, "entity": document.sketches[0].entities[0].id, "point": None}
    selection.modify_selection(document, add=[ref])
    selection.set_sketch_visible(document, sketch_id, False)

    with pytest.raises(ValueError, match="hidden"):
        selection.modify_selection(document, add=[ref, dict(ref, point="cx")])
    selection.modify_selection(document, remove=[dict(ref)])

    assert selection.get_selection(document).items == []


def test_toggling_a_selected_ref_of_a_hidden_body_removes_it_but_a_hidden_one_cannot_be_added(two_bodies: Document):
    """落ちたら、隠したボディの選択済みの面を toggle で外せない。逆に、隠したボディの面を toggle で足せてしまう。"""
    selection.set_selection(
        two_bodies, refs=[shape_ref(two_bodies, "a", "face", 1), shape_ref(two_bodies, "b", "face", 1)]
    )
    set_visible(two_bodies, "a", False)

    selection.toggle_selection(two_bodies, [{"body": "a", "kind": "face", "index": 1}])
    assert [(item["body"], item["index"]) for item in selection.get_selection(two_bodies).items] == [("b", 1)]

    with pytest.raises(ValueError, match="hidden"):
        selection.toggle_selection(two_bodies, [{"body": "a", "kind": "face", "index": 2}])
    assert [(item["body"], item["index"]) for item in selection.get_selection(two_bodies).items] == [("b", 1)]


def test_toggling_the_same_ref_twice_changes_nothing_and_keeps_the_order(doc: Document):
    """落ちたら、入っている参照を2回 toggle すると末尾へ動く(`fillet` の辺リストと半径リストの対応がずれる)。"""

    def edge(index: int) -> dict:
        return shape_ref(doc, "box", "edge", index)

    def indexes() -> list:
        return [item["index"] for item in selection.get_selection(doc).items]

    selection.set_selection(doc, refs=[edge(1), edge(2), edge(3)])
    selection.toggle_selection(doc, [edge(1), edge(1)])
    assert indexes() == [1, 2, 3]
    selection.toggle_selection(doc, [edge(4), edge(4)])
    assert indexes() == [1, 2, 3]
    selection.toggle_selection(doc, [edge(2), edge(2), edge(2)])
    assert indexes() == [1, 3], "奇数回は1回として外れる"


def test_set_selection_keeps_only_the_first_of_duplicate_refs(doc: Document):
    """落ちたら、同じ参照を複数渡すと件数が水増しされる(`measure` の合計が二重になり、`toggle` が1回で両方外す)。"""
    first = shape_ref(doc, "box", "edge", 2)
    result = selection.set_selection(
        doc,
        refs=[first, shape_ref(doc, "box", "edge", 1), dict(first), {"body": "box", "kind": "edge", "index": "1"}],
    )

    assert [item["index"] for item in result.items] == [2, 1]


@pytest.mark.parametrize("index", [None, [1], True, 2.9, float("inf"), float("nan"), "x", {"a": 1}])
def test_a_ref_index_that_is_not_an_integer_is_a_value_error_and_leaves_the_selection(doc: Document, index):
    """落ちたら、`null`・配列は TypeError、`inf` は OverflowError(MCP では internal)で落ち、`2.9`・`true` は別の面を選ぶ。"""
    keep = shape_ref(doc, "box", "edge", 1)
    selection.set_selection(doc, refs=[keep])
    bad = {"body": "box", "kind": "edge", "index": index}

    for call in (
        lambda: selection.set_selection(doc, refs=[bad]),
        lambda: selection.modify_selection(doc, add=[bad]),
        lambda: selection.modify_selection(doc, remove=[bad]),
        lambda: selection.toggle_selection(doc, [bad]),
    ):
        with pytest.raises(ValueError):
            call()
    assert [item["index"] for item in selection.get_selection(doc).items] == [1]


def test_an_integral_float_and_a_numeric_string_are_still_accepted_as_an_index(doc: Document):
    result = selection.set_selection(
        doc, refs=[{"body": "box", "kind": "edge", "index": 3.0}, {"body": "box", "kind": "edge", "index": "2"}]
    )
    assert [item["index"] for item in result.items] == [3, 2]


def test_a_body_name_that_is_not_a_string_is_a_value_error(doc: Document):
    """落ちたら、ボディ名が配列の参照が TypeError(unhashable)や KeyError で落ち、種別ごとに例外の型が揃わない。"""
    for kind in ("face", "body"):
        with pytest.raises(ValueError):
            selection.modify_selection(doc, add=[{"body": ["box"], "kind": kind, "index": 1}])


def test_a_body_can_be_selected_whole(session: Session) -> None:
    """**ソリッドを丸ごと指せる**(面を全部入れる案は採っていない)。

    落ちるのは「`kind='body'` の参照が選択機構を通らない」とき。削除・計測・最適化の保持/除外ソリッドは
    選択から対象を取るので、通らなければそれらの verb がボディを指せない。
    """
    body = pad(session, SIDE, SIDE, SIDE)
    ref = selection.select_body(session.document, body.id)
    assert ref == {"body": body.id, "kind": "body", "index": None, "provenance": None}
    assert selection.ref_kind(ref) == selection.BODY_KIND

    select(session, [{"body": body.id, "kind": "body"}])
    items = session.get_selection()["items"]
    # **面が展開されて入っていない**(2835面のボディで選択集合が膨れる案を採らないこと)
    assert items == [ref], items
    assert selection.require_selected_bodies(session.document) == [body.id]


def test_the_same_body_is_not_selected_twice(session: Session) -> None:
    """同じボディを2回 add しても重複しない(等価判定が添字を見ていないこと)。"""
    body = pad(session, SIDE, SIDE, SIDE)
    refs = [{"body": body.id, "kind": "body"}]
    session.clear_selection()
    session.modify_selection(refs, [])
    session.modify_selection(refs, [])
    assert len(session.get_selection()["items"]) == 1
    session.modify_selection([], refs)
    assert session.get_selection()["items"] == []


def _ts_union_arms(path: Path, name: str) -> set:
    """`export type <name> = A | B | C` の右辺を集合で返す。"""
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"^export type {name} = (.+)$", text, re.M)
    assert match, f"{name} の宣言を {path.name} から読めていない"
    return {arm.strip() for arm in match.group(1).split("|") if arm.strip()}


def test_the_typescript_selectable_kinds_match_the_core() -> None:
    """TSの `SelectableKind` の顔ぶれが、コアの `SELECTABLE_KINDS` と一致する。"""
    shape_kinds = {arm.strip("'") for arm in _ts_union_arms(SESSION_TS, "ShapeKind")}
    declared = set()
    for arm in _ts_union_arms(COMMAND_TS, "SelectableKind"):
        declared |= shape_kinds if arm == "ShapeKind" else {arm.strip("'")}
    assert declared == set(selection.SELECTABLE_KINDS), {
        "TSのみ": sorted(declared - set(selection.SELECTABLE_KINDS)),
        "コアのみ": sorted(set(selection.SELECTABLE_KINDS) - declared),
    }
