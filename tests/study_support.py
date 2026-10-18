"""`tools/thread_sim/study.py` の worker に差し替える計算(worker のプロセスが import できるよう、テスト本体とは別のモジュールに置く)。"""
from __future__ import annotations

import os


def computed(spec, printer_kw, e_list, mat_kw):
    return dict(spec, ok=True)


def dies_late_on_b(spec, printer_kw, e_list, mat_kw):
    """設計 B は少し走ってから worker ごと落ちる(他の設計は即座に終わる)。結果を受け取る側が遅いと、
    次の設計を配る前にプールが壊れたと分かる。"""
    if spec["name"] == "B":
        import time

        time.sleep(0.3)
        os._exit(1)
    return dict(spec, ok=True)


def dies_on_c(spec, printer_kw, e_list, mat_kw):
    """設計 C の計算で worker のプロセスごと落ちる(メモリ不足・ネイティブ部品の異常終了の代わり)。"""
    if spec["name"] == "C":
        os._exit(1)
    return dict(spec, ok=True)
