"""スケッチの入口が、後で壊れた形を残す値を断る契約。
落ちたとき何を意味するか: 桁外れのオフセット距離・誤差ほどの掃引の円弧が入口を通り、以後が失敗するスケッチや別の形が文書に残る。
"""
from __future__ import annotations

import pytest

from duocad.document import Document
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
