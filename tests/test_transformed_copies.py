"""ボディの複製を足す共通部品(`verbs._common.add_transformed_copies`)は、全部足せるときだけ足す。"""
from __future__ import annotations

import pytest

from duocad import naming
from duocad.document import Body, Document
from duocad.verbs._common import add_transformed_copies
from document_support import box_shape


def _two_bodies() -> tuple:
    document = Document()
    first = Body(id="body-a", name="a", kind="solid", shape=box_shape())
    second = Body(id="body-b", name="b", kind="solid", shape=box_shape())
    document.add_body(first)
    document.add_body(second)
    return document, [first, second]


@pytest.mark.parametrize("new_ids", [["body-new", "body-b"], ["body-new", "body-new"]], ids=["existing", "duplicate"])
def test_a_colliding_id_adds_no_copy_and_wastes_no_number(new_ids):
    """落ちたら: 1体目だけが文書に残り(ログには積まれない)、名前に使う通し番号も飛ぶ。"""
    document, bodies = _two_bodies()
    numbering = dict(document.log._numbering)

    with pytest.raises(ValueError, match="already exists"):
        add_transformed_copies(document, bodies, lambda shape: shape, new_ids, allocate=True)

    assert [body.id for body in document.bodies] == ["body-a", "body-b"]
    assert document.log._numbering == numbering
    assert document.active_body().id == "body-b"


def test_copies_get_the_ids_and_numbers_they_were_given():
    document, bodies = _two_bodies()

    numbers = add_transformed_copies(document, bodies, lambda shape: shape, ["body-c", "body-d"], allocate=True)

    assert [body.id for body in document.bodies] == ["body-a", "body-b", "body-c", "body-d"]
    assert [body.name for body in document.bodies[2:]] == [naming.label(naming.BODY, number) for number in numbers]
