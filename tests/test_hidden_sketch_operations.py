"""選択後に隠したスケッチは新規操作から守り、redo の成否は表示状態に依存させない。"""
import copy

import pytest

from duocad import query, selection
from duocad.mcp_server import DocumentTools
from duocad.sketch import get_sketch, sketch_create
from document_support import document_state, replay_through_json


OPERATIONS = [
    ("extrude", {"distance": 2, "mode": "new"}, True),
    ("revolve", {"axis": "x", "angle": 90, "mode": "new"}, True),
    ("loft", {"mode": "new"}, True),
    ("envelope_regions", {"radius": 0, "offset": 2}, True),
    ("move_sketch_entities", {"dx": 1, "dy": 2}, False),
    ("move_sketch_entities", {"mode": "rotate", "angle": 30, "center": [0, 0], "copy": True}, False),
    ("mirror_sketch_entities", {}, False),
    ("delete", {}, False),
]


def prepare(session, verb, arguments, region):
    sketch_id = session.sketch_create("XY")["sketch_id"]
    entity_id = session.add_rectangle(sketch_id, [2, 3], 10, 6)["entity_id"]
    axis_id = session.add_line(sketch_id, [0, 0], [30, 0])["entity_id"] if verb == "mirror_sketch_entities" else None
    args = copy.deepcopy(arguments)
    refs = [{"sketch": sketch_id, "index": 1}] if region else [{"sketch": sketch_id, "entity": entity_id}]
    sketch_ids = [sketch_id]
    if verb == "loft":
        plane = {"kind": "offset", "base": {"kind": "base", "name": "XY"}, "distance": 20}
        second = sketch_create(session.document, plane)
        session.add_rectangle(second, [2, 3], 10, 6)
        sketch_ids.append(second)
        refs.append({"sketch": second, "index": 1})
    if axis_id:
        args["axis"] = {"sketch": sketch_id, "entity": axis_id}
    selection.set_selection(session.document, refs=refs)
    return sketch_ids, args


def snapshot(doc):
    return (doc.log.to_json(created=0), copy.deepcopy(doc.parameters),
            copy.deepcopy(doc.sketches), [(body.id, body.shape) for body in doc.bodies])


@pytest.mark.parametrize("verb,arguments,region", OPERATIONS)
@pytest.mark.parametrize("surface", ["session", "mcp"])
def test_hidden_selected_sketches_are_refused_atomically(session, verb, arguments, region, surface):
    sketches, args = prepare(session, verb, arguments, region)
    session.set_sketch_visible(sketches[-1], False)
    before = snapshot(session.document)
    assert selection.get_selection(session.document).items
    with pytest.raises(ValueError, match="hidden"):
        if surface == "mcp":
            DocumentTools(session).call(verb, args)
        else:
            getattr(session, verb)(**args)
    assert snapshot(session.document) == before


@pytest.mark.parametrize("verb,arguments,region", OPERATIONS)
def test_redo_of_sketch_operations_survives_hiding_the_source(session, verb, arguments, region):
    sketches, args = prepare(session, verb, arguments, region)
    getattr(session, verb)(**args)
    expected = session.document.log.to_json(created=0)
    expected_state = document_state(session.document)
    expected_volumes = [query.volume(body.shape) for body in session.document.bodies]
    assert document_state(replay_through_json(session.document)) == expected_state
    session.undo()
    for sketch_id in sketches:
        session.set_sketch_visible(sketch_id, False)
    session.redo()
    assert session.document.log.to_json(created=0) == expected
    assert document_state(session.document) == expected_state
    assert [query.volume(body.shape) for body in session.document.bodies] == pytest.approx(expected_volumes)
    assert all(not get_sketch(session.document, sketch_id).visible for sketch_id in sketches)


@pytest.mark.parametrize("verb,args", [("move_sketch_entities", {"dx": 1, "dy": 2}), ("delete", {})])
def test_one_hidden_target_prevents_changes_to_visible_targets(session, verb, args):
    refs = []
    for x in (0, 30):
        sketch_id = session.sketch_create("XY")["sketch_id"]
        entity_id = session.add_rectangle(sketch_id, [x, 0], 10, 6)["entity_id"]
        refs.append({"sketch": sketch_id, "entity": entity_id})
    selection.set_selection(session.document, refs=refs)
    session.set_sketch_visible(sketch_id, False)
    before = snapshot(session.document)
    with pytest.raises(ValueError, match="hidden"):
        getattr(session, verb)(**args)
    assert snapshot(session.document) == before


def test_delete_items_can_still_remove_a_hidden_sketch_by_id(session):
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.set_sketch_visible(sketch_id, False)
    session.delete_items(sketches=[sketch_id])
    assert not session.document.sketches
