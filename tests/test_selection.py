"""A1契約: 選択集合をコアの第一級オブジェクトにする(項番1〜14)。

**このファイルが定義するのは「交互操作が成立するための唯一の必要条件」である**
(実装計画書§2.1)。AIが選択を提案し、人が過不足を直し、AIがそれに verb を適用する——
この一巡は、選択集合が**コアにある共有オブジェクト**でなければ原理的に閉じない
(選択の正がブラウザ側にあると、AIから読めも書けもしない)。

満たすべき5条件(§2.1): **永続 / 双方向(AIが書ける) / 種別を保持 / 差分編集可能 /
出所を区別(提案か確定か)**。

## 設計(理由は tests/README.md の A 節が正)

- **`origin` は集合単位**(§3.5のデータモデルどおり)。要素単位の `pending: bool` は採らない
- **遷移**: `propose_selection`→"proposed" / `modify_selection`(人が触る)→"user" /
  `commit_selection`→"user"。**人が触ること自体が承認**である
- **`origin="proposed"` のまま verb には渡せない**(`confirmed_items` が ValueError)。
  AIは `commit_selection()` を明示的に呼べば自分の提案を実行できる —— 禁止ではなく
  「黙って通さない」(§6予防6)
- **選択の変更は操作ログに積まない**が、**イベントは発火する**(下の項番12〜14)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import kernel, selection
from duocad.document import Body, Document
from duocad.oplog import OpLog
from duocad.region import region_ref, regions
from duocad.server import Session
from duocad.sketch import add_circle, sketch_create
from duocad.verbs import extrude_face, set_visible

#: 立方体ではなく直方体にする。3辺の長さが違えば「Zに平行な辺だけ」を選ぶ述語(test_predicates)が
#: 長さでも区別でき、添字の偶然の一致でテストが通ってしまう余地が減る。
BOX_X, BOX_Y, BOX_Z = 30.0, 30.0, 40.0

#: 押し出し量(mm)。項番5(操作をまたいで選択が残る)で文書を実際に変えるために使う。
EXTRUDE_MM = 5.0


def _box_shape(dx: float = BOX_X, dy: float = BOX_Y, dz: float = BOX_Z):
    return BRepPrimAPI_MakeBox(dx, dy, dz).Shape()


@pytest.fixture()
def doc() -> Document:
    """直方体のソリッドを1つ持つ文書(面6・辺12・頂点8)。"""
    document = Document()
    document.add_body(Body(id="box", name="box", kind="solid", shape=_box_shape()))
    return document


@pytest.fixture()
def two_bodies() -> Document:
    """ソリッドを2つ持つ文書(可視性の契約と、複数Bodyにまたがる選択のため)。"""
    document = Document()
    document.add_body(Body(id="a", name="a", kind="solid", shape=_box_shape()))
    document.add_body(Body(id="b", name="b", kind="solid", shape=_box_shape(10.0, 10.0, 10.0)))
    return document


@pytest.fixture()
def sketch_doc():
    """半径20の円を1つ持つスケッチの文書と、その sketch_id(領域はちょうど1個)。

    領域は計算されるもの(§3.3)なので、`select_region` の範囲検査を実際の領域数に
    対して働かせるには、図形のあるスケッチが要る。
    """
    document = Document()
    sketch_id = sketch_create(document, "XY")
    add_circle(document, sketch_id, (0.0, 0.0), 20.0)
    return document, sketch_id


@pytest.fixture()
def session(tmp_path: Path) -> Session:
    """STEPを読まないセッション(選択の契約は形状の中身に依存しない)。"""
    made = Session(None, 0.2)
    made.document.add_body(Body(id="box", name="box", kind="solid", shape=_box_shape()))
    return made


def _first_extrudable_face(shape) -> int:
    """押し出しに成功する最初の面の1始まり添字(添字の決め打ちを避ける。test_visibility.pyと同じ流儀)。"""
    for index in range(1, kernel.face_count(shape) + 1):
        try:
            kernel.extrude_face_on_shape(shape, index, EXTRUDE_MM)
        except Exception:  # noqa: BLE001  曲面・退化など押せない面は飛ばす
            continue
        return index
    raise AssertionError("押し出せる面が1つも無い(テストの前提が崩れている)")


# --------------------------------------------------------------------------------------
# 項番1: 面・辺・頂点・領域を選択に入れられ、種別が保持される
# --------------------------------------------------------------------------------------
def test_selection_lives_on_the_document_not_on_the_app_layer(doc: Document):
    """選択は**コアの状態**である(実装計画書§2の図で文書・操作ログと並んでいる)。

    ここがアプリ層にあると、AIは選択を読むことも書くこともできず、
    §2.1の交互操作は原理的に成立しない。**この1本がA全体の前提**。
    """
    assert isinstance(doc.selection, selection.Selection)
    assert doc.selection.items == []
    assert doc.selection.origin == selection.ORIGIN_USER


def test_each_kind_of_reference_keeps_its_kind(doc: Document):
    """面・辺・頂点の参照が、それぞれ `kind` を保持したまま選択に入る(条件3)。"""
    refs = [
        selection.select_face(doc, "box", 1),
        selection.select_edge(doc, "box", 1),
        selection.select_vertex(doc, "box", 1),
    ]
    result = selection.modify_selection(doc, add=refs)
    assert [item["kind"] for item in result.items] == ["face", "edge", "vertex"]


def test_shape_refs_have_the_shape_of_the_spec(doc: Document):
    """ShapeRef の形は §3.5 のとおり(`provenance` は day1 の空スロット)。"""
    ref = selection.select_edge(doc, "box", 3)
    assert ref == {"body": "box", "kind": "edge", "index": 3, "provenance": None}


def test_edge_and_vertex_indexes_follow_the_kernel_numbering(doc: Document):
    """辺・頂点の添字は `kernel` の数え方(1始まり)で、範囲外は ValueError。

    **添字の出どころが `kernel` でないと「選んだ辺と違う辺が丸まる」**という、
    例外も出ない事故になる(query.py の `describe_edges` の警告と同じ理由)。
    """
    shape = doc.bodies[0].shape
    assert kernel.edge_count(shape) == 12
    assert kernel.count_subshapes(shape, "vertex") == 8

    selection.select_edge(doc, "box", 12)
    selection.select_vertex(doc, "box", 8)
    with pytest.raises(ValueError):
        selection.select_edge(doc, "box", 13)
    with pytest.raises(ValueError):
        selection.select_vertex(doc, "box", 0)


def test_regions_can_be_selected_with_a_region_ref(sketch_doc):
    """領域は RegionRef(`{"sketch":.., "index":.., "provenance": None}`)で選ぶ(§3.5)。

    領域は面・辺・頂点と違い**Bodyではなくスケッチに属する**ので、参照の形が別になる。
    ここを ShapeRef に押し込むと `body` に嘘のIDが入る。

    形は M2 の `duocad.region.RegionRef.to_dict()` と**同じもの**を使う
    (別形式を組み立て直すと、操作ログの `selection` に2つの形が混ざる)。
    """
    document, sketch_id = sketch_doc
    ref = selection.select_region(document, sketch_id, 1)
    assert ref == region_ref(sketch_id, 1).to_dict()
    assert set(ref) == {"sketch", "index", "provenance"}
    assert ref["provenance"] is None


def test_selecting_a_region_validates_the_sketch_and_the_index(sketch_doc):
    """存在しないスケッチは KeyError、範囲外の領域添字は ValueError(黙って通さない)。

    領域は**計算されるもの**(§3.3)で、図形を1本足しただけで個数が変わる。
    その場で数え直して検査しないと、存在しない領域を指す参照が選択に入る。
    """
    document, sketch_id = sketch_doc
    assert len(regions(document, sketch_id)) == 1

    with pytest.raises(KeyError):
        selection.select_region(document, "no-such-sketch", 1)
    with pytest.raises(ValueError):
        selection.select_region(document, sketch_id, 2)


def test_regions_go_into_the_selection_like_any_other_kind(sketch_doc, doc: Document):
    """**領域が選択集合に実際に入る**こと(項番1の「領域」。参照を作れるだけでは足りない)。

    差分編集・重複除去・種別での絞り込みが、面や辺と同じように領域にも効くこと。
    RegionRef は `kind` キーを持たない(§3.5)ので、種別の判定は `"sketch"` キーの有無で
    導く(`selection.ref_kind`)。ここを面・辺と別扱いにすると、領域だけが
    「選べるが差分編集できない」二級市民になる。
    """
    document, sketch_id = sketch_doc
    region = selection.select_region(document, sketch_id, 1)

    result = selection.modify_selection(document, add=[region, dict(region)])
    assert len(result.items) == 1, "同じ領域を2回addしても重複しない"
    assert selection.ref_kind(result.items[0]) == selection.REGION_KIND
    assert selection.confirmed_items(document, kind=selection.REGION_KIND) == [region]

    assert selection.modify_selection(document, remove=[dict(region)]).items == []


def test_confirmed_items_rejects_an_unknown_kind(doc: Document):
    """`kind` の綴り間違いは ValueError(0件として返すとverbが黙って何もしない)。"""
    selection.modify_selection(doc, add=[selection.select_edge(doc, "box", 1)])
    with pytest.raises(ValueError):
        selection.confirmed_items(doc, kind="edges")


def test_selecting_from_a_missing_body_raises(doc: Document):
    """存在しない body_id は KeyError(黙って空振りさせない。§6予防6)。"""
    with pytest.raises(KeyError):
        selection.select_edge(doc, "nope", 1)


# --------------------------------------------------------------------------------------
# 項番2〜4: 差分編集・重複しない・空にできる
# --------------------------------------------------------------------------------------
def test_modify_selection_adds_and_removes_without_rebuilding(doc: Document):
    """`modify_selection(add=, remove=)` は**差分編集**である(条件4)。

    作り直し(毎回全部を渡し直す)しかできないと、人が1本足すだけの操作でも
    AIが作った集合を全部書き写す必要があり、交互操作の往復が現実的でなくなる。
    """
    first = [selection.select_edge(doc, "box", index) for index in (1, 2, 3)]
    selection.modify_selection(doc, add=first)

    result = selection.modify_selection(
        doc,
        add=[selection.select_edge(doc, "box", 4)],
        remove=[selection.select_edge(doc, "box", 2)],
    )
    assert [item["index"] for item in result.items] == [1, 3, 4]


def test_adding_the_same_reference_twice_does_not_duplicate(doc: Document):
    """同じ参照を2回 add しても重複しない(項番3)。

    重複すると、フィレットの半径リストと辺リストの長さがずれる・同じ辺を2回丸めるなど、
    verb 側で意味の壊れた引数になる。
    """
    first = selection.select_face(doc, "box", 1)
    second = selection.select_face(doc, "box", 2)
    selection.modify_selection(doc, add=[first, second])

    result = selection.modify_selection(doc, add=[dict(first)])
    assert len(result.items) == 2
    # **位置も動かさない**(末尾へ移し替えない)。同じものをもう一度クリックしただけで
    # 並びが変わると、fillet の辺リストと半径リストの対応が一緒にずれる。
    assert [item["index"] for item in result.items] == [1, 2]


def test_modify_selection_refuses_a_malformed_ref(doc: Document):
    """人が触る主経路でも、壊れた参照は落ちて選択を変更しない(`propose` と同じ契約)。

    ここを素通りさせると、UIの実装ミスがそのまま壊れた参照として選択に紛れ込み、
    verbに渡る段階まで発覚しない。
    """
    selection.modify_selection(doc, add=[selection.select_edge(doc, "box", 1)])
    with pytest.raises(ValueError):
        selection.modify_selection(doc, add=[{"body": "box", "index": 1}])
    with pytest.raises(KeyError):
        selection.modify_selection(
            doc, add=[{"body": "nope", "kind": "edge", "index": 1, "provenance": None}]
        )
    assert [item["index"] for item in selection.get_selection(doc).items] == [1]


def test_removing_something_that_is_not_selected_is_a_no_op(doc: Document):
    """選択に無いものの remove は空振りでよい(UIのトグルがそのまま乗る)。"""
    selection.modify_selection(doc, add=[selection.select_edge(doc, "box", 1)])
    result = selection.modify_selection(doc, remove=[selection.select_edge(doc, "box", 9)])
    assert [item["index"] for item in result.items] == [1]


def test_remove_wins_when_the_same_reference_is_added_and_removed(doc: Document):
    """同じ参照が add と remove の両方にあるとき、**remove が勝つ**(引き算を後に適用)。

    どちらが勝つかを決めておかないと、UIが「全選択してから除外」を組み立てたときの
    結果が実装依存になる。
    """
    ref = selection.select_edge(doc, "box", 5)
    result = selection.modify_selection(doc, add=[ref], remove=[dict(ref)])
    assert result.items == []


def test_clear_selection_empties_it(doc: Document):
    """`clear_selection` で空になる(項番4)。空集合の origin は "user" に戻る。"""
    selection.modify_selection(doc, add=[selection.select_face(doc, "box", 1)])
    result = selection.clear_selection(doc)
    assert result.items == []
    assert result.origin == selection.ORIGIN_USER


def test_selection_spans_multiple_bodies(two_bodies: Document):
    """選択は**Bodyをまたげる**(§3.2「選択が body_id を持つ」ことの実利)。"""
    result = selection.modify_selection(
        two_bodies,
        add=[
            selection.select_face(two_bodies, "a", 1),
            selection.select_face(two_bodies, "b", 1),
        ],
    )
    assert [item["body"] for item in result.items] == ["a", "b"]


def test_the_same_index_on_two_bodies_is_not_a_duplicate(two_bodies: Document):
    """`body` が違えば同じ添字でも別物(重複除去が `index` だけを見ていたら落ちる)。"""
    result = selection.modify_selection(
        two_bodies,
        add=[
            selection.select_edge(two_bodies, "a", 1),
            selection.select_edge(two_bodies, "b", 1),
        ],
    )
    assert len(result.items) == 2


def test_hidden_bodies_cannot_be_selected(two_bodies: Document):
    """不可視Bodyは指せない(M3.5で決めた「見えないものは選べない」の一貫性)。"""
    set_visible(two_bodies, "b", False)
    assert selection.selectable_bodies(two_bodies) == ["a"]
    with pytest.raises(ValueError):
        selection.select_face(two_bodies, "b", 1)


# --------------------------------------------------------------------------------------
# 項番5〜7: 永続する / ログに載らない / 保存されない
# --------------------------------------------------------------------------------------
def test_operating_the_raw_verb_function_does_not_touch_selection(doc: Document):
    """**選択集合そのものは永続する**(条件1「永続」)。verb関数(このモジュールの
    `extrude_face` のような、`duocad/verbs/*.py` の生の関数)は形しか触らない。

    **2026-08-20以前はこのテスト名が `test_selection_survives_an_operation` で、
    「押し出しの後も選択は残る」という製品レベルの契約を表していた。** 本人が
    「失効/後継を返す契約」を退けて「形を変えたら全リセット」へ確定したので、
    製品として見える経路(WS・MCP)は `duocad/server.py` の `Session` を必ず通り、
    `Session._reset_selection_if_needed`(`duocad/catalog.py` の `resets_selection` が正)
    が確定後に選択を空にする。**そのリセットは Session 層の関心事であって、verb関数
    自身の責務ではない** —— verb関数(`extrude_face` 等)は形状だけを差し替え、
    選択集合(コアの別のオブジェクト)には触らない、という分離をこのテストは固定する。

    製品レベルの「形を変えたら選択は空になる」契約は
    `tests/test_mcp_selection.py::test_the_whole_interleaved_lap_runs_through_mcp_alone`
    (fillet確定後に `count == 0`)と `tests/test_server_selection_reset.py` が固定する。
    """
    face_index = _first_extrudable_face(doc.bodies[0].shape)
    before = selection.modify_selection(doc, add=[selection.select_face(doc, "box", face_index)])
    kept = [dict(item) for item in before.items]

    extrude_face(doc, face_index, EXTRUDE_MM)

    assert selection.get_selection(doc).items == kept


def test_changing_the_selection_does_not_touch_the_operation_log(doc: Document):
    """**選択の変更は操作ログに載らない**(選択は文書を変更しないため。項番6)。

    `set_visible` が `Body.visible` という**文書の状態**を変えるのでログに載るのとは
    対照的である。この非対称は意図的で、undoの単位(§2「verbの粒度」)を選択が汚さない
    ようにするためでもある —— 選択し直すたびにundo1手を消費する編集は使い物にならない。
    """
    before = len(doc.log.entries)
    selection.propose_selection(doc, refs=[selection.select_face(doc, "box", 1)])
    selection.modify_selection(doc, add=[selection.select_edge(doc, "box", 1)])
    selection.commit_selection(doc)
    selection.clear_selection(doc)
    assert len(doc.log.entries) == before


def test_set_visible_still_lands_in_the_log(doc: Document):
    """対照実験: 文書の状態を変える `set_visible` は**ログに載る**。

    上のテストが「ログが増えないこと」を見ているので、そもそもこの文書でログが増える
    経路が生きていることを確かめておかないと、両方緑のまま何も検査していない状態になる。
    """
    before = len(doc.log.entries)
    set_visible(doc, "box", False)
    assert len(doc.log.entries) == before + 1


def test_selection_is_not_saved(doc: Document):
    """**選択は保存されない**(セッション状態。項番7)。保存・再生の対象は操作ログだけ。

    保存の経路は `doc.log.to_json()` しか無いので、再生した文書の選択は空になる。
    **選択が空でないまま保存し、選択のある文書へ再生しても消える**ところまで見る
    (「新しい文書の選択が空」を見るだけでは、実装が間違っていても緑になる)。

    `Operation.selection` フィールドは2026-08-20に廃止済み(値は常に運ばれない)なので、
    ここで見るのは「保存されたJSONに選択の痕跡(選んだ面の参照)が一切現れないこと」。
    """
    set_visible(doc, "box", False)  # ログに1行積んでおく(空のログを再生しても何も検査できない)
    set_visible(doc, "box", True)
    selected = selection.select_face(doc, "box", 1)
    selection.propose_selection(doc, refs=[selected])
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


# --------------------------------------------------------------------------------------
# 項番8〜11: 出所(提案と確定)
# --------------------------------------------------------------------------------------
def test_proposed_items_are_distinguishable(doc: Document):
    """`propose_selection` で入れた項目は**提案として区別できる**(項番8・条件5)。"""
    result = selection.propose_selection(doc, refs=[selection.select_face(doc, "box", 1)])
    assert result.origin == selection.ORIGIN_PROPOSED
    assert result.is_proposed() is True
    assert result.to_dict()["origin"] == selection.ORIGIN_PROPOSED


def test_the_ai_proposes_the_human_fixes_and_the_ai_reads_it_back(doc: Document):
    """**§2.1の交互操作そのもの。このファイルの核心の1本**(項番9)。

    AIが提案 → 人が過不足を直す → **AIが直した結果を読み直せる**。
    3つが同じ1つのオブジェクトを触っていなければ、この往復は閉じない。
    """
    proposed = selection.propose_selection(
        doc, refs=[selection.select_edge(doc, "box", index) for index in (1, 2, 3)]
    )
    assert [item["index"] for item in proposed.items] == [1, 2, 3]

    # 人が過不足を直す(1本足して1本引く)
    selection.modify_selection(
        doc,
        add=[selection.select_edge(doc, "box", 4)],
        remove=[selection.select_edge(doc, "box", 2)],
    )

    # AIが読み直す。**人の編集が見えており、かつ確定になっている**
    read_back = selection.get_selection(doc)
    assert [item["index"] for item in read_back.items] == [1, 3, 4]
    assert read_back.origin == selection.ORIGIN_USER


def test_commit_selection_confirms_without_changing_the_items(doc: Document):
    """`commit_selection` は確定に変えるだけで**中身は変えない**(項番10)。

    人が「そのままでよい」と承認する経路。中身が変わってしまうと、
    承認したものと実行されるものが食い違う。
    """
    refs = [selection.select_edge(doc, "box", index) for index in (1, 2)]
    selection.propose_selection(doc, refs=refs)
    committed = selection.commit_selection(doc)
    assert committed.origin == selection.ORIGIN_USER
    assert [item["index"] for item in committed.items] == [1, 2]


def test_commit_selection_is_idempotent(doc: Document):
    """既に確定しているものを commit しても失敗しない(冪等)。"""
    selection.modify_selection(doc, add=[selection.select_edge(doc, "box", 1)])
    selection.commit_selection(doc)
    assert selection.commit_selection(doc).origin == selection.ORIGIN_USER


def test_human_editing_a_proposal_confirms_it(doc: Document):
    """**決めた遷移規則**(項番11): 人が `modify_selection` で触ったら確定になる。

    人が中身を見て直すこと自体が承認だからである。ここを "proposed" のままにすると、
    人が直した集合を実行するのに**もう1回「承認」ボタンを押させる**ことになり、
    「AIが提案 → 人が直す → AIが適用」の往復に無意味な1手が増える。
    """
    selection.propose_selection(doc, refs=[selection.select_edge(doc, "box", 1)])
    result = selection.modify_selection(doc, add=[selection.select_edge(doc, "box", 2)])
    assert result.origin == selection.ORIGIN_USER


def test_a_proposal_cannot_be_fed_to_a_verb(doc: Document):
    """**決めた遷移規則**(項番11): `origin="proposed"` のままでは verb に渡せない。

    「AIが自分の提案を人の確認なしに実行してよいか」への答え。**禁止ではなく
    「黙って通さない」**(§6予防6)——AIは `commit_selection()` を明示的に呼べば実行できる。
    1手を明示させることで、提案のまま適用された操作が後から区別できなくなる事故を防ぐ。
    """
    selection.propose_selection(doc, refs=[selection.select_edge(doc, "box", 1)])
    with pytest.raises(ValueError):
        selection.confirmed_items(doc)

    selection.commit_selection(doc)
    assert len(selection.confirmed_items(doc)) == 1


def test_confirmed_items_can_be_filtered_by_kind(doc: Document):
    """`confirmed_items(kind=...)` で種別を絞れる(verbは自分が食える種別だけ要る)。"""
    selection.modify_selection(
        doc,
        add=[selection.select_face(doc, "box", 1), selection.select_edge(doc, "box", 1)],
    )
    assert len(selection.confirmed_items(doc, kind="edge")) == 1
    assert len(selection.confirmed_items(doc, kind="face")) == 1


def test_edge_indices_feed_fillet_directly(doc: Document):
    """確定した選択が `fillet(edges=[...])` へそのまま渡せる形で取り出せる。

    **ここが繋がっていないと、選択集合は「読めるだけの飾り」になる**。
    実際に丸めるところまでは A2 の検証項目(このタスクは契約まで)。
    """
    selection.modify_selection(
        doc, add=[selection.select_edge(doc, "box", index) for index in (1, 3)]
    )
    assert selection.edge_indices(doc) == [1, 3]


def test_edge_indices_refuses_to_guess_across_bodies(two_bodies: Document):
    """複数Bodyの辺が混ざったまま body_id を省略したら ValueError(どれのつもりかを黙って決めない)。"""
    selection.modify_selection(
        two_bodies,
        add=[
            selection.select_edge(two_bodies, "a", 1),
            selection.select_edge(two_bodies, "b", 1),
        ],
    )
    with pytest.raises(ValueError):
        selection.edge_indices(two_bodies)
    assert selection.edge_indices(two_bodies, body_id="a") == [1]


def test_proposing_again_replaces_the_previous_selection(doc: Document):
    """`propose_selection` は**置き換え**(差分は `modify_selection` の仕事)。

    ここが追加になっていると、AIが述語を外して提案し直すたびに古い候補が残り、
    人が消す作業を押し付けられる。
    """
    selection.propose_selection(doc, refs=[selection.select_edge(doc, "box", 1)])
    result = selection.propose_selection(doc, refs=[selection.select_edge(doc, "box", 2)])
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
    """手書きの壊れた参照も**落ちて、選択を変更しない**(項番20の参照版)。

    例外の種類は `select_*` と揃える —— 形が不正なら ValueError、存在しないBodyは KeyError。
    どこが悪いかで例外が変わらないと、MCPがAIへ返す説明を作れない。
    """
    keep = [selection.select_edge(doc, "box", 1)]
    selection.modify_selection(doc, add=keep)

    with pytest.raises(expected):
        selection.propose_selection(doc, refs=[bad])

    after = selection.get_selection(doc)
    assert [item["index"] for item in after.items] == [1]
    assert after.origin == selection.ORIGIN_USER


def test_propose_selection_needs_exactly_one_source(doc: Document):
    """`refs` と `predicate` は**どちらか一方**。両方・どちらも無しは ValueError。"""
    with pytest.raises(ValueError):
        selection.propose_selection(doc)
    with pytest.raises(ValueError):
        selection.propose_selection(
            doc,
            refs=[selection.select_edge(doc, "box", 1)],
            predicate={"kind": "edge", "where": []},
        )


# --------------------------------------------------------------------------------------
# 項番12〜14: イベント(コア→アプリ層の上り方向)
# --------------------------------------------------------------------------------------
def test_changing_the_selection_fires_a_change_event(session: Session):
    """選択が変わると**変更イベントが発火する**(項番12)。

    ログに載らないこととイベントを出さないことは別である。AIが選択を書き換えたら
    画面のハイライトは描き直されなければならない(§2「コマンドは下り、イベントは上り」)。
    """
    events = []
    session.add_listener(events.append)
    session.propose_selection(refs=[selection.select_edge(session.document, "box", 1)])
    assert len(events) == 1


def test_every_selection_api_fires_exactly_one_event(session: Session):
    """変更する4つのAPIがそれぞれ1回だけ発火し、読み取りは発火しない。

    2回発火すると、購読側(`Server._on_core_change`)が同じ状態を2回配ることになる
    (M4で「1つの操作につき1回だけ」を契約にしたのと同じ理由)。
    """
    events = []
    session.add_listener(events.append)
    ref = selection.select_edge(session.document, "box", 1)

    session.propose_selection(refs=[ref])
    session.modify_selection(add=[selection.select_edge(session.document, "box", 2)])
    session.commit_selection()
    session.clear_selection()
    assert len(events) == 4

    session.get_selection()
    assert len(events) == 4


def test_the_event_carries_enough_to_redraw(session: Session):
    """イベントは**UIが描き直すのに足る情報**を持つ(項番13)。

    「何が選択されたか」と「提案か確定か」の両方。片方だけだと、
    AIの提案を人の確定と同じ見た目で描いてしまう(承認操作が意味を失う)。
    """
    events = []
    session.add_listener(events.append)
    session.propose_selection(refs=[selection.select_edge(session.document, "box", 1)])

    payload = events[0]
    assert payload["change"] == "selection"
    assert payload["selection"]["origin"] == selection.ORIGIN_PROPOSED
    assert [item["index"] for item in payload["selection"]["items"]] == [1]
    assert payload["selection"]["count"] == 1


def test_the_event_carries_the_client_op_back(session: Session):
    """`client_op` をそのまま載せて返す(操作を出したクライアントが自分の計測と突き合わせる)。

    `set_visible` の `applied` と同じ約束。載らないと、UI側は自分が出した選択操作の
    応答なのか、他人(AI)の変更なのかを区別できない。
    """
    events = []
    session.add_listener(events.append)
    ref = selection.select_edge(session.document, "box", 1)

    assert session.propose_selection(refs=[ref], client_op=7)["client_op"] == 7
    assert events[0]["client_op"] == 7
    # 出し手のいない変更(MCP等)では None
    assert session.clear_selection()["client_op"] is None


def test_selection_events_ride_the_same_up_path_as_set_visible(session: Session):
    """**別系統を新設していない**こと(項番14)。

    `set_visible` の変更がブラウザへ届く経路は `Session._emit` → `add_listener` の
    購読者(`Server._on_core_change`)である。選択も**同じ購読者リストに乗る**ので、
    購読をやめれば両方止まる。専用のイベントバスを新設していたら、この1本が落ちる。
    """
    events = []
    session.add_listener(events.append)

    session.set_visible("box", False)
    session.set_visible("box", True)
    assert len(events) == 2

    session.propose_selection(refs=[selection.select_edge(session.document, "box", 1)])
    assert len(events) == 3

    session.remove_listener(events.append)
    session.commit_selection()
    assert len(events) == 3, "購読をやめた後も届くなら、選択は別の経路で配られている"


def test_selection_is_part_of_the_pushed_state(session: Session):
    """選択は `state_meta()` に載る —— **これが無いとAIの変更は画面へ届かない**。

    `Server._on_core_change` は **MCP起点の変更から `applied` を落として**配る
    (`applied` は操作を出したクライアントへの確認応答なので、他人の操作に付けられない)。
    したがってブラウザに届くのは `state` だけであり、`set_visible` の効果が
    `bodies[].visible` で届いているのと同じ場所に選択も載っている必要がある。

    イベントだけ実装して状態に載せないと、**AI→人の方向だけが黙って死ぬ**
    (人→AIは動くのでテストが無ければ気づけない)。
    """
    session.propose_selection(refs=[selection.select_edge(session.document, "box", 1)])
    state = session.state_meta()
    assert state["selection"]["origin"] == selection.ORIGIN_PROPOSED
    assert state["selection"]["count"] == 1


def test_the_event_does_not_pretend_to_be_a_verb(session: Session):
    """選択のイベントは verb の `applied` と**キーで区別できる**(選択はverbではない)。

    `verb` を名乗ると、アプリ層が操作ログの1行として扱ってしまう経路ができる
    (選択はログに載らないので、その行はどこにも存在しない)。
    """
    events = []
    session.add_listener(events.append)
    session.clear_selection()
    assert "verb" not in events[0]


def test_a_failing_listener_does_not_undo_the_selection(session: Session):
    """購読者が落ちても**選択の変更は成立したまま**にする(M4で決めた `_emit` の契約)。

    通知の失敗で確定した状態を巻き戻すことはできない。落ちたことは警告に残す
    (黙って握りつぶさない。§6予防6)。
    """
    def broken(_applied):
        raise RuntimeError("購読者が落ちた")

    session.add_listener(broken)
    session.propose_selection(refs=[selection.select_edge(session.document, "box", 1)])

    assert session.get_selection()["count"] == 1
    assert any("配信に失敗" in warning for warning in session.warnings)
