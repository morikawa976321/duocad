"""面のオフセットが壊れた形を成功で返さず、断り文が原因の値と次の一手を言うか。

落ちたら、止まり穴の底が反対側の面を貫いた形を通す、再生が保存済みの文書を断る、断り文が原因と違う、のどれかを意味する。
"""
from __future__ import annotations

import pytest
from OCP.BRepAlgoAPI import BRepAlgoAPI_Check, BRepAlgoAPI_Cut
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from duocad import kernel_face_edit, query


def _plane_facing(shape, normal, area=None) -> int:
    (index,) = [
        f["index"]
        for f in query.describe_faces(shape)
        if f["kind"] == "plane" and f["normal"] == list(normal) and (area is None or abs(f["area"] - area) < 1e-6)
    ]
    return index


def _pocket_plate():
    """40×30×10 の板の上面から、20×10 で深さ 5 の止まりポケット。底は z=5。"""
    plate = BRepPrimAPI_MakeBox(40, 30, 10).Shape()
    pocket = BRepPrimAPI_MakeBox(gp_Pnt(10, 10, 5), gp_Pnt(30, 20, 11)).Shape()
    body = BRepAlgoAPI_Cut(plate, pocket).Shape()
    return body, _plane_facing(body, (0.0, 0.0, 1.0), 200.0)


def test_pocket_floor_may_move_short_of_the_opposite_face():
    body, floor = _pocket_plate()
    result = kernel_face_edit.offset_faces(body, [floor], -4.9)
    assert BRepAlgoAPI_Check(result["shape"]).IsValid()


@pytest.mark.parametrize("distance", [-5.0, -5.5, -8.0])
def test_pocket_floor_moved_to_or_past_the_opposite_face_is_refused(distance):
    body, floor = _pocket_plate()
    with pytest.raises(ValueError, match=r"cross without sharing an edge.*距離の大きさを小さくして"):
        kernel_face_edit.offset_faces(body, [floor], distance)


def test_replaying_a_saved_operation_is_not_refused_for_crossing():
    """ログの再生は検査を掛けない(保存済みの文書が開けなくなるため)。"""
    body, floor = _pocket_plate()
    result = kernel_face_edit.offset_faces(body, [floor], -5.5, interactive=False)
    assert result["shape"] is not None


def test_slot_side_face_refusal_names_the_parallel_faces():
    plate = BRepPrimAPI_MakeBox(60, 40, 10).Shape()
    body = BRepAlgoAPI_Cut(plate, BRepPrimAPI_MakeBox(gp_Pnt(20, 15, 3), gp_Pnt(40, 25, 11)).Shape()).Shape()
    for center in (20, 40):
        cylinder = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(center, 20, 3), gp_Dir(0, 0, 1)), 5, 8).Shape()
        body = BRepAlgoAPI_Cut(body, cylinder).Shape()
    side = _plane_facing(body, (0.0, -1.0, 0.0), 140.0)
    with pytest.raises(ValueError, match=r"丸めの両側の面 \d+ と \d+ が平行") as refused:
        kernel_face_edit.offset_faces(body, [side], 1.0)
    assert "一定半径の丸めでない" not in str(refused.value)


def test_distance_too_large_for_a_box_says_to_shrink_the_distance():
    body = BRepPrimAPI_MakeBox(20, 20, 10).Shape()
    top = _plane_facing(body, (0.0, 0.0, 1.0))
    with pytest.raises(ValueError, match="距離の大きさを小さくするか、別の面を動かす"):
        kernel_face_edit.offset_faces(body, [top], -12.5)


def _count_worker_runs(monkeypatch) -> list:
    from duocad import kernel_worker

    calls = []
    original = kernel_worker.run

    def counting(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(kernel_worker, "run", counting)
    return calls


def test_a_replayed_result_is_not_reused_for_an_interactive_edit(monkeypatch):
    """検査を掛けない再生が覚えた結果を、検査を掛ける画面の操作に返さない。"""
    body, floor = _pocket_plate()
    kernel_face_edit.offset_faces(body, [floor], -5.5, interactive=False)
    with pytest.raises(ValueError, match="cross without sharing an edge"):
        kernel_face_edit.offset_faces(body, [floor], -5.5, interactive=True)


def test_the_same_kind_of_call_reuses_the_remembered_result(monkeypatch):
    body, floor = _pocket_plate()
    calls = _count_worker_runs(monkeypatch)
    kernel_face_edit.offset_faces(body, [floor], -2.0, interactive=True)
    kernel_face_edit.offset_faces(body, [floor], -2.0, interactive=True)
    assert len(calls) == 1
    kernel_face_edit.offset_faces(body, [floor], -3.0, interactive=False)
    kernel_face_edit.offset_faces(body, [floor], -3.0, interactive=False)
    assert len(calls) == 2


def test_redo_reuses_the_result_the_confirmed_edit_computed(monkeypatch):
    """確定(検査あり)のあとの redo(再生)は計算し直さない。"""
    body, floor = _pocket_plate()
    calls = _count_worker_runs(monkeypatch)
    confirmed = kernel_face_edit.offset_faces(body, [floor], -2.0, interactive=True)
    redone = kernel_face_edit.offset_faces(body, [floor], -2.0, interactive=False)
    assert len(calls) == 1
    assert redone["shape"] is confirmed["shape"]
