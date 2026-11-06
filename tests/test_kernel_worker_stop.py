"""kernel_worker.stop: 計算中でも待たずに子を止める。"""
from __future__ import annotations

import threading
import time

from duocad import kernel_worker


class _Worker:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_stop_closes_the_child_without_waiting_for_a_running_request(monkeypatch):
    worker = _Worker()
    monkeypatch.setattr(kernel_worker, "_WORKER", worker)
    monkeypatch.setattr(kernel_worker, "_ACTIVE", threading.Event())
    started = time.monotonic()
    kernel_worker.stop()
    assert worker.closed
    assert time.monotonic() - started < 1.0
