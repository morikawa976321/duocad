"""形状編集・移動・回転をまたぐundo/redoと再生で、ボディの形と位置が食い違わないこと。"""
from __future__ import annotations

import pytest

from duocad import kernel, query
from duocad.document import Document
from duocad.verbs import fillet, move_body
from document_support import box_shape, document_state, new_box, replay_through_json

from duocad import (
    brep_geometry as _brep_geometry,
)


def _geometry(document):
    result = {}
    for body in document.bodies:
        box = kernel.bounding_box(body.shape, cache=None)
        result[body.id] = [_brep_geometry.volume(body.shape), *_brep_geometry.centroid(body.shape), *box["min"], *box["max"]]
    return result


def _check_replay(document, expected):
    restored = replay_through_json(document)
    assert document_state(document) == document_state(restored)
    for actual in (_geometry(document), _geometry(restored)):
        assert actual.keys() == expected.keys()
        for body_id in expected:
            assert actual[body_id] == pytest.approx(expected[body_id], rel=1e-9, abs=1e-9), body_id


@pytest.mark.parametrize("change", [
    lambda shape: kernel.translate_shape(shape, 1.0, 0.0, 0.0, copy=True),
    lambda shape: box_shape(10.0, 20.0, 30.0),
], ids=["same-volume-wrong-position", "wrong-size"])
def test_the_geometry_check_detects_changes_missing_from_the_log(change):
    document = Document()
    body_id = new_box(document, 20.0, 30.0, 40.0)
    before = document_state(document)
    body = document.get_body(body_id)
    body.shape = change(body.shape)
    assert document_state(document) == before
    with pytest.raises(AssertionError):
        _check_replay(document, _geometry(document))


def test_mixed_body_edits_keep_their_geometry_through_undo_redo_replay_and_a_new_branch():
    document = Document()
    body_id = new_box(document, 20.0, 30.0, 40.0)
    expected = [_geometry(document)]
    edge = next(
        edge["index"] for edge in query.describe_edges(document.get_body(body_id).shape)
        if edge["length"] == pytest.approx(40.0)
    )
    operations = [
        lambda: fillet(document, [edge], 2.0, body_id=body_id),
        lambda: move_body(document, [body_id], dx=11.0, dy=-7.0, dz=5.0),
        lambda: move_body(
            document, [body_id], mode="rotate", axis_point=[3.0, -2.0, 1.0],
            axis_dir=[1.0, 2.0, 3.0], angle=37.5,
        ),
    ]
    for operation in operations:
        operation()
        expected.append(_geometry(document))
        _check_replay(document, expected[-1])

    assert expected[1][body_id][0] < expected[0][body_id][0]
    assert expected[1][body_id][1:] != expected[2][body_id][1:]
    assert expected[2][body_id][1:] != expected[3][body_id][1:]
    for index in (2, 1, 0):
        document.log.undo(document)
        _check_replay(document, expected[index])
    for index in (1, 2, 3):
        document.log.redo(document)
        _check_replay(document, expected[index])

    document.log.undo(document)
    move_body(document, [body_id], dx=-13.0, dy=17.0, dz=2.0)
    assert not document.log.can_redo
    _check_replay(document, _geometry(document))
