"""領域検出の共有(`region._DETECTED`)とスケッチの版(`state.sketches[].revision`)。issue #172。"""
from __future__ import annotations

import pytest

from duocad import kernel_sketch, region
from duocad.server import Session


@pytest.fixture
def counted(monkeypatch):
    """`kernel_sketch.sketch_regions`(`BRepAlgoAPI_Splitter`)を呼んだ回数。表は空から始める。"""
    calls = []
    real = kernel_sketch.sketch_regions
    monkeypatch.setattr(kernel_sketch, "sketch_regions", lambda *args, **kwargs: calls.append(1) or real(*args, **kwargs))
    monkeypatch.setattr(region, "_DETECTED", type(region._DETECTED)())
    return calls


def _grid(session: Session, count: int) -> str:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    for i in range(count):
        session.add_rectangle(sketch_id, [(i % 5) * 12.0, (i // 5) * 12.0], 10.0, 10.0)
    return sketch_id


def _revision(session: Session, sketch_id: str) -> int:
    return next(item["revision"] for item in session.state_meta()["sketches"] if item["sketch_id"] == sketch_id)


def test_selecting_many_regions_detects_once(counted) -> None:
    """落ちたら、領域の参照1件ごとに `BRepAlgoAPI_Splitter` を走らせ直す(60 領域で 2.4 秒かかっていた形)。"""
    session = Session(0.2)
    sketch_id = _grid(session, 10)
    session.state_meta()
    before = len(counted)
    session.modify_selection([{"sketch": sketch_id, "index": index} for index in range(1, 11)], [])
    session.state_meta()
    assert len(counted) == before, "選択・配信が同じスケッチの領域を検出し直している"


def test_a_changed_sketch_is_detected_again(counted) -> None:
    """落ちたら、図形を足しても前の領域が返り、新しい領域が指せない・押し出せない。"""
    session = Session(0.2)
    sketch_id = _grid(session, 2)
    assert len(region.regions(session.document, sketch_id)) == 2
    session.add_rectangle(sketch_id, [100.0, 0.0], 5.0, 5.0)
    assert len(region.regions(session.document, sketch_id)) == 3


def test_callers_get_their_own_copies(counted) -> None:
    """落ちたら、呼び手が結果を書き換えると表に残った結果まで変わり、次の呼び手が壊れた領域を受け取る。"""
    session = Session(0.2)
    sketch_id = _grid(session, 2)
    first = region._regions_detailed(session.document, sketch_id)
    first[0]["index"] = 99
    first[0]["centroid"][0] = 1e9
    first.clear()
    again = region._regions_detailed(session.document, sketch_id)
    assert [item["index"] for item in again] == [1, 2]
    assert all(abs(item["centroid"][0]) < 1e6 for item in again)


def test_extruding_selected_regions_matches_the_uncached_answer(counted) -> None:
    """落ちたら、共有した検出結果で押し出した形が、その場で検出した形と食い違う(領域 2 = 2 番目に大きい 110mm² の長方形)。"""

    def extrude_second(session: Session) -> list[float]:
        sketch_id = session.sketch_create("XY")["sketch_id"]
        for i, width in enumerate((10.0, 11.0, 12.0)):
            session.add_rectangle(sketch_id, [i * 20.0, 0.0], width, 10.0)
        session.modify_selection([{"sketch": sketch_id, "index": 2}], [])
        session.extrude(4.0, mode="new")
        return [body["volume"] for body in session.state_meta()["bodies"]]

    cached = extrude_second(Session(0.2))
    region._DETECTED.clear()
    fresh = extrude_second(Session(0.2))
    assert cached == pytest.approx([440.0])
    assert fresh == pytest.approx([440.0])


def test_the_sketch_revision_moves_only_with_what_is_drawn() -> None:
    """落ちたら、選択を変えるだけで画面が全スケッチを作り直す(版が上がる)か、図形を足しても作り直さない(版が上がらない)。"""
    session = Session(0.2)
    sketch_id = _grid(session, 3)
    first = _revision(session, sketch_id)
    session.modify_selection([{"sketch": sketch_id, "index": 1}], [])
    session.set_sketch_visible(sketch_id, False)
    session.set_sketch_visible(sketch_id, True)
    assert _revision(session, sketch_id) == first
    session.add_rectangle(sketch_id, [100.0, 0.0], 5.0, 5.0)
    second = _revision(session, sketch_id)
    assert second > first
    session.set_view_resolution(0.001, 100.0)
    assert _revision(session, sketch_id) > second, "粗さが変われば領域の三角形が変わる"


def test_sketch_revisions_never_repeat_across_documents(tmp_path) -> None:
    """落ちたら、文書を開き直した後の同じ id のスケッチが前の文書と同じ版を名乗り、画面が古いスケッチを描き続ける。"""
    session = Session(0.2, models_dir=tmp_path)
    sketch_id = _grid(session, 1)
    before = _revision(session, sketch_id)
    session.new_document()
    other = _grid(session, 2)
    assert _revision(session, other) > before


def test_faces_handed_out_are_never_shared(counted) -> None:
    """落ちたら、共有した検出結果の面がそのまま外へ出て、別の粗さの三角形分割が混ざる・同じ領域を2回押し出した
    ボディどうしが同じ面を共有する(ブーリアンが共有面として扱う)。"""
    session = Session(0.2)
    sketch_id = _grid(session, 1)
    first = region._regions_detailed(session.document, sketch_id)[0]["face"]
    again = region._regions_detailed(session.document, sketch_id)[0]["face"]
    assert len(counted) == 1
    assert not first.IsSame(again)
    assert not first.IsSame(region._DETECTED[next(iter(region._DETECTED))][0]["face"])
