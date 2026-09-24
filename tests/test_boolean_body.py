"""verb `boolean_body` の契約 — union/cut/intersect、複数ツール交差、非ソリッド拒否、undo/redo。"""
from __future__ import annotations

import math

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS

from duocad import query, selection
from duocad.document import Body, Document, new_body_id
from duocad.server import Session
from duocad.verbs import boolean_body as boolean_body_verb

DEVIATION = 0.2
SIDE = 20.0
TOOL = 10.0


@pytest.fixture()
def session(tmp_path) -> Session:
    return Session(None, DEVIATION, models_dir=tmp_path / "models")


def _box_shape(dx: float, dy: float, dz: float, ox: float = 0.0, oy: float = 0.0, oz: float = 0.0):
    if ox == 0.0 and oy == 0.0 and oz == 0.0:
        return BRepPrimAPI_MakeBox(dx, dy, dz).Shape()
    return BRepPrimAPI_MakeBox(gp_Pnt(ox, oy, oz), dx, dy, dz).Shape()


def _face_only(shape) -> object:
    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_FACE)
    assert explorer.More()
    explorer.Next()
    return TopoDS.Face_s(explorer.Current())


def _add_body(doc: Document, name: str, shape, *, visible: bool = True) -> str:
    body_id = new_body_id()
    doc.add_body(Body(id=body_id, name=name, kind="solid", shape=shape, visible=visible))
    return body_id


def _target_ref(body_id: str) -> dict:
    return {"body": body_id, "kind": "face", "index": 1}


def _select(session: Session, refs: list) -> None:
    session.clear_selection()
    session.modify_selection(refs, [])


def _two_boxes(doc: Document) -> tuple[str, str]:
    """20mm 立方体 + 内側に 10mm 立方体。"""
    target_id = _add_body(doc, "target", _box_shape(SIDE, SIDE, SIDE))
    tool_id = _add_body(doc, "tool", _box_shape(TOOL, TOOL, TOOL))
    return target_id, tool_id


def _three_boxes(doc: Document) -> tuple[str, str, str]:
    """20mm 立方体 + 角2か所に 10mm 立方体(互いに重ならない)。"""
    target_id = _add_body(doc, "target", _box_shape(SIDE, SIDE, SIDE))
    tool_a_id = _add_body(doc, "tool_a", _box_shape(TOOL, TOOL, TOOL))
    tool_b_id = _add_body(
        doc, "tool_b", _box_shape(TOOL, TOOL, TOOL, SIDE - TOOL, SIDE - TOOL, SIDE - TOOL)
    )
    return target_id, tool_a_id, tool_b_id


def _run(session: Session, target_id: str, tool_ids: list[str], mode: str) -> None:
    _select(
        session,
        [{"body": tool_id, "kind": "face", "index": 1} for tool_id in tool_ids],
    )
    session.boolean_body(_target_ref(target_id), mode)


def _volume(doc: Document, body_id: str) -> float:
    return query.volume(doc.get_body(body_id).shape)


# --------------------------------------------------------------------------------------
# 基本3モード
# --------------------------------------------------------------------------------------
def test_union_adds_the_part_of_the_tool_outside_the_target(session: Session) -> None:
    """x方向に5mmはみ出したツールを union すると 8000 -> 8500(ツールのボディは消える)。"""
    target_id = _add_body(session.document, "target", _box_shape(SIDE, SIDE, SIDE))
    tool_id = _add_body(session.document, "tool", _box_shape(TOOL, TOOL, TOOL, SIDE - TOOL / 2))
    _run(session, target_id, [tool_id], "union")
    assert len(session.document.bodies) == 1
    assert math.isclose(
        _volume(session.document, target_id), SIDE**3 + TOOL**3 / 2, rel_tol=1e-6
    )


def test_cut_removes_the_tool_volume(session: Session) -> None:
    """内側 10mm 立方体を cut すると体積 8000 -> 7000。"""
    target_id, tool_id = _two_boxes(session.document)
    _run(session, target_id, [tool_id], "cut")
    assert len(session.document.bodies) == 1
    assert math.isclose(_volume(session.document, target_id), SIDE**3 - TOOL**3, rel_tol=1e-6)


def test_intersect_with_two_tools_is_union_of_intersections(session: Session) -> None:
    """intersect は target ∩ (tool1 ∪ tool2) —— 角2か所で 1000+1000=2000 になる。"""
    target_id, tool_a_id, tool_b_id = _three_boxes(session.document)
    _run(session, target_id, [tool_a_id, tool_b_id], "intersect")
    assert len(session.document.bodies) == 1
    assert math.isclose(_volume(session.document, target_id), 2 * TOOL**3, rel_tol=1e-5)


# --------------------------------------------------------------------------------------
# エラー
# --------------------------------------------------------------------------------------
def test_non_solid_target_is_rejected(session: Session) -> None:
    doc = session.document
    solid_id = _add_body(doc, "solid", _box_shape(SIDE, SIDE, SIDE))
    shell_id = _add_body(doc, "shell", _face_only(_box_shape(SIDE, SIDE, SIDE)))
    _select(session, [{"body": solid_id, "kind": "face", "index": 1}])
    with pytest.raises(ValueError, match="target body .* is not a solid"):
        boolean_body_verb(doc, _target_ref(shell_id), [solid_id], mode="union")


def test_non_solid_tool_is_rejected(session: Session) -> None:
    doc = session.document
    target_id = _add_body(doc, "target", _box_shape(SIDE, SIDE, SIDE))
    shell_id = _add_body(doc, "shell", _face_only(_box_shape(SIDE, SIDE, SIDE)))
    with pytest.raises(ValueError, match="tool body .* is not a solid"):
        boolean_body_verb(doc, _target_ref(target_id), [shell_id], mode="union")


def test_target_cannot_also_be_a_tool(session: Session) -> None:
    doc = session.document
    target_id, tool_id, _ = _three_boxes(doc)
    with pytest.raises(ValueError, match="cannot also be a tool"):
        boolean_body_verb(doc, _target_ref(target_id), [tool_id, target_id], mode="union")


# --------------------------------------------------------------------------------------
# undo / redo
# --------------------------------------------------------------------------------------
def test_undo_redo_restores_target_shape_and_removed_tools(session: Session) -> None:
    """1回の undo でターゲット形状と消えたツールが戻り、redo で再演算される。"""
    doc = session.document
    target_id, tool_a_id, tool_b_id = _three_boxes(doc)
    target_before = doc.get_body(target_id).shape
    tool_a_before = doc.get_body(tool_a_id).shape
    tool_b_before = doc.get_body(tool_b_id).shape
    body_ids_before = {body.id for body in doc.bodies}

    _run(session, target_id, [tool_a_id, tool_b_id], "cut")
    assert {body.id for body in doc.bodies} == {target_id}
    assert not math.isclose(
        _volume(doc, target_id), query.volume(target_before), rel_tol=1e-9
    )

    session.undo()
    assert {body.id for body in doc.bodies} == body_ids_before
    assert query.volume(doc.get_body(target_id).shape) == pytest.approx(
        query.volume(target_before), rel=1e-9
    )
    assert query.volume(doc.get_body(tool_a_id).shape) == pytest.approx(
        query.volume(tool_a_before), rel=1e-9
    )
    assert query.volume(doc.get_body(tool_b_id).shape) == pytest.approx(
        query.volume(tool_b_before), rel=1e-9
    )
    assert selection.get_selection(doc).items == []

    session.redo()
    assert {body.id for body in doc.bodies} == {target_id}
    assert math.isclose(_volume(doc, target_id), SIDE**3 - 2 * TOOL**3, rel_tol=1e-6)


def test_preview_runs_without_logging(session: Session) -> None:
    """プレビューはログを積まず、形だけ試して戻す。"""
    target_id, tool_id = _two_boxes(session.document)
    _select(session, [{"body": tool_id, "kind": "face", "index": 1}])
    before_count = len(session.document.log.entries)
    before_volume = _volume(session.document, target_id)
    result = session.preview(
        "boolean_body", {"target_ref": _target_ref(target_id), "mode": "cut"}
    )
    assert len(session.document.log.entries) == before_count
    assert len(session.document.bodies) == 2
    assert math.isclose(_volume(session.document, target_id), before_volume, rel_tol=1e-9)
    assert result["verb"] == "boolean_body"
    assert result["bodies"][0]["body_id"] == target_id
