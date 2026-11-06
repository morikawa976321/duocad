"""`add_mesh_body` の名前の検査は `rename` と同じ。落ちたら空の名前や前後に空白のある名前のメッシュボディが作れる。"""
from __future__ import annotations

import pytest

from duocad.document import Document
from duocad.verbs import add_mesh_body

POSITIONS = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
INDICES = [0, 2, 1, 0, 1, 3, 0, 3, 2, 1, 2, 3]


@pytest.mark.parametrize("name", ["", "   ", " m", "m ", "a\nb"])
def test_a_bad_name_is_refused_and_leaves_no_trace(name):
    doc = Document()

    with pytest.raises(ValueError):
        add_mesh_body(doc, POSITIONS, INDICES, name=name)

    assert doc.bodies == [] and doc.log.entries == []


def test_the_same_name_may_be_used_twice():
    doc = Document()
    add_mesh_body(doc, POSITIONS, INDICES, name="m")
    add_mesh_body(doc, POSITIONS, INDICES, name="m")

    assert [body.name for body in doc.bodies] == ["m", "m"]
