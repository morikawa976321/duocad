"""Document / Body のデータモデルの契約。"""
from __future__ import annotations

import pytest

from duocad.document import Body, Document, unknown_id_message


def test_an_unknown_body_kind_is_refused():
    """落ちたら、知らない kind(撤去した "reference" 等)のBodyが作れてしまう。"""
    with pytest.raises(ValueError, match="kind must be one of"):
        Body(id="b1", name="x", kind="reference")


def test_remove_body_clears_active_body_when_it_was_active():
    """remove_bodyでactiveだったボディを取り除くと、active_body()はNoneに戻る
    (取り除いたボディを指したまま参照が残ることを防ぐ)。"""
    doc = Document()
    body = Body(id="b1", name="first", kind="solid")
    doc.add_body(body)
    assert doc.active_body() is body  # 前提: add_bodyで自動的にactiveになっている

    doc.remove_body("b1")
    assert doc.active_body() is None


def test_set_active_body_unknown_id_raises_key_error():
    """存在しないBodyIdをset_active_bodyに渡すとKeyErrorになる(黙って無視しない)。"""
    doc = Document()
    with pytest.raises(KeyError):
        doc.set_active_body("does-not-exist")


def test_unknown_id_message_lists_the_valid_ids():
    """存在しないIDのエラー文は、**有効なIDをその場に並べる**。

    行き止まりのエラー(「そのIDは違う」だけ)は、呼び出し元のAIを存在しない図形IDの
    捏造へ倒しかねない。
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
