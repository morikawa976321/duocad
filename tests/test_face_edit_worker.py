"""面の編集を計算する子プロセス(`duocad/kernel_worker.py`)の契約(docs/FACE_EDIT.md「子プロセス」)。

落ちたら、子が期限切れ・異常終了したときに文書やログが変わる、次の要求が通らない、再生に期限が掛かる、のどれかを意味する。
"""
from __future__ import annotations

import json
import threading

import pytest
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopoDS import TopoDS

import duocad.verbs as verbs
from duocad import kernel, kernel_face_edit, kernel_worker, query
from duocad.document import Body, Document
from duocad.mesh_geometry import compound, indexed_map
from document_support import replay_through_json

BOX_VOLUME = 40.0 * 30.0 * 20.0


def _filleted_box(doc: Document) -> tuple:
    """40×30×20 の箱の1辺を半径5で丸めたボディと、丸めの面の添字。"""
    box = BRepPrimAPI_MakeBox(40.0, 30.0, 20.0).Shape()
    maker = BRepFilletAPI_MakeFillet(box)
    maker.Add(5.0, TopoDS.Edge_s(indexed_map(box, TopAbs_ShapeEnum.TopAbs_EDGE).FindKey(1)))
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
    assert kernel.face_count(doc.get_body(body_id).shape) == 6


def test_a_worker_that_dies_during_a_call_is_refused_and_the_next_call_works():
    """計算中に子が落ちたら断り(文書もログも変えない)、次の要求で子を立て直す。"""
    plate = BRepPrimAPI_MakeBox(100.0, 100.0, 5.0).Shape()
    tools = [
        BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(10.0 + 13.0 * i, 10.0 + 13.0 * j, -1.0), gp_Dir(0, 0, 1)), 2.0, 7.0).Shape()
        for i in range(7)
        for j in range(7)
    ]
    doc = Document()
    doc.add_body(Body(id="plate", name="plate", kind="solid", shape=BRepAlgoAPI_Cut(plate, compound(tools)).Shape()))
    holes = [f["index"] for f in query.describe_faces(doc.get_body("plate").shape) if f["kind"] == "cylinder_full"]
    assert len(holes) == 49
    kernel_face_edit.offset_faces(doc.get_body("plate").shape, [holes[0]], -0.5)  # 子を先に立てておく(起動を落とす窓に入れない)
    before = _state(doc)
    process = kernel_worker._WORKER._process
    killer = threading.Timer(0.5, process.kill)  # 49 個を一度に消す計算は約 3.5 秒(docs/FACE_EDIT.md「規模」)
    killer.start()
    try:
        with pytest.raises(ValueError, match="途中で終了"):
            verbs.delete_faces(doc, holes, body_id="plate")
    finally:
        killer.cancel()
    assert _state(doc) == before
    verbs.delete_faces(doc, holes[1:2], body_id="plate")
    assert kernel.face_count(doc.get_body("plate").shape) == 6 + 48
