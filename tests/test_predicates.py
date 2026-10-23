"""述語による選択の契約。"""
from __future__ import annotations

import pytest

from duocad import kernel, predicates, query, selection
from duocad.document import Body, Document
from duocad.region import region_ref
from duocad.sketch import add_circle, add_rectangle, base_plane, sketch_create
from duocad.selection import set_visible
from duocad.verbs import extrude
from document_support import BOX_X, BOX_Y, BOX_Z, box_shape as _box_shape, shape_ref


#: 長さの比較に使う許容(mm)。STEP由来の形状も同じ述語で扱うので厳密比較にしない。
LENGTH_TOLERANCE = 1e-6


def _top_face_index(shape) -> int:
    """法線が +Z を向く面の1始まり添字(`kernel.tessellate` の外向き法線から導く)。"""
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


def test_faces_can_be_selected_by_normal(doc: Document):
    """`faces where normal·Z > 0.9` で上向きの面が選べる(§5の例そのもの)。"""
    shape = doc.bodies[0].shape
    matched = predicates.evaluate(
        doc,
        {"kind": "face", "where": [{"op": "normal_dot_gt", "axis": "Z", "value": 0.9}]},
    )
    assert [item["index"] for item in matched] == [_top_face_index(shape)]
    assert all(item["kind"] == "face" for item in matched)


def test_face_predicates_can_combine_conditions(doc: Document):
    """`where` は **AND**(全条件を満たすものだけ)。ORが要るときは2回評価して
    `modify_selection` で足す(理由は `duocad/predicates.py` のdocstring)。
    """
    upward = {"op": "normal_dot_gt", "axis": "Z", "value": 0.9}
    huge = {"op": "area_gt", "value": BOX_X * BOX_Y * 2}
    assert len(predicates.evaluate(doc, {"kind": "face", "where": [upward]})) == 1
    assert predicates.evaluate(doc, {"kind": "face", "where": [upward, huge]}) == []


def test_kind_is_actually_filters_by_the_classified_kind(doc: Document):
    """`kind_is` が**分類ラベルで実際に絞れる**こと(`validate` / `describe_ops` に
    opが載っていることだけを見ていると、評価側が `kernel.classify_face` /
    `classify_edge` と違うラベルを見ていても気づけない)。
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


def test_kind_and_area_conditions_tell_a_cylinder_from_its_caps():
    """箱は平面と直線しか持たないので `kind_is` の非該当も `area_gt` の該当も見えない。円柱(側面 1885 / 底 314 mm²)で見る。"""
    document = Document()
    sketch_id = sketch_create(document, base_plane("XY"))
    add_circle(document, sketch_id, (0.0, 0.0), 20.0)
    extrude(document, [region_ref(sketch_id, 1)], 30.0, mode="join")

    def faces(*where):
        return predicates.evaluate(document, {"kind": "face", "where": list(where)})

    def edges(*where):
        return predicates.evaluate(document, {"kind": "edge", "where": list(where)})

    assert len(faces({"op": "kind_is", "value": "plane"})) == 2
    assert len(faces({"op": "kind_is", "value": "cylinder_full"})) == 1
    assert len(faces({"op": "area_gt", "value": 1000})) == 1
    assert len(faces({"op": "area_gt", "value": 100})) == 3
    assert faces({"op": "area_gt", "value": 2000}) == []
    assert len(edges({"op": "kind_is", "value": "circle"})) == 2
    assert len(edges({"op": "kind_is", "value": "line"})) == 1


def test_a_vector_axis_is_normalized_before_the_dot_product(doc: Document):
    """軸ベクトルの長さは結果に効かない(箱の +X・+Z 面の法線と斜め軸 [1,0,1] の内積は 0.707)。"""

    def upward(axis, value):
        return predicates.evaluate(
            doc, {"kind": "face", "where": [{"op": "normal_dot_gt", "axis": axis, "value": value}]}
        )

    assert len(upward([1, 0, 1], 0.7)) == 2
    assert upward([1, 0, 1], 0.8) == []
    assert upward([0, 0, 5], 1.5) == []
    assert len(upward([0, 0, 5], 0.9)) == 1


def test_an_empty_where_matches_everything_of_that_kind(doc: Document):
    """`where: []` はその種別の全部(「全面を選ぶ」を特別扱いしないで書けるように)。"""
    matched = predicates.evaluate(doc, {"kind": "face", "where": []})
    assert len(matched) == kernel.face_count(doc.bodies[0].shape) == 6


def test_edges_can_be_selected_by_direction_and_length(doc: Document):
    """`edges where parallel_to Z and length > 20` で辺が選べる(§5の例そのもの)。"""
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
    """`length_gt` / `length_lt` は**厳密な不等号**(境界を含まない) —— 「長さ30の辺を
    length_gt 30 で選ぶ」が通ると、寸法をそのまま書いた述語が意図せず余分な辺を拾う。
    """
    long_edges = predicates.evaluate(
        doc, {"kind": "edge", "where": [{"op": "length_gt", "value": BOX_X}]}
    )
    assert len(long_edges) == 4  # 長さ40の4本だけ(長さ30の8本は含まない)

    short_edges = predicates.evaluate(
        doc, {"kind": "edge", "where": [{"op": "length_lt", "value": BOX_Z}]}
    )
    assert len(short_edges) == 8


def test_results_group_by_body_in_document_order():
    """複数Bodyでも「`doc.bodies` の順 → その中で添字昇順」になる。"""
    document = Document()
    document.add_body(Body(id="a", name="a", kind="solid", shape=_box_shape()))
    document.add_body(Body(id="b", name="b", kind="solid", shape=_box_shape()))
    matched = predicates.evaluate(document, {"kind": "edge", "where": []})

    assert [item["body"] for item in matched] == ["a"] * 12 + ["b"] * 12
    for body_id in ("a", "b"):
        indexes = [item["index"] for item in matched if item["body"] == body_id]
        assert indexes == sorted(indexes)


def test_edges_on_the_boundary_of_a_face_can_be_selected(doc: Document):
    """**関係述語**「ある面の外周の辺」。"""
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


def test_the_boundary_excludes_the_rim_of_a_hole():
    """**穴の縁は外周に含めない**(この述語の契約の本体)。"""
    document = Document()
    size, hole_radius = 20.0, 4.0
    sketch_id = sketch_create(document, base_plane("XY"))
    add_rectangle(document, sketch_id, (0.0, 0.0), size, size)
    body_id = extrude(document, [region_ref(sketch_id, 1)], size, mode="join").args["body_id"]

    hole_sketch = sketch_create(document, base_plane("XY"))
    add_circle(document, hole_sketch, (size / 2, size / 2), 2 * hole_radius)
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
    """`face` は**Body局所の添字**なので、可視Bodyが複数あるなら `body` の指定が要る。"""
    document = Document()
    document.add_body(Body(id="a", name="a", kind="solid", shape=_box_shape()))
    document.add_body(Body(id="b", name="b", kind="solid", shape=_box_shape()))
    predicate = {"kind": "edge", "where": [{"op": "on_boundary_of_face", "face": 1}]}

    with pytest.raises(ValueError):
        predicates.evaluate(document, predicate)

    scoped = predicates.evaluate(document, {**predicate, "body": "a"})
    assert {item["body"] for item in scoped} == {"a"}


def test_hidden_bodies_are_not_scanned_by_predicates():
    """不可視Bodyは述語の対象にならない(M3.5「見えないものは選べない」の一貫性)。"""
    document = Document()
    document.add_body(Body(id="a", name="a", kind="solid", shape=_box_shape()))
    document.add_body(Body(id="b", name="b", kind="solid", shape=_box_shape()))

    both = predicates.evaluate(document, {"kind": "edge", "where": []})
    assert {item["body"] for item in both} == {"a", "b"}

    set_visible(document, "b", False)
    visible_only = predicates.evaluate(document, {"kind": "edge", "where": []})
    assert {item["body"] for item in visible_only} == {"a"}


def test_naming_a_hidden_body_explicitly_is_an_error():
    """`body` を明示して不可視Bodyを指したら ValueError(黙って0件にしない)。"""
    document = Document()
    document.add_body(Body(id="a", name="a", kind="solid", shape=_box_shape()))
    set_visible(document, "a", False)
    with pytest.raises(ValueError):
        predicates.evaluate(document, {"kind": "edge", "where": [], "body": "a"})


def test_a_zero_match_proposal_still_replaces_the_previous_selection(doc: Document):
    """0件の提案でも置き換えは起きる(古い候補が残らない)。"""
    selection.set_selection(doc, refs=[shape_ref(doc, "box", "edge", 1)])
    result = selection.set_selection(
        doc, predicate={"kind": "edge", "where": [{"op": "length_gt", "value": 10_000}]}
    )
    assert result.items == []


@pytest.mark.parametrize(
    "bad, why",
    [
        # 「存在しないop」は下の「面のopを辺に使っている」と同じ枝に落ちるので置いていない
        # (判別力は下の方が広い —— kindスコープを外す退行を、実在するopの方だけが検出できる)。
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
    """不正な述語は ValueError(未知のop・型違い・kindに無いop・未知の軸)。"""
    with pytest.raises(ValueError):
        predicates.validate(bad)
    with pytest.raises(ValueError):
        predicates.evaluate(doc, bad)


def test_an_invalid_predicate_leaves_the_selection_untouched(doc: Document):
    """不正な述語で `set_selection` が落ちても、**選択は変更されない**。

    ここが守られていないと、AIが述語を打ち間違えるたびに人の選択が消える。
    実装は「全条件を検査してから評価する」順序でこれを守ること
    (途中まで評価して落ちる作りだと、守れるかどうかが実装の偶然になる)。
    """
    selection.modify_selection(
        doc, add=[shape_ref(doc, "box", "edge", index) for index in (1, 2)]
    )
    before = [dict(item) for item in selection.get_selection(doc).items]

    with pytest.raises(ValueError):
        selection.set_selection(doc, predicate={"kind": "edge", "where": [{"op": "nope"}]})

    after = selection.get_selection(doc)
    assert [dict(item) for item in after.items] == before


def test_a_predicate_that_fails_at_evaluation_also_leaves_the_selection_untouched(doc: Document):
    """**`validate` を通ってから落ちる述語**でも選択は変わらない。

    上のテストは `validate` 段階で落ちる例しか見ていない。`on_boundary_of_face` の
    範囲外は形式としては正しく、**実際に形状を見るまで分からない**ので、
    「全条件を検査してから評価する」が効いているかはこちらでしか確かめられない。
    """
    selection.modify_selection(doc, add=[shape_ref(doc, "box", "edge", 1)])
    before = [dict(item) for item in selection.get_selection(doc).items]

    bad = {"kind": "edge", "where": [{"op": "on_boundary_of_face", "face": 99}]}
    predicates.validate(bad)  # 形式としては正しい(ここでは落ちない)
    with pytest.raises(ValueError):
        selection.set_selection(doc, predicate=bad)

    after = selection.get_selection(doc)
    assert [dict(item) for item in after.items] == before


def test_validate_returns_a_normalized_copy(doc: Document):
    """`validate` は正規化した写しを返し、**渡した辞書を書き換えない**。"""
    original = {"kind": "edge", "where": [{"op": "length_gt", "value": 20}]}
    snapshot = {"kind": "edge", "where": [{"op": "length_gt", "value": 20}]}
    normalized = predicates.validate(original)
    assert original == snapshot
    assert normalized["kind"] == "edge"


def test_the_op_catalogue_covers_every_supported_op():
    """`describe_ops()` が **opの唯一の正**であること(MCPのスキーマがここから作られる)。"""
    catalogue = predicates.describe_ops()
    assert set(catalogue) == set(predicates.PREDICATE_KINDS)
    assert {entry["op"] for entry in catalogue["face"]} == set(predicates.FACE_OPS)
    assert {entry["op"] for entry in catalogue["edge"]} == set(predicates.EDGE_OPS)


def test_parallel_to_selects_straight_edges_only_not_arcs_whose_chord_is_parallel(session):
    """落ちたら、半円・アーチの弦が軸に揃うだけで曲線の辺まで「軸に平行な辺」として選ばれ、そのまま fillet/chamfer に渡る。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [0.0, 1.0], 10.0, 2.0)
    session.modify_selection([{"sketch": sketch_id, "index": 1}], [])
    session.revolve(axis="x", angle=180.0)
    document = session.document
    shape = document.bodies[-1].shape

    chord_parallel_arcs = [
        edge["index"]
        for edge in query.describe_edges(shape)
        if edge["kind"] != "line" and edge["length"] > 0
        and abs(edge["end"][0] - edge["start"][0]) < 1e-6 and abs(edge["end"][2] - edge["start"][2]) < 1e-6
        and abs(edge["end"][1] - edge["start"][1]) > 1e-6
    ]
    assert chord_parallel_arcs, "検査の前提: 弦が Y に平行な円弧がある"

    picked = predicates.evaluate(document, {"kind": "edge", "where": [{"op": "parallel_to", "axis": "Y"}]})

    kinds = {edge["index"]: edge["kind"] for edge in query.describe_edges(shape)}
    assert picked and {kinds[ref["index"]] for ref in picked} == {"line"}
    assert not set(chord_parallel_arcs) & {ref["index"] for ref in picked}


@pytest.mark.parametrize(
    "axis",
    [[float("inf"), 0.0, 0.0], [float("nan"), 0.0, 0.0], ["1", "0", "0"], [True, 0, 0], [0, 0, 0]],
)
def test_a_degenerate_or_non_numeric_axis_vector_is_a_value_error(axis):
    """落ちたら、無限大は NaN の軸で成功(0件)になり、文字列・真偽値は数として通る。"""
    with pytest.raises(ValueError):
        predicates.validate({"kind": "edge", "where": [{"op": "parallel_to", "axis": axis}]})


def test_a_large_but_finite_axis_vector_is_normalized():
    """落ちたら、成分の2乗が溢れて OverflowError になる(MCP では internal で返り、AI が直せない)。"""
    normalized = predicates.validate({"kind": "edge", "where": [{"op": "parallel_to", "axis": [1e308, 1e308, 0.0]}]})
    assert normalized["where"][0]["axis"] == pytest.approx([2 ** -0.5, 2 ** -0.5, 0.0])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "3"])
def test_a_threshold_that_is_not_a_finite_number_is_a_value_error(value):
    with pytest.raises(ValueError):
        predicates.validate({"kind": "edge", "where": [{"op": "length_gt", "value": value}]})
