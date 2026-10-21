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
    """平面上の点 `world` から、半径 `radius`・z=5 の水平な円を平面へ写した楕円までの距離(本体と別の方法で検算する)。"""
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


@pytest.mark.parametrize("inner", [9.99, 9.999])
def test_outline_of_a_thin_tube_keeps_the_ring(inner: float) -> None:
    """外半径 10・内半径 9.99 / 9.999 の管の外形は、細い環(外の円と内の円)。落ちたら割った面の内側の点が弦の外(穴の中)に出て、
    環の影を落としているか、内側の点が見つからず断っている。"""
    session = Session(0.2)
    tube = BRepAlgoAPI_Cut(
        BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 10.0, 5.0).Shape(),
        BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, -1), gp_Dir(0, 0, 1)), inner, 7.0).Shape(),
    ).Shape()
    session.document.add_body(Body(id="tube", name="tube", kind="solid", shape=tube))
    sketch_id = session.sketch_create("XY")["sketch_id"]
    select(session, [{"body": "tube", "kind": "body", "index": None}])
    created = _entities(session, sketch_id, session.project_outline(sketch_id)["created"])
    assert sorted(entity["params"]["diameter"] for entity in created) == pytest.approx([2 * inner, 20.0])


def test_a_bump_between_samples_is_not_flattened_into_a_line() -> None:
    """ノットの区間の中だけに山を持つ B-スプラインの辺は、線分1本に潰さない。落ちたら等間隔の標本が山を飛び越え、
    写した形を失っている。"""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    from OCP.Geom import Geom_BSplineCurve
    from OCP.TColgp import TColgp_Array1OfPnt
    from OCP.TColStd import TColStd_Array1OfInteger, TColStd_Array1OfReal

    from duocad import kernel_project

    inner = [0.004, 0.008, 0.012, 0.02]
    flat = [0.0] * 4 + inner + [1.0] * 4
    knots = TColStd_Array1OfReal(1, len(inner) + 2)
    multiplicities = TColStd_Array1OfInteger(1, len(inner) + 2)
    for i, (value, multiplicity) in enumerate([(0.0, 4), *[(k, 1) for k in inner], (1.0, 4)]):
        knots.SetValue(i + 1, value)
        multiplicities.SetValue(i + 1, multiplicity)
    poles = TColgp_Array1OfPnt(1, len(flat) - 4)
    for i in range(len(flat) - 4):
        greville = sum(flat[i + 1 : i + 4]) / 3.0
        poles.SetValue(i + 1, gp_Pnt(10.0 * greville, 10.0 if i == 3 else 0.0, 3.0))
    edge = BRepBuilderAPI_MakeEdge(Geom_BSplineCurve(poles, knots, multiplicities, 3)).Edge()
    entities = kernel_project.edge_entities({"kind": "base", "name": "XY"}, [edge])
    assert entities and all(entity["kind"] != "line" for entity in entities)
    assert max(max(entity["params"][key] for key in ("y0", "cy1", "cy2", "y1")) for entity in entities) > 1.0


def test_the_same_ellipse_with_its_axis_flipped_is_kept_once() -> None:
    """長軸の向きが逆(角度 0 と 180)の同じ楕円は1つにする。落ちたら1回の投影の中で同じ楕円が2つ残る。"""
    from duocad import kernel_project

    same = {"cx": 1.0, "cy": 2.0, "width": 20.0, "height": 16.0}
    entities = [{"kind": "ellipse", "params": {**same, "angle": 0.0}}, {"kind": "ellipse", "params": {**same, "angle": 180.0}}]
    assert len(kernel_project._deduplicated(entities)) == 1
    arcs = [
        {"kind": "arc", "params": {"cx": 0.0, "cy": 0.0, "radius": 5.0, "start_deg": -90.0, "end_deg": 90.0}},
        {"kind": "arc", "params": {"cx": 0.0, "cy": 0.0, "radius": 5.0, "start_deg": 270.0, "end_deg": 90.0}},
    ]
    assert len(kernel_project._deduplicated(arcs)) == 1


def test_a_curve_swinging_inside_one_knot_span_is_not_flattened() -> None:
    """ノットの区間の中で上下に振れ、ノットと中点ではどれも y = 0 になる B-スプラインを、線分1本に潰さない。落ちたら潰れの判定が
    標本の点だけを見ている(制御点を見ていない)。"""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    from OCP.Geom import Geom_BSplineCurve
    from OCP.TColgp import TColgp_Array1OfPnt
    from OCP.TColStd import TColStd_Array1OfInteger, TColStd_Array1OfReal

    from duocad import kernel_project

    values, mults = [0.0, 0.004, 0.008, 1.0], [4, 3, 3, 4]
    knots = TColStd_Array1OfReal(1, 4)
    multiplicities = TColStd_Array1OfInteger(1, 4)
    for i, (value, multiplicity) in enumerate(zip(values, mults)):
        knots.SetValue(i + 1, value)
        multiplicities.SetValue(i + 1, multiplicity)
    ys = [0.0, 0.0, 0.0, 0.0, 10.0, -10.0, 0.0, 0.0, 0.0, 0.0]
    poles = TColgp_Array1OfPnt(1, len(ys))
    for i, y in enumerate(ys):
        poles.SetValue(i + 1, gp_Pnt(10.0 * i / (len(ys) - 1), y, 2.0))
    curve = Geom_BSplineCurve(poles, knots, multiplicities, 3)
    assert abs(curve.Value(0.005).Y()) > 1.0, "題材の振れが立っていない"
    entities = kernel_project.edge_entities({"kind": "base", "name": "XY"}, [BRepBuilderAPI_MakeEdge(curve).Edge()])
    assert not (len(entities) == 1 and entities[0]["kind"] == "line")


def test_angles_that_fold_to_the_same_value_are_one_key_after_rounding() -> None:
    """軸の角度 20.123456 と 200.123456 の同じ楕円は1つにする。落ちたら丸めてから畳み、20.123456000000004 が別の鍵に残る。"""
    from duocad import kernel_project

    same = {"cx": 1.0, "cy": 2.0, "width": 20.0, "height": 16.0}
    entities = [{"kind": "ellipse", "params": {**same, "angle": 20.123456}}, {"kind": "ellipse", "params": {**same, "angle": 200.123456}}]
    assert len(kernel_project._deduplicated(entities)) == 1


def test_a_narrow_groove_over_a_base_plate_keeps_one_outline() -> None:
    """底板の上に幅 0.0001 mm の溝を挟んで2つの段がある形の外形は、底板の長方形1つ。落ちたら溝の下の細片を影から外し、
    影が2つに割れている(細片は隣の面で決める)。"""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse

    session = Session(0.2)
    base = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10, 10, 1).Shape()
    left = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 1), 5 - 0.00005, 10, 1).Shape()
    right = BRepPrimAPI_MakeBox(gp_Pnt(5 + 0.00005, 0, 1), 5 - 0.00005, 10, 1).Shape()
    solid = BRepAlgoAPI_Fuse(BRepAlgoAPI_Fuse(base, left).Shape(), right).Shape()
    session.document.add_body(Body(id="stepped", name="stepped", kind="solid", shape=solid))
    sketch_id = session.sketch_create("XY")["sketch_id"]
    select(session, [{"body": "stepped", "kind": "body", "index": None}])
    created = _entities(session, sketch_id, session.project_outline(sketch_id)["created"])
    plane = sketch.get_sketch(session.document, sketch_id).plane
    assert _region_areas(plane, created) == pytest.approx([100.0], abs=1e-6)


def test_the_trimmed_part_of_a_spline_that_folds_onto_a_line_becomes_one_segment() -> None:
    """B-スプラインの辺 [0, 0.5] の部分だけが平面へ写すと線の上を往復する。辺の外の制御点を見ずに、線分1本にする。落ちたら
    自己重複するベジエが出て、その線を含む領域が作れない。"""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    from OCP.Geom import Geom_BSplineCurve
    from OCP.TColgp import TColgp_Array1OfPnt
    from OCP.TColStd import TColStd_Array1OfInteger, TColStd_Array1OfReal

    from duocad import kernel_project

    knots = TColStd_Array1OfReal(1, 3)
    multiplicities = TColStd_Array1OfInteger(1, 3)
    for i, (value, multiplicity) in enumerate([(0.0, 4), (0.5, 3), (1.0, 4)]):
        knots.SetValue(i + 1, value)
        multiplicities.SetValue(i + 1, multiplicity)
    coords = [(0, 0, 0), (10, 0, 1), (-10, 0, 2), (0, 0, 3), (1, 10, 4), (2, -10, 5), (3, 0, 6)]
    poles = TColgp_Array1OfPnt(1, len(coords))
    for i, xyz in enumerate(coords):
        poles.SetValue(i + 1, gp_Pnt(*xyz))
    curve = Geom_BSplineCurve(poles, knots, multiplicities, 3)
    entities = kernel_project.edge_entities({"kind": "base", "name": "XY"}, [BRepBuilderAPI_MakeEdge(curve, 0.0, 0.5).Edge()])
    assert [entity["kind"] for entity in entities] == ["line"]
    p = entities[0]["params"]
    assert sorted((p["x0"], p["x1"])) == pytest.approx([-2.886751, 2.886751], abs=1e-5)


def test_a_tiny_arc_of_a_huge_circle_that_collapses_to_a_point_is_dropped() -> None:
    """半径 1000 の円の長さ 0.01 mm の円弧で、平面へ写すと 1e-7 mm に縮むものは図形にしない。落ちたら円全体の向きだけを見て、
    幅 2000 mm の閉じた楕円を出している。"""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    from OCP.Geom import Geom_Circle
    from OCP.gp import gp_Ax2 as Ax2

    from duocad import kernel_project

    circle = Geom_Circle(Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 1, 1e-5), gp_Dir(1, 0, 0)), 1000.0)
    edge = BRepBuilderAPI_MakeEdge(circle, 0.0, 1e-5).Edge()
    assert kernel_project.edge_entities({"kind": "base", "name": "XY"}, [edge]) == []


def test_the_outline_of_a_tilted_o_ring_closes_into_regions() -> None:
    """傾けた O リング(トーラス。大半径 15・小半径 3)の外形から、本体の環と中の穴の2つの領域ができる。落ちたら外形の辺の端点が
    4e-7 mm ずれたまま図形になり、外周が閉じない(端点を揃えていない)。"""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeTorus

    session = Session(0.2)
    ring = BRepPrimAPI_MakeTorus(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(1, 2, 3)), 15.0, 3.0).Shape()
    session.document.add_body(Body(id="ring", name="ring", kind="solid", shape=ring))
    sketch_id = session.sketch_create("YZ")["sketch_id"]
    select(session, [{"body": "ring", "kind": "body", "index": None}])
    created = _entities(session, sketch_id, session.project_outline(sketch_id)["created"])
    plane = sketch.get_sketch(session.document, sketch_id).plane
    areas = _region_areas(plane, created)
    assert len(areas) == 2
    assert areas == pytest.approx([26.638, 385.027], abs=0.05)


def test_endpoints_closer_than_the_weld_tolerance_are_joined_and_arcs_stay_on_their_circle() -> None:
    """揃える距離より近い端点は1点になり、円弧の端は円の上のまま(線分の端が円弧の端へ寄る)。落ちたら STEP の辺の端点の
    ずれ(形状許容差まで)で写した外周が閉じない。"""
    from duocad import kernel_project

    entities = [
        {"kind": "line", "params": {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 0.003}},
        {"kind": "arc", "params": {"cx": 10.0, "cy": 5.0, "radius": 5.0, "start_deg": -90.0, "end_deg": 90.0}},
        {"kind": "line", "params": {"x0": 10.004, "y0": 10.0, "x1": 0.0, "y1": 10.0}},
        {"kind": "line", "params": {"x0": 0.0, "y0": 10.0, "x1": 0.0, "y1": 0.0}},
    ]
    welded = kernel_project._welded(entities)
    assert welded[0]["params"]["x1"] == pytest.approx(10.0) and welded[0]["params"]["y1"] == pytest.approx(0.0, abs=1e-12)
    assert welded[2]["params"]["x0"] == pytest.approx(10.0) and welded[1]["params"]["end_deg"] == pytest.approx(90.0)
    areas = _region_areas({"kind": "base", "name": "XY"}, welded)
    assert areas == pytest.approx([100.0 + math.pi * 25.0 / 2.0], abs=1e-4)


def test_an_almost_full_arc_closed_by_a_tiny_segment_stays_a_hole() -> None:
    """ほぼ一周する円弧(半径 10)と、その両端を結ぶ 0.005 mm の線分で閉じた穴は、端点を揃えると円の穴になる。落ちたら揃えた後の
    大きさで判定して、直径 20 の穴ごと消している。"""
    from duocad import kernel_project

    end = math.radians(359.971352)
    hole = [
        {"kind": "arc", "params": {"cx": 20.0, "cy": 20.0, "radius": 10.0, "start_deg": 0.0, "end_deg": math.degrees(end)}},
        {"kind": "line", "params": {"x0": 20.0 + 10.0 * math.cos(end), "y0": 20.0 + 10.0 * math.sin(end), "x1": 30.0, "y1": 20.0}},
    ]
    welded = kernel_project._welded(hole)
    assert [entity["kind"] for entity in welded] == ["circle"]
    assert welded[0]["params"]["diameter"] == pytest.approx(20.0)
