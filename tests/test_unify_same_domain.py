"""同一平面の面の統合(`brep_operations.unify_same_domain`)で、本番が試行と違うときの関門。"""
from __future__ import annotations

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt

from duocad import brep_checks, brep_geometry, brep_operations


def _two_boxes():
    """隣り合う箱2つの和(統合で 10 面 → 6 面)。"""
    first = BRepPrimAPI_MakeBox(gp_Pnt(0.0, 0.0, 0.0), 10.0, 10.0, 5.0).Shape()
    second = BRepPrimAPI_MakeBox(gp_Pnt(10.0, 0.0, 0.0), 10.0, 10.0, 5.0).Shape()
    return brep_operations.build_boolean("fuse", first, second).Shape()


def test_a_real_result_that_differs_from_the_trial_is_still_checked(monkeypatch):
    """落ちたら、本番が試行と違う中身になったときに妥当性を確かめずに返している(壊れた形がボディに入る)。"""
    fused = _two_boxes()
    volume = brep_geometry.volume(fused)
    verdicts = iter([True, False])  # 試行は通り、本番は壊れている
    monkeypatch.setattr(brep_operations, "_same_content", lambda shape, other: False)
    monkeypatch.setattr(brep_checks, "is_valid", lambda shape: next(verdicts))
    with pytest.raises(ValueError, match="試行と異なる結果"):
        brep_operations.unify_same_domain(fused, volume)
