"""`tools/thread_sim/study.py` の worker に差し替える計算(worker のプロセスが import できるよう、テスト本体とは別のモジュールに置く)。"""
from __future__ import annotations

import os


def computed(spec, printer_kw, e_list, mat_kw):
    return dict(spec, ok=True)


def dies_on_c(spec, printer_kw, e_list, mat_kw):
    """設計 C の計算で worker のプロセスごと落ちる(メモリ不足・ネイティブ部品の異常終了の代わり)。"""
    if spec["name"] == "C":
        os._exit(1)
    return dict(spec, ok=True)
