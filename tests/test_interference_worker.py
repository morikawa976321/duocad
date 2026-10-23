"""干渉チェックの子プロセス(`duocad/interference_worker.py`)。故障を注入した子で、終わらない計算を待たずに上限と回収を確かめる。
落ちたら、時間の上限・子の作り直し・未着手の数え方・取り消しのどれかが崩れている。"""
from __future__ import annotations

import sys
import threading
import time

import pytest

from duocad import interference
from duocad.interference_worker import WorkerProcess
from interference_support import box, check, document_of, pair_of

FAKE_CHILD = r'''
import json, os, sys, time
mode = sys.argv[1]
marker = sys.argv[2] if len(sys.argv) > 2 else None
def reply(seq, result):
    print(json.dumps({"seq": seq, "ok": True, "result": result}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    op, seq = message["op"], message["seq"]
    if op == "load":
        reply(seq, {"loaded": 0})
    elif op == "relation":
        if mode == "hang_relation":
            time.sleep(600)
        if mode == "hang_once" and not os.path.exists(marker):
            open(marker, "w").close()
            time.sleep(600)
        if mode == "crash_relation":
            os._exit(3)
        if mode == "garbage_relation":
            print("not json", flush=True)
            continue
        if mode == "stale_relation":
            reply(seq - 5, {"kind": "overlap", "volume": 1.0, "thickness": 1.0, "obb_min": 1.0})
        if mode == "noisy":
            sys.stderr.write("x" * 5000000)
            sys.stderr.flush()
        reply(seq, {"kind": "none"})
    elif op == "distance":
        if mode == "hang_distance":
            time.sleep(600)
        reply(seq, {"ok": True, "distance": 0.1, "points": [[0, 0, 0], [0.1, 0, 0]], "stage": "whole", "upper_bound": False})
'''

OVERLAPPING = [("a", box(0, 0, 0, 10, 10, 10)), ("b", box(5, 0, 0, 10, 10, 10))]
FAST = interference.Limits(gf_seconds=0.6, distance_seconds=0.6, total_seconds=30.0)


def fake_factory(mode, *extra, made=None):
    def factory(load, cancel):
        worker = WorkerProcess(load, command=[sys.executable, "-c", FAKE_CHILD, mode, *extra], cancel=cancel)
        if made is not None:
            made.append(worker)
        return worker

    return factory


def test_the_real_child_gives_the_same_pairs_as_the_in_process_run():
    document = document_of(("a", box(0, 0, 0, 10, 10, 10)), ("b", box(5, 0, 0, 10, 10, 10)), ("c", box(15, 0, 0, 10, 10, 10)), ("d", box(25.1, 0, 0, 10, 10, 10)))
    expected = check(document, 0.2)
    started = time.monotonic()
    actual = check(document, 0.2, worker_factory=interference.default_worker)
    assert time.monotonic() - started < 60
    keys = ("a", "b", "relation", "clearance", "volume", "contact", "area")
    assert [{k: p.get(k) for k in keys} for p in actual["pairs"]] == [{k: p.get(k) for k in keys} for p in expected["pairs"]]
    assert actual["counts"] == expected["counts"] and actual["complete"] and actual["child_restarts"] == 0


def test_a_child_that_never_answers_is_stopped_at_the_limit_and_the_distance_is_still_measured():
    made: list = []
    started = time.monotonic()
    result = check(document_of(*OVERLAPPING), 0.2, limits=FAST, worker_factory=fake_factory("hang_relation", made=made))
    assert time.monotonic() - started < 15
    entry = pair_of(result, "a", "b")
    assert entry["relation"] == "unknown" and "打ち切" in entry["reason"]
    assert entry["distance"] == 0.1 and entry["clearance"] == "short"
    assert result["timeouts"] == [{"a": "a", "b": "b", "phase": "relation"}] and result["child_restarts"] == 1
    assert made[0]._process is None  # 子は回収された


def test_after_a_timeout_the_child_is_rebuilt_and_the_next_pair_is_answered(tmp_path):
    document = document_of(*OVERLAPPING, ("c", box(30, 0, 0, 10, 10, 10)), ("d", box(32, 0, 0, 10, 10, 10)))
    result = check(document, limits=FAST, worker_factory=fake_factory("hang_once", str(tmp_path / "marker")))
    assert result["counts"]["unknown"] == 1 and result["child_restarts"] == 1 and result["not_started"] == 0
    assert result["counts"]["separated"] == 5  # 残りの組は作り直した子が答えた


def test_a_distance_that_never_finishes_is_nulled_once_and_not_retried():
    document = document_of(("a", box(0, 0, 0, 10, 10, 10)), ("b", box(10.1, 0, 0, 10, 10, 10)))
    result = check(document, 0.2, limits=FAST, worker_factory=fake_factory("hang_distance"))
    entry = pair_of(result, "a", "b")
    assert entry["relation"] == "separated" and entry["clearance"] is None and "打ち切" in entry["clearance_reason"]
    assert result["timeouts"] == [{"a": "a", "b": "b", "phase": "distance"}] and result["counts"]["clearance_null"] == 1


def test_a_child_that_dies_midway_marks_the_pair_unknown_and_skips_its_distance():
    result = check(document_of(*OVERLAPPING), 0.2, limits=FAST, worker_factory=fake_factory("crash_relation"))
    entry = pair_of(result, "a", "b")
    assert entry["relation"] == "unknown" and "終了" in entry["reason"]
    assert entry["clearance"] is None and "distance" not in entry
    assert result["child_restarts"] == 1


def test_a_child_that_keeps_dying_stops_the_run_and_the_rest_is_not_started():
    document = document_of(*[(name, box(i, 0, 0, 10, 10, 10)) for i, name in enumerate("abcd")])
    result = check(document, limits=FAST, worker_factory=fake_factory("crash_relation"))
    assert result["counts"]["unknown"] == 6 and result["not_started"] == 3 and result["complete"] is False
    assert sum(result["counts"][k] for k in interference._RELATIONS) == 6


def test_a_child_that_cannot_start_is_reported_per_pair_without_hanging():
    def factory(load, cancel):
        return WorkerProcess(load, command=["/nonexistent/duocad-child"], cancel=cancel)

    result = check(document_of(*OVERLAPPING), limits=FAST, worker_factory=factory)
    assert "起動できない" in pair_of(result, "a", "b")["reason"]


def test_a_broken_reply_is_an_unknown_pair():
    result = check(document_of(*OVERLAPPING), limits=FAST, worker_factory=fake_factory("garbage_relation"))
    assert "壊れている" in pair_of(result, "a", "b")["reason"] and result["child_restarts"] == 1


def test_a_late_reply_from_before_is_discarded():
    result = check(document_of(*OVERLAPPING), limits=FAST, worker_factory=fake_factory("stale_relation"))
    assert result["counts"]["separated"] == 1 and result["counts"]["overlap"] == 0


def test_a_flood_of_standard_error_does_not_stall_the_child():
    made: list = []
    started = time.monotonic()
    result = check(document_of(*OVERLAPPING), limits=FAST, worker_factory=fake_factory("noisy", made=made))
    assert result["counts"]["separated"] == 1 and time.monotonic() - started < 15
    assert len(made[0].stderr_tail().encode()) <= 2 * 8192 + 4096


def test_the_overall_limit_leaves_the_rest_unstarted_and_returns_what_was_measured():
    document = document_of(*[(name, box(i, 0, 0, 10, 10, 10)) for i, name in enumerate("abcd")])
    limits = interference.Limits(gf_seconds=30.0, distance_seconds=5.0, total_seconds=1.0)
    started = time.monotonic()
    result = check(document, limits=limits, worker_factory=fake_factory("hang_relation"))
    assert time.monotonic() - started < 15
    assert result["complete"] is False and result["not_started"] == 5 and result["counts"]["unknown"] == 6


def test_a_cancel_stops_the_run_and_collects_the_child():
    made: list = []
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(interference.InterferenceCancelled):
        check(document_of(*OVERLAPPING), limits=interference.Limits(30.0, 5.0, 60.0), cancel=cancel, worker_factory=fake_factory("hang_relation", made=made))
    assert time.monotonic() - started < 10 and made[0]._process is None
