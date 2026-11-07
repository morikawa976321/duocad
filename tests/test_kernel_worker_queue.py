"""kernel_worker.run: 順番待ちで期限が来たときは、計算が遅かったのではなく始められなかったと言う。"""
from __future__ import annotations

import threading

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import kernel_worker


def test_a_timeout_while_queued_behind_another_computation_says_it_never_started(monkeypatch):
    monkeypatch.setattr(kernel_worker, "_ACTIVE", threading.Event())
    box = BRepPrimAPI_MakeBox(10, 10, 10).Shape()
    with pytest.raises(kernel_worker.TimedOut, match="fillet: 別の計算が実行中のため、1 秒以内に始められなかった"):
        kernel_worker.run("fillet", box, {}, timeout=1.0, op="blend_edges")
