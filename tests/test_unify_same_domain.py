"""同一平面の面の統合(`brep_operations.unify_same_domain`)の検査の回数と、本番が試行と違うときの関門。"""
from __future__ import annotations

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from duocad import brep_checks, brep_geometry, brep_operations, brep_topology


def _two_boxes():
    """隣り合う箱2つの和(統合で 10 面 → 6 面)。"""
    first = BRepPrimAPI_MakeBox(gp_Pnt(0.0, 0.0, 0.0), 10.0, 10.0, 5.0).Shape()
    second = BRepPrimAPI_MakeBox(gp_Pnt(10.0, 0.0, 0.0), 10.0, 10.0, 5.0).Shape()
    return brep_operations.build_boolean("fuse", first, second).Shape()


def _plate_with_holes_and_a_flush_block():
    """穴 6 個の板の側面に、上下の面を揃えて接する箱を足した和(上面・底面・側面が同一平面)。"""
    plate = BRepPrimAPI_MakeBox(gp_Pnt(0.0, 0.0, 0.0), 40.0, 30.0, 5.0).Shape()
    for column in range(3):
        for row in range(2):
            hole = BRepPrimAPI_MakeCylinder(
                gp_Ax2(gp_Pnt(8.0 + 12.0 * column, 9.0 + 12.0 * row, -1.0), gp_Dir(0.0, 0.0, 1.0)), 2.0, 7.0
            ).Shape()
            plate = brep_operations.build_boolean("cut", plate, hole).Shape()
    block = BRepPrimAPI_MakeBox(gp_Pnt(40.0, 0.0, 0.0), 10.0, 30.0, 5.0).Shape()
    return brep_operations.build_boolean("fuse", plate, block).Shape()


@pytest.mark.parametrize("make", [_two_boxes, _plate_with_holes_and_a_flush_block], ids=["箱2つ", "穴あき板と接する箱"])
def test_an_adopted_unification_is_checked_once(monkeypatch, make):
    """落ちたら、本番の統合を試行と同じ中身と見分けられず BRepCheck をもう一度走らせている(遅いだけで形は同じ)。"""
    fused = make()
    volume = brep_geometry.volume(fused)
    faces_before = brep_topology.face_count(fused)
    calls = []
    check = brep_checks.is_valid
    monkeypatch.setattr(brep_checks, "is_valid", lambda shape: calls.append(shape) or check(shape))
    unified, history, adopted = brep_operations.unify_same_domain(fused, volume)
    assert adopted is True and history is not None
    assert brep_topology.face_count(unified) < faces_before
    assert check(unified)
    assert len(calls) == 1


def test_a_real_result_that_differs_from_the_trial_is_still_checked(monkeypatch):
    """落ちたら、本番が試行と違う中身になったときに妥当性を確かめずに返している(壊れた形がボディに入る)。"""
    fused = _two_boxes()
    volume = brep_geometry.volume(fused)
    verdicts = iter([True, False])  # 試行は通り、本番は壊れている
    monkeypatch.setattr(brep_operations, "_same_content", lambda shape, other: False)
    monkeypatch.setattr(brep_checks, "is_valid", lambda shape: next(verdicts))
    with pytest.raises(ValueError, match="試行と異なる結果"):
        brep_operations.unify_same_domain(fused, volume)


@pytest.mark.parametrize("make", [_two_boxes, _plate_with_holes_and_a_flush_block], ids=["箱2つ", "穴あき板と接する箱"])
def test_an_adopted_unification_hands_over_the_volume_it_integrated(make):
    """落ちたら、統合した形に引き継いだ体積が、その形を積分し直した値とずれている(体積の表示・検査が変わる)。"""
    fused = make()
    unified, _history, adopted = brep_operations.unify_same_domain(fused, brep_geometry.volume(fused))
    assert adopted is True
    assert brep_geometry.known_volume(unified) is not None
    assert brep_geometry.volume(unified) == brep_geometry._integrated_volume_props(unified).Mass()
