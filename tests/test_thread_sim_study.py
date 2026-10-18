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


def test_a_pool_that_breaks_while_the_consumer_is_busy_is_replaced_not_raised() -> None:
    """落ちたら、結果を保存している間に別の設計の worker が落ちると、次の設計を配る段が BrokenProcessPool で例外終了し、
    落ちた設計のエラー行も、残りの設計の結果も得られない。"""
    import time

    todo = [(dict(name=name), {}, {}) for name in "ABCD"]
    results = {}
    for design, row in S.run_pool(todo, study_support.dies_late_on_b, [0.0], 2):
        results[design["name"]] = row
        if design["name"] == "A":
            time.sleep(1.0)  # 受け取る側が遅い間に B が落ちる

    assert set(results) == set("ABCD")
    assert results["B"]["error"] == S.WORKER_DIED
    assert all(results[name].get("ok") for name in "ACD")


def test_a_last_line_cut_inside_a_utf8_character_does_not_stop_the_resume(fake_study) -> None:
    """落ちたら、日本語を含む行が文字の途中で切れた JSONL を読む段が UnicodeDecodeError で止まり、再開できない。"""
    whole = json.dumps(dict(name="A", ok=True), ensure_ascii=False).encode("utf-8")
    line = json.dumps(dict(name="B", note="日本語の備考"), ensure_ascii=False).encode("utf-8")
    cut = line[: line.index("備".encode("utf-8")) + 1]  # 「備」の3バイトのうち最初の1バイトで切る
    with pytest.raises(UnicodeDecodeError):
        cut.decode("utf-8")  # 通常の復号では読めない切り方になっている
    fake_study.write_bytes(whole + b"\n" + cut)

    assert S.main(["fake", "--workers", "1"]) == 0

    done = set()
    for line in fake_study.read_bytes().decode("utf-8", errors="replace").splitlines():
        try:
            done.add(json.loads(line)["name"])
        except (json.JSONDecodeError, KeyError):
            pass
    assert done >= set(NAMES)
