"""WebSocket の添え物 `expected_generation`(`duocad/net/ws.py` の `_dispatch`)。

落ちたら、画面が文書の切り替えの配信を受け取る前に送った取り込みが、切り替わった後の文書へ入る。
"""
from __future__ import annotations

from duocad.server import Session
from session_support import send_one


def test_a_command_sent_for_the_document_before_a_switch_is_refused(session: Session) -> None:
    session.new_document()
    seen = session.generation
    session.new_document()

    answer = send_one(
        session, {"cmd": "sketch_create", "base_plane": "XY", "expected_generation": seen, "client_op": 7}
    )

    assert (answer["type"], answer["kind"], answer["client_op"]) == ("error", "refusal", 7)
    assert session.document.sketches == []


def test_a_command_sent_for_the_current_document_runs(session: Session) -> None:
    session.new_document()

    answer = send_one(
        session, {"cmd": "sketch_create", "base_plane": "XY", "expected_generation": session.generation}
    )

    assert answer["type"] == "state"
    assert len(session.document.sketches) == 1
