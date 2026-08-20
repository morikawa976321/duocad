"""M0契約: Operation / OpLog(実装計画書 §3.6 操作ログ、§6予防1・2)。

操作ログはM0の中核。ここが薄いとM1以降が全部揺れる、という前提で書く。
provenanceスロットの存在確認はスキーマレベルの検証で実装済みなので素で通る。
append/undo/redo/to_json/from_json/replay はNotImplementedErrorスタブなので赤になる。
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from duocad.document import Document
from duocad.io_step import import_step
from duocad.oplog import Operation, OpLog
from duocad.query import volume

from models import SHELF as STEP_PATH  # テスト共通のSTEPの置き場(tests/models.py)


def test_operation_has_provenance_slot_defaulting_to_none():
    """Operationはprovenanceフィールドを持ち、値はNoneでよい(スロットの存在自体が契約。§6予防2)。"""
    op = Operation(seq=1, verb="noop", args={})
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
    assert len(doc.bodies) == 17  # M3.5: shelf は17ソリッドに分解される

    doc.log.undo(doc)
    assert doc.bodies == []


def test_redo_does_not_record_the_operation_twice():
    """redoは既存のエントリを有効化するだけで、新しい操作を積み直さない。

    redo/replayの実装が公開API(io_step.import_step)を呼び直すと、その中でappend()が走り
    同じ操作が二重にログへ乗る。ログは唯一の正(実装計画書§3.1)なので、二重記録は
    そのまま保存・再生の破綻になる。seqが振り直されていないことまで見る。
    """
    doc = Document()
    import_step(doc, STEP_PATH)
    seqs_before = [op.seq for op in doc.log.entries]

    doc.log.undo(doc)
    assert doc.log.entries == []

    doc.log.redo(doc)
    assert len(doc.log.entries) == 1
    assert [op.seq for op in doc.log.entries] == seqs_before
    assert len(doc.bodies) == 17  # M3.5: shelf は17ソリッドに分解される


def test_to_json_emits_actual_json_with_the_logged_fields():
    """to_json()の出力は本物のJSONで、各操作のseq/verb/args/provenanceが読める形で入る
    (保存形式はJSON。SQLiteもpickleもrepr+evalも不可。実装計画書§3.6 —
    gitで差分が読める・AIが全履歴を読める、が理由なので「往復すれば何でもよい」ではない)。"""
    log = OpLog()
    log.append("import_step", {"path": "x.step", "body_id": "b1"})

    parsed = json.loads(log.to_json())  # JSONでなければここで例外になる

    # 外側の入れ物(素のリスト / {"operations": [...]} 等)は縛らない。
    # 縛るのは「操作が1件ずつJSONオブジェクトとして読めること」。
    if isinstance(parsed, dict):
        candidates = [v for v in parsed.values() if isinstance(v, list)]
        assert len(candidates) == 1, f"操作の配列を1つだけ含むこと: keys={sorted(parsed)}"
        ops = candidates[0]
    else:
        ops = parsed
    assert isinstance(ops, list) and len(ops) == 1
    assert ops[0]["seq"] == 1
    assert ops[0]["verb"] == "import_step"
    assert ops[0]["args"] == {"path": "x.step", "body_id": "b1"}
    assert ops[0]["provenance"] is None
    assert "selection" not in ops[0], (
        "selectionフィールドは2026-08-20に廃止。書き込み側が復活していないか"
    )


def test_to_json_from_json_roundtrip_matches_exactly():
    """to_json()→from_json()の往復でログの内容(seq/verb/args/provenance)が完全一致する
    (保存形式はJSON。SQLiteは使わない)。"""
    log = OpLog()
    log.append("import_step", {"path": "x.step", "body_id": "b1"})
    log.append("import_step", {"path": "y.step", "body_id": "b2"})

    dumped = log.to_json()
    restored = OpLog.from_json(dumped)

    original_entries = [dataclasses.asdict(e) for e in log.entries]
    restored_entries = [dataclasses.asdict(e) for e in restored.entries]
    assert restored_entries == original_entries


def test_from_json_accepts_legacy_selection_key_and_ignores_it():
    """2026-08-20より前に保存されたファイルには `selection` キーが入っている
    (当時の `Operation` が持っていたフィールド)。後方互換のため、キーが有っても
    読み込みが壊れず、値は単に無視されること(既存の保存ファイルが開けなくなるのは不可)。
    """
    legacy_json = json.dumps({
        "version": 1,
        "operations": [
            {
                "seq": 1,
                "verb": "import_step",
                "args": {"path": "x.step", "body_id": "b1"},
                "selection": [{"body": "b1", "kind": "face", "index": 3}],
                "provenance": None,
            }
        ],
    })

    log = OpLog.from_json(legacy_json)

    assert len(log.entries) == 1
    operation = log.entries[0]
    assert operation.seq == 1
    assert operation.verb == "import_step"
    assert operation.args == {"path": "x.step", "body_id": "b1"}
    assert not hasattr(operation, "selection")


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


def test_replay_does_not_append_to_the_log_it_replays():
    """replayは自分のエントリを読んで文書を再構築するだけで、ログ自身を書き換えない。

    「開くときはログから再生する」(実装計画書§3.1)運用では、再生したログがそのまま
    その文書のログになる。再生のたびにエントリが増えるなら、開くたびに履歴が倍になる。
    """
    original = Document()
    import_step(original, STEP_PATH)

    restored_log = OpLog.from_json(original.log.to_json())
    entries_before = [dataclasses.asdict(e) for e in restored_log.entries]

    restored_log.replay(Document())

    assert [dataclasses.asdict(e) for e in restored_log.entries] == entries_before