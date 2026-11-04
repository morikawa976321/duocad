"""面の編集の子が返す結果の体積(`volume_after`)を、親が戻った形の体積として覚える。"""
from __future__ import annotations

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from duocad import brep_geometry, kernel_face_edit, query


def _plate_with_a_hole():
    plate = BRepPrimAPI_MakeBox(gp_Pnt(-20, -10, 0), 40, 20, 5).Shape()
    hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(3, 1, -1), gp_Dir(0, 0, 1)), 2.0, 7).Shape()
    return BRepAlgoAPI_Cut(plate, hole).Shape()


def _index_of(shape, kind: str, pick=lambda face: True) -> int:
    return next(face["index"] for face in query.describe_faces(shape) if face["kind"].startswith(kind) and pick(face))


def test_the_parent_knows_the_volume_of_a_face_edit_result_without_integrating_it():
    """落ちたら、親が面の編集の結果の体積を積分し直す(穴の多い部品のゴースト・配信で1回 0.1 秒)か、子が返した体積が
    親で積分し直した値と1ビットでも違う(体積の表示・検査が子を通したかで変わる)。"""
    shape = _plate_with_a_hole()
    deleted = kernel_face_edit.delete_faces(shape, [_index_of(shape, "cylinder")])["shape"]
    moved = kernel_face_edit.offset_faces(shape, [_index_of(shape, "plane", lambda face: face["normal"][0] > 0.9)], 0.5)["shape"]
    for result, expected in ((deleted, 40 * 20 * 5), (moved, 40.5 * 20 * 5 - 3.141592653589793 * 4 * 5)):
        known = brep_geometry.known_volume(result)
        assert known is not None
        assert known == brep_geometry._integrated_volume_props(result).Mass()
        assert abs(known - expected) < 1e-6 * expected
