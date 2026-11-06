"""重ならない道具での cut を新しい操作として断る。再生・やり直しは断らない(保存済みの文書を開けるように)。"""
from __future__ import annotations

import math

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt

from duocad import kernel, query
from duocad.document import Body, Document, new_body_id
from duocad.oplog import Operation
from duocad.server import Session
from duocad.verbs import boolean_body as boolean_body_verb
from duocad.verbs.boolean_body import _do_boolean_body
from session_support import select as _select


def _box(size: float, ox: float = 0.0, oy: float = 0.0, oz: float = 0.0):
    return BRepPrimAPI_MakeBox(gp_Pnt(ox, oy, oz), size, size, size).Shape()


def _add(doc: Document, name: str, shape) -> str:
    body_id = new_body_id()
    doc.add_body(Body(id=body_id, name=name, kind="solid", shape=shape))
    return body_id


def _setup(session: Session, tool_origin: float) -> tuple[str, str]:
    target_id = _add(session.document, "target", _box(20.0))
    tool_id = _add(session.document, "tool", _box(7.0, tool_origin, 5.0, 5.0))
    return target_id, tool_id


@pytest.mark.parametrize("tool_origin", [40.0, 20.0, -7.0], ids=["離れている", "面が接するだけ", "反対側で接する"])
def test_cut_with_a_tool_that_does_not_overlap_is_refused_and_nothing_changes(
    session: Session, tool_origin: float
) -> None:
    """落ちたら、重ならない道具の cut が成功してツールのボディだけが消える。"""
    target_id, tool_id = _setup(session, tool_origin)
    _select(session, [{"body": tool_id, "kind": "face", "index": 1}])
    ops_before = len(session.document.log._entries)
    with pytest.raises(ValueError, match="触れていない"):
        session.boolean_body({"body": target_id, "kind": "face", "index": 1}, "cut")
    assert {body.id for body in session.document.bodies} == {target_id, tool_id}
    assert math.isclose(query.volume(session.document.get_body(target_id).shape), 20.0**3, rel_tol=1e-9)
    assert len(session.document.log._entries) == ops_before


def test_cut_that_overlaps_still_succeeds(session: Session) -> None:
    target_id, tool_id = _setup(session, 2.0)
    _select(session, [{"body": tool_id, "kind": "face", "index": 1}])
    session.boolean_body({"body": target_id, "kind": "face", "index": 1}, "cut")
    assert math.isclose(query.volume(session.document.get_body(target_id).shape), 20.0**3 - 7.0**3, rel_tol=1e-6)


@pytest.mark.parametrize(
    "tool_origin, removed",
    [((999.5, 999.5, 999.5), 0.5**3), ((400.0, 400.0, 999.99), 0.5 * 0.5 * 0.01), ((400.0, 400.0, 400.0), 0.5**3)],
    ids=["角を削る", "面を浅く削る", "内側をくり抜く"],
)
def test_a_small_cut_into_a_large_body_is_not_mistaken_for_an_untouched_tool(
    session: Session, tool_origin: tuple, removed: float
) -> None:
    """落ちたら、体積の差がターゲットの体積に比例する許容差(一辺 1000 の箱で 1 mm³)に埋もれる切り取りが、触れていないと断られる。"""
    target_id = _add(session.document, "target", _box(1000.0))
    tool_id = _add(session.document, "tool", _box(0.5, *tool_origin))
    _select(session, [{"body": tool_id, "kind": "face", "index": 1}])
    session.boolean_body({"body": target_id, "kind": "face", "index": 1}, "cut")
    assert [body.id for body in session.document.bodies] == [target_id]
    assert query.volume(session.document.get_body(target_id).shape) == pytest.approx(1000.0**3 - removed, abs=1e-3)


@pytest.mark.parametrize("tool_origin", [(400.0, 400.0, 1000.0), (1400.0, 400.0, 400.0)], ids=["面が接するだけ", "離れている"])
def test_a_small_tool_that_does_not_overlap_a_large_body_is_still_refused(session: Session, tool_origin: tuple) -> None:
    target_id = _add(session.document, "target", _box(1000.0))
    tool_id = _add(session.document, "tool", _box(0.5, *tool_origin))
    _select(session, [{"body": tool_id, "kind": "face", "index": 1}])
    with pytest.raises(ValueError, match="触れていない"):
        session.boolean_body({"body": target_id, "kind": "face", "index": 1}, "cut")
    assert {body.id for body in session.document.bodies} == {target_id, tool_id}


def test_union_with_a_separate_tool_is_not_refused(session: Session) -> None:
    """離れた道具の union は断らない(離れた2体を1ボディにする使い方は本人判断)。"""
    target_id, tool_id = _setup(session, 40.0)
    _select(session, [{"body": tool_id, "kind": "face", "index": 1}])
    session.boolean_body({"body": target_id, "kind": "face", "index": 1}, "union")
    assert [body.id for body in session.document.bodies] == [target_id]


def test_replaying_a_cut_that_removed_nothing_is_not_refused(session: Session) -> None:
    """落ちたら、何も削らなかった cut を含む保存済みの文書が再生で開けなくなる。"""
    target_id, tool_id = _setup(session, 40.0)
    operation = Operation(
        seq=1,
        verb="boolean_body",
        args={"target_body_id": target_id, "tool_bodies": [tool_id], "mode": "cut"},
    )
    _do_boolean_body(session.document, operation)
    assert [body.id for body in session.document.bodies] == [target_id]


def test_unknown_boolean_mode_lists_the_choices() -> None:
    with pytest.raises(ValueError, match=r"one of \['union', 'cut', 'intersect'\]"):
        kernel.boolean_bodies(_box(5.0), [_box(1.0)], "join")
