"""押し出しのテーパー(`taper`。正で太る・負で細る。Autodesk Fusion と同じ符号)。issue #182。

検算は算術で決まる値: 距離 z の断面は元の領域を a·z(a = tan(taper))だけ外へずらしたもので、角は尖ったまま。

    長方形 w×h      : ∫(w+2az)(h+2az)dz = whH + (w+h)aH² + (4/3)a²H³
    円 半径 r       : 円錐台 πH/3·(r² + r·r' + r'²)、r' = r + aH
    穴(半径 r)     : 外周と逆に動く(r' = r − aH)
"""
from __future__ import annotations

import json
import math

import pytest
# 断面の面を直に作るためだけに OCP を使う(境界検査は `duocad/` 配下だけが対象)
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeWire,
)
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.GC import GC_MakeArcOfCircle
from OCP.Geom import Geom_BezierCurve
from OCP.TColgp import TColgp_Array1OfPnt
from OCP.TopAbs import TopAbs_FACE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Elips, gp_Pnt

from duocad import kernel, kernel_extrude, query
from duocad.document import Body, Document
from duocad.mcp_server import DocumentTools, McpServer
from duocad.region import region_ref, regions
from duocad.server import Session
from duocad.sketch import add_circle, add_ellipse, add_rectangle, sketch_create
from duocad.verbs import chamfer, extrude, extrude_faces, fillet
from document_support import replay_through_json
from mcp_support import call

REL = 1.0e-9
UP = (0.0, 0.0, 1.0)


def _slope(taper: float) -> float:
    return math.tan(math.radians(taper))


def _frustum(w: float, h: float, height: float, taper: float) -> float:
    a = _slope(taper)
    return w * h * height + (w + h) * a * height**2 + 4.0 / 3.0 * a**2 * height**3


def _cone(r: float, height: float, taper: float) -> float:
    top = r + _slope(taper) * height
    return math.pi * height / 3.0 * (r * r + r * top + top * top)


def _polygon(*points):
    polygon = BRepBuilderAPI_MakePolygon()
    for x, y in points:
        polygon.Add(gp_Pnt(x, y, 0.0))
    polygon.Close()
    return BRepBuilderAPI_MakeFace(polygon.Wire(), True).Face()


def _rect(w: float = 20.0, h: float = 10.0):
    return _polygon((0.0, 0.0), (w, 0.0), (w, h), (0.0, h))


def _disk(r: float, cx: float = 0.0, cy: float = 0.0):
    circle = gp_Circ(gp_Ax2(gp_Pnt(cx, cy, 0.0), gp_Dir(0.0, 0.0, 1.0)), r)
    wire = BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(circle).Edge()).Wire()
    return BRepBuilderAPI_MakeFace(wire, True).Face()


def _holed(r: float = 2.0):
    """長方形 20×10 の中央に半径 r の穴。"""
    shape = BRepAlgoAPI_Cut(_rect(), _disk(r, 10.0, 5.0)).Shape()
    return TopoDS.Face_s(TopExp_Explorer(shape, TopAbs_FACE).Current())


def _ellipse():
    curve = gp_Elips(gp_Ax2(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(0.0, 0.0, 1.0)), 6.0, 3.0)
    wire = BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(curve).Edge()).Wire()
    return BRepBuilderAPI_MakeFace(wire, True).Face()


def _line_arc_bezier():
    """下辺が直線、右が円弧、上がベジエ、左が直線の領域。"""
    a, b, c, d = gp_Pnt(0, 0, 0), gp_Pnt(10, 0, 0), gp_Pnt(10, 10, 0), gp_Pnt(0, 10, 0)
    arc = GC_MakeArcOfCircle(b, gp_Pnt(14, 5, 0), c).Value()
    poles = TColgp_Array1OfPnt(1, 4)
    for i, point in enumerate([c, gp_Pnt(7, 14, 0), gp_Pnt(3, 7, 0), d], 1):
        poles.SetValue(i, point)
    wire = BRepBuilderAPI_MakeWire()
    for edge in (
        BRepBuilderAPI_MakeEdge(a, b).Edge(),
        BRepBuilderAPI_MakeEdge(arc).Edge(),
        BRepBuilderAPI_MakeEdge(Geom_BezierCurve(poles)).Edge(),
        BRepBuilderAPI_MakeEdge(d, a).Edge(),
    ):
        wire.Add(edge)
    return BRepBuilderAPI_MakeFace(wire.Wire(), True).Face()


def _new(face, distance: float, taper: float) -> dict:
    return kernel_extrude.extrude_regions([face], distance, "new", UP, None, taper=taper)


# ======================================================================================
# 幾何(kernel_extrude)
# ======================================================================================
@pytest.mark.parametrize("taper", [5.0, -5.0, 20.0, -20.0])
def test_a_rectangle_becomes_a_frustum_with_sharp_corners(taper):
    """長方形の体積が角錐台の値と合わないか、角が丸まる(平面以外の面ができる)。"""
    result = _new(_rect(), 10.0, taper)

    assert math.isclose(result["volume_after"], _frustum(20.0, 10.0, 10.0, taper), rel_tol=REL)
    kinds = {face["kind"] for face in query.describe_faces(result["shape"])}
    assert kinds == {"plane"}, kinds
    assert kernel.face_count(result["shape"]) == 6


@pytest.mark.parametrize("taper", [5.0, -5.0])
def test_a_circle_becomes_a_cone_frustum(taper):
    result = _new(_disk(5.0), 10.0, taper)

    assert math.isclose(result["volume_after"], _cone(5.0, 10.0, taper), rel_tol=REL)


@pytest.mark.parametrize("taper", [5.0, -5.0])
def test_a_hole_moves_opposite_to_the_outline(taper):
    """穴が外周と同じ向きに動いている(正で穴が広がる)か、穴が無視されている。"""
    result = _new(_holed(2.0), 10.0, taper)

    expected = _frustum(20.0, 10.0, 10.0, taper) - _cone(2.0, 10.0, -taper)
    assert math.isclose(result["volume_after"], expected, rel_tol=REL)


@pytest.mark.parametrize("face", [_ellipse, _line_arc_bezier], ids=["ellipse", "line-arc-bezier"])
def test_curved_outlines_taper_both_ways(face):
    """楕円・ベジエの側面が傾かない(DraftAngle はこれを断る)か、太る/細るが逆になっている。"""
    straight = _new(face(), 10.0, 0.0)["volume_after"]
    grown = _new(face(), 10.0, 5.0)
    shrunk = _new(face(), 10.0, -5.0)

    assert grown["volume_after"] > straight > shrunk["volume_after"] > 0
    for result in (grown, shrunk):
        assert BRepCheck_Analyzer(result["shape"]).IsValid()
    reach = 10.0 * _slope(5.0)
    base = kernel.bounding_box(face())
    top = kernel.bounding_box(grown["shape"])
    assert top["max"][0] == pytest.approx(base["max"][0] + reach, abs=1e-6)
    assert top["min"][1] == pytest.approx(base["min"][1] - reach, abs=1e-6)


def test_a_negative_distance_tapers_along_the_way_it_goes():
    """負の距離(逆向き)で太る/細るが反転している。"""
    result = _new(_rect(), -10.0, 5.0)

    assert math.isclose(result["volume_after"], _frustum(20.0, 10.0, 10.0, 5.0), rel_tol=REL)
    box = kernel.bounding_box(result["shape"])
    assert box["min"][2] == pytest.approx(-10.0, abs=1e-9)
    assert box["min"][0] == pytest.approx(-10.0 * _slope(5.0), abs=1e-9)


def _notched():
    """上辺に深さ 0.5 の V 字の切り欠き。太ると埋まる。"""
    return _polygon((0, 0), (20, 0), (20, 10), (10.5, 10), (10, 9.5), (9.5, 10), (0, 10))


def _chamfered():
    """角に長さ √2 の面取り辺。細ると reach ≈ 1.71 で消える。"""
    return _polygon((0, 0), (20, 0), (20, 9), (19, 10), (0, 10))


def _slotted():
    """幅 4 の溝を持つ U 字。太ると溝の壁がぶつかる。"""
    return _polygon((0, 0), (20, 0), (20, 20), (12, 20), (12, 5), (8, 5), (8, 20), (0, 20))


@pytest.mark.parametrize(
    "face, taper",
    [
        (_rect, -40.0),  # 幅 10 が潰れる
        (lambda: _disk(5.0), -40.0),  # 円錐が頂点を越える(BRepCheck も自己交差の検査も通す)
        (_slotted, 12.0),  # 溝の壁どうしがぶつかる
        (lambda: _holed(2.0), 12.0),  # 穴が閉じる
        (lambda: _holed(2.0), -20.0),  # 穴が外周を破る
        (_chamfered, -15.0),  # 面取り辺が消える
        (_notched, 10.0),  # 切り欠きが埋まる
    ],
    ids=["rect-width", "circle-apex", "slot-walls", "hole-closes", "hole-breaks-out", "edge-vanishes", "notch-fills"],
)
def test_a_collapsing_section_is_refused(face, taper):
    """断面が潰れる組み合わせが、潰れた形のまま成功として通る。"""
    with pytest.raises(ValueError, match="collapses"):
        _new(face(), 10.0, taper)


@pytest.mark.parametrize("face, taper", [(_slotted, 5.0), (_chamfered, -5.0), (_notched, 2.0)])
def test_the_same_outlines_pass_before_they_collapse(face, taper):
    """潰れの検出が、潰れていない形まで断っている。"""
    result = _new(face(), 10.0, taper)
    assert BRepCheck_Analyzer(result["shape"]).IsValid()


@pytest.mark.parametrize("taper", [90.0, -90.0, 120.0, float("nan"), float("inf")])
def test_taper_must_be_below_90_degrees(taper):
    with pytest.raises(ValueError, match="taper"):
        _new(_rect(), 10.0, taper)


def test_zero_taper_is_the_plain_extrusion():
    """0 で別経路(MakeDraft)を通り、今までの押し出しと形が変わる。"""
    plain = kernel_extrude.extrude_regions([_rect()], 10.0, "new", UP, None)
    zero = _new(_rect(), 10.0, 0.0)

    assert zero["volume_after"] == plain["volume_after"]
    assert kernel.face_count(zero["shape"]) == kernel.face_count(plain["shape"])
    assert "taper" not in zero["provenance"]


# ======================================================================================
# verb(ログ・undo・再生・面の押し出し)
# ======================================================================================
def _sketch_rect(document: Document, corner=(0.0, 0.0), w: float = 20.0, h: float = 10.0) -> list:
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, corner, w, h)
    return [region_ref(sketch_id, regions(document, sketch_id)[0].index)]


def _state(document: Document) -> tuple:
    return (
        json.dumps(document.log.to_json(), sort_keys=True),
        [(body.id, kernel.volume(body.shape), kernel.face_count(body.shape)) for body in document.bodies],
    )


def test_taper_is_logged_only_when_set_and_replays():
    """テーパーがログに残らず再生で真っ直ぐに戻る、または 0 のときも記録して古いログと args が変わる。"""
    document = Document()
    plain = extrude(document, _sketch_rect(document), 10.0, mode="new")
    tapered = extrude(document, _sketch_rect(document, (30.0, 0.0)), 10.0, mode="new", taper=-5.0)

    assert "taper" not in plain.args
    assert tapered.args["taper"] == -5.0
    assert tapered.provenance["taper"] == -5.0

    restored = replay_through_json(document)
    assert [kernel.volume(body.shape) for body in restored.bodies] == pytest.approx(
        [2000.0, _frustum(20.0, 10.0, 10.0, -5.0)], rel=REL
    )


def test_tapered_extrusion_undoes_and_redoes():
    document = Document()
    refs = _sketch_rect(document)
    extrude(document, refs, 10.0, mode="join")
    before = kernel.volume(document.bodies[0].shape)

    extrude(document, _sketch_rect(document, (5.0, 2.0), 10.0, 6.0), 4.0, mode="cut", taper=10.0)
    after = kernel.volume(document.bodies[0].shape)
    document.log.undo(document)
    assert kernel.volume(document.bodies[0].shape) == pytest.approx(before, rel=REL)
    document.log.redo(document)
    assert kernel.volume(document.bodies[0].shape) == pytest.approx(after, rel=REL)


def test_cut_tapers_the_tool_body():
    """cut の向き: 負のテーパーで掘る側の立体が細る = 穴が奥ほど狭まる(Fusion の定義どおり押し出す立体に掛かる)。"""
    document = Document()
    extrude(document, _sketch_rect(document, (0.0, 0.0), 40.0, 30.0), 10.0, mode="join")
    sketch_id = sketch_create(document, "XY")
    add_circle(document, sketch_id, (20.0, 15.0), 6.0)
    hole = [region_ref(sketch_id, regions(document, sketch_id)[0].index)]

    extrude(document, hole, 10.0, mode="cut", taper=-10.0)

    expected = 40.0 * 30.0 * 10.0 - _cone(3.0, 10.0, -10.0)
    assert kernel.volume(document.bodies[0].shape) == pytest.approx(expected, rel=REL)


def test_a_collapsing_taper_changes_neither_the_document_nor_the_log():
    document = Document()
    extrude(document, _sketch_rect(document), 10.0, mode="join")
    refs = _sketch_rect(document, (30.0, 0.0))
    before = _state(document)

    with pytest.raises(ValueError, match="collapses"):
        extrude(document, refs, 10.0, mode="new", taper=-40.0)

    assert _state(document) == before


def _box_document() -> Document:
    document = Document()
    shape = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 20.0, 20.0, 20.0).Shape()
    document.add_body(Body(id="b", name="b", kind="solid", shape=shape))
    return document


def _top(shape) -> int:
    top_z = kernel.bounding_box(shape)["max"][2]
    (index,) = [
        int(face["index"])
        for face in query.describe_faces(shape)
        if face["normal"][2] > 0.999 and abs(face["center"][2] - top_z) < 1e-6
    ]
    return index


def test_a_solid_face_extrudes_with_taper_and_finds_its_lid():
    """面の押し出しにテーパーが効かないか、押した面の行き先(`after_face_indices`)を見失う。"""
    document = _box_document()
    top = _top(document.get_body("b").shape)

    operation = extrude_faces(document, [{"body": "b", "kind": "face", "index": top}], 5.0, mode="join", taper=-10.0)

    shape = document.get_body("b").shape
    assert kernel.volume(shape) == pytest.approx(8000.0 + _frustum(20.0, 20.0, 5.0, -10.0), rel=REL)
    assert operation.args["taper"] == -10.0
    assert operation.provenance["after_face_indices"] == [_top(shape)]
    restored = replay_through_json(document, into=_box_document())
    assert kernel.volume(restored.get_body("b").shape) == pytest.approx(kernel.volume(shape), rel=REL)


@pytest.mark.parametrize("finish", ["fillet", "chamfer"])
def test_tapered_sides_take_fillets_and_chamfers(finish):
    """傾いた側面の辺に丸め・面取りが掛からない。"""
    document = Document()
    extrude(document, _sketch_rect(document), 10.0, mode="new", taper=-5.0)
    shape = document.bodies[0].shape
    edges = list(range(1, kernel.edge_count(shape) + 1))

    (fillet if finish == "fillet" else chamfer)(document, edges, 1.0)

    finished = document.bodies[0].shape
    assert BRepCheck_Analyzer(finished).IsValid()
    assert kernel.volume(finished) < kernel.volume(shape)


def test_mcp_extrude_takes_a_taper():
    """MCP の `extrude` に渡したテーパーがコアへ届かない。"""
    session = Session(0.2)
    server = McpServer(DocumentTools(session))
    sketch_id = call(server, "sketch_create", {"base_plane": "XY"})["sketch_id"]
    call(server, "add_rectangle", {"sketch_id": sketch_id, "corner": [0, 0], "width": 20, "height": 10})
    call(server, "set_selection", {"refs": [{"sketch": sketch_id, "index": 1}]})

    call(server, "extrude", {"distance": 10, "taper": 5, "mode": "new"})

    body = session.document.bodies[0]
    assert query.volume(body.shape) == pytest.approx(_frustum(20.0, 10.0, 10.0, 5.0), rel=REL)


def test_an_ellipse_sketch_region_tapers():
    document = Document()
    sketch_id = sketch_create(document, "XY")
    add_ellipse(document, sketch_id, (0.0, 0.0), 12.0, 6.0)
    refs = [region_ref(sketch_id, regions(document, sketch_id)[0].index)]

    extrude(document, refs, 5.0, mode="new", taper=5.0)

    box = kernel.bounding_box(document.bodies[0].shape)
    assert box["max"][0] == pytest.approx(6.0 + 5.0 * _slope(5.0), abs=1e-6)
