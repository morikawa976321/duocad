"""`envelope_regions` の契約 —— 選択領域を D(d)( D(-R)( D(R)(和集合) ) ) で包んだ単一の領域線を同じスケッチへ足す。"""
from __future__ import annotations

import math

import pytest

from duocad import kernel_sketch, sketch
from duocad.server import Session
from document_support import replay_through_json
from session_support import select as _select

PLANE = {"kind": "base", "name": "XY"}


def _region_count(session: Session, sketch_id: str) -> int:
    return len(next(item for item in session.state_meta()["sketches"] if item["sketch_id"] == sketch_id)["regions"])


def _envelope(session: Session, sketch_id: str, radius: float, offset: float, regions=None) -> dict:
    """選択領域(既定は全部)を包む。呼ぶ前後の選択は都度置き直す(確定後に選択は空になる)。"""
    if regions is None:
        regions = range(1, _region_count(session, sketch_id) + 1)
    _select(session, [{"sketch": sketch_id, "index": index} for index in regions])
    return session.envelope_regions(radius, offset)


U_SHAPE = [(0, 0, 20, 100), (100, 0, 20, 100), (0, 0, 120, 20)]


def _sketch_with_rects(session: Session, rects) -> str:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    for x, y, width, height in rects:
        session.add_rectangle(sketch_id, [x, y], width, height)
    return sketch_id


def _created_entities(session: Session, sketch_id: str, created: list) -> list:
    sk = sketch.get_sketch(session.document, sketch_id)
    return [
        {"kind": entity.kind, "params": sketch.entity_params(session.document, sketch_id, entity.id)}
        for entity in sk.entities
        if entity.id in set(created)
    ]


def _area_of_created(session: Session, sketch_id: str, created: list) -> float:
    """足した図形だけでスケッチ領域を数え直し、**ちょうど1領域**であることを確かめて面積を返す。"""
    found = kernel_sketch.sketch_regions(PLANE, _created_entities(session, sketch_id, created))
    assert len(found) == 1, f"足した図形が閉じた1領域になっていない: {[r['area'] for r in found]}"
    return found[0]["area"]


def _snapshot(session: Session, sketch_id: str) -> tuple:
    return (len(sketch.get_sketch(session.document, sketch_id).entities), len(session.document.log.entries))


def test_gap_wider_than_twice_r_is_rejected_and_narrower_is_bridged(session: Session) -> None:
    """隙間5mmの2長方形: R=2 は2個に分かれて拒まれ、R=3 は1枚になる。落ちたら分裂判定か閉じ方が壊れている。"""
    sketch_id = _sketch_with_rects(session, [(0, 0, 10, 10), (15, 0, 10, 10)])
    before = _snapshot(session, sketch_id)

    with pytest.raises(ValueError, match="2 個に分かれます。R か d を大きくしてください"):
        _envelope(session, sketch_id, 2.0, 0.0)
    assert _snapshot(session, sketch_id) == before, "拒んだら文書にもログにも残さない"

    applied = _envelope(session, sketch_id, 3.0, 0.0)
    area = _area_of_created(session, sketch_id, applied["created"])
    assert 200.0 < area < 250.0, "2枚の長方形(200)より広く、隙間を全部埋めた長方形(250)より狭い(へこみの円弧の分だけ)"


def test_u_shape_is_filled_only_at_the_inner_corners(session: Session) -> None:
    """U字形(柱の間隔80mm)に R=10: 面積はUより大きく凸包より小さい。落ちたら凸包になっているかクロージングが効いていない。"""
    sketch_id = _sketch_with_rects(session, U_SHAPE)
    u_area = 20 * 100 * 2 + 80 * 20
    hull_area = 120 * 100

    applied = _envelope(session, sketch_id, 10.0, 0.0)
    area = _area_of_created(session, sketch_id, applied["created"])

    assert u_area < area < hull_area
    assert math.isclose(area - u_area, 2 * (10.0**2 - math.pi * 10.0**2 / 4), rel_tol=1e-6), "内側の2隅にR=10のすみ肉が付くだけ"


def test_output_edges_are_lines_and_arcs_only(session: Session) -> None:
    """出力の図形は線分・円弧・円だけ。落ちたら近似曲線(BSpline・ポリライン)が出ている。"""
    sketch_id = _sketch_with_rects(session, [(0, 0, 10, 10), (12, 3, 10, 10)])
    applied = _envelope(session, sketch_id, 2.0, 1.5)

    kinds = {entity["kind"] for entity in _created_entities(session, sketch_id, applied["created"])}
    assert kinds <= {"line", "arc", "circle"} and "arc" in kinds


def test_positive_offset_grows_negative_shrinks_and_too_negative_is_rejected(session: Session) -> None:
    """d>0 で面積が増え、d<0 で減り、空になる d は別の文で拒まれる。落ちたら d の符号か消失判定が壊れている。"""
    def area(offset: float) -> float:
        """毎回まっさらなスケッチで包む(前の包みの線が領域を割らないように)。"""
        fresh = _sketch_with_rects(session, U_SHAPE)
        return _area_of_created(session, fresh, _envelope(session, fresh, 10.0, offset)["created"])

    assert area(-3.0) < area(0.0) < area(3.0)

    sketch_id = _sketch_with_rects(session, U_SHAPE)

    before = _snapshot(session, sketch_id)
    with pytest.raises(ValueError, match="オフセットで領域が消えます。d を 0 に近づけてください"):
        _envelope(session, sketch_id, 10.0, -30.0)
    assert _snapshot(session, sketch_id) == before


def test_zero_r_and_zero_d_give_the_union_and_reject_separate_regions(session: Session) -> None:
    """R=d=0: 重なる2長方形は和集合の1枚(175)になり、離れた2つは拒まれる。落ちたら和集合の結合が壊れている。"""
    overlapping = _sketch_with_rects(session, [(0, 0, 10, 10), (5, 5, 10, 10)])
    applied = _envelope(session, overlapping, 0.0, 0.0)
    assert math.isclose(_area_of_created(session, overlapping, applied["created"]), 175.0, rel_tol=1e-6)

    apart = _sketch_with_rects(session, [(0, 0, 10, 10), (20, 0, 10, 10)])
    with pytest.raises(ValueError, match="2 個に分かれます"):
        _envelope(session, apart, 0.0, 0.0)


@pytest.mark.parametrize("radius, offset", [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, -1.0), (2.0, 0.0)])
def test_tangent_circle_and_rectangle_never_crash(session: Session, radius: float, offset: float) -> None:
    """辺に1点で接する円と長方形でも、例外で落ちずに単一か分裂かが判定できる。落ちたら縮退のブーリアンが破綻している。"""
    sketch_id = _sketch_with_rects(session, [(0, 0, 10, 10)])
    session.add_circle(sketch_id, [15.0, 5.0], 5.0)
    try:
        applied = _envelope(session, sketch_id, radius, offset)
    except ValueError as error:
        assert "個に分かれます" in str(error) or "消えます" in str(error), str(error)
        return
    assert _area_of_created(session, sketch_id, applied["created"]) > 0.0


def test_region_with_a_hole_counts_as_a_single_region(session: Session) -> None:
    """円環(穴つき1枚)は単一の領域として通り、外周と穴の2つの円が出る。落ちたら穴つきの面を分裂と数えている。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sketch_id, [0.0, 0.0], 20.0)
    session.add_circle(sketch_id, [0.0, 0.0], 4.0)

    applied = _envelope(session, sketch_id, 1.0, -1.0, regions=[1])

    radii = sorted(entity["params"]["radius"] for entity in _created_entities(session, sketch_id, applied["created"]))
    assert radii == pytest.approx([5.0, 19.0])


def test_selection_across_sketches_and_negative_r_are_rejected(session: Session) -> None:
    """複数スケッチにまたがる選択と負のRは拒まれ、何も足されない。落ちたら別平面の領域を混ぜて包んでいる。"""
    first = _sketch_with_rects(session, [(0, 0, 10, 10)])
    second = _sketch_with_rects(session, [(0, 0, 10, 10)])
    _select(session, [{"sketch": first, "index": 1}, {"sketch": second, "index": 1}])
    with pytest.raises(ValueError, match="single sketch"):
        session.envelope_regions(1.0, 0.0)
    with pytest.raises(ValueError, match="radius は 0 以上"):
        _envelope(session, first, -1.0, 0.0)
    assert _snapshot(session, first)[0] == 1 and _snapshot(session, second)[0] == 1


def test_undo_removes_only_the_envelope_and_replay_reproduces_it(session: Session) -> None:
    """undo は足した線だけを消し、JSONを経由した再生は同じ図形を同じIDで作る。落ちたら redo/replay がログの数値を使っていない。"""
    sketch_id = _sketch_with_rects(session, [(0, 0, 10, 10), (15, 0, 10, 10)])
    applied = _envelope(session, sketch_id, 3.0, 0.0)
    with_envelope = _created_entities(session, sketch_id, applied["created"])

    replayed = replay_through_json(session.document)
    replayed_ids = [e.id for e in sketch.get_sketch(replayed, sketch_id).entities]
    assert set(applied["created"]) <= set(replayed_ids)

    session.undo()
    assert len(sketch.get_sketch(session.document, sketch_id).entities) == 2, "元の2長方形だけが残る"
    session.redo()
    assert _created_entities(session, sketch_id, applied["created"]) == with_envelope


def test_bezier_boundary_is_rejected_instead_of_approximated() -> None:
    """ベジエを含む領域は、近似の曲線を出さずに拒む。落ちたら線分・円弧以外が出力に混ざりうる。"""
    entities = [
        {"kind": "bezier", "params": {"x0": 0.0, "y0": 0.0, "cx1": 3.0, "cy1": 8.0, "cx2": 7.0, "cy2": 8.0, "x1": 10.0, "y1": 0.0}},
        {"kind": "line", "params": {"x0": 10.0, "y0": 0.0, "x1": 0.0, "y1": 0.0}},
    ]
    regions = kernel_sketch.sketch_regions(PLANE, entities)
    (piece,) = kernel_sketch.envelope_faces(PLANE, [regions[0]["face"]], 0.0, 0.0)
    with pytest.raises(ValueError, match="ベジエ"):
        kernel_sketch.face_boundary_entities(PLANE, piece)


def test_enveloping_again_over_earlier_envelopes_terminates(session: Session) -> None:
    """包んだ線が重なった領域を全部選んで包み直しても止まる。落ちたら(止まらなければ)面の統合が重なった面で回り続けている。"""
    sketch_id = _sketch_with_rects(session, U_SHAPE)
    for offset in (0.0, 3.0, -3.0):
        applied = _envelope(session, sketch_id, 10.0, offset)
        assert applied["created"]


def _preview(session: Session, sketch_id: str, radius: float, offset: float) -> dict:
    _select(session, [{"sketch": sketch_id, "index": index} for index in range(1, _region_count(session, sketch_id) + 1)])
    return session.preview("envelope_regions", {"radius": radius, "offset": offset})


def test_preview_shows_split_pieces_without_failing_or_touching_the_document(session: Session) -> None:
    """プレビューは分裂しても拒まず全部の線と個数を返し、文書を変えない。落ちたら入力中にトーストが出るか幽霊が文書に残る。"""
    sketch_id = _sketch_with_rects(session, [(0, 0, 10, 10), (15, 0, 10, 10)])
    before = _snapshot(session, sketch_id)

    split = _preview(session, sketch_id, 2.0, 0.0)["sketch_ghost"]
    assert split["sketch_id"] == sketch_id and split["pieces"] == 2
    joined = _preview(session, sketch_id, 3.0, 0.0)["sketch_ghost"]
    assert joined["pieces"] == 1
    assert {entity["kind"] for entity in joined["entities"]} <= {"line", "arc", "circle"}
    assert [entity["kind"] for entity in split["entities"]] == ["line"] * 8, "2枚の長方形の辺がそのまま全部出る"
    assert "arc" in {entity["kind"] for entity in joined["entities"]}
    assert _preview(session, sketch_id, 0.0, -30.0)["sketch_ghost"] == {"sketch_id": sketch_id, "entities": [], "pieces": 0}
    assert _snapshot(session, sketch_id) == before


def test_preview_draws_what_the_verb_would_add(session: Session) -> None:
    """単一領域のときプレビューの線は確定で足される線と同じ。落ちたら幽霊と結果が食い違う。"""
    sketch_id = _sketch_with_rects(session, U_SHAPE)
    ghost = _preview(session, sketch_id, 10.0, 2.0)["sketch_ghost"]
    applied = _envelope(session, sketch_id, 10.0, 2.0)
    assert ghost["entities"] == _created_entities(session, sketch_id, applied["created"])
