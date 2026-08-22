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
from duocad.oplog import Operation, OpLog
from duocad.query import volume

def test_append_seq_is_monotonically_increasing():
    """append()を複数回呼ぶとseqが1から単調増加する。"""
    log = OpLog()
    op1 = log.append("noop", {})
    op2 = log.append("noop", {})
    op3 = log.append("noop", {})
    assert [op1.seq, op2.seq, op3.seq] == [1, 2, 3]


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


