"""ねじの結果の使い回し(`kernel_thread.thread_face` の鍵)。

落ちたら: 同じ形へのねじの redo が計算し直しになった(空欄の隙間・ピッチで覚えた結果に、ログの解決後の数値で当たらない)か、
空欄で計算したねじと解決後の数値で計算したねじが別物になった(redo が確定と違う形を借りる)。
"""
from __future__ import annotations

import pytest

from duocad import brep_topology as _brep_topology, kernel_thread
from duocad.document import Document
from duocad.verbs import thread
from test_chamfer import new_cylinder
from test_thread import _cylinder_face, _hole


def _counting(monkeypatch) -> list:
    calls: list = []
    real = kernel_thread._thread_face

    def counted(*args):
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(kernel_thread, "_thread_face", counted)
    return calls


@pytest.mark.parametrize("make", ["hole", "shaft"])
def test_redo_of_a_thread_with_blank_clearance_reuses_the_result(monkeypatch, make):
    """空欄の隙間で刻んだねじ(雌・雄)の undo → redo は計算し直さず、確定と同じ形の物へ戻る。"""
    doc = Document()
    if make == "hole":
        body_id, face = _hole(doc)
    else:
        body_id = new_cylinder(doc, radius=3.0, height=10.0)
        face = _cylinder_face(doc, body_id)
    calls = _counting(monkeypatch)

    thread(doc, face, "trapezoid45", body_id=body_id)
    threaded = doc.get_body(body_id).shape
    doc.log.undo(doc)
    doc.log.redo(doc)

    assert len(calls) == 1
    assert doc.get_body(body_id).shape is threaded


def test_the_resolved_clearance_and_pitch_compute_the_same_thread_as_the_blanks():
    """空欄で計算したねじと、解決後の数値で計算し直したねじが同じ(由来・数値・面の数・体積)。redo が覚えた結果を借りてよい前提。"""
    doc = Document()
    body_id, face = _hole(doc)
    shape = doc.get_body(body_id).shape

    blank = kernel_thread._thread_face(shape, face, "trapezoid30", None, None)
    explicit = kernel_thread._thread_face(shape, face, "trapezoid30", blank["clearance"], blank["pitch"])

    assert explicit["provenance"] == blank["provenance"]
    for key in ("female", "pitch", "clearance", "limit", "warning", "recommended"):
        assert explicit[key] == blank[key]
    assert _brep_topology.face_count(explicit["shape"]) == _brep_topology.face_count(blank["shape"])
    assert explicit["volume_after"] == pytest.approx(blank["volume_after"], rel=1e-12)
