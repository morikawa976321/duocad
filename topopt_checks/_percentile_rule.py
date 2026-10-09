"""2026-10-09 より前の製品の代表応力(材料全体の 99.5% 点)で製品の入口を回す差し替え(研究の再現用)。

製品は部材域の固定物理体積の上位値に移った(docs/TOPOPT_STRESS_GATE_DILUTION.md)。99.5% 点の母数依存を測った研究
(`idle_validation/`・`measure_idle_*.py`)は旧規則を測ったものなので、製品の入口を通すときはこれで包む。
"""
import dataclasses
from contextlib import contextmanager
from unittest.mock import patch

from duocad import optimize_run


@contextmanager
def percentile_rule():
    real = optimize_run._build_search_conditions

    def legacy(*args, **kwargs):
        return dataclasses.replace(real(*args, **kwargs), member_volume_mm3=None)

    with patch.object(optimize_run, "_build_search_conditions", legacy):
        yield
