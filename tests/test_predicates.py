"""A1契約: 述語による選択(項番15〜20・2026-08-11)。

実装計画書§2のアーキテクチャは **「AIは述語で引数を作る / 人はポインティングで引数を作る」**
で分かれている。人のクリックに当たるのが `selection.select_face` 等(test_selection.py)、
AIの述語に当たるのがこのファイルである。§5の例は
`faces where normal·Z > 0.9` / `edges where parallel_to Z and length > 20`。

**表現形式は構造化(JSON)**にした。DSL文字列を採らない理由は `duocad/predicates.py` の
docstringが正(要点: MCPのツールはスキーマを持つので、構造化引数ならAIが書式を外したときに
その場で落ちる。文字列DSLは「パースに成功して意味が違う」＝黙って別の辺を丸める、という
最悪の失敗を作りうる)。

**このタスクでは実装を書かない(スタブのみ)。赤で終わるのが正常。**

期待値の出どころ: 30×30×40 の直方体。面6・辺12・頂点8。上面(法線 +Z)は1枚だけ、
その外周は長さ30の辺4本、Zに平行な辺は長さ40の4本。**添字を決め打ちにせず、
`kernel.tessellate` の法線と `query.describe_edges` の長さから毎回導く**
(添字の決め打ちは、分解や数え方の細部が変わるとテストが静かに別の実体を見に行く)。
"""
from __future__ import annotations

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import kernel, predicates, query, selection
from duocad.document import Body, Document
from duocad.region import region_ref
from duocad.sketch import add_circle, add_rectangle, base_plane, sketch_create
from duocad.verbs import extrude, set_visible

BOX_X, BOX_Y, BOX_Z = 30.0, 30.0, 40.0

#: 長さの比較に使う許容(mm)。STEP由来の形状も同じ述語で扱うので厳密比較にしない。
LENGTH_TOLERANCE = 1e-6


def _box_shape(dx: float = BOX_X, dy: float = BOX_Y, dz: float = BOX_Z):
    return BRepPrimAPI_MakeBox(dx, dy, dz).Shape()


@pytest.fixture()
def doc() -> Document:
    document = Document()
    document.add_body(Body(id="box", name="box", kind="solid", shape=_box_shape()))
    return document


def _top_face_index(shape) -> int:
    """法線が +Z を向く面の1始まり添字(`kernel.tessellate` の外向き法線から導く)。

    立方体の底面は `TopAbs_REVERSED` で入っており、面の向きを揃えないと底面も +Z を
    向く。`tessellate` は外向きに揃えてある(test_m2_regressions.py と同じ流儀)。
    """
    indexes = [
        face["index"] for face in kernel.tessellate(shape)["faces"] if face["normal"][2] > 0.999
    ]
    assert len(indexes) == 1, f"上向きの面は1枚のはず(実際は{len(indexes)}枚)"
    return indexes[0]


def _edges_by_index(shape) -> dict:
    return {edge["index"]: edge for edge in query.describe_edges(shape)}


def _is_parallel_to_z(edge: dict) -> bool:
    start, end = edge["start"], edge["end"]
    return (
        abs(start[0] - end[0]) < LENGTH_TOLERANCE
        and abs(start[1] - end[1]) < LENGTH_TOLERANCE
        and abs(start[2] - end[2]) > LENGTH_TOLERANCE
    )


# --------------------------------------------------------------------------------------
# 項番15: 述語で面を選べる
# --------------------------------------------------------------------------------------
def test_faces_can_be_selected_by_normal(doc: Document):
    """`faces where normal·Z > 0.9` で上向きの面が選べる(§5の例そのもの)。

    直方体で満たすのは上面のちょうど1枚。2枚返るなら法線が外向きに揃っていない
    (底面が内向きの +Z を返している)。
    """
    shape = doc.bodies[0].shape
    matched = predicates.evaluate(
        doc,
        {"kind": "face", "where": [{"op": "normal_dot_gt", "axis": "Z", "value": 0.9}]},
    )
    assert [item["index"] for item in matched] == [_top_face_index(shape)]
    assert all(item["kind"] == "face" for item in matched)


def test_face_predicates_can_combine_conditions(doc: Document):
    """`where` は **AND**(全条件を満たすものだけ)。

    ORを持ち込まないのは、論理演算子を足した時点で述語言語のパーサを作り始めることに
    なるため(`duocad/predicates.py`)。ORが要るときは2回評価して `modify_selection` で足す。
    """
    upward = {"op": "normal_dot_gt", "axis": "Z", "value": 0.9}
    huge = {"op": "area_gt", "value": BOX_X * BOX_Y * 2}
    assert len(predicates.evaluate(doc, {"kind": "face", "where": [upward]})) == 1
    assert predicates.evaluate(doc, {"kind": "face", "where": [upward, huge]}) == []


def test_kind_is_actually_filters_by_the_classified_kind(doc: Document):
    """`kind_is` が**分類ラベルで実際に絞れる**こと(A2のQAが指摘した穴)。

    `validate` と `describe_ops` にopが載っていることだけを見ていると、
    評価側が `kernel.classify_face` / `classify_edge` と違うラベルを見ていても気づけない。
    直方体は6面とも平面・12辺とも直線なので、両方向を1本で固定できる。
    """
    shape = doc.bodies[0].shape
    planes = predicates.evaluate(
        doc, {"kind": "face", "where": [{"op": "kind_is", "value": "plane"}]}
    )
    assert len(planes) == kernel.face_count(shape) == 6

    lines = predicates.evaluate(
        doc, {"kind": "edge", "where": [{"op": "kind_is", "value": "line"}]}
    )
    assert len(lines) == kernel.edge_count(shape) == 12
    assert predicates.evaluate(
        doc, {"kind": "edge", "where": [{"op": "kind_is", "value": "circle"}]}
    ) == []


def test_an_empty_where_matches_everything_of_that_kind(doc: Document):
    """`where: []` はその種別の全部(「全面を選ぶ」を特別扱いしないで書けるように)。"""
    matched = predicates.evaluate(doc, {"kind": "face", "where": []})
    assert len(matched) == kernel.face_count(doc.bodies[0].shape) == 6


# --------------------------------------------------------------------------------------
# 項番16: 述語で辺を選べる
# --------------------------------------------------------------------------------------
def test_edges_can_be_selected_by_direction_and_length(doc: Document):
    """`edges where parallel_to Z and length > 20` で辺が選べる(§5の例そのもの)。

    30×30×40 の直方体なら、Zに平行な辺4本(長さ40)だけが該当する。
    X・Y方向の辺も長さ30で「20超」は満たすので、**向きの条件が効いていなければ12本返る**。
    """
    shape = doc.bodies[0].shape
    matched = predicates.evaluate(
        doc,
        {
            "kind": "edge",
            "where": [{"op": "parallel_to", "axis": "Z"}, {"op": "length_gt", "value": 20}],
        },
    )
    edges = _edges_by_index(shape)
    assert len(matched) == 4
    for item in matched:
        assert item["kind"] == "edge"
        assert _is_parallel_to_z(edges[item["index"]])
        assert abs(edges[item["index"]]["length"] - BOX_Z) < 1e-6


def test_length_conditions_are_strict_comparisons(doc: Document):
    """`length_gt` / `length_lt` は**厳密な不等号**(境界を含まない)。

    「長さ30の辺を length_gt 30 で選ぶ」が通ってしまうと、寸法をそのまま書いた述語が
    意図せず余分な辺を拾う。境界を含めたい書き方が要るなら別のopとして足すこと。
    """
    long_edges = predicates.evaluate(
        doc, {"kind": "edge", "where": [{"op": "length_gt", "value": BOX_X}]}
    )
    assert len(long_edges) == 4  # 長さ40の4本だけ(長さ30の8本は含まない)

    short_edges = predicates.evaluate(
        doc, {"kind": "edge", "where": [{"op": "length_lt", "value": BOX_Z}]}
    )
    assert len(short_edges) == 8


def test_results_are_ordered_by_body_then_index(doc: Document):
    """順序は「`doc.bodies` の順 × 添字の昇順」で安定する。

    順序が実装依存だと、UIの一覧が操作のたびに並び替わり、
    `fillet` の半径リストと辺リストの対応も再現しなくなる。
    """
    matched = predicates.evaluate(doc, {"kind": "edge", "where": []})
    assert [item["index"] for item in matched] == sorted(item["index"] for item in matched)


def test_results_group_by_body_in_document_order():
    """複数Bodyでも「`doc.bodies` の順 → その中で添字昇順」になる。

    1Bodyだけで見ていると**Body順の部分は壊れても落ちない**(QA指摘)。
    Bodyをまたいで添字が交互に出ると、UIの一覧が部品ごとにまとまらない。
    """
    document = Document()
    document.add_body(Body(id="a", name="a", kind="solid", shape=_box_shape()))
    document.add_body(Body(id="b", name="b", kind="solid", shape=_box_shape()))
    matched = predicates.evaluate(document, {"kind": "edge", "where": []})

    assert [item["body"] for item in matched] == ["a"] * 12 + ["b"] * 12
    for body_id in ("a", "b"):
        indexes = [item["index"] for item in matched if item["body"] == body_id]
        assert indexes == sorted(indexes)


# --------------------------------------------------------------------------------------
# 項番17: 関係述語 —— ある面の外周の辺
# --------------------------------------------------------------------------------------
def test_edges_on_the_boundary_of_a_face_can_be_selected(doc: Document):
    """**関係述語**「ある面の外周の辺」(項番17)。

    フィレットの実用(**「上面の外周を丸める」**)に直結する。これが無いと、
    その操作は辺の添字を決め直して並べる以外に表現できず、形が変わるたびに壊れる。
    """
    shape = doc.bodies[0].shape
    top = _top_face_index(shape)
    matched = predicates.evaluate(
        doc, {"kind": "edge", "where": [{"op": "on_boundary_of_face", "face": top}]}
    )
    edges = _edges_by_index(shape)
    assert len(matched) == 4
    for item in matched:
        edge = edges[item["index"]]
        assert abs(edge["length"] - BOX_X) < 1e-6
        # **上面の外周であることまで固定する**。件数と長さだけだと底面の外周でも緑になり、
        # 「どの面の外周か」を無視した実装が通ってしまう(直方体は上下が合同なため)。
        assert abs(edge["start"][2] - BOX_Z) < 1e-6
        assert abs(edge["end"][2] - BOX_Z) < 1e-6


def test_the_boundary_of_the_bottom_face_is_a_different_set(doc: Document):
    """対照: 底面を指せば**別の4本**が返る(面の指定が実際に効いていること)。"""
    shape = doc.bodies[0].shape
    edges = _edges_by_index(shape)
    bottom = [
        face["index"] for face in kernel.tessellate(shape)["faces"] if face["normal"][2] < -0.999
    ]
    assert len(bottom) == 1

    top_matched = predicates.evaluate(
        doc,
        {"kind": "edge", "where": [{"op": "on_boundary_of_face", "face": _top_face_index(shape)}]},
    )
    bottom_matched = predicates.evaluate(
        doc, {"kind": "edge", "where": [{"op": "on_boundary_of_face", "face": bottom[0]}]}
    )
    assert len(bottom_matched) == 4
    assert {item["index"] for item in top_matched}.isdisjoint(
        {item["index"] for item in bottom_matched}
    )
    for item in bottom_matched:
        assert abs(edges[item["index"]]["start"][2]) < 1e-6


def test_the_boundary_of_a_face_is_ready_for_fillet(doc: Document):
    """「上面の外周を丸める」が **述語 → 選択 → verbの引数** で1本に繋がること。

    A1はここまで(契約)。実際に丸まるところまでは A2 の検証項目。
    """
    top = _top_face_index(doc.bodies[0].shape)
    selection.propose_selection(
        doc,
        predicate={"kind": "edge", "where": [{"op": "on_boundary_of_face", "face": top}]},
    )
    selection.commit_selection(doc)
    assert len(selection.edge_indices(doc, body_id="box")) == 4


def test_the_boundary_excludes_the_rim_of_a_hole():
    """**穴の縁は外周に含めない**(項番17の契約の本体。QAが指摘した穴)。

    穴無しの直方体だけで検査していると、面の**全ワイヤ**を返す誤実装でも
    「件数4・長さ30・Z=40」で緑になってしまう。貫通穴を1つ開けた箱の上面は
    外周4本＋穴の縁1本(円)を持つので、ここで初めて両者が区別できる。

    実用上の意味: 「上面の外周を丸める」つもりの操作が、**黙って穴の縁まで丸める**
    (穴の縁を丸めるとトーラスになる。`test_fillet.py` 項番3で実測済み)。

    素材は duocadのverbだけで作る(生のOCPで組み立てない。`test_fillet.py` と同じ流儀)。
    """
    document = Document()
    size, hole_radius = 20.0, 4.0
    sketch_id = sketch_create(document, base_plane("XY"))
    add_rectangle(document, sketch_id, (0.0, 0.0), size, size)
    body_id = extrude(document, [region_ref(sketch_id, 1)], size, mode="join").args["body_id"]

    hole_sketch = sketch_create(document, base_plane("XY"))
    add_circle(document, hole_sketch, (size / 2, size / 2), hole_radius)
    extrude(document, [region_ref(hole_sketch, 1)], size, mode="cut", body_id=body_id)

    shape = next(body for body in document.bodies if body.id == body_id).shape
    top = [
        face["index"] for face in kernel.tessellate(shape)["faces"] if face["normal"][2] > 0.999
    ]
    assert len(top) == 1, "上面は1枚(穴を開けても増えない)"

    edges = _edges_by_index(shape)
    circular = [index for index, edge in edges.items() if edge["kind"] == "circle"]
    assert circular, "検査の前提: 穴の縁(円い辺)が実在すること"

    matched = predicates.evaluate(
        document, {"kind": "edge", "where": [{"op": "on_boundary_of_face", "face": top[0]}]}
    )
    indexes = {item["index"] for item in matched}
    assert len(indexes) == 4, "外周は4本(穴の縁を足して5本になっていない)"
    assert indexes.isdisjoint(circular), "穴の縁が外周に混ざっている"
    for index in indexes:
        assert abs(edges[index]["length"] - size) < 1e-6


def test_relation_predicate_rejects_a_face_index_out_of_range(doc: Document):
    """存在しない面を指した関係述語は ValueError(黙って0件にしない)。"""
    with pytest.raises(ValueError):
        predicates.evaluate(
            doc, {"kind": "edge", "where": [{"op": "on_boundary_of_face", "face": 99}]}
        )


def test_the_relation_predicate_refuses_to_guess_which_body():
    """`face` は**Body局所の添字**なので、可視Bodyが複数あるなら `body` の指定が要る。

    「全Bodyの3番目の面」という解釈は取らない —— 部品ごとに面の数え方は無関係なので、
    その集合には意味が無い。可視Bodyがちょうど1つなら省略してよい(上のテスト群がそれ)。
    """
    document = Document()
    document.add_body(Body(id="a", name="a", kind="solid", shape=_box_shape()))
    document.add_body(Body(id="b", name="b", kind="solid", shape=_box_shape()))
    predicate = {"kind": "edge", "where": [{"op": "on_boundary_of_face", "face": 1}]}

    with pytest.raises(ValueError):
        predicates.evaluate(document, predicate)

    scoped = predicates.evaluate(document, {**predicate, "body": "a"})
    assert {item["body"] for item in scoped} == {"a"}


# --------------------------------------------------------------------------------------
# 項番18: 不可視のBodyは述語の対象にならない
# --------------------------------------------------------------------------------------
def test_hidden_bodies_are_not_scanned_by_predicates():
    """不可視Bodyは述語の対象にならない(M3.5「見えないものは選べない」の一貫性)。

    ここが漏れると、**画面に出ていない部品がAIの述語経由で黙って変形する**。
    可視性をアプリ層のチェックボックスにしなかった理由そのもの(`duocad/selection.py`)。
    """
    document = Document()
    document.add_body(Body(id="a", name="a", kind="solid", shape=_box_shape()))
    document.add_body(Body(id="b", name="b", kind="solid", shape=_box_shape()))

    both = predicates.evaluate(document, {"kind": "edge", "where": []})
    assert {item["body"] for item in both} == {"a", "b"}

    set_visible(document, "b", False)
    visible_only = predicates.evaluate(document, {"kind": "edge", "where": []})
    assert {item["body"] for item in visible_only} == {"a"}


def test_naming_a_hidden_body_explicitly_is_an_error():
    """`body` を明示して不可視Bodyを指したら ValueError(黙って0件にしない)。

    「指定したのに何も返らない」と「見えないので対象外」は、AIから見て区別が付かない。
    """
    document = Document()
    document.add_body(Body(id="a", name="a", kind="solid", shape=_box_shape()))
    set_visible(document, "a", False)
    with pytest.raises(ValueError):
        predicates.evaluate(document, {"kind": "edge", "where": [], "body": "a"})


def test_the_body_key_scopes_the_search():
    """`body` を明示すると、そのBodyだけを走査する。"""
    document = Document()
    document.add_body(Body(id="a", name="a", kind="solid", shape=_box_shape()))
    document.add_body(Body(id="b", name="b", kind="solid", shape=_box_shape()))
    matched = predicates.evaluate(document, {"kind": "edge", "where": [], "body": "b"})
    assert {item["body"] for item in matched} == {"b"}


# --------------------------------------------------------------------------------------
# 項番19: 一致0件はエラーにしない(ただし件数が分かる)
# --------------------------------------------------------------------------------------
def test_zero_matches_is_not_an_error(doc: Document):
    """一致0件は**エラーではない**。空のリストを返す(項番19)。

    例外にすると、AIは「述語が不正だった」のか「条件に合う辺が無かった」のかを
    区別できず、直しようがない。
    """
    assert predicates.evaluate(
        doc, {"kind": "edge", "where": [{"op": "length_gt", "value": 10_000}]}
    ) == []


def test_the_caller_can_see_the_match_count(doc: Document):
    """**件数が呼び出し元に分かる**こと(項番19)。AIが述語を外したのに気づけるように。

    0件の提案を「成功」としか報告しないと、交互操作は人が画面を見るまで黙って止まる。
    """
    result = selection.propose_selection(
        doc, predicate={"kind": "edge", "where": [{"op": "length_gt", "value": 10_000}]}
    )
    assert result.to_dict()["count"] == 0
    assert result.items == []


def test_a_zero_match_proposal_still_replaces_the_previous_selection(doc: Document):
    """0件の提案でも置き換えは起きる(古い候補が残らない)。

    ここで「0件なら何もしない」にすると、外した述語のあとに古い選択が残り、
    人が承認したつもりで**別の集合**にverbがかかる。
    """
    selection.propose_selection(doc, refs=[selection.select_edge(doc, "box", 1)])
    result = selection.propose_selection(
        doc, predicate={"kind": "edge", "where": [{"op": "length_gt", "value": 10_000}]}
    )
    assert result.items == []


# --------------------------------------------------------------------------------------
# 項番20: 不正な述語は失敗し、選択は変更されない
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad, why",
    [
        ({"kind": "edge", "where": [{"op": "no_such_op"}]}, "未知のop"),
        ({"kind": "edge", "where": [{"op": "length_gt", "value": "20"}]}, "型違い(文字列)"),
        ({"kind": "edge", "where": [{"op": "length_gt"}]}, "必須の引数が無い"),
        ({"kind": "edge", "where": [{"op": "normal_dot_gt", "axis": "Z", "value": 0.9}]},
         "面のopを辺に使っている"),
        ({"kind": "solid", "where": []}, "未知のkind"),
        ({"where": []}, "kindが無い"),
        ({"kind": "edge"}, "whereが無い"),
        ({"kind": "edge", "where": {"op": "length_gt", "value": 20}}, "whereがリストでない"),
        ({"kind": "edge", "where": [{"op": "parallel_to", "axis": "W"}]}, "未知の軸"),
    ],
)
def test_invalid_predicates_are_rejected(doc: Document, bad: dict, why: str):
    """不正な述語は ValueError(未知のop・型違い・kindに無いop・未知の軸)。

    黙って空集合を返すと、**0件一致と区別が付かない**(項番19との対)。
    """
    with pytest.raises(ValueError):
        predicates.validate(bad)
    with pytest.raises(ValueError):
        predicates.evaluate(doc, bad)


def test_an_invalid_predicate_leaves_the_selection_untouched(doc: Document):
    """不正な述語で `propose_selection` が落ちても、**選択は変更されない**(項番20)。

    ここが守られていないと、AIが述語を打ち間違えるたびに人の選択が消える。
    実装は「全条件を検査してから評価する」順序でこれを守ること
    (途中まで評価して落ちる作りだと、守れるかどうかが実装の偶然になる)。
    """
    selection.modify_selection(
        doc, add=[selection.select_edge(doc, "box", index) for index in (1, 2)]
    )
    before = [dict(item) for item in selection.get_selection(doc).items]

    with pytest.raises(ValueError):
        selection.propose_selection(doc, predicate={"kind": "edge", "where": [{"op": "nope"}]})

    after = selection.get_selection(doc)
    assert [dict(item) for item in after.items] == before
    assert after.origin == selection.ORIGIN_USER


def test_a_predicate_that_fails_at_evaluation_also_leaves_the_selection_untouched(doc: Document):
    """**`validate` を通ってから落ちる述語**でも選択は変わらない(A2のQAが指摘した穴)。

    上のテストは `validate` 段階で落ちる例しか見ていない。`on_boundary_of_face` の
    範囲外は形式としては正しく、**実際に形状を見るまで分からない**ので、
    「全条件を検査してから評価する」が効いているかはこちらでしか確かめられない。
    """
    selection.modify_selection(doc, add=[selection.select_edge(doc, "box", 1)])
    before = [dict(item) for item in selection.get_selection(doc).items]

    bad = {"kind": "edge", "where": [{"op": "on_boundary_of_face", "face": 99}]}
    predicates.validate(bad)  # 形式としては正しい(ここでは落ちない)
    with pytest.raises(ValueError):
        selection.propose_selection(doc, predicate=bad)

    after = selection.get_selection(doc)
    assert [dict(item) for item in after.items] == before
    assert after.origin == selection.ORIGIN_USER


def test_validate_returns_a_normalized_copy(doc: Document):
    """`validate` は正規化した写しを返し、**渡した辞書を書き換えない**。

    呼び出し元(MCPのツール引数)の辞書を書き換えると、AIが送った内容と
    ログに残る内容が食い違う。
    """
    original = {"kind": "edge", "where": [{"op": "length_gt", "value": 20}]}
    snapshot = {"kind": "edge", "where": [{"op": "length_gt", "value": 20}]}
    normalized = predicates.validate(original)
    assert original == snapshot
    assert normalized["kind"] == "edge"


def test_the_op_catalogue_covers_every_supported_op():
    """`describe_ops()` が **opの唯一の正**であること(F1のスキーマがここから作られる)。

    バリデータと公開スキーマが二重管理になると、片方にだけ足したopが
    「MCPからは呼べるが検証を素通りする」状態で入る。
    """
    catalogue = predicates.describe_ops()
    assert set(catalogue) == set(predicates.PREDICATE_KINDS)
    assert {entry["op"] for entry in catalogue["face"]} == set(predicates.FACE_OPS)
    assert {entry["op"] for entry in catalogue["edge"]} == set(predicates.EDGE_OPS)
