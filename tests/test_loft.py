"""ロフト(`duocad.verbs.loft`)。

検算に使う値(どれも算術で決まる):

    20 角の正方形どうし(平行・間隔 30)      : 角柱 20·20·30 = 12000(断面3つ・間隔 60 なら 24000)
    20 角の正方形 → 高さ 30 の点             : 四角錐 20²·30/3 = 4000
    直径 20 の円 → 高さ 30 の点              : 円錐 π·10²·30/3

始点・向きの揃え方は、合同な配置どうしで体積が一致することで見る(OCCT の自動の揃えに任せると、平面の
軸の取り方しだいで同じ形が潰れたり蝶ネクタイ形になったりした。docs/CAD_LIBRARY_PITFALLS.md「ロフト」)。
"""
from __future__ import annotations

import math

import pytest
from OCP.BRepCheck import BRepCheck_Analyzer

from duocad import query
from duocad.document import Document
from document_support import replay_through_json
from duocad.server import Session
from session_support import select
from duocad.region import region_ref, regions
from duocad.sketch import add_circle, add_ellipse, add_line, add_rectangle, sketch_create
from duocad.verbs import extrude, loft

from duocad import (
    brep_geometry as _brep_geometry,
    brep_topology as _brep_topology,
)

VOLUME_REL_TOL = 1.0e-9


def _offset(name: str, distance: float) -> dict:
    return {"kind": "offset", "base": {"kind": "base", "name": name}, "distance": distance}


def _section(document: Document, plane, kind: str, *args, **kwargs):
    """平面にスケッチを作って図形を1つ描き、`(sketch_id, entity_id, その領域の参照)` を返す。"""
    sketch_id = sketch_create(document, plane)
    draw = {"rect": add_rectangle, "circle": add_circle, "ellipse": add_ellipse}[kind]
    entity = draw(document, sketch_id, *args, **kwargs)
    found = regions(document, sketch_id)
    assert len(found) == 1
    return sketch_id, entity, region_ref(sketch_id, found[0].index)


def _square(document: Document, plane, side: float = 20.0, corner=None):
    corner = (-side / 2, -side / 2) if corner is None else corner
    return _section(document, plane, "rect", corner, side, side)[2]


def _volume(document: Document) -> float:
    return query.volume(document.active_body().shape)


def _valid(document: Document) -> bool:
    return BRepCheck_Analyzer(document.active_body().shape).IsValid()


def test_identical_squares_make_a_prism():
    document = Document()
    loft(document, [_square(document, "XY"), _square(document, _offset("XY", 30.0))])
    assert math.isclose(_volume(document), 12000.0, rel_tol=VOLUME_REL_TOL)


def test_three_identical_squares_make_a_prism_through_the_middle():
    document = Document()
    sections = [_square(document, _offset("XY", z)) for z in (0.0, 30.0, 60.0)]
    loft(document, sections)
    assert math.isclose(_volume(document), 24000.0, rel_tol=VOLUME_REL_TOL)


def test_a_square_to_a_circle_is_a_valid_transition():
    """四角から丸へ変わる立体。体積は角柱(12000)と円柱(π·10²·30)の間に入る。"""
    document = Document()
    _, _, circle = _section(document, _offset("XY", 30.0), "circle", (0.0, 0.0), 20.0)
    loft(document, [_square(document, "XY"), circle])
    assert math.pi * 100.0 * 30.0 < _volume(document) < 12000.0
    assert _valid(document)


def test_points_converge_the_ends():
    document = Document()
    apex_sketch, apex, _ = _section(document, _offset("XY", 30.0), "circle", (0.0, 0.0), 4.0)
    loft(document, [_square(document, "XY")], end_point={"sketch": apex_sketch, "entity": apex, "point": "cx"})
    assert math.isclose(_volume(document), 4000.0, rel_tol=VOLUME_REL_TOL)

    cone = Document()
    apex_sketch, apex, _ = _section(cone, _offset("XY", -30.0), "circle", (0.0, 0.0), 4.0)
    _, _, base = _section(cone, "XY", "circle", (0.0, 0.0), 20.0)
    loft(cone, [base], start_point={"sketch": apex_sketch, "entity": apex, "point": "cx"})
    assert math.isclose(_volume(cone), math.pi * 100.0 * 30.0 / 3.0, rel_tol=1.0e-6)


def test_perpendicular_planes_do_not_collapse():
    """XY の正方形 → YZ / XZ の平面(90°)の正方形。2つは Z 軸まわりの回転で合同なので体積が一致する。

    落ちたら(体積0や不一致)、始点・向きの揃えが平面の軸の取り方に引きずられている(OCCT 任せだと YZ 側が体積0)。
    """
    volumes = []
    for name in ("YZ", "XZ"):
        document = Document()
        _, _, far = _section(document, _offset(name, 30.0), "rect", (-10.0, 20.0), 20.0, 20.0)
        loft(document, [_square(document, "XY"), far])
        assert _valid(document)
        volumes.append(_volume(document))
    assert volumes[0] > 0.0
    assert math.isclose(volumes[0], volumes[1], rel_tol=1.0e-9)


def test_a_box_top_to_each_side_gives_the_same_volume():
    """箱の上面の正方形 → 4つの側面の正方形。配置は Z 軸まわり 90° ごとに合同なので体積が一致する。

    落ちたら、面のスケッチの枠(u 軸の向き)しだいで対応がねじれている(OCCT 任せだと ±X で 500、±Y で 1500)。
    """
    volumes = []
    for side in range(4):
        document = Document()
        base = sketch_create(document, "XY")
        add_rectangle(document, base, (-10.0, -10.0), 20.0, 20.0)
        extrude(document, [region_ref(base, regions(document, base)[0].index)], 20.0)
        body = document.active_body()
        faces = {}
        for index in range(1, _brep_topology.face_count(body.shape) + 1):
            _, normal = _brep_geometry.face_plane(body.shape, index)
            faces[tuple(round(v) for v in normal)] = index
        top = _square(document, {"kind": "face", "body": body.id, "index": faces[(0, 0, 1)]}, 10.0)
        normal = [(1, 0, 0), (0, 1, 0), (-1, 0, 0), (0, -1, 0)][side]
        wall = _square(document, {"kind": "face", "body": body.id, "index": faces[normal]}, 10.0)
        loft(document, [top, wall], mode="new")
        volumes.append(query.volume(document.bodies[-1].shape))
    assert volumes[0] > 0.0
    for other in volumes[1:]:
        assert math.isclose(other, volumes[0], rel_tol=1.0e-9)


def test_an_ellipse_does_not_depend_on_where_its_seam_is():
    """楕円 → 90° 回した楕円。同じ曲線を「幅と高さを入れ替えた楕円」で描いても(継ぎ目の位置だけが違う)体積が一致する。

    落ちたら、閉曲線の継ぎ目を揃えていない(OCCT 任せだと 90° 回した側が体積0に潰れる)。
    """
    volumes = []
    for args in (((0.0, 0.0), 40.0, 10.0, 90.0), ((0.0, 0.0), 10.0, 40.0, 0.0)):
        document = Document()
        _, _, bottom = _section(document, "XY", "ellipse", (0.0, 0.0), 40.0, 10.0)
        _, _, top = _section(document, _offset("XY", 30.0), "ellipse", *args)
        loft(document, [bottom, top])
        volumes.append(_volume(document))
    assert volumes[0] > math.pi * 20.0 * 5.0 * 30.0 / 2.0
    assert math.isclose(volumes[0], volumes[1], rel_tol=1.0e-6)


def test_shifts_move_the_correspondence_and_a_collapse_is_rejected():
    """shifts で始点を1頂点ずらすと 90° ねじれて体積が減る。180° ずらすと、正方形どうしは中央が1点にすぼまり、
    長方形 → 円は側面が軸を横切って自己交差する。どちらも例外で、何も変えない。

    落ちたら、体積が正のまま壊れた形が通っている(どちらも BRepCheck は通す。`BRepAlgoAPI_Check` の自己干渉を
    切っていないか)。
    """
    document = Document()
    bottom, top = _square(document, "XY"), _square(document, _offset("XY", 30.0))
    loft(document, [bottom, top], shifts=[0, 1])
    twisted = _volume(document)
    assert 0.0 < twisted < 12000.0
    assert _valid(document)

    document.log.undo(document)
    with pytest.raises(ValueError, match="intersects"):
        loft(document, [bottom, top], shifts=[0, 2])
    long = _section(document, "XY", "rect", (-20.0, -5.0), 40.0, 10.0)[2]
    _, _, circle = _section(document, _offset("XY", 30.0), "circle", (0.0, 0.0), 20.0)
    large, small = _square(document, _offset("XY", 60.0)), _square(document, _offset("XY", 90.0), 10.0)
    before = len(document.log.entries)
    with pytest.raises(ValueError, match="collapsed|intersects"):
        loft(document, [long, circle], shifts=[0, 2])
    with pytest.raises(ValueError, match="intersects"):
        loft(document, [large, small], shifts=[0, 2])
    assert document.bodies == []
    assert len(document.log.entries) == before


def test_a_smooth_loft_that_pinches_between_sections_is_rejected():
    """半径 10・1・1 の円を順につなぐと、なめらかな補間が断面の間で軸まで食い込んで潰れる。断面の上ではどこも正常。

    落ちたら、断面の間だけで起きる自己交差を見逃している(sol レビューの例)。
    """
    document = Document()
    sections = [
        _section(document, _offset("XY", z), "circle", (0.0, 0.0), diameter)[2]
        for z, diameter in ((0.0, 20.0), (30.0, 2.0), (60.0, 2.0))
    ]
    with pytest.raises(ValueError, match="intersects"):
        loft(document, sections)
    assert document.bodies == []


def test_a_section_curling_behind_its_centre_needs_a_shift_or_is_explained():
    """重心から見て裏に回り込む凹み(U字)は極角で対応が決まらない。U字 → 円は始点をずらすと通り、
    U字 → 正方形はどうずらしても自己交差するので、凹みのせいだと分かる文言の例外で何も変えない。

    落ちたら、凹んだ断面の扱い(長さの割合で割る → OCCT の揃え)か、その失敗の説明が変わった。
    """
    u_shape = [(-10, -10), (10, -10), (10, 10), (4, 10), (4, -4), (-4, -4), (-4, 10), (-10, 10)]

    def u_and(kind, *args):
        document = Document()
        bottom = sketch_create(document, "XY")
        for p0, p1 in zip(u_shape, u_shape[1:] + u_shape[:1]):
            add_line(document, bottom, p0, p1)
        _, _, top = _section(document, _offset("XY", 30.0), kind, *args)
        return document, [region_ref(bottom, 1), top]

    document, sections = u_and("circle", (0.0, 0.0), 20.0)
    loft(document, sections, shifts=[1, 0])
    assert _volume(document) > 0.0
    assert _valid(document)

    document, sections = u_and("rect", (-10.0, -10.0), 20.0, 20.0)
    before = len(document.log.entries)
    with pytest.raises(ValueError, match="凹み"):
        loft(document, sections)
    assert document.bodies == []
    assert len(document.log.entries) == before


def test_cut_carves_the_loft_out_of_a_body():
    document = Document()
    base = sketch_create(document, "XY")
    add_rectangle(document, base, (-10.0, -10.0), 20.0, 20.0)
    extrude(document, [region_ref(base, regions(document, base)[0].index)], 20.0)

    loft(document, [_square(document, "XY", 10.0), _square(document, _offset("XY", 20.0), 10.0)], mode="cut")

    assert len(document.bodies) == 1
    assert math.isclose(_volume(document), 8000.0 - 2000.0, rel_tol=VOLUME_REL_TOL)


def test_join_and_new_choose_the_target_like_extrude():
    document = Document()
    base = sketch_create(document, "XY")
    add_rectangle(document, base, (-10.0, -10.0), 20.0, 20.0)
    extrude(document, [region_ref(base, regions(document, base)[0].index)], 2.0)
    sections = [_square(document, _offset("XY", 2.0), 10.0), _square(document, _offset("XY", 12.0), 10.0)]

    loft(document, sections, mode="join")
    assert len(document.bodies) == 1
    loft(document, sections, mode="new")
    assert len(document.bodies) == 2


def test_a_section_with_a_hole_is_rejected_without_changes():
    """穴のある領域(枠)は断面にできない。文書もログも変わらない。"""
    document = Document()
    ring_sketch = sketch_create(document, "XY")
    add_rectangle(document, ring_sketch, (-10.0, -10.0), 20.0, 20.0)
    add_rectangle(document, ring_sketch, (-5.0, -5.0), 10.0, 10.0)
    ring = region_ref(ring_sketch, 1)
    top = _square(document, _offset("XY", 30.0))
    before = len(document.log.entries)

    with pytest.raises(ValueError, match="hole"):
        loft(document, [ring, top])

    assert document.bodies == []
    assert len(document.log.entries) == before


@pytest.mark.parametrize(
    "case, message",
    [
        ("one_section", "at least two sections"),
        ("same_plane", "same plane"),
        ("point_on_plane", "lies on the plane"),
        ("shift_count", "one entry per section"),
        ("shift_type", "integers"),
        ("bad_point", "sketch point"),
        ("bad_mode", "mode"),
    ],
)
def test_bad_arguments_are_rejected(case, message):
    document = Document()
    bottom = _square(document, "XY")
    top = _square(document, _offset("XY", 30.0))
    point_sketch, point_entity, _ = _section(document, _offset("XY", 60.0), "circle", (30.0, 0.0), 4.0)
    beside = _section(document, "XY", "circle", (40.0, 0.0), 4.0)[2]
    on_plane = {"sketch": point_sketch, "entity": point_entity, "point": "cx"}
    calls = {
        "one_section": lambda: loft(document, [bottom]),
        "same_plane": lambda: loft(document, [bottom, beside]),
        "point_on_plane": lambda: loft(document, [bottom], end_point=_point_on_plane_of(document)),
        "shift_count": lambda: loft(document, [bottom, top], shifts=[1]),
        "shift_type": lambda: loft(document, [bottom, top], shifts=[0, 0.5]),
        "bad_point": lambda: loft(document, [bottom], end_point={"sketch": point_sketch}),
        "bad_mode": lambda: loft(document, [bottom, top], mode="add"),
    }
    with pytest.raises(ValueError, match=message):
        calls[case]()
    assert document.bodies == []


def _point_on_plane_of(document: Document) -> dict:
    """断面と同じ平面(XY)に置いた点(線分の端点)。"""
    sketch_id = sketch_create(document, "XY")
    line = add_line(document, sketch_id, (30.0, 0.0), (35.0, 0.0))
    return {"sketch": sketch_id, "entity": line, "point": "x1"}


def test_loft_is_undoable_and_replays_to_the_same_shape():
    """1行で記録され(点と shifts は参照・数のまま)、undo で消え、保存したログの再生で同じ体積に戻る。"""
    document = Document()
    apex_sketch, apex, _ = _section(document, _offset("XY", 60.0), "circle", (0.0, 0.0), 4.0)
    _, _, middle = _section(document, _offset("XY", 30.0), "circle", (0.0, 0.0), 20.0)
    point = {"sketch": apex_sketch, "entity": apex, "point": "cx"}
    operation = loft(document, [_square(document, "XY"), middle], end_point=point, shifts=[0, 1])
    volume = _volume(document)

    assert operation.verb == "loft"
    assert operation.args["end_point"] == point
    assert operation.args["shifts"] == [0, 1]
    restored = replay_through_json(document)
    assert math.isclose(_volume(restored), volume, rel_tol=VOLUME_REL_TOL)

    document.log.undo(document)
    assert document.bodies == []
    document.log.redo(document)
    assert math.isclose(_volume(document), volume, rel_tol=VOLUME_REL_TOL)


def test_the_public_entry_takes_sections_in_selection_order(tmp_path):
    """公開面(MCP・WS)は選択の領域を選んだ順に断面にし、その順でログに載せる(逆順に選べば同じ形を逆から作る)。"""
    volumes = []
    for reverse in (False, True):
        session = Session(0.2, models_dir=tmp_path / str(reverse))
        refs = []
        for z, kind in ((0.0, "circle"), (30.0, "rect"), (60.0, "circle")):
            sketch_id = session.sketch_create_offset(z, "XY")["sketch_id"] if z else session.sketch_create("XY")["sketch_id"]
            if kind == "circle":
                session.add_circle(sketch_id, [0.0, 0.0], 20.0)
            else:
                session.add_rectangle(sketch_id, [-10.0, -10.0], 20.0, 20.0)
            refs.append({"sketch": sketch_id, "index": 1})
        if reverse:
            refs.reverse()
        select(session, refs)

        result = session.loft(mode="join")

        assert result["sections"] == 3
        recorded = session.document.log.entries[-1].args["sections"]
        assert [(item["sketch"], item["index"]) for item in recorded] == [(r["sketch"], r["index"]) for r in refs]
        volumes.append(_volume(session.document))
    assert math.isclose(volumes[0], volumes[1], rel_tol=1.0e-6)
