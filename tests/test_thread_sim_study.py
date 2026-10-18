"""`tools/thread_sim/study.py`(ねじ山形の比較を並列で回し、1設計1行の JSONL に書く)の再開の契約。

落ちたら、途中で止まった実行を同じコマンドで続けたときに、計算していない設計が「済み」になる・書きかけの行が次の行と連結される。
"""
from __future__ import annotations

import json

import pytest

import study_support
from tools.thread_sim import study as S

NAMES = "ABCDEF"


def rows(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def fake_study(tmp_path, monkeypatch):
    """6設計 A〜F の偽の study。結果は `tmp_path/fake.jsonl`。"""
    monkeypatch.setattr(S, "OUT", tmp_path)
    monkeypatch.setitem(S.STUDIES, "fake", lambda: ([dict(name=name) for name in NAMES], {}, [0.0], {}))
    monkeypatch.setattr(S, "run_one", study_support.computed)
    return tmp_path / "fake.jsonl"


def test_a_dead_worker_marks_only_the_designs_that_were_running(fake_study, monkeypatch) -> None:
    """落ちたら、worker が1つ落ちたときに、まだ配っていない設計まで error 行になり、同じコマンドでは二度と回らない。"""
    monkeypatch.setattr(S, "run_one", study_support.dies_on_c)

    assert S.main(["fake", "--workers", "1"]) == 1

    by_name = {row["name"]: row for row in rows(fake_study)}
    assert set(by_name) == set(NAMES)
    assert [name for name, row in by_name.items() if "error" in row] == ["C"]
    assert by_name["C"]["error"] == S.WORKER_DIED
    assert all(by_name[name].get("ok") for name in "ABDEF")


def test_redo_errors_recomputes_the_design_that_was_running_when_the_worker_died(fake_study, monkeypatch) -> None:
    """落ちたら、worker が落ちた設計を `--redo-errors` が回し直さない(落ちた行を計算の失敗として拾えていない)。"""
    monkeypatch.setattr(S, "run_one", study_support.dies_on_c)
    S.main(["fake", "--workers", "1"])
    monkeypatch.setattr(S, "run_one", study_support.computed)

    assert S.main(["fake", "--workers", "1", "--redo-errors"]) == 0

    by_name = {row["name"]: row for row in rows(fake_study)}
    assert sorted(by_name) == list(NAMES)
    assert all(row.get("ok") for row in by_name.values())


def test_a_cut_off_last_line_does_not_swallow_the_next_result(fake_study) -> None:
    """落ちたら、末尾が改行で終わらない JSONL に次の結果が連結され、その行が読めなくなって設計が失われる。"""
    fake_study.write_bytes(b'{"name": "A", "ok": true}\n{"name": "B", "ok": ')

    S.main(["fake", "--workers", "1"])

    parsed = {}
    for line in fake_study.read_text(encoding="utf-8").splitlines():
        try:
            parsed[json.loads(line)["name"]] = json.loads(line)
        except json.JSONDecodeError:
            continue  # 書きかけの断片
    assert sorted(parsed) == list(NAMES)
