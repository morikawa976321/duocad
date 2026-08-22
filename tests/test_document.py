"""Document / Body のデータモデルの契約。"""
from __future__ import annotations

import pytest

from duocad.document import Body, Document, unknown_id_message


def test_document_units_cannot_be_reassigned():
    """unitsへの代入はAttributeErrorになる(セッターを持たないため固定である)。"""
    doc = Document()
    with pytest.raises(AttributeError):
        doc.units = "in"


def test_body_kind_must_be_solid_or_reference():
    """Bodyのkindは"solid"か"reference"以外を受け付けない(ValueError)。"""
    with pytest.raises(ValueError):
        Body(id="b1", name="x", kind="mesh")


def test_body_accepts_solid_and_reference_kinds():
    """kind="solid"とkind="reference"はどちらも正当なBodyとして生成できる。"""
    solid = Body(id="b1", name="solid-body", kind="solid")
    reference = Body(id="b2", name="ref-body", kind="reference")
    assert solid.kind == "solid"
    assert reference.kind == "reference"


def test_new_document_active_body_is_none():
    """ボディが1つも無いDocumentのactive_body()はNoneを返す。"""
    assert Document().active_body() is None


def test_add_body_appends_to_bodies_list_and_returns_its_id():
    """add_bodyはbodiesへ追加し、追加したBodyのidを返す(v1でUIは1つしか見せないがデータは複数ボディ対応)。"""
    doc = Document()
    body = Body(id="b1", name="first", kind="solid")
    returned_id = doc.add_body(body)
    assert returned_id == "b1"
    assert doc.bodies == [body]


def test_add_body_makes_it_the_active_body():
    """add_bodyで追加したボディは自動でactiveになる。"""
    doc = Document()
    body = Body(id="b1", name="first", kind="solid")
    doc.add_body(body)
    assert doc.active_body() is body


def test_remove_body_removes_it_from_bodies_list():
    """remove_bodyはbodiesから該当ボディを取り除く。"""
    doc = Document()
    body = Body(id="b1", name="first", kind="solid")
    doc.add_body(body)
    doc.remove_body("b1")
    assert doc.bodies == []


def test_remove_body_clears_active_body_when_it_was_active():
    """remove_bodyでactiveだったボディを取り除くと、active_body()はNoneに戻る
    (取り除いたボディを指したまま参照が残ることを防ぐ)。"""
    doc = Document()
    body = Body(id="b1", name="first", kind="solid")
    doc.add_body(body)
    assert doc.active_body() is body  # 前提: add_bodyで自動的にactiveになっている

    doc.remove_body("b1")
    assert doc.active_body() is None


def test_set_active_body_switches_the_active_body():
    """set_active_bodyでactiveなボディを明示的に切り替えられる(複数ボディ対応の要)。"""
    doc = Document()
    body1 = Body(id="b1", name="first", kind="solid")
    body2 = Body(id="b2", name="second", kind="reference")
    doc.add_body(body1)
    doc.add_body(body2)
    doc.set_active_body("b1")
    assert doc.active_body() is body1


def test_set_active_body_unknown_id_raises_key_error():
    """存在しないBodyIdをset_active_bodyに渡すとKeyErrorになる(黙って無視しない)。"""
    doc = Document()
    with pytest.raises(KeyError):
        doc.set_active_body("does-not-exist")


def test_reference_body_is_not_editable():
    """kind="reference"のボディはis_editable=Falseで、編集操作(verb)の対象にならない。"""
    doc = Document()
    ref_body = Body(id="b1", name="imported", kind="reference")
    doc.add_body(ref_body)
    assert doc.is_editable("b1") is False


def test_solid_body_is_editable():
    """kind="solid"のボディはis_editable=True。"""
    doc = Document()
    solid_body = Body(id="b1", name="sketch-result", kind="solid")
    doc.add_body(solid_body)
    assert doc.is_editable("b1") is True


def test_unknown_id_message_lists_the_valid_ids():
    """存在しないIDのエラー文は、**有効なIDをその場に並べる**(2026-08-22)。

    行き止まりのエラー(「そのIDは違う」だけ)はAIを推測へ倒す。実測でローカルLLMが
    存在しない図形IDを捏造したのを受けて、ID系のエラー文をここへ一本化した。
    """
    message = unknown_id_message("entity", "c1", ["entity-aaa", "entity-bbb"])
    assert "'c1'" in message
    assert "entity-aaa" in message and "entity-bbb" in message


def test_unknown_id_message_names_the_scope_it_searched():
    """どこを探して無かったのかを言う(スケッチ違いは図形IDが合わない普通の原因)。"""
    message = unknown_id_message("entity", "c1", [], where="sketch 'sk_2'")
    assert "sketch 'sk_2'" in message
    assert "none exist yet" in message


def test_unknown_id_message_sends_you_to_the_listing_tool_when_too_many():
    """有効IDが多すぎるときは全部並べず件数だけ言い、列挙ツールへ送る。

    エラー文がスケッチ1枚ぶんの図形IDで埋まると、それだけでAIの文脈を食う。
    並べきれないなら、次の一手は「一覧を取り直す」である。
    """
    ids = [f"entity-{index:04d}" for index in range(40)]
    message = unknown_id_message("entity", "c1", ids)
    assert "entity-0000" in message
    assert "entity-0039" not in message
    assert "20 more" in message
    assert "describe_document" in message
