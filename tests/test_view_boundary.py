"""`Session` と `DocumentView` の継ぎ目そのもの。"""
from __future__ import annotations

from document_support import second_body
from session_support import session_with_box

SIDE = 20.0


def test_the_view_and_the_session_share_one_warnings_list(tmp_path) -> None:
    """`view` が積んだ警告が `Session.take_warnings()` から出る —— **吸い出した後も**。"""
    session = session_with_box(SIDE, SIDE, SIDE, models_dir=tmp_path)
    assert session.view.warnings is session.warnings, "最初から別のリストになっている"

    session.view.warnings.append("view が積んだ警告")
    assert session.take_warnings() == ["view が積んだ警告"]
    assert session.warnings == []

    # ここが本題 —— 吸い出しでリストを束ね直していると、以後 `view` の警告は届かない。
    session.view.warnings.append("吸い出した後に view が積んだ警告")
    assert session.take_warnings() == ["吸い出した後に view が積んだ警告"]
    assert session.view.warnings is session.warnings


def test_replacing_the_document_repoints_the_view(tmp_path) -> None:
    """文書を差し替えたら `view` も新しい文書を見る(古い文書を配り続けない)。"""
    session = session_with_box(SIDE, SIDE, SIDE, models_dir=tmp_path)
    second_body(session.document)
    session.save_document("view-boundary.json")

    first = session.document
    session.new_document()
    assert session.view.document is session.document
    assert session.view.document is not first
    assert session.state_meta()["bodies"] == [], "新規文書なのに前の文書のBodyが配られている"

    session.open_document("view-boundary.json")
    assert session.view.document is session.document
    assert [body["body_id"] for body in session.state_meta()["bodies"]] != []


def test_the_view_does_not_prune_while_a_preview_is_running(tmp_path) -> None:
    """「プレビュー中か」は `Session` から引数で届く。届かないと投機的な生存集合で剪定する。"""
    session = session_with_box(SIDE, SIDE, SIDE, models_dir=tmp_path)
    created_id = second_body(session.document)
    session.geometry(created_id)
    session.edges(created_id)
    session.vertices(created_id)
    session.body_meta(session.document.get_body(created_id))

    session.undo()
    assert created_id not in [body.id for body in session.document.bodies]

    session.view.state_meta(can_undo=False, applied_count=0, previewing=True)
    assert created_id in session.view._geometry_cache, "プレビュー中なのに剪定した"
    assert created_id in session.view._edge_cache
    assert created_id in session.view._vertex_cache
    assert created_id in session.view._volume_cache

    session.view.state_meta(can_undo=False, applied_count=0, previewing=False)
    assert created_id not in session.view._geometry_cache, "プレビューが明けても剪定しない"
    assert created_id not in session.view._edge_cache
    assert created_id not in session.view._vertex_cache
    assert created_id not in session.view._volume_cache


