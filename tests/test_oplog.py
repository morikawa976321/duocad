"""Operation / OpLog(操作ログ)の契約。"""
from __future__ import annotations

import dataclasses
import json

import pytest

from duocad.oplog import OpLog
from document_support import assert_finite_json


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_the_saved_json_check_rejects_non_finite_numbers_even_inside_arrays(number):
    with pytest.raises(ValueError):
        assert_finite_json('{"operations": [{"args": {"point": [0, ' + number + ']}}]}')


def test_the_saved_json_check_accepts_finite_numbers_and_unicode():
    assert_finite_json('{"name": "部品", "point": [0, -2.5, 1e300], "visible": true}')


def test_to_json_emits_actual_json_with_the_logged_fields():
    """to_json()の出力は本物のJSONで、各操作のseq/verb/args/provenanceが読める形で入る
    (保存形式はJSON。SQLiteもpickleもrepr+evalも不可。docs/DESIGN_DECISIONS.md「技術選定の根拠」 —
    gitで差分が読める・AIが全履歴を読める、が理由なので「往復すれば何でもよい」ではない)。"""
    log = OpLog()
    log.append("import_step", {"path": "x.step", "body_id": "b1"})

    ops = json.loads(log.to_json())["operations"]  # JSONでなければここで例外になる

    assert len(ops) == 1
    assert ops[0]["seq"] == 1
    assert ops[0]["verb"] == "import_step"
    assert ops[0]["args"] == {"path": "x.step", "body_id": "b1"}
    assert ops[0]["provenance"] is None


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


def test_from_json_ignores_unknown_operation_keys():
    """`Operation` に無いキーが入っていても読み込みが壊れず、値は単に無視されること
    (過去のファイルに残る、いまは使わないキーを開けなくしない)。
    """
    legacy_json = json.dumps({
        "version": OpLog.FORMAT_VERSION,
        "operations": [
            {
                "seq": 1,
                "verb": "import_step",
                "args": {"path": "x.step", "body_id": "b1"},
                "no_longer_used": [{"body": "b1", "kind": "face", "index": 3}],
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
    assert not hasattr(operation, "no_longer_used")


def test_from_json_refuses_the_old_format_version_explicitly():
    """旧形式(version 1)は**読まずに断る**。"""
    old = json.dumps({
        "version": 1,
        "operations": [
            {"seq": 1, "verb": "import_step", "args": {"path": "x.step", "body_ids": ["b1"]}}
        ],
    })

    with pytest.raises(ValueError) as caught:
        OpLog.from_json(old)

    assert "version" in str(caught.value)


def test_from_json_refuses_a_future_format_version():
    """知らない版も断る(黙って今の版として読まない)。"""
    future = json.dumps({"version": OpLog.FORMAT_VERSION + 1, "operations": []})

    with pytest.raises(ValueError):
        OpLog.from_json(future)
