"""スケッチの入口が、後で壊れた形を残す値を断る契約。
落ちたとき何を意味するか: 桁外れのオフセット距離・誤差ほどの掃引の円弧が入口を通り、以後が失敗するスケッチや別の形が文書に残る。
"""
from __future__ import annotations

import pytest

from duocad.document import Document
from duocad.parameters import set_parameter
from duocad.sketch import MAX_OFFSET_DISTANCE, get_sketch, offset_plane, sketch_create
from document_support import replay_through_json


def test_an_offset_sketch_at_the_limit_is_created_and_one_beyond_is_refused(session) -> None:
    ok = session.sketch_create_offset(MAX_OFFSET_DISTANCE, "XY")["sketch_id"]
    assert get_sketch(session.document, ok) is not None
    before = len(session.document.sketches)
    log_before = len(session.document.log.entries)
    for distance in (MAX_OFFSET_DISTANCE * 1.001, -MAX_OFFSET_DISTANCE * 2, 1e12, 1e300):
        with pytest.raises(ValueError, match="mm 以下"):
            session.sketch_create_offset(distance, "XY")
    assert len(session.document.sketches) == before
    assert len(session.document.log.entries) == log_before


def test_replaying_an_offset_beyond_the_limit_is_not_refused() -> None:
    """断りは新しい操作の入口だけで、保存済みの文書の再生には掛けない。"""
    document = Document()
    sketch_id = sketch_create(document, offset_plane("XY", 5e6))
    restored = replay_through_json(document)
    assert get_sketch(restored, sketch_id).plane["distance"] == 5e6


@pytest.mark.parametrize("end_deg", [1e-8, 5e-7])
def test_an_arc_whose_sweep_is_only_rounding_error_is_refused(session, end_deg) -> None:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    with pytest.raises(ValueError, match="sweep"):
        session.add_arc(sketch_id, [0.0, 0.0], 10.0, 0.0, end_deg)
    with pytest.raises(ValueError, match="sweep"):
        session.add_arc(sketch_id, [0.0, 0.0], 10.0, 359.99999999, 360.0)
    assert get_sketch(session.document, sketch_id).entities == []


def test_zero_to_360_stays_a_full_circle_and_the_smallest_accepted_sweep_is_an_arc(session) -> None:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    full = session.add_arc(sketch_id, [0.0, 0.0], 10.0, 0.0, 360.0)["entity_id"]
    tiny = session.add_arc(sketch_id, [20.0, 0.0], 10.0, 0.0, 1e-6)["entity_id"]
    assert {full, tiny} <= {entity.id for entity in get_sketch(session.document, sketch_id).entities}


@pytest.mark.parametrize("offset", [0.1, 10.1, 33.3, 123.456])
def test_a_full_circle_arc_survives_a_move_that_shifts_both_angles_by_rounding(offset) -> None:
    """360 の倍数ちょうどのつもりの掃引が、引き算の丸めで 1e-13 度ほど残っても全周として通る。"""
    from duocad.sketch import arc_sweep_is_degenerate

    assert not arc_sweep_is_degenerate(offset, offset + 360.0)


def test_svg_import_drops_an_arc_whose_sweep_is_only_rounding_error(tmp_path) -> None:
    """SVG の弧も入口と同じ基準で捨てる(通すと取り込みが全体の失敗か全周の円になる)。"""
    from duocad.svg_import import read_svg_report

    path = tmp_path / "tiny.svg"
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
        '<path d="M 50 50 A 10 10 0 0 1 50.0000001 50.0"/><path d="M 20 20 L 40 20"/></svg>',
        encoding="utf-8",
    )
    _, shapes, _ = read_svg_report(path)
    assert [shape["kind"] for shape in shapes] == ["line"]


def _document_with_a_tiny_sweep_arc() -> tuple[Document, str]:
    """この断りより前の版で作った文書: 入口を通さず、掃引 1e-8 度の円弧を履歴に入れる。"""
    from duocad.sketch import add_arc, sketch_create
    from duocad.verbs import move_sketch_entities

    document = Document()
    sketch_id = sketch_create(document, "XY")
    arc = add_arc(document, sketch_id, (0.0, 0.0), 10.0, 0.0, 1e-8)
    move_sketch_entities(document, [{"sketch": sketch_id, "entity": arc}], dx=1.0, dy=0.0)
    return document, arc


def test_replaying_a_document_with_a_tiny_sweep_arc_and_a_move_is_not_refused() -> None:
    """落ちたら、断りより前に作った文書(誤差ほどの掃引の円弧を動かした履歴)が開けなくなる。"""
    document, arc = _document_with_a_tiny_sweep_arc()
    restored = replay_through_json(document)
    params = {e.id: dict(e.params) for e in restored.sketches[0].entities}
    assert arc in params


def test_undo_and_redo_of_a_move_over_a_tiny_sweep_arc_are_not_refused(session) -> None:
    """落ちたら、古い文書でやり直し(redo)が ValueError になる。"""
    document, _arc = _document_with_a_tiny_sweep_arc()
    session.document.log = type(document.log).from_json(document.log.to_json())
    session.document.log.replay(session.document)
    session.undo()
    session.redo()
    assert len(session.document.sketches[0].entities) == 1


def test_changing_an_arc_angle_parameter_to_a_tiny_sweep_is_refused_but_other_edits_pass(session) -> None:
    """落ちたら、角度の書き換えが誤差ほどの掃引の円弧を作る。半径など角度以外の編集は断られない。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    entity = session.add_arc(sketch_id, [0.0, 0.0], 10.0, 0.0, 90.0)["entity_id"]
    refs = next(e for e in get_sketch(session.document, sketch_id).entities if e.id == entity).params
    with pytest.raises(ValueError, match="sweep"):
        set_parameter(session.document, refs["end_deg"], 1e-8)
    set_parameter(session.document, refs["radius"], 12.0)
    set_parameter(session.document, refs["end_deg"], 180.0)
