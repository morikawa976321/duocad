"""回転体(`duocad.verbs.revolve`)。

検算はすべて算術で決まる値(パップス・ギュルダンの定理: 体積 = 断面積 × 重心が描く円周 × 角度/360):

    10×5 の長方形を長さ10の辺のまわりに 360°   : 円柱 π·5²·10
    y=3..5 の長方形(長さ10)を x 軸まわりに 360°: 管 π·(5²−3²)·10
    半径5の半円を直径のまわりに 360°            : 球 4/3·π·5³
"""
from __future__ import annotations

import math

import pytest
from OCP.BRep import BRep_Tool
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.TopoDS import TopoDS

from duocad import kernel, query
from duocad.document import Document
from document_support import replay_through_json
from duocad.server import Session
from session_support import select
from duocad.region import region_ref, regions
from duocad.sketch import add_arc, add_line, add_rectangle, sketch_create
from duocad.verbs import extrude, fillet, revolve

from duocad import (
    brep_topology as _brep_topology,
)

VOLUME_REL_TOL = 1.0e-9


def _rectangle(document: Document, corner, width, height, plane="XY"):
    sketch_id = sketch_create(document, plane)
    entity = add_rectangle(document, sketch_id, corner, width, height)
    found = regions(document, sketch_id)
    assert len(found) == 1
    return sketch_id, entity, [region_ref(sketch_id, found[0].index)]


def _volume(document: Document) -> float:
    return query.volume(document.active_body().shape)


def test_a_rectangle_around_its_edge_is_a_cylinder():
    """辺の1本(x 軸上)を軸に 360° 回すと円柱。落ちたら軸の位置か向きをスケッチ座標から3Dへ写し損ねている。"""
    document = Document()
    sketch_id, _, refs = _rectangle(document, (0.0, 0.0), 10.0, 5.0)
    edge = add_line(document, sketch_id, (0.0, 0.0), (10.0, 0.0))

    revolve(document, refs, {"sketch": sketch_id, "entity": edge})

    assert math.isclose(_volume(document), math.pi * 25.0 * 10.0, rel_tol=VOLUME_REL_TOL)
    box = kernel.bounding_box(document.active_body().shape)
    assert box["size"] == pytest.approx([10.0, 10.0, 10.0], abs=1e-4)


def test_a_rectangle_away_from_the_axis_is_a_pipe():
    """軸から離した長方形はパイプ。スケッチの x 軸(`"x"`)を軸にできることも見る。"""
    document = Document()
    _, _, refs = _rectangle(document, (0.0, 3.0), 10.0, 2.0)

    revolve(document, refs, "x")

    assert math.isclose(_volume(document), math.pi * (25.0 - 9.0) * 10.0, rel_tol=VOLUME_REL_TOL)


def test_a_half_disc_around_its_diameter_is_a_sphere():
    """円弧を含む断面(半円)を直径のまわりに回すと球。曲線の断面が回せること。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    add_arc(document, sketch_id, (0.0, 0.0), 5.0, 0.0, 180.0)
    diameter = add_line(document, sketch_id, (-5.0, 0.0), (5.0, 0.0))
    found = regions(document, sketch_id)
    assert len(found) == 1

    revolve(document, [region_ref(sketch_id, found[0].index)], {"sketch": sketch_id, "entity": diameter})

    assert math.isclose(_volume(document), 4.0 / 3.0 * math.pi * 125.0, rel_tol=1e-6)
    assert BRepCheck_Analyzer(document.active_body().shape).IsValid() is True


@pytest.mark.parametrize("angle", [90.0, -90.0])
def test_a_partial_angle_gives_that_fraction_and_the_sign_picks_the_side(angle):
    """90° は 360° の 1/4。正は軸の向きまわりの右ねじ(x 軸まわりなら +y が +z へ)、負はその逆。"""
    document = Document()
    _, _, refs = _rectangle(document, (0.0, 3.0), 10.0, 2.0)

    revolve(document, refs, "x", angle)

    assert math.isclose(_volume(document), math.pi * 16.0 * 10.0 / 4.0, rel_tol=VOLUME_REL_TOL)
    box = kernel.bounding_box(document.active_body().shape)
    if angle > 0:
        assert box["max"][2] == pytest.approx(5.0, abs=1e-4) and box["min"][2] == pytest.approx(0.0, abs=1e-4)
    else:
        assert box["min"][2] == pytest.approx(-5.0, abs=1e-4) and box["max"][2] == pytest.approx(0.0, abs=1e-4)


def test_symmetric_splits_the_angle_across_the_sketch_plane():
    """両側(対称)は平面から ±angle/2。体積は同じで、z の範囲が対称になる。"""
    document = Document()
    _, _, refs = _rectangle(document, (0.0, 3.0), 10.0, 2.0)

    revolve(document, refs, "x", 90.0, symmetric=True)

    assert math.isclose(_volume(document), math.pi * 16.0 * 10.0 / 4.0, rel_tol=VOLUME_REL_TOL)
    box = kernel.bounding_box(document.active_body().shape)
    assert box["max"][2] == pytest.approx(-box["min"][2], abs=1e-4)
    assert box["max"][2] == pytest.approx(5.0 * math.sin(math.radians(45.0)), abs=1e-4)


def test_cut_makes_a_groove_around_a_cylinder():
    """`cut` で円柱の外周に溝(Oリング溝)を切る。溝の体積 = π·(5²−4²)·2。"""
    document = Document()
    sketch_id, _, refs = _rectangle(document, (0.0, 0.0), 10.0, 5.0)
    revolve(document, refs, "x")
    _, _, groove = _rectangle(document, (4.0, 4.0), 2.0, 1.0)

    revolve(document, groove, "x", mode="cut")

    assert len(document.bodies) == 1
    expected = math.pi * 25.0 * 10.0 - math.pi * 9.0 * 2.0
    assert math.isclose(_volume(document), expected, rel_tol=VOLUME_REL_TOL)


def test_join_and_new_choose_the_target_like_extrude():
    """join は既存ボディへ足し、new は独立したボディを作る(押し出しと同じ既定)。"""
    document = Document()
    _, _, plate = _rectangle(document, (0.0, 0.0), 10.0, 10.0)
    extrude(document, plate, 2.0)
    _, _, ring = _rectangle(document, (2.0, 2.0), 1.0, 1.0)

    revolve(document, ring, "y", mode="join")
    assert len(document.bodies) == 1

    revolve(document, ring, "y", mode="new")
    assert len(document.bodies) == 2


def test_an_axis_crossing_the_region_is_rejected_without_changes():
    """軸が領域を横切ると自己交差するので例外。文書もログも変わらない。"""
    document = Document()
    _, _, refs = _rectangle(document, (0.0, -1.0), 10.0, 5.0)
    before = len(document.log.entries)

    with pytest.raises(ValueError, match="crosses"):
        revolve(document, refs, "x")

    assert document.bodies == []
    assert len(document.log.entries) == before


@pytest.mark.parametrize(
    "axis, angle, message",
    [
        ("z", 360.0, "axis must be"),
        ("x", 0.0, "angle"),
        ("x", 361.0, "angle"),
    ],
)
def test_bad_arguments_are_rejected(axis, angle, message):
    document = Document()
    _, _, refs = _rectangle(document, (0.0, 3.0), 10.0, 2.0)
    with pytest.raises(ValueError, match=message):
        revolve(document, refs, axis, angle)
    assert document.bodies == []


def test_the_axis_must_be_a_line_of_the_same_sketch():
    """別スケッチの線分・線分以外の図形は軸にできない(軸はそのスケッチ平面の上でなければならない)。"""
    document = Document()
    sketch_id, rectangle, refs = _rectangle(document, (0.0, 3.0), 10.0, 2.0)
    other = sketch_create(document, "XZ")
    line = add_line(document, other, (0.0, 0.0), (10.0, 0.0))

    with pytest.raises(ValueError, match="sketch"):
        revolve(document, refs, {"sketch": other, "entity": line})
    with pytest.raises(ValueError, match="line"):
        revolve(document, refs, {"sketch": sketch_id, "entity": rectangle})


def test_revolve_is_undoable_and_replays_to_the_same_shape():
    """1行で記録され、undo で消え、保存したログの再生で同じ体積に戻る。"""
    document = Document()
    sketch_id, _, refs = _rectangle(document, (0.0, 3.0), 10.0, 2.0, plane="XZ")
    axis = add_line(document, sketch_id, (0.0, 0.0), (10.0, 0.0))
    operation = revolve(document, refs, {"sketch": sketch_id, "entity": axis}, 270.0)
    volume = _volume(document)

    assert operation.verb == "revolve"
    assert operation.args["axis"] == {"sketch": sketch_id, "entity": axis}
    restored = replay_through_json(document)
    assert math.isclose(_volume(restored), volume, rel_tol=VOLUME_REL_TOL)

    document.log.undo(document)
    assert document.bodies == []
    document.log.redo(document)
    assert math.isclose(_volume(document), volume, rel_tol=VOLUME_REL_TOL)


def test_the_seam_edge_of_a_full_revolution_does_not_break_fillet():
    """360° の回転体の継ぎ目の辺(シーム)を丸めようとしても、壊れた形を通さず例外になるか、正しい形になる。

    落ちたら(BRepCheck が通らない形が文書に入った)、フィレットのピックからシームを外す必要がある。
    """
    document = Document()
    _, _, refs = _rectangle(document, (0.0, 3.0), 10.0, 2.0)
    revolve(document, refs, "x")
    shape = document.active_body().shape

    seams = _seam_edge_indices(shape)
    assert seams, "360° の回転体にシームが無い(前提が崩れた)"
    for seam in seams:
        before = len(document.log.entries)
        try:
            fillet(document, [seam], 0.5)
        except ValueError as error:
            assert "fillet failed while building" in str(error), "引数の検査ではなく OCCT の構築失敗で断られる題材"
            assert len(document.log.entries) == before
            continue
        assert BRepCheck_Analyzer(document.active_body().shape).IsValid() is True
        document.log.undo(document)


def _seam_edge_indices(shape) -> list:
    """周期面の継ぎ目の辺(同じ面に2回使われる辺)の添字(1始まり。`brep_topology._edge_map` の数え方)。"""
    edges, faces = _brep_topology._edge_map(shape), _brep_topology._face_map(shape)
    found = []
    for index in range(1, edges.Extent() + 1):
        edge = TopoDS.Edge_s(edges.FindKey(index))
        if any(BRep_Tool.IsClosed_s(edge, TopoDS.Face_s(faces.FindKey(f))) for f in range(1, faces.Extent() + 1)):
            found.append(index)
    return found


@pytest.mark.parametrize(
    "axis_args",
    [{}, {"axis": "x", "axis_line": {"sketch": "s", "entity": "e"}}],
    ids=["neither", "both"],
)
def test_the_public_entry_takes_exactly_one_of_axis_and_axis_line(tmp_path, axis_args):
    """公開面(MCP・WS)は軸を `axis` と `axis_line` に分けて受ける。どちらも無い・両方あるは例外で何も変えない。"""
    session = Session(0.2, models_dir=tmp_path)
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [0.0, 3.0], 10.0, 2.0)
    select(session, [{"sketch": sketch_id, "index": 1}])

    with pytest.raises(ValueError, match="exactly one"):
        session.revolve(mode="join", **axis_args)
    assert session.document.bodies == []


def test_the_public_entry_revolves_around_a_picked_line(tmp_path):
    """`axis_line` で渡した線分が、verb の `axis` として記録される。"""
    session = Session(0.2, models_dir=tmp_path)
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [0.0, 3.0], 10.0, 2.0)
    line = session.add_line(sketch_id, [0.0, 0.0], [10.0, 0.0])["entity_id"]
    select(session, [{"sketch": sketch_id, "index": 1}])

    session.revolve(axis_line={"sketch": sketch_id, "entity": line, "point": None}, mode="join")

    assert math.isclose(_volume(session.document), math.pi * 16.0 * 10.0, rel_tol=VOLUME_REL_TOL)
    assert session.document.log.entries[-1].args["axis"] == {"sketch": sketch_id, "entity": line}
