"""寸法の作り直し(`parameters.rebuild`)は、最適化条件も空から再生する。"""
from __future__ import annotations

from duocad.document import Document
from duocad.parameters import parameter_of, set_parameter
from duocad.region import region_ref
from duocad.sketch import add_rectangle, sketch_create
from duocad.verbs import extrude
from duocad.verbs.optimize_setup import set_exclude_bodies, set_preserve_bodies


def test_rebuild_after_a_body_changed_roles_does_not_fail_with_role_conflict() -> None:
    """落ちたら、除外に割り当てたボディを保持へ移した文書の寸法の作り直しが、残った条件と衝突して RoleConflict で落ちる。"""
    document = Document()
    sketch = sketch_create(document, "XY")
    rectangle = add_rectangle(document, sketch, (0, 0), 20, 20)
    extrude(document, [region_ref(sketch, 1)], 20, mode="new")
    body = document.bodies[0]
    width = parameter_of(document, sketch, rectangle, "width")
    set_exclude_bodies(document, [body.id])
    set_exclude_bodies(document, [])
    set_preserve_bodies(document, [body.id])
    revision = document.optimize.revision

    set_parameter(document, width, 22)

    assert document.optimize.preserve_bodies == [document.bodies[0].id]
    assert document.optimize.exclude_bodies == []
    assert document.optimize.revision > revision
