"""スケッチの塗り(領域の三角形)を粗さごとに覚える(`view._SketchFills`)と、検出と塗りの分離(`region.detect` / `region.fill`)。"""
from __future__ import annotations

import pytest

from duocad import kernel, kernel_sketch, region, view
from duocad.server import Session

#: 寄った粗さと引いた粗さ(1画素の長さ[mm])。半径 20 の円は、どちらでも線形偏差の側で分割が決まる。
NEAR = 0.002
FAR = 0.1
SIZE = 100.0


@pytest.fixture
def counted(monkeypatch):
    """`kernel.tessellate`(ボディが無いので全部が領域の塗り)と `kernel_sketch.sketch_regions`(検出)を呼んだ回数。"""
    calls = {"tessellate": [], "detect": 0}
    tessellate = kernel.tessellate
    detect = kernel_sketch.sketch_regions

    def counting_tessellate(shape, *args, **kwargs):
        calls["tessellate"].append(shape)
        return tessellate(shape, *args, **kwargs)

    def counting_detect(*args, **kwargs):
        calls["detect"] += 1
        return detect(*args, **kwargs)

    monkeypatch.setattr(kernel, "tessellate", counting_tessellate)
    monkeypatch.setattr(kernel_sketch, "sketch_regions", counting_detect)
    monkeypatch.setattr(region, "_DETECTED", type(region._DETECTED)())
    return calls


def _plate(session: Session) -> str:
    """穴 2 つの板(領域 3)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [-60.0, -30.0], 120.0, 60.0)
    session.add_circle(sketch_id, [-30.0, 0.0], 40.0)
    session.add_circle(sketch_id, [30.0, 0.0], 40.0)
    return sketch_id


def _regions(session: Session, sketch_id: str) -> list:
    return next(item for item in session.state_meta()["sketches"] if item["sketch_id"] == sketch_id)["regions"]


def test_returning_to_a_resolution_seen_before_reuses_the_fill(counted) -> None:
    """落ちたら、寄って戻る・引いて戻るたびに全領域を三角形にし直す(穴 384 個の板で配信1回 1.3 秒)か、
    使い回した塗りがその場で求めた塗りと食い違う。"""
    session = Session(0.2)
    sketch_id = _plate(session)
    session.set_view_resolution(FAR, SIZE)
    far = _regions(session, sketch_id)
    session.set_view_resolution(NEAR, SIZE)
    near = _regions(session, sketch_id)
    assert sum(len(item["indices"]) for item in near) > sum(len(item["indices"]) for item in far)

    before = len(counted["tessellate"])
    session.set_view_resolution(FAR, SIZE)
    again_far = _regions(session, sketch_id)
    session.set_view_resolution(NEAR, SIZE)
    again_near = _regions(session, sketch_id)

    assert len(counted["tessellate"]) == before
    assert again_far == far
    assert again_near == near
    assert again_near == region.regions_with_geometry(session.document, sketch_id, session.view.view_deviation)


def test_the_revision_still_moves_when_a_kept_fill_comes_back(counted) -> None:
    """落ちたら、覚えていた塗りへ戻ったときに版が上がらず、画面が前の粗さの塗りを描き続ける。"""
    session = Session(0.2)
    sketch_id = _plate(session)

    def revision() -> int:
        return next(item for item in session.state_meta()["sketches"] if item["sketch_id"] == sketch_id)["revision"]

    session.set_view_resolution(FAR, SIZE)
    first = revision()
    session.set_view_resolution(NEAR, SIZE)
    second = revision()
    session.set_view_resolution(FAR, SIZE)
    assert first < second < revision()


def test_only_the_last_few_resolutions_are_kept(counted) -> None:
    """落ちたら、ズームのたびに段ぶんの塗りが溜まり続ける(覚える段の数に上限が無い)。"""
    session = Session(0.2)
    sketch_id = _plate(session)
    steps = [FAR / 2 ** k for k in range(view.SKETCH_FILLS_PER_SKETCH + 1)]
    for world_per_pixel in steps:
        session.set_view_resolution(world_per_pixel, SIZE)
        _regions(session, sketch_id)
    assert len(session.view._sketch_fills[sketch_id].fills) == view.SKETCH_FILLS_PER_SKETCH

    before = len(counted["tessellate"])
    session.set_view_resolution(steps[1], SIZE)
    _regions(session, sketch_id)
    assert len(counted["tessellate"]) == before, "覚えている段なのに測り直した"
    session.set_view_resolution(steps[0], SIZE)
    _regions(session, sketch_id)
    assert len(counted["tessellate"]) > before, "いちばん古い段を捨てていない"


def test_a_changed_sketch_is_filled_again_at_a_resolution_seen_before(counted) -> None:
    """落ちたら、図形を足した後でも、前に見た粗さへ戻ると足す前の塗り(足した領域が無い)が出る。"""
    session = Session(0.2)
    sketch_id = _plate(session)
    session.set_view_resolution(FAR, SIZE)
    assert len(_regions(session, sketch_id)) == 3
    session.set_view_resolution(NEAR, SIZE)
    _regions(session, sketch_id)
    session.add_rectangle(sketch_id, [100.0, 0.0], 5.0, 5.0)
    session.set_view_resolution(FAR, SIZE)
    regions = _regions(session, sketch_id)
    assert len(regions) == 4
    assert regions == region.regions_with_geometry(session.document, sketch_id, session.view.view_deviation)


def test_changing_the_resolution_does_not_detect_the_regions_again(counted) -> None:
    """落ちたら、検出の表(`region._DETECTED`。編集で 32 件を超えると古いものが出ていく)に無いスケッチは、
    カメラが粗さを変えるたびに `BRepAlgoAPI_Splitter` から計算し直す。"""
    session = Session(0.2)
    sketch_id = _plate(session)
    _regions(session, sketch_id)
    region._DETECTED.clear()
    before = counted["detect"]
    session.set_view_resolution(NEAR, SIZE)
    assert len(_regions(session, sketch_id)) == 3
    assert counted["detect"] == before


def test_the_fill_never_meshes_the_shared_detected_faces(counted) -> None:
    """落ちたら、塗りが検出の表と共有の面に三角形を書き込み、別のスレッドの押し出しが写す面と取り合う
    (`kernel_sketch.copy_face` の docstring)。"""
    session = Session(0.2)
    sketch_id = _plate(session)
    detection = region.detect(session.document.sketches[-1].plane, region.expanded_entities(session.document, sketch_id))
    region.fill(detection, 0.05)
    shared = [item["face"] for item in next(iter(region._DETECTED.values()))]
    assert len(counted["tessellate"]) == len(shared) == 3
    assert not any(meshed.IsSame(face) for meshed in counted["tessellate"] for face in shared)


def test_reopening_the_document_builds_the_fills_again(tmp_path, counted) -> None:
    """落ちたら、開き直した文書(同じ id・同じ中身のスケッチ)が前の文書で作った塗りを使い、壊れたスケッチの知らせが二度と出ない。"""
    session = Session(0.2, models_dir=tmp_path)
    sketch_id = _plate(session)
    _regions(session, sketch_id)
    session.save_document("plate.json")
    before = len(counted["tessellate"])
    session.open_document("plate.json")
    assert len(_regions(session, sketch_id)) == 3
    assert len(counted["tessellate"]) == before + 3
