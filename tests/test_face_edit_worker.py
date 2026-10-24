"""面の編集を計算する子プロセス(`duocad/kernel_worker.py`)の契約(docs/FACE_EDIT.md「子プロセス」)。

落ちたら、子が期限切れ・異常終了したときに文書やログが変わる、次の要求が通らない、再生に期限が掛かる、のどれかを意味する。
"""
from __future__ import annotations

import json
import sys

import pytest
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopoDS import TopoDS

import duocad.verbs as verbs
from duocad import kernel_face_edit, kernel_worker, query, worker_process
from duocad.document import Body, Document

from document_support import replay_through_json

from duocad import (
    brep_topology as _brep_topology,
)

BOX_VOLUME = 40.0 * 30.0 * 20.0


def _filleted_box(doc: Document) -> tuple:
    """40×30×20 の箱の1辺を半径5で丸めたボディと、丸めの面の添字。"""
    box = BRepPrimAPI_MakeBox(40.0, 30.0, 20.0).Shape()
    maker = BRepFilletAPI_MakeFillet(box)
    maker.Add(5.0, TopoDS.Edge_s(_brep_topology.indexed_map(box, TopAbs_ShapeEnum.TopAbs_EDGE).FindKey(1)))
    maker.Build()
    doc.add_body(Body(id="rounded", name="rounded", kind="solid", shape=maker.Shape()))
    (rounded,) = [f["index"] for f in query.describe_faces(maker.Shape()) if f["kind"].startswith("cylinder")]
    return "rounded", rounded


def _state(doc: Document) -> tuple:
    return json.dumps(doc.log.to_json(), sort_keys=True), tuple((body.id, body.shape) for body in doc.bodies)


def test_a_call_over_the_time_limit_is_refused_and_the_next_call_works(monkeypatch):
    """期限を過ぎた計算は断り(文書もログも変えない)、子を立て直して次の要求は通る。"""
    doc = Document()
    body_id, rounded = _filleted_box(doc)
    before = _state(doc)
    monkeypatch.setattr(kernel_worker, "INTERACTIVE_TIMEOUT_S", 1e-6)
    with pytest.raises(ValueError, match="秒以内に返事が無かった"):
        verbs.delete_faces(doc, [rounded], body_id=body_id)
    assert _state(doc) == before
    monkeypatch.undo()
    verbs.delete_faces(doc, [rounded], body_id=body_id)
    assert query.volume(doc.get_body(body_id).shape) == pytest.approx(BOX_VOLUME, abs=1e-6)


def test_replaying_the_log_has_no_time_limit(monkeypatch):
    """ログの再生(文書を開く・redo)には画面の期限を掛けない(負荷しだいで保存できた文書が開けなくならない)。"""
    doc = Document()
    body_id, rounded = _filleted_box(doc)
    restored = Document()
    _filleted_box(restored)
    verbs.delete_faces(doc, [rounded], body_id=body_id)
    monkeypatch.setattr(kernel_worker, "INTERACTIVE_TIMEOUT_S", 1e-6)
    replay_through_json(doc, into=restored)
    assert query.volume(restored.get_body(body_id).shape) == pytest.approx(BOX_VOLUME, abs=1e-6)


def test_a_worker_that_was_killed_between_calls_is_restarted():
    doc = Document()
    body_id, rounded = _filleted_box(doc)
    kernel_face_edit.offset_faces(BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(), [1], 1.0)  # 子を立てる
    kernel_worker._WORKER._process.kill()
    kernel_worker._WORKER._process.wait()
    verbs.delete_faces(doc, [rounded], body_id=body_id)
    assert _brep_topology.face_count(doc.get_body(body_id).shape) == 6


def test_a_worker_that_dies_during_a_call_is_refused_and_the_next_call_works(tmp_path, monkeypatch):
    """計算中に子が落ちたら断り(文書もログも変えない)、次の要求で子を立て直す。"""
    child = '''
import json, os, sys
from pathlib import Path
marker = Path(sys.argv[1])
if not marker.exists():
    message = json.loads(sys.stdin.readline())
    marker.write_text(message["op"], encoding="utf-8")
    os._exit(3)
from duocad import kernel_worker
kernel_worker.child_main()
'''
    doc = Document()
    body_id, rounded = _filleted_box(doc)
    before = _state(doc)
    marker = tmp_path / "received"
    with worker_process.WorkerProcess([sys.executable, "-c", child, str(marker)]) as worker:
        monkeypatch.setattr(kernel_worker, "_WORKER", worker)
        with pytest.raises(ValueError, match="途中で終了"):
            verbs.delete_faces(doc, [rounded], body_id=body_id)
        assert marker.read_text(encoding="utf-8") == "face_edit"
        assert _state(doc) == before
        assert worker._process is None
        verbs.delete_faces(doc, [rounded], body_id=body_id)
        assert worker.starts == 2
    assert query.volume(doc.get_body(body_id).shape) == pytest.approx(BOX_VOLUME, abs=1e-6)
