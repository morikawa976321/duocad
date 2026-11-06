"""取り消したあとの新しい操作が、捨てたやり直しの件数を応答に載せる。落ちたら、やり直せる分が黙って消える。"""
from __future__ import annotations

from session_support import pad, session_with_box


def test_a_new_operation_after_undo_reports_how_many_redos_it_threw_away():
    session = session_with_box()
    pad(session, 10, 10, 10, corner=(100.0, 100.0))
    pad(session, 10, 10, 10, corner=(200.0, 200.0))
    session.undo()
    session.undo()
    assert session.document.log.redo_count == 2
    applied = session.sketch_create("XY")
    assert applied["discarded_redo"] == 2 and session.document.log.redo_count == 0


def test_nothing_is_reported_without_undo_and_redo_itself_does_not_discard():
    session = session_with_box()
    first = session.sketch_create("XY")
    assert "discarded_redo" not in first
    session.undo()
    redone = session.redo()
    assert "discarded_redo" not in redone
