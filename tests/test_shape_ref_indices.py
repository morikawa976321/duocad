"""選択以外の参照でも添字を切り捨てず、不正な入力は文書を変えない。"""
import pytest

from duocad import selection
from duocad.mcp_server import DocumentTools, ToolError
from duocad.verbs._common import resolve_shape_ref


@pytest.mark.parametrize("index", [1, 1.0, "1"])
def test_shape_refs_accept_integral_indices(box_session, index):
    body, resolved = resolve_shape_ref(
        box_session.document, {"body": "box", "index": index}, kind="edge", what="axis", mesh_hint=""
    )
    assert body.id == "box" and resolved == 1 and type(resolved) is int


@pytest.mark.parametrize("index", [1.9, True, False, None, [], float("inf"), float("nan"), "1.9"])
@pytest.mark.parametrize("verb,arguments,ref_key", [
    ("move_body", {"mode": "rotate", "angle": 90}, "axis_edge"),
    ("mirror_body", {}, "plane_face"),
])
def test_invalid_reference_indices_do_not_change_geometry_or_log(box_session, index, verb, arguments, ref_key):
    selection.set_selection(box_session.document, refs=[{"body": "box", "kind": "body"}])
    original = box_session.document.get_body("box").shape
    tools = DocumentTools(box_session)
    with pytest.raises(ValueError):
        resolve_shape_ref(box_session.document, {"body": "box", "index": index}, kind="edge", what="axis", mesh_hint="")
    with pytest.raises((ValueError, ToolError)):
        tools.call(verb, {**arguments, ref_key: {"body": "box", "index": index}})
    assert box_session.document.get_body("box").shape is original
    assert len(box_session.document.bodies) == 1
    assert not box_session.document.log.entries
