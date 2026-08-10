"""M0契約: Operation / OpLog(実装計画書 §3.6 操作ログ、§6予防1・2)。

操作ログはM0の中核。ここが薄いとM1以降が全部揺れる、という前提で書く。
provenanceスロットの存在確認はスキーマレベルの検証で日1実装済みなので素で通る。
append/undo/redo/to_json/from_json/replay はNotImplementedErrorスタブなので赤になる。
"""
from __future__ import annotations

import dataclasses

import pytest

from minicad.document import Document
from minicad.io_step import import_step
from minicad.oplog import Operation, OpLog
from minicad.query import volume

STEP_PATH = r"C:\Users\tm976\mf\3d-models\shelf_attachment_GD.step"


def test_operation_has_provenance_slot_defaulting_to_none():
    """Operationはprovenanceフィールドを持ち、値はNoneでよい(スロットの存在自体が契約。§6予防2)。"""
    op = Operation(seq=1, verb="noop", args={}, selection=[])
    assert hasattr(op, "provenance")
    assert op.provenance is None


def test_operation_dataclass_declares_provenance_field():
    """Operationのフィールド定義自体にprovenanceが含まれる(dataclasses.fields によるスキーマ検査)。"""
    field_names = {f.name for f in dataclasses.fields(Operation)}
    assert "provenance" in field_names


def test_append_seq_is_monotonically_increasing():
    """append()を複数回呼ぶとseqが1から単調増加する。"""
    log = OpLog()
    op1 = log.append("noop", {})
    op2 = log.append("noop", {})
    op3 = log.append("noop", {})
    assert [op1.seq, op2.seq, op3.seq] == [1, 2, 3]


def test_undo_reverts_document_state():
    """undoは直前の操作をdocumentから取り消す(importしたボディが消える)。"""
    doc = Document()
    import_step(doc, STEP_PATH)
    assert len(doc.bodies) == 1

    doc.log.undo(doc)
    assert doc.bodies == []


def test_redo_reapplies_undone_operation():
    """redoはundoした操作をやり直し、documentへ再度反映する。"""
    doc = Document()
    import_step(doc, STEP_PATH)

    doc.log.undo(doc)
    doc.log.redo(doc)
    assert len(doc.bodies) == 1


def test_to_json_from_json_roundtrip_matches_exactly():
    """to_json()→from_json()の往復でログの内容(seq/verb/args/selection/provenance)が完全一致する
    (保存形式はJSON。SQLiteは使わない)。"""
    log = OpLog()
    log.append("import_step", {"path": "x.step", "body_id": "b1"})
    log.append("import_step", {"path": "y.step", "body_id": "b2"})

    dumped = log.to_json()
    restored = OpLog.from_json(dumped)

    original_entries = [dataclasses.asdict(e) for e in log.entries]
    restored_entries = [dataclasses.asdict(e) for e in restored.entries]
    assert restored_entries == original_entries


def test_replay_reproduces_same_document_state():
    """from_jsonしたログをreplayすると、元と同じ文書状態になる(ボディ数と各ボディの体積で比較)。"""
    original = Document()
    import_step(original, STEP_PATH)

    dumped = original.log.to_json()
    restored_log = OpLog.from_json(dumped)

    replayed = Document()
    restored_log.replay(replayed)

    assert len(replayed.bodies) == len(original.bodies)
    original_volumes = sorted(volume(b.shape) for b in original.bodies)
    replayed_volumes = sorted(volume(b.shape) for b in replayed.bodies)
    assert original_volumes == replayed_volumes
