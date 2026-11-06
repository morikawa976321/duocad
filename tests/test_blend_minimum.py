"""フィレットの半径・面取りの距離の下限(押し出しの距離と同じ 1e-6 mm)。新しい操作は断り、再生は断らない。"""
from __future__ import annotations

import pytest

from duocad.document import Document
from duocad.oplog import Operation
from duocad.verbs import chamfer, fillet
from duocad.verbs.chamfer import _do_chamfer
from duocad.verbs.fillet import _do_fillet
from document_support import edges_of_kind, new_box


@pytest.mark.parametrize("verb", [fillet, chamfer], ids=["fillet", "chamfer"])
def test_a_value_below_the_minimum_is_refused_and_the_log_is_unchanged(verb) -> None:
    """落ちたら、1e-9 のフィレット・面取りが成功してログに載る。"""
    doc = Document()
    body_id = new_box(doc)
    edge = edges_of_kind(doc, body_id, "line")[:1]
    entries_before = len(doc.log.entries)
    with pytest.raises(ValueError, match="at least 1e-06 mm"):
        verb(doc, edge, 1e-9, body_id=body_id)
    assert len(doc.log.entries) == entries_before


@pytest.mark.parametrize(
    ("verb", "do", "name", "key"),
    [(fillet, _do_fillet, "fillet", "radius"), (chamfer, _do_chamfer, "chamfer", "distance")],
    ids=["fillet", "chamfer"],
)
def test_replaying_a_value_below_the_minimum_is_not_refused(verb, do, name, key) -> None:
    """落ちたら、下限未満で確定済みの保存済みの文書が再生で開けなくなる。"""
    doc = Document()
    body_id = new_box(doc)
    edge = edges_of_kind(doc, body_id, "line")[0]
    do(doc, Operation(seq=1, verb=name, args={"edges": [edge], key: 1e-9, "body_id": body_id}))
    assert doc.get_body(body_id).shape is not None
