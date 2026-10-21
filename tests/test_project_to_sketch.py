"""スケッチの投影(issue #184)—— ソリッドの辺・面・ボディの外形をスケッチの平面へ写した図形にする。"""
from __future__ import annotations

import math

import pytest
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakeSphere
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf

from duocad import kernel_sketch, query, sketch
from duocad.document import Body
from duocad.server import Session
from document_support import replay_through_json
from session_support import select

#: 30×20×10 の箱(角 (10, 20, 5))に、半径 4 の貫通穴(中心 (25, 30))と半径 2・深さ 5 の止まり穴(中心 (15, 25)、上から)。
BOX = (10.0, 20.0, 5.0, 30.0, 20.0, 10.0)
THROUGH = (25.0, 30.0, 4.0)
BLIND = (15.0, 25.0, 2.0)


def _holed_box():
    x, y, z, dx, dy, dz = BOX
    shape = BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), dx, dy, dz).Shape()
    cx, cy, r = THROUGH
    shape = BRepAlgoAPI_Cut(shape, BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(cx, cy, 0.0), gp_Dir(0, 0, 1)), r, 30.0).Shape()).Shape()
    bx, by, br = BLIND
    blind = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(bx, by, z + dz / 2.0), gp_Dir(0, 0, 1)), br, 30.0).Shape()
    return BRepAlgoAPI_Cut(shape, blind).Shape()


@pytest.fixture()
def session() -> Session:
    made = Session(0.2)
    made.document.add_body(Body(id="part", name="part", kind="solid", shape=_holed_box()))
    return made


def _faces(session: Session, body_id: str = "part"):
    return query.describe_faces(session.document.get_body(body_id).shape)


def _top_face(session: Session) -> int:
    top = BOX[2] + BOX[5]
    return next(f["index"] for f in _faces(session) if f["kind"] == "plane" and f["normal"][2] > 0.99 and abs(f["center"][2] - top) < 1e-6)


def _entities(session: Session, sketch_id: str, created: list) -> list:
    return [
        {"kind": entity.kind, "params": sketch.entity_params(session.document, sketch_id, entity.id)}
        for entity in sketch.get_sketch(session.document, sketch_id).entities
        if entity.id in set(created)
    ]


def _kinds(entities: list) -> dict:
    counts: dict = {}
    for entity in entities:
        counts[entity["kind"]] = counts.get(entity["kind"], 0) + 1
    return counts


def _region_areas(plane: dict, entities: list) -> list:
    return sorted(round(region["area"], 4) for region in kernel_sketch.sketch_regions(plane, entities))


def test_projecting_the_top_face_adds_its_outline_and_both_hole_circles(session: Session) -> None:
    """上面を真上のスケッチへ写すと外周の4本と、貫通穴・止まり穴の2つの円。落ちたら面のループを取りこぼしている。"""
    sketch_id = session.sketch_create_offset(40.0, "XY")["sketch_id"]
    select(session, [{"body": "part", "kind": "face", "index": _top_face(session)}])
    applied = session.project_to_sketch(sketch_id)
    created = _entities(session, sketch_id, applied["created"])
    assert _kinds(created) == {"line": 4, "circle": 2}
    assert applied["approximated"] == 0
    diameters = sorted(entity["params"]["diameter"] for entity in created if entity["kind"] == "circle")
    assert diameters == pytest.approx([2 * BLIND[2], 2 * THROUGH[2]])
    x, y, _, dx, dy, _ = BOX
    corners = {
        (round(e["params"][kx], 9), round(e["params"][ky], 9)) for e in created if e["kind"] == "line" for kx, ky in (("x0", "y0"), ("x1", "y1"))
    }
    assert corners == {(x, y), (x + dx, y), (x + dx, y + dy), (x, y + dy)}


def test_projected_regions_can_be_extruded(session: Session) -> None:
    """写した外周と穴の円で閉じた領域ができ、そのまま押し出せる。落ちたら写した図形がつながっていない。"""
    sketch_id = session.sketch_create_offset(40.0, "XY")["sketch_id"]
    select(session, [{"body": "part", "kind": "face", "index": _top_face(session)}])
    session.project_to_sketch(sketch_id)
    regions = next(item for item in session.state_meta()["sketches"] if item["sketch_id"] == sketch_id)["regions"]
    _, _, _, dx, dy, _ = BOX
    expected = dx * dy - math.pi * (THROUGH[2] ** 2 + BLIND[2] ** 2)
    assert regions[0]["area"] == pytest.approx(expected, abs=1e-6)
    select(session, [{"sketch": sketch_id, "index": 1}])
    before = len(session.document.bodies)
    session.extrude(3.0, mode="new")
    assert len(session.document.bodies) == before + 1


def test_a_face_perpendicular_to_the_plane_becomes_a_single_line(session: Session) -> None:
    """平面に垂直な側面は線分1本(縦の辺は点に潰れて消え、上下の辺は同じ線分になる)。落ちたら重なった線が2本できる。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    side = next(f["index"] for f in _faces(session) if f["kind"] == "plane" and f["normal"][0] > 0.99)
    select(session, [{"body": "part", "kind": "face", "index": side}])
    created = _entities(session, sketch_id, session.project_to_sketch(sketch_id)["created"])
    assert _kinds(created) == {"line": 1}


def test_only_vertical_edges_are_refused_and_nothing_changes(session: Session) -> None:
    """平面に垂直な辺だけを写すと全部が点に潰れ、断る。文書もログも変わらない。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    shape = session.document.get_body("part").shape
    vertical = [e["index"] for e in query.describe_edges(shape) if e["kind"] == "line" and abs(e["start"][2] - e["end"][2]) > 1.0]
    select(session, [{"body": "part", "kind": "edge", "index": index} for index in vertical])
    entities_before = len(sketch.get_sketch(session.document, sketch_id).entities)
    log_before = len(session.document.log.entries)
    with pytest.raises(ValueError, match="点に潰れ"):
        session.project_to_sketch(sketch_id)
    assert len(sketch.get_sketch(session.document, sketch_id).entities) == entities_before
    assert len(session.document.log.entries) == log_before


def test_a_circle_projected_onto_a_tilted_plane_is_an_exact_ellipse(session: Session) -> None:
    """貫通穴の上の円を 30 度傾けた平面へ写すと、長径 8・短径 8 cos 30° の閉じた楕円(近似しない)。"""
    session.document.add_body(Body(id="tilted", name="tilted", kind="solid", shape=_tilted_plate(30.0)))
    face = next(f["index"] for f in _faces(session, "tilted") if f["kind"] == "plane" and f["normal"][2] > 0.8)
    sketch_id = sketch.sketch_create(session.document, sketch.face_plane("tilted", face))
    shape = session.document.get_body("part").shape
    top = BOX[2] + BOX[5]
    circle = next(
        e["index"] for e in query.describe_edges(shape)
        if e["kind"] == "circle" and abs(e["start"][2] - top) < 1e-6 and abs(e["start"][0] - (THROUGH[0] + THROUGH[2])) < 1e-6
    )
    select(session, [{"body": "part", "kind": "edge", "index": circle}])
    applied = session.project_to_sketch(sketch_id)
    (ellipse,) = _entities(session, sketch_id, applied["created"])
    assert ellipse["kind"] == "ellipse" and applied["approximated"] == 0
    assert ellipse["params"]["width"] == pytest.approx(2 * THROUGH[2])
    assert ellipse["params"]["height"] == pytest.approx(2 * THROUGH[2] * math.cos(math.radians(30.0)))


def test_an_arc_projected_onto_a_tilted_plane_is_approximated_within_the_tolerance(session: Session) -> None:
    """円弧(半円)を斜めの平面へ写すと楕円弧になり、3次ベジエの列で近似される。点はどれも正しい楕円の上 ± 誤差の上限。"""
    half = BRepAlgoAPI_Cut(
        BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 10.0, 5.0).Shape(),
        BRepPrimAPI_MakeBox(gp_Pnt(-20, -20, -1), 40, 20, 10).Shape(),
    ).Shape()
    session.document.add_body(Body(id="half", name="half", kind="solid", shape=half))
    session.document.add_body(Body(id="tilted", name="tilted", kind="solid", shape=_tilted_plate(40.0)))
    face = next(f["index"] for f in _faces(session, "tilted") if f["kind"] == "plane" and f["normal"][2] > 0.7)
    sketch_id = sketch.sketch_create(session.document, sketch.face_plane("tilted", face))
    arc = next(e["index"] for e in query.describe_edges(half) if e["kind"] == "circle" and abs(e["start"][2] - 5.0) < 1e-6)
    select(session, [{"body": "half", "kind": "edge", "index": arc}])
    applied = session.project_to_sketch(sketch_id)
    created = _entities(session, sketch_id, applied["created"])
    assert applied["approximated"] == len(created) > 0
    assert {entity["kind"] for entity in created} == {"bezier"}
    frame = kernel_sketch.sketch_plane_frame(sketch.get_sketch(session.document, sketch_id).plane)
    worst = 0.0
    for entity in created:
        for k in range(21):
            u, v = _bezier_point(entity["params"], k / 20)
            world = [frame["origin"][i] + u * frame["u"][i] + v * frame["v"][i] for i in range(3)]
            worst = max(worst, _distance_to_projected_circle(world, frame["normal"], 10.0))
    assert worst <= kernel_sketch.APPROXIMATION_TOLERANCE


def test_outline_of_a_holed_box_keeps_the_through_hole_and_drops_the_blind_hole(session: Session) -> None:
    """外形を写すと外周と貫通穴だけ(止まり穴の底は影の内側)。押し出せる領域の面積は外周 − 貫通穴。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    select(session, [{"body": "part", "kind": "body", "index": None}])
    created = _entities(session, sketch_id, session.project_outline(sketch_id)["created"])
    assert _kinds(created) == {"line": 4, "circle": 1}
    _, _, _, dx, dy, _ = BOX
    plane = sketch.get_sketch(session.document, sketch_id).plane
    assert _region_areas(plane, created) == pytest.approx(sorted([dx * dy - math.pi * THROUGH[2] ** 2, math.pi * THROUGH[2] ** 2]), abs=1e-4)


def test_outline_of_a_cylinder_lying_down_is_a_rectangle() -> None:
    """横倒しの円柱(軸が X)を XY へ写すと長方形4本(端の円は縁から見て線に潰れ、輪郭線と重なる)。"""
    session = Session(0.2)
    shape = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)), 5.0, 40.0).Shape()
    session.document.add_body(Body(id="rod", name="rod", kind="solid", shape=shape))
    sketch_id = session.sketch_create("XY")["sketch_id"]
    select(session, [{"body": "rod", "kind": "body", "index": None}])
    created = _entities(session, sketch_id, session.project_outline(sketch_id)["created"])
    assert _kinds(created) == {"line": 4}
    plane = sketch.get_sketch(session.document, sketch_id).plane
    assert _region_areas(plane, created) == pytest.approx([400.0], abs=1e-6)


def test_outline_of_two_bodies_is_the_union_of_their_shadows() -> None:
    """重なって見える箱と球の外形は、2つの影を合わせた1周。落ちたらボディごとの外形が重なって出る。"""
    session = Session(0.2)
    session.document.add_body(Body(id="a", name="a", kind="solid", shape=BRepPrimAPI_MakeBox(20, 20, 5).Shape()))
    session.document.add_body(Body(id="b", name="b", kind="solid", shape=BRepPrimAPI_MakeSphere(gp_Pnt(20, 10, 30), 5.0).Shape()))
    sketch_id = session.sketch_create("XY")["sketch_id"]
    select(session, [{"body": "a", "kind": "body", "index": None}, {"body": "b", "kind": "body", "index": None}])
    created = _entities(session, sketch_id, session.project_outline(sketch_id)["created"])
    plane = sketch.get_sketch(session.document, sketch_id).plane
    areas = _region_areas(plane, created)
    assert len(areas) == 1
    assert areas[0] == pytest.approx(400.0 + math.pi * 25.0 / 2.0, abs=1e-3)


def test_undo_removes_the_projection_and_replay_reproduces_it(session: Session) -> None:
    """undo で写した図形だけが消え、ログの再生で同じ図形になる(形を引き直さない)。"""
    sketch_id = session.sketch_create_offset(40.0, "XY")["sketch_id"]
    select(session, [{"body": "part", "kind": "face", "index": _top_face(session)}])
    applied = session.project_to_sketch(sketch_id)
    projected = _entities(session, sketch_id, applied["created"])
    replayed = replay_through_json(session.document)
    assert [
        {"kind": entity.kind, "params": sketch.entity_params(replayed, sketch_id, entity.id)}
        for entity in sketch.get_sketch(replayed, sketch_id).entities
    ] == projected
    session.undo()
    assert sketch.get_sketch(session.document, sketch_id).entities == []


def test_the_preview_ghost_is_the_same_as_the_committed_entities(session: Session) -> None:
    """プレビューの線は確定で足される線と同じ。落ちたら幽霊と結果が食い違う。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    select(session, [{"body": "part", "kind": "body", "index": None}])
    ghost = session.preview("project_outline", {"sketch_id": sketch_id})["sketch_ghost"]
    applied = session.project_outline(sketch_id)
    assert ghost["pieces"] == 1
    assert ghost["entities"] == _entities(session, sketch_id, applied["created"])


def test_a_mesh_body_is_refused(session: Session) -> None:
    from duocad.document import make_mesh

    tetra = make_mesh([0.0, 0.0, 0.0, 10.0, 0.0, 0.0, 0.0, 10.0, 0.0, 0.0, 0.0, 10.0], [0, 2, 1, 0, 1, 3, 0, 3, 2, 1, 2, 3])
    session.document.add_body(Body(id="mesh", name="mesh", kind="mesh", mesh=tetra))
    sketch_id = session.sketch_create("XY")["sketch_id"]
    select(session, [{"body": "mesh", "kind": "body", "index": None}])
    with pytest.raises(ValueError, match="メッシュ"):
        session.project_outline(sketch_id)


# --------------------------------------------------------------------------------------
# 道具
# --------------------------------------------------------------------------------------
def _tilted_plate(degrees: float):
    """x 軸まわりに傾けた 100×100×2 の板(上面に斜めのスケッチを貼る)。"""
    turn = gp_Trsf()
    turn.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)), math.radians(degrees))
    return BRepBuilderAPI_Transform(BRepPrimAPI_MakeBox(gp_Pnt(-50, -50, 60), 100, 100, 2).Shape(), turn, True).Shape()


def _bezier_point(params: dict, t: float) -> tuple:
    r = 1.0 - t
    weights = (r * r * r, 3.0 * r * r * t, 3.0 * r * t * t, t * t * t)
    keys = (("x0", "y0"), ("cx1", "cy1"), ("cx2", "cy2"), ("x1", "y1"))
    return (
        sum(w * params[kx] for w, (kx, _) in zip(weights, keys)),
        sum(w * params[ky] for w, (_, ky) in zip(weights, keys)),
    )


def _distance_to_projected_circle(world: list, normal: list, radius: float) -> float:
    """平面上の点 `world` から、原点中心・半径 `radius`・z=5 の水平な円を同じ平面へ写した楕円までの距離(別の方法で検算する)。

    楕円上の点は円上の点 (r cos θ, r sin θ, 5) を法線方向に平面へ落としたもの。θ の粗い標本で最も近い所を探し、黄金分割で詰める。
    """
    n = normal
    origin_offset = sum(world[i] * n[i] for i in range(3))

    def dist(theta: float) -> float:
        p = (radius * math.cos(theta), radius * math.sin(theta), 5.0)
        height = sum(p[i] * n[i] for i in range(3)) - origin_offset
        return math.dist([p[i] - height * n[i] for i in range(3)], world)

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