"""子プロセスの共通の部品(`duocad/worker_process.py`)。故障を注入した子で、作り直しと数え方を確かめる。

落ちたら、要求の合間に死んだ子へ要求を送って失敗する、作り直しの回数が呼び手の結果に載らない、のどちらかを意味する。
"""
from __future__ import annotations

import sys

from duocad import worker_process
from duocad.interference_worker import InProcessWorker
from interference_support import box, check, document_of

#: 1回答えるたびに終わる子(`worker_process` の線を話す)
EXITS_AFTER_REPLY = r'''
import json, os, sys
for line in sys.stdin:
    message = json.loads(line)
    print(json.dumps({"seq": message["seq"], "ok": True, "result": {"echo": message.get("value")}}), flush=True)
    os._exit(0)
'''


def test_a_child_that_died_between_requests_is_revived_before_the_next_request():
    with worker_process.WorkerProcess([sys.executable, "-c", EXITS_AFTER_REPLY]) as worker:
        assert worker.request({"value": 1}, 30.0) == {"echo": 1}
        worker._process.wait()
        assert worker.request({"value": 2}, 30.0) == {"echo": 2}
        assert (worker.starts, worker.revived) == (2, 1)


class _RevivingWorker:
    """答えは正しいが、途中で子を2回作り直したと申告する(`WorkerProcess.revived`)。"""

    revived = 2

    def __init__(self, load, cancel):
        self.inner = InProcessWorker(load, cancel=cancel)

    def request(self, message, timeout):
        return self.inner.request(message, timeout)

    def restart(self):
        self.inner.restart()

    def close(self):
        self.inner.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def test_children_revived_between_requests_count_as_restarts_of_the_check():
    result = check(document_of(("a", box(0, 0, 0, 10, 10, 10)), ("b", box(5, 0, 0, 10, 10, 10))), worker_factory=_RevivingWorker)
    assert result["child_restarts"] == 2
