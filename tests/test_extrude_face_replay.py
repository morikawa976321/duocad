"""verb `extrude_face` は再生専用で残してある: 古い保存ログを読み込んで再生すると従来と同じ形になる。"""
from __future__ import annotations

import math
from pathlib import Path

from duocad import kernel, query
from duocad.document import Document
from duocad.oplog import OpLog

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "extrude_face_log.json"

#: 保存時の体積と面数(20³ + 20²·10 − 3·20·30 + 4·17·30)
SAVED_VOLUME = 12240.0
SAVED_FACES = 6


def _replayed() -> Document:
    log = OpLog.from_json(FIXTURE.read_text(encoding="utf-8"))
    document = Document()
    document.log = log
    log.replay(document)
    return document


def test_a_saved_log_with_extrude_face_replays_to_the_saved_shape():
    document = _replayed()

    assert [entry.verb for entry in document.log.entries].count("extrude_face") == 3
    (body,) = document.bodies
    assert math.isclose(query.volume(body.shape), SAVED_VOLUME, rel_tol=1e-9)
    assert kernel.face_count(body.shape) == SAVED_FACES


def test_undo_and_redo_of_a_replayed_extrude_face_restore_the_shapes():
    document = _replayed()
    (body,) = document.bodies

    document.log.undo(document)
    assert math.isclose(query.volume(body.shape), 8000.0 + 4000.0 - 1800.0, rel_tol=1e-9)

    document.log.redo(document)
    assert math.isclose(query.volume(body.shape), SAVED_VOLUME, rel_tol=1e-9)


def test_extrude_face_is_not_a_public_verb():
    """公開面(カタログ・Session・MCP)から外れている。登録だけが再生のために残る。"""
    from duocad import catalog
    from duocad.mcp_server import DocumentTools, McpServer
    from duocad.server import Session
    from mcp_support import request

    assert "extrude_face" not in catalog.BY_NAME
    assert not hasattr(Session, "extrude_face")
    assert not hasattr(DocumentTools, "extrude_face")
    server = McpServer(DocumentTools(Session(None, 0.2)))
    listed = request(server, "tools/list")["result"]["tools"]
    assert "extrude_face" not in [tool["name"] for tool in listed]
    error = request(server, "tools/call", {"name": "extrude_face", "arguments": {"distance": 1.0}})["error"]
    assert "unknown tool" in error["message"]
