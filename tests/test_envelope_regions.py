"""`envelope_regions` の契約 —— 選択領域を D(d)( D(-R)( D(R)(和集合) ) ) で包んだ単一の領域線を同じスケッチへ足す。"""
from __future__ import annotations

import math
import subprocess
import sys

import pytest

from duocad import kernel_sketch, sketch
from duocad.paths import REPO_ROOT
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
    session.add_circle(sketch_id, [15.0, 5.0], 10.0)
    try:
        applied = _envelope(session, sketch_id, radius, offset)
    except ValueError as error:
        assert "個に分かれます" in str(error) or "消えます" in str(error), str(error)
        return
    assert _area_of_created(session, sketch_id, applied["created"]) > 0.0


def test_region_with_a_hole_counts_as_a_single_region(session: Session) -> None:
    """円環(穴つき1枚)は単一の領域として通り、外周と穴の2つの円が出る。落ちたら穴つきの面を分裂と数えている。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sketch_id, [0.0, 0.0], 40.0)
    session.add_circle(sketch_id, [0.0, 0.0], 8.0)

    applied = _envelope(session, sketch_id, 1.0, -1.0, regions=[1])

    diameters = sorted(entity["params"]["diameter"] for entity in _created_entities(session, sketch_id, applied["created"]))
    assert diameters == pytest.approx([10.0, 38.0])


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


BEZIER_ARCH = {"x0": 0.0, "y0": 0.0, "cx1": 3.0, "cy1": 8.0, "cx2": 7.0, "cy2": 8.0, "x1": 10.0, "y1": 0.0}


def test_bezier_boundary_comes_back_as_the_same_bezier_when_nothing_is_rounded() -> None:
    """R = d = 0 で包んだベジエの領域は、元と同じ制御点のベジエを返す(近似しない)。落ちたらベジエを近似し直して制御点がずれる
    か、ベジエの辺を断っている。"""
    entities = [
        {"kind": "bezier", "params": dict(BEZIER_ARCH)},
        {"kind": "line", "params": {"x0": 10.0, "y0": 0.0, "x1": 0.0, "y1": 0.0}},
    ]
    regions = kernel_sketch.sketch_regions(PLANE, entities)
    (piece,) = kernel_sketch.envelope_faces(PLANE, [regions[0]["face"]], 0.0, 0.0)
    out = kernel_sketch.face_boundary_entities(PLANE, piece)
    (bezier,) = [entity for entity in out if entity["kind"] == "bezier"]
    assert not bezier.get("approximated")
    forward = [bezier["params"][key] for key in ("x0", "y0", "cx1", "cy1", "cx2", "cy2", "x1", "y1")]
    backward = [bezier["params"][key] for key in ("x1", "y1", "cx2", "cy2", "cx1", "cy1", "x0", "y0")]
    expected = [BEZIER_ARCH[key] for key in ("x0", "y0", "cx1", "cy1", "cx2", "cy2", "x1", "y1")]
    assert forward == pytest.approx(expected, abs=1e-9) or backward == pytest.approx(expected, abs=1e-9)


ELLIPSE = (20.0, 10.0, 30.0)  # 幅・高さ・角度


def _ellipse_sketch(session: Session) -> str:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    width, height, angle = ELLIPSE
    session.add_ellipse(sketch_id, [0.0, 0.0], width, height, angle)
    return sketch_id


def _distance_to_ellipse(x: float, y: float) -> float:
    """原点中心の `ELLIPSE` までの最短距離(角の標本から黄金分割で詰める。近似の検算用に、本体とは別の方法で測る)。"""
    width, height, angle = ELLIPSE
    a, b = width / 2.0, height / 2.0
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    lx, ly = x * c + y * s, -x * s + y * c

    def dist(theta: float) -> float:
        return math.hypot(lx - a * math.cos(theta), ly - b * math.sin(theta))

    step = 2.0 * math.pi / 720
    best = min(range(720), key=lambda k: dist(k * step)) * step
    lo, hi = best - step, best + step
    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    for _ in range(80):
        m1, m2 = hi - ratio * (hi - lo), lo + ratio * (hi - lo)
        if dist(m1) < dist(m2):
            hi = m2
        else:
            lo = m1
    return dist((lo + hi) / 2.0)


def _bezier_point(params: dict, t: float) -> tuple:
    r = 1.0 - t
    weights = (r * r * r, 3.0 * r * r * t, 3.0 * r * t * t, t * t * t)
    keys = (("x0", "y0"), ("cx1", "cy1"), ("cx2", "cy2"), ("x1", "y1"))
    return (
        sum(w * params[kx] for w, (kx, _) in zip(weights, keys)),
        sum(w * params[ky] for w, (_, ky) in zip(weights, keys)),
    )


@pytest.mark.parametrize("offset", [2.0, -1.0])
def test_ellipse_offset_is_approximated_by_beziers_within_the_tolerance(session: Session, offset: float) -> None:
    """楕円を d で包むと確定まで通り、出力のベジエの点はどれも楕円から |d| ± 誤差の上限 の距離にある。落ちたら楕円の
    平行曲線を断っている(以前の挙動)か、近似の誤差が上限を超えている。"""
    sketch_id = _ellipse_sketch(session)
    applied = _envelope(session, sketch_id, 0.0, offset)
    created = _created_entities(session, sketch_id, applied["created"])
    assert {entity["kind"] for entity in created} == {"bezier"}
    assert applied["approximated"] == len(created)
    worst = max(
        abs(_distance_to_ellipse(*_bezier_point(entity["params"], k / 40)) - abs(offset))
        for entity in created
        for k in range(41)
    )
    assert worst <= kernel_sketch.APPROXIMATION_TOLERANCE
    assert len(created) <= 40, "区間の数が細かすぎない(誤差の上限 0.001 mm で 20×10 の楕円に 24 区間を実測)"


def test_approximated_beziers_join_with_continuous_tangents(session: Session) -> None:
    """近似したベジエは隣と同じ点でつながり、つなぎ目の接線の向きがそろう。落ちたら押し出した側面に折れ目ができる。"""
    sketch_id = _ellipse_sketch(session)
    applied = _envelope(session, sketch_id, 0.0, 2.0)
    created = _created_entities(session, sketch_id, applied["created"])
    ends = {}
    for entity in created:
        p = entity["params"]
        ends.setdefault((round(p["x0"], 6), round(p["y0"], 6)), []).append((p["cx1"] - p["x0"], p["cy1"] - p["y0"]))
        ends.setdefault((round(p["x1"], 6), round(p["y1"], 6)), []).append((p["x1"] - p["cx2"], p["y1"] - p["cy2"]))
    assert all(len(tangents) == 2 for tangents in ends.values()), "端点は2本ずつで共有される(閉じた1本の線)"
    for (ux, uy), (vx, vy) in ends.values():
        sine = abs(ux * vy - uy * vx) / (math.hypot(ux, uy) * math.hypot(vx, vy))
        assert sine < 1e-6


def test_rounding_an_ellipse_alone_gives_the_same_ellipse_back(session: Session) -> None:
    """へこみの無い楕円を R だけで包むと、元と同じ楕円の図形1つになる(近似のベジエにしない)。落ちたら R 広げて R 縮めた
    ずらし曲線のまま近似している。"""
    sketch_id = _ellipse_sketch(session)
    applied = _envelope(session, sketch_id, 2.0, 0.0)
    (entity,) = _created_entities(session, sketch_id, applied["created"])
    assert entity["kind"] == "ellipse" and applied["approximated"] == 0
    width, height, angle = ELLIPSE
    assert entity["params"] == pytest.approx({"cx": 0.0, "cy": 0.0, "width": width, "height": height, "angle": angle})


def test_rounding_then_offsetting_an_ellipse_stays_a_single_region(session: Session) -> None:
    """楕円を R で丸めてから d で広げても1つの領域で、面積は楕円を d だけ広げたもの(π a b + 周長 d + π d²)。落ちたら OCCT の
    2Dオフセットが楕円のずらし曲線のずらし曲線で壊れた形(2枚・負の面積)を返し、それをそのまま使っている。"""
    sketch_id = _ellipse_sketch(session)
    applied = _envelope(session, sketch_id, 1.0, 1.5)
    width, height, _ = ELLIPSE
    perimeter = kernel_sketch.ellipse_perimeter({"cx": 0.0, "cy": 0.0, "width": width, "height": height, "angle": 0.0})
    expected = math.pi * width * height / 4.0 + perimeter * 1.5 + math.pi * 1.5**2
    assert _area_of_created(session, sketch_id, applied["created"]) == pytest.approx(expected, abs=0.05)


def test_half_ellipse_offset_approximates_the_elliptic_arc_and_keeps_the_steiner_area(session: Session) -> None:
    """楕円を中心を通る線分で割った半分を d で広げると、楕円弧のずらし曲線だけが近似のベジエになり、面積は凸な形の
    平行体の公式(A + 周長 d + π d²)に合う。落ちたら楕円弧(閉じていない楕円)の辺を断っているか、近似が形を崩している。"""
    width, height, angle = ELLIPSE
    sketch_id = _ellipse_sketch(session)
    session.add_line(sketch_id, [-15.0, 0.0], [15.0, 0.0])
    offset = 1.0
    applied = _envelope(session, sketch_id, 0.0, offset, regions=[1])
    created = _created_entities(session, sketch_id, applied["created"])
    assert applied["approximated"] > 0
    assert {entity["kind"] for entity in created} <= {"bezier", "line", "arc"}
    a, b = width / 2.0, height / 2.0
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    half_chord = 1.0 / math.sqrt((c / a) ** 2 + (s / b) ** 2)  # 楕円の中心から x 軸の向きへの半径
    full = kernel_sketch.ellipse_perimeter({"cx": 0.0, "cy": 0.0, "width": width, "height": height, "angle": 0.0})
    perimeter = full / 2.0 + 2.0 * half_chord
    expected = math.pi * a * b / 2.0 + perimeter * offset + math.pi * offset**2
    assert _area_of_created(session, sketch_id, applied["created"]) == pytest.approx(expected, abs=0.05)


def test_ellipse_ghost_is_the_same_as_the_committed_lines(session: Session) -> None:
    """近似を含むときも、プレビューの線は確定で足される線と同じ。落ちたら幽霊と結果が食い違う。"""
    sketch_id = _ellipse_sketch(session)
    ghost = _preview(session, sketch_id, 1.0, 1.5)["sketch_ghost"]
    applied = _envelope(session, sketch_id, 1.0, 1.5)
    assert ghost["pieces"] == 1
    assert ghost["entities"] == _created_entities(session, sketch_id, applied["created"])


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


_EQUAL_AXIS_ELLIPSE_CHILD = """
import sys, tempfile
sys.path.insert(0, "tests")
from session_support import select
from duocad.server import Session

for radius, offset in ((0.0, 2.0), (3.0, 0.0), (0.0, -1.0), (1.0, -0.5)):
    session = Session(0.2, models_dir=tempfile.mkdtemp(), exports_dir=tempfile.mkdtemp())
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_ellipse(sketch_id, [12.0, -12.0], 7.0, 7.0, 45.0)
    select(session, [{"sketch": sketch_id, "index": 1}])
    ghost = session.preview("envelope_regions", {"radius": radius, "offset": offset})["sketch_ghost"]
    assert ghost["pieces"] == 1, ghost["pieces"]
    session.envelope_regions(radius, offset)
print("survived")
"""


def test_enveloping_an_equal_axis_ellipse_does_not_kill_the_process() -> None:
    """縦横が等しい楕円(`docs/ELLIPSE.md` が許す)を包んでも、確定もプレビューも通る。落ちたら離心率0の楕円の辺が
    `BRepOffsetAPI_MakeOffset` に渡り access violation でプロセスごと死ぬ(Python の例外にならないので別プロセスで試す)。"""
    done = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", _EQUAL_AXIS_ELLIPSE_CHILD],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0 and done.stdout.strip() == "survived", (done.returncode, done.stderr[-800:])


def _circles_and_rectangles(session: Session, shift: float = 0.0) -> str:
    """長方形2つと円2つが重なるスケッチ(領域2番と4番を包むと、円の頂点まわりに `GeomAbs_OffsetCurve` の辺が出る組がある)。
    `shift` は全体を (shift, shift) だけ平行移動する(図形の位置によらず同じ結果になるべき)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [9.8689 + shift, -8.6363 + shift], 10.4716, 5.6985)
    session.add_rectangle(sketch_id, [1.978 + shift, 5.1147 + shift], 12.6231, 7.2368)
    session.add_circle(sketch_id, [3.7041 + shift, 1.6792 + shift], 8.6522)
    session.add_circle(sketch_id, [-2.3877 + shift, 4.8011 + shift], 12.9946)
    return sketch_id


def _enveloped_area(session: Session, radius: float, offset: float, shift: float = 0.0) -> float:
    sketch_id = _circles_and_rectangles(session, shift)
    applied = _envelope(session, sketch_id, radius, offset, regions=[2, 4])
    kinds = {entity["kind"] for entity in _created_entities(session, sketch_id, applied["created"])}
    assert kinds <= {"line", "arc", "circle"}
    return _area_of_created(session, sketch_id, applied["created"])


@pytest.mark.parametrize("radius, offset", [(0.5, -0.5), (0.5, -0.25), (0.5, -0.6)])
def test_circles_and_rectangles_are_enveloped_where_offset_curve_edges_appear(session: Session, radius: float, offset: float) -> None:
    """円と長方形だけの領域は、R と d が円の頂点まわりに `GeomAbs_OffsetCurve` の辺を出す組でも包める。落ちたら
    その辺を円弧へ直せず、ベジエも楕円も無いのに「ベジエ曲線を含む領域・楕円を丸めた領域は包めない」と拒む。
    正しさは d をわずかにずらした両側の面積の中間に入ることで見る(円弧の直し方を間違えると面積が飛ぶ)。"""
    area = _enveloped_area(session, radius, offset)
    below = _enveloped_area(session, radius, offset - 5.0e-4)
    above = _enveloped_area(session, radius, offset + 5.0e-4)
    assert area == pytest.approx((below + above) / 2.0, abs=1.0e-3)
    assert below < area < above


@pytest.mark.parametrize("shift", [1000.0, -1000.0])
def test_the_offset_curve_fit_does_not_depend_on_where_the_shapes_are(session: Session, shift: float) -> None:
    """同じ図形を原点から (±1000, ±1000) mm 離しても包める。落ちたら、3点を通る円を絶対座標の二乗の差で解いていて、
    0.01〜0.1 mm の短い弧で桁が落ち、「標本点が直線にも円にも乗らない」で拒む(原点付近では通る)。"""
    assert _enveloped_area(session, 0.5, -0.5, shift) == pytest.approx(_enveloped_area(session, 0.5, -0.5), abs=1.0e-3)


def test_rounding_then_offsetting_an_ellipse_where_the_first_offset_fails_retries(session: Session) -> None:
    """R=1, d=2 では最初の2Dオフセットが IsDone()=False で失敗する(実測)。辺を近似し直して1つの領域にする。落ちたら
    失敗の例外が、壊れた形のときだけの近似し直しを飛ばしている。"""
    sketch_id = _ellipse_sketch(session)
    applied = _envelope(session, sketch_id, 1.0, 2.0)
    width, height, _ = ELLIPSE
    perimeter = kernel_sketch.ellipse_perimeter({"cx": 0.0, "cy": 0.0, "width": width, "height": height, "angle": 0.0})
    expected = math.pi * width * height / 4.0 + perimeter * 2.0 + math.pi * 2.0**2
    assert _area_of_created(session, sketch_id, applied["created"]) == pytest.approx(expected, abs=0.05)


def _distance_to_polyline(point: tuple, polyline: list) -> float:
    best = math.inf
    for (ax, ay), (bx, by) in zip(polyline, polyline[1:]):
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0.0 else max(0.0, min(1.0, ((point[0] - ax) * dx + (point[1] - ay) * dy) / length2))
        best = min(best, math.hypot(point[0] - ax - t * dx, point[1] - ay - t * dy))
    return best


def test_a_bump_narrower_than_the_samples_is_not_skipped() -> None:
    """[0.020, 0.025] だけに山を持つ有理 B-スプラインの辺を近似しても、山の形が誤差の上限内に残る。落ちたら区間の標本が山を
    飛び越え、平らな1本のベジエにしている(ノットで区切っていない)。"""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    from OCP.Geom import Geom_BSplineCurve
    from OCP.TColgp import TColgp_Array1OfPnt
    from OCP.TColStd import TColStd_Array1OfInteger, TColStd_Array1OfReal
    from OCP.gp import gp_Pnt

    # 両端の4重ノットの間に [0.019, 0.026] だけノットを置き、その間だけに効く制御点を持ち上げる。ほかの制御点はグレヴィル点
    # (x = 10 t が正確に出る位置)に置くので、両端の接線から作った1本のベジエは山の外でぴったり合う。
    inner = [0.019, 0.020, 0.0225, 0.025, 0.026]
    flat = [0.0] * 4 + inner + [1.0] * 4
    knots = TColStd_Array1OfReal(1, len(inner) + 2)
    multiplicities = TColStd_Array1OfInteger(1, len(inner) + 2)
    for i, (value, multiplicity) in enumerate([(0.0, 4), *[(k, 1) for k in inner], (1.0, 4)]):
        knots.SetValue(i + 1, value)
        multiplicities.SetValue(i + 1, multiplicity)
    count = len(flat) - 4
    poles = TColgp_Array1OfPnt(1, count)
    weights = TColStd_Array1OfReal(1, count)
    for i in range(count):
        greville = sum(flat[i + 1 : i + 4]) / 3.0
        lifted = i == 4  # 台が [0.019, 0.026] の制御点
        poles.SetValue(i + 1, gp_Pnt(10.0 * greville, 1.0 if lifted else 0.0, 0.0))
        weights.SetValue(i + 1, 2.0 if lifted else 1.0)
    curve = Geom_BSplineCurve(poles, weights, knots, multiplicities, 3)
    edge = BRepBuilderAPI_MakeEdge(curve).Edge()

    entities = kernel_sketch.edge_entities(kernel_sketch._ax3_from_plane(PLANE), edge)
    assert entities and all(entity.get("approximated") for entity in entities)
    polyline = []
    for entity in entities:
        polyline.extend(_bezier_point(entity["params"], k / 200) for k in range(201))
    peak = max(curve.Value(k / 4000).Y() for k in range(4001))
    assert peak > 0.3, "題材の山が立っていない"
    worst = max(
        _distance_to_polyline((curve.Value(k / 4000).X(), curve.Value(k / 4000).Y()), polyline) for k in range(4001)
    )
    assert worst <= kernel_sketch.APPROXIMATION_TOLERANCE + 1e-6


def test_a_smooth_curve_with_many_knots_is_not_split_at_every_knot() -> None:
    """形を変えずにノットを 513 個足した有理 B-スプラインの四分円は、ノットの数によらず数本のベジエになる。落ちたら
    ノットごとに区間を割って区間の上限(512)で断っている。"""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    from OCP.GC import GC_MakeArcOfCircle
    from OCP.GeomConvert import GeomConvert
    from OCP.gp import gp_Pnt

    arc = GC_MakeArcOfCircle(gp_Pnt(10.0, 0.0, 0.0), gp_Pnt(10.0 / math.sqrt(2.0), 10.0 / math.sqrt(2.0), 0.0), gp_Pnt(0.0, 10.0, 0.0)).Value()
    spline = GeomConvert.CurveToBSplineCurve_s(arc)
    low, high = spline.FirstParameter(), spline.LastParameter()
    for k in range(1, 514):
        spline.InsertKnot(low + (high - low) * k / 514, 1, 1.0e-12)
    edge = BRepBuilderAPI_MakeEdge(spline).Edge()
    entities = kernel_sketch.edge_entities(kernel_sketch._ax3_from_plane(PLANE), edge)
    assert 0 < len(entities) <= 16
    worst = max(
        abs(math.hypot(*_bezier_point(entity["params"], k / 40)) - 10.0) for entity in entities for k in range(41)
    )
    assert worst <= kernel_sketch.APPROXIMATION_TOLERANCE


def test_a_bump_away_from_the_knot_midpoint_is_not_skipped() -> None:
    """ノットの区間 [0.020, 0.025] の中で、ノットと区間の中点では y = 0 になり、その間で振れる有理 B-スプラインの辺を近似しても、
    振れが誤差の上限内に残る。落ちたら標本がノットとその間の中点だけで、区間の中の振れを飛び越えている。"""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    from OCP.Geom import Geom_BSplineCurve
    from OCP.TColgp import TColgp_Array1OfPnt
    from OCP.TColStd import TColStd_Array1OfInteger, TColStd_Array1OfReal
    from OCP.gp import gp_Pnt

    breaks = [0.0, 0.020, 0.025, 1.0]
    knots = TColStd_Array1OfReal(1, 4)
    multiplicities = TColStd_Array1OfInteger(1, 4)
    for i, (value, multiplicity) in enumerate(zip(breaks, [4, 3, 3, 4])):
        knots.SetValue(i + 1, value)
        multiplicities.SetValue(i + 1, multiplicity)
    poles = TColgp_Array1OfPnt(1, 10)
    weights = TColStd_Array1OfReal(1, 10)
    # 3つのベジエの区間(制御点を共有)。x は区間ごとに等間隔で x = 10 t、y は真ん中の区間だけ 60 s (s - 1/2)(s - 1)
    xs = [10.0 * (a + (b - a) * k / 3) for a, b in zip(breaks, breaks[1:]) for k in range(3)] + [10.0]
    ys = [0.0, 0.0, 0.0, 0.0, 10.0, -10.0, 0.0, 0.0, 0.0, 0.0]
    for i in range(10):
        poles.SetValue(i + 1, gp_Pnt(xs[i], ys[i], 0.0))
        weights.SetValue(i + 1, 1.001 if i == 8 else 1.0)  # y = 0 の区間の重みを変えて有理にする(近似の経路を通す)
    curve = Geom_BSplineCurve(poles, weights, knots, multiplicities, 3)
    assert curve.IsRational()
    assert abs(curve.Value(0.0225).Y()) < 1e-9 and abs(curve.Value(0.021).Y()) > 1.0, "題材の振れが立っていない"
    edge = BRepBuilderAPI_MakeEdge(curve).Edge()
    entities = kernel_sketch.edge_entities(kernel_sketch._ax3_from_plane(PLANE), edge)
    polyline = []
    for entity in entities:
        polyline.extend(_bezier_point(entity["params"], k / 400) for k in range(401))
    samples = [0.020 + 0.005 * k / 400 for k in range(401)]
    worst = max(_distance_to_polyline((curve.Value(t).X(), curve.Value(t).Y()), polyline) for t in samples)
    assert worst <= kernel_sketch.APPROXIMATION_TOLERANCE + 1e-6


def test_a_trimmed_bezier_edge_keeps_the_curve_of_the_edge() -> None:
    """ベジエの辺の一部を図形にしても、辺の曲線そのものは書き換わらない。落ちたら `Segment` を辺の曲線に掛けている。"""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    from OCP.Geom import Geom_BezierCurve
    from OCP.TColgp import TColgp_Array1OfPnt
    from OCP.gp import gp_Pnt

    poles = TColgp_Array1OfPnt(1, 4)
    for i, (x, y) in enumerate([(0, 0), (3, 8), (7, 8), (10, 0)]):
        poles.SetValue(i + 1, gp_Pnt(x, y, 0))
    bezier = Geom_BezierCurve(poles)
    edge = BRepBuilderAPI_MakeEdge(bezier, 0.2, 0.8).Edge()
    kernel_sketch.edge_entities(kernel_sketch._ax3_from_plane(PLANE), edge)
    assert (bezier.Pole(1).X(), bezier.Pole(4).X()) == (0.0, 10.0)
