"""図形を足す新しい操作は、隠したスケッチへは足さない。redo と再生は表示状態に依存しない。
落ちたとき何を意味するか: 隠したスケッチへ描く・取り込む・投影できてしまう(反復・鏡映・移動・削除は断るのに揃わない)。
"""
from __future__ import annotations

import pytest

from duocad import query
from duocad.sketch import get_sketch
from session_support import SIDE, box as _box, select as _select

DRAWING = [
    ("add_point", lambda s, k: s.add_point(k, [1.0, 1.0])),
    ("add_line", lambda s, k: s.add_line(k, [0.0, 0.0], [5.0, 0.0])),
    ("add_circle", lambda s, k: s.add_circle(k, [0.0, 0.0], 4.0)),
    ("add_ellipse", lambda s, k: s.add_ellipse(k, [0.0, 0.0], 6.0, 4.0)),
    ("add_rectangle", lambda s, k: s.add_rectangle(k, [0.0, 0.0], 5.0, 3.0)),
    ("add_arc", lambda s, k: s.add_arc(k, [0.0, 0.0], 5.0, 0.0, 90.0)),
]


@pytest.mark.parametrize("verb,draw", DRAWING, ids=[name for name, _ in DRAWING])
def test_drawing_into_a_hidden_sketch_is_refused_and_leaves_nothing(session, verb, draw) -> None:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.set_sketch_visible(sketch_id, False)
    log_before = len(session.document.log.entries)
    with pytest.raises(ValueError, match="hidden"):
        draw(session, sketch_id)
    assert get_sketch(session.document, sketch_id).entities == []
    assert len(session.document.log.entries) == log_before


def test_the_drawing_goes_through_again_once_the_sketch_is_shown(session) -> None:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.set_sketch_visible(sketch_id, False)
    session.set_sketch_visible(sketch_id, True)
    session.add_circle(sketch_id, [0.0, 0.0], 4.0)
    assert len(get_sketch(session.document, sketch_id).entities) == 1


def test_redo_of_a_drawing_survives_hiding_the_sketch(session) -> None:
    """断りは新しい操作の入口だけ。undo のあとに隠しても redo は通る。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sketch_id, [0.0, 0.0], 4.0)
    session.undo()
    session.set_sketch_visible(sketch_id, False)
    session.redo()
    assert len(get_sketch(session.document, sketch_id).entities) == 1


def test_importing_an_svg_into_a_hidden_sketch_is_refused(session, tmp_path) -> None:
    path = tmp_path / "line.svg"
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
        '<path d="M 10 10 L 40 10 L 40 30 L 10 30 Z"/></svg>',
        encoding="utf-8",
    )
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.set_sketch_visible(sketch_id, False)
    with pytest.raises(ValueError, match="hidden"):
        session.import_svg(sketch_id, str(path))
    assert get_sketch(session.document, sketch_id).entities == []


@pytest.mark.parametrize("verb", ["project_to_sketch", "project_outline"])
def test_projecting_into_a_hidden_sketch_is_refused(session, verb) -> None:
    body, _ = _box(session)
    target = session.sketch_create_offset(SIDE + 10.0, "XY")["sketch_id"]
    session.set_sketch_visible(target, False)
    top = next(f["index"] for f in query.describe_faces(body.shape) if f["kind"] == "plane" and f["normal"][2] > 0.99)
    _select(session, [{"body": body.id, "kind": "body"}] if verb == "project_outline" else [{"body": body.id, "kind": "face", "index": top}])
    with pytest.raises(ValueError, match="hidden"):
        getattr(session, verb)(target)
    assert get_sketch(session.document, target).entities == []
    session.set_sketch_visible(target, True)
    getattr(session, verb)(target)
    assert get_sketch(session.document, target).entities
