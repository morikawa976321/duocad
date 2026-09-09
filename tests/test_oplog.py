"""Operation / OpLog(操作ログ)の契約。

操作ログはコアの中核で、ここが薄いと上の層が全部揺れる。
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from duocad.oplog import OpLog

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


def test_from_json_ignores_unknown_operation_keys():
    """`Operation` に無いキー(かつて存在した `selection` 等)が入っていても読み込みが
    壊れず、値は単に無視されること。

    **入れ物の版(`version`)の話とは別**。版が違うファイルは `_check_version` が
    はっきり断る(下のテスト)が、操作1件の中の余分なキーは無視して読めるのが正しい。
    """
    legacy_json = json.dumps({
        "version": OpLog.FORMAT_VERSION,
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



def test_to_json_stamps_the_current_format_version():
    """`to_json` が出す入れ物には現在の版が入る(読み手が版で判断できること)。

    版は「読み込んだSTEPの形がログの中にあるか」を分ける印なので、
    書き出し側が版を書かないと、下の拒否が意味を持たなくなる。
    """
    payload = json.loads(OpLog().to_json())

    assert payload["version"] == OpLog.FORMAT_VERSION
    assert OpLog.FORMAT_VERSION == 2


def test_from_json_refuses_the_old_format_version_explicitly():
    """旧形式(version 1)は**読まずに断る**。

    版1の `import_step` は `args={"path": ...}` だけを持ち、形が外部STEPにある。
    今の再生経路はログの `args["step"]` から形を作るので、黙って読み進めると
    KeyError になり「保存形式の版が古い」という本当の理由が見えない。
    版の話だと分かる文言で断ること。
    """
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
