"""凍結中の形状最適化のテスト(`tests/topopt/`)の下ごしらえ。既定の実行では集めない(`tests/conftest.py`)。"""
from __future__ import annotations

import pytest


@pytest.fixture
def finished_shape_check_at_a_third_of_the_test_element(monkeypatch):
    """完成形検査の細かさを、製品経路を通す検査の要素(約 8 mm)の 1/3 にする(製品経路を通す検査のモジュールが
    `pytestmark` で使う)。外すと製品の 0.67 mm(1/12)で解き、完成形まで通す検査が1本 60〜95 秒になる(2026-10-11 実測)。
    製品の値での分割数は test_member_volume_gate が固定する。"""
    from duocad.topopt import final_check

    monkeypatch.setattr(final_check, "MEASURE_SPACING_MM", 3.0)
