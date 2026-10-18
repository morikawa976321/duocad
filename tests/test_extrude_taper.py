"""押し出しのテーパー(`taper`。正で太る・負で細る。Autodesk Fusion と同じ符号)。issue #182。

検算は算術で決まる値: 距離 z の断面は元の領域を a·z(a = tan(taper))だけ外へずらしたもので、角は尖ったまま。

    長方形 w×h      : ∫(w+2az)(h+2az)dz = whH + (w+h)aH² + (4/3)a²H³
    円 半径 r       : 円錐台 πH/3·(r² + r·r' + r'²)、r' = r + aH
    穴(半径 r)     : 外周と逆に動く(r' = r − aH)
"""
from __future__ import annotations

import json
import math
import subprocess
import sys

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
from duocad.paths import REPO_ROOT
from duocad.region import _regions_detailed, region_ref, regions
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


@pytest.mark.parametrize("taper", [89.5, -89.5, 89.9, -89.9, 90.0, -90.0, 120.0, float("nan"), float("inf")])
def test_taper_must_be_at_most_89_degrees(taper):
    """89.8° 以上で MakeDraft が別の形を成功として返す(89.9° で体積 2000)。上限 89° を越える角度が通る。"""
    with pytest.raises(ValueError, match="taper must satisfy"):
        _new(_rect(), 10.0, taper)


def test_the_steepest_allowed_taper_builds_the_right_shape():
    result = _new(_rect(), 10.0, kernel_extrude.MAX_TAPER)
    assert math.isclose(result["volume_after"], _frustum(20.0, 10.0, 10.0, kernel_extrude.MAX_TAPER), rel_tol=REL)


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


# ======================================================================================
# sol レビューの指摘(2026-10-18)
# ======================================================================================
_TINY_CHILD = """
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
from OCP.gp import gp_Pnt
from duocad import kernel_extrude

polygon = BRepBuilderAPI_MakePolygon(gp_Pnt(0, 0, 0), gp_Pnt(20, 0, 0), gp_Pnt(20, 10, 0), gp_Pnt(0, 10, 0), True)
face = BRepBuilderAPI_MakeFace(polygon.Wire(), True).Face()
for distance in (1e-5, -5e-5):
    try:
        kernel_extrude.extrude_regions([face], distance, "new", (0.0, 0.0, 1.0), None, taper=5.0)
    except ValueError as error:
        print("refused", str(error)[:30])
    else:
        print("built")
print("survived")
"""


def test_a_very_short_tapered_extrusion_is_refused_without_killing_the_process():
    """距離 1e-4 mm 未満のテーパーで `BRepOffsetAPI_MakeDraft` が access violation を起こす(実測。別プロセスで試す)。"""
    done = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", _TINY_CHILD],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0, (done.returncode, done.stderr[-800:])
    assert done.stdout.split()[-1] == "survived"
    assert done.stdout.count("refused") == 2, done.stdout


def test_the_shortest_allowed_tapered_extrusion_builds():
    result = _new(_rect(), kernel_extrude.MIN_TAPER_DISTANCE, 5.0)
    assert BRepCheck_Analyzer(result["shape"]).IsValid()


def _shifted_ellipse():
    """継ぎ目を π/64 ずらした楕円 6×3(曲率半径の最小 1.5 が等間隔の標本の間に落ちる)。"""
    curve = gp_Elips(gp_Ax2(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(0.0, 0.0, 1.0)), 6.0, 3.0)
    start = math.pi / 64.0
    edge = BRepBuilderAPI_MakeEdge(curve, start, start + 2.0 * math.pi).Edge()
    return BRepBuilderAPI_MakeFace(BRepBuilderAPI_MakeWire(edge).Wire(), True).Face()


def test_a_cusp_between_curvature_samples_is_refused():
    """内向きのずれ 1.5015 が最小曲率半径 1.5 を超えて輪郭が折り返すのに、標本の間なので成功として通る。"""
    with pytest.raises(ValueError, match="collapses"):
        _new(_shifted_ellipse(), 10.0, -8.539170673763662)


def test_the_shifted_ellipse_passes_just_before_its_cusp():
    """曲率の最小値を詰めた結果、潰れていない側まで断っている。"""
    reach = 1.49
    result = _new(_shifted_ellipse(), 10.0, -math.degrees(math.atan(reach / 10.0)))
    assert BRepCheck_Analyzer(result["shape"]).IsValid()


def _half_disk():
    """半径 5 の右半分の円(円弧の端で直径と直角に交わる)。"""
    arc = GC_MakeArcOfCircle(gp_Circ(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 5.0), -math.pi / 2, math.pi / 2, True)
    wire = BRepBuilderAPI_MakeWire(
        BRepBuilderAPI_MakeEdge(arc.Value()).Edge(),
        BRepBuilderAPI_MakeEdge(gp_Pnt(0, 5, 0), gp_Pnt(0, -5, 0)).Edge(),
    ).Wire()
    return BRepBuilderAPI_MakeFace(wire, True).Face()


@pytest.mark.parametrize("taper", [math.degrees(math.atan(0.55)), 5.0])
def test_a_growing_half_disk_is_not_refused(taper):
    """円弧の端で、曲率の中心へ寄せた点が直径の上(境界)に乗り、外向きを内向きと取り違えて断る。"""
    result = _new(_half_disk(), 10.0, taper)

    assert BRepCheck_Analyzer(result["shape"]).IsValid()
    box = kernel.bounding_box(result["shape"])
    assert box["min"][0] == pytest.approx(-10.0 * _slope(taper), abs=1e-6)
    assert box["max"][0] == pytest.approx(5.0 + 10.0 * _slope(taper), abs=1e-6)


def _bezier_face(poles, closing):
    """3次ベジエ `poles` の終点から `closing` の点を線分でたどって始点へ戻る領域。"""
    array = TColgp_Array1OfPnt(1, len(poles))
    for i, (x, y) in enumerate(poles, 1):
        array.SetValue(i, gp_Pnt(x, y, 0.0))
    wire = BRepBuilderAPI_MakeWire()
    wire.Add(BRepBuilderAPI_MakeEdge(Geom_BezierCurve(array)).Edge())
    points = [poles[-1], *closing, poles[0]]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        wire.Add(BRepBuilderAPI_MakeEdge(gp_Pnt(x0, y0, 0.0), gp_Pnt(x1, y1, 0.0)).Edge())
    return BRepBuilderAPI_MakeFace(wire.Wire(), True).Face()


def _cubic_poles(coefficients_x, coefficients_y):
    """べき基底 a0 + a1·t + a2·t² + a3·t³ の係数から3次ベジエの極。"""
    def poles(a):
        return (a[0], a[0] + a[1] / 3.0, a[0] + 2.0 * a[1] / 3.0 + a[2] / 3.0, a[0] + a[1] + a[2] + a[3])
    return list(zip(poles(coefficients_x), poles(coefficients_y)))


def _sharp_bezier():
    """x = 1e5·(u³/3 + 1e-5·u)、y = 500·u²(u = t − 0.507)。t = 0.507 で曲率半径 0.001 mm の鋭い山を持つ。"""
    c = 0.507
    x = [1e5 * (-(c**3) / 3.0 - 1e-5 * c), 1e5 * (c * c + 1e-5), 1e5 * -c, 1e5 / 3.0]
    y = [500.0 * c * c, -1000.0 * c, 500.0, 0.0]
    poles = _cubic_poles(x, y)
    return _bezier_face(poles, [(poles[-1][0], 10000.0), (poles[0][0], 10000.0)])


def test_a_sharp_curvature_peak_between_golden_section_probes_is_refused():
    """曲率半径 0.001 mm の山を内向きに 0.0015 mm ずらすと輪郭が折り返すのに、成功として通る(黄金分割が別の極大へ寄る)。"""
    with pytest.raises(ValueError, match="collapses"):
        _new(_sharp_bezier(), 1.0, -0.08594360481195855)


@pytest.mark.parametrize("taper", [1.0, -1.0])
def test_a_straight_bezier_with_coincident_poles_tapers(taper):
    """極が重なり端で曲率が定まらない直線状のベジエ(SVG の `C 0 0 0 0 20 0`)で、OCCT の例外が漏れる。"""
    face = _bezier_face([(0.0, 0.0), (0.0, 0.0), (0.0, 0.0), (20.0, 0.0)], [(20.0, 10.0), (0.0, 10.0)])

    result = _new(face, 1.0, taper)

    assert math.isclose(result["volume_after"], _frustum(20.0, 10.0, 1.0, taper), rel_tol=1e-6)


def test_a_curved_bezier_with_a_coincident_end_pole_grows_and_refuses_cleanly_when_shrunk():
    """端の極が重なった曲がったベジエで OCCT の例外が漏れる、または太る向きまで断る。

    端の極が重なると、その端で曲率半径が0へ向かう(y ∝ x^1.5 の形)。細る向きでは曲線だけ見れば輪郭が折り返すので
    潰れとして断る(角で切り落とされて実害が無い場合も断る。慎重側に倒している)。
    """
    face = _bezier_face([(0.0, 0.0), (0.0, 0.0), (10.0, -4.0), (20.0, 0.0)], [(20.0, 10.0), (0.0, 10.0)])
    straight = _new(face, 1.0, 0.0)["volume_after"]

    grown = _new(face, 1.0, 1.0)
    assert grown["volume_after"] > straight
    assert BRepCheck_Analyzer(grown["shape"]).IsValid()
    with pytest.raises(ValueError, match="collapses"):
        _new(face, 1.0, -1.0)


# ======================================================================================
# sol レビューの指摘(3回目)
# ======================================================================================
def _sketch_ellipse_face(width: float, height: float):
    document = Document()
    sketch_id = sketch_create(document, "XY")
    add_ellipse(document, sketch_id, (0.0, 0.0), width, height)
    return _regions_detailed(document, sketch_id)[0]["face"]


def _ellipse_parallel_volume(a: float, b: float, height: float, taper: float) -> float:
    """半径 a・b の楕円を傾けた立体の体積。滑らかな凸の輪郭の断面積は A0 + P·t + π·t²(Steiner。尖点ができない範囲で
    内側にも成り立つ)で、t = tan(taper)·z を 0..H で積分する。周長 P は数値積分で出す(OCCT の長さは精度を渡さないと
    この楕円で 0.13% 大きい)。"""
    steps = 4096
    perimeter = sum(
        math.hypot(a * math.sin(2 * math.pi * (k + 0.5) / steps), b * math.cos(2 * math.pi * (k + 0.5) / steps))
        for k in range(steps)
    ) * 2 * math.pi / steps
    slope = _slope(taper)
    return math.pi * a * b * height + perimeter * slope * height**2 / 2.0 + math.pi * slope**2 * height**3 / 3.0


@pytest.mark.parametrize("distance, taper", [(0.1, 5.0), (0.1, -5.0), (0.01, 5.0), (1.0, -5.0), (10.0, 20.0), (10.0, -8.0)])
def test_a_short_ellipse_extrusion_is_not_refused(distance, taper):
    """楕円を傾けた側面(B スプラインの近似)と、ふた・底の縫い目が 1e-6 で閉じず、短い押し出しが潰れとして断られる。"""
    face = _sketch_ellipse_face(12.0, 6.0)

    result = _new(face, distance, taper)

    assert math.isclose(result["volume_after"], _ellipse_parallel_volume(6.0, 3.0, distance, taper), rel_tol=1e-5)
    assert BRepCheck_Analyzer(result["shape"]).IsValid()


@pytest.mark.parametrize("distance, taper", [(1.0, -1.0), (10.0, 5.0)])
def test_a_straight_bezier_with_both_end_poles_doubled_is_a_line(distance, taper):
    """両端の極が重なった直線のベジエ(SVG の `C0 0 20 0 20 0`)で、断られるか、体積が 2.4% ずれる(曲面の積分が不正確)。"""
    face = _bezier_face([(0.0, 0.0), (0.0, 0.0), (20.0, 0.0), (20.0, 0.0)], [(20.0, 10.0), (0.0, 10.0)])

    result = _new(face, distance, taper)

    assert math.isclose(result["volume_after"], _frustum(20.0, 10.0, distance, taper), rel_tol=1e-9)


# ======================================================================================
# sol レビューの指摘(4回目)
# ======================================================================================
@pytest.mark.parametrize("width, height, distance", [(12.0, 1.0, 10.0), (12.0, 0.1, 1.0)])
def test_the_volume_of_a_thin_tapered_ellipse_is_accurate(width, height, distance):
    """細い楕円を傾けた B スプラインの側面で、既定の体積の積分が 3〜152% ずれる(形は正しい)。"""
    face = _sketch_ellipse_face(width, height)

    result = _new(face, distance, 5.0)

    expected = _ellipse_parallel_volume(width / 2.0, height / 2.0, distance, 5.0)
    assert math.isclose(result["volume_after"], expected, rel_tol=1e-5)
    assert math.isclose(kernel.volume(result["shape"]), expected, rel_tol=1e-5)


def _short_chamfer():
    """長方形 20×10 の角を 0.1 mm 面取りした凸の五角形(面取り辺の長さ 0.141)。"""
    return _polygon((0.0, 10.0), (20.0, 10.0), (20.0, 0.1), (19.9, 0.0), (0.0, 0.0))


def _convex_polygon_volume(area, perimeter, corner_factor, height, taper):
    """凸多角形を角を尖らせたまま t だけずらした面積は A0 + P·t + K·t²(K = Σ tan(外角/2))。t = tan(taper)·z を積分する。"""
    a = _slope(taper)
    return area * height + perimeter * a * height**2 / 2.0 + corner_factor * a * a * height**3 / 3.0


@pytest.mark.parametrize("taper", [-0.3, 0.3, 5.0])
def test_a_short_chamfer_edge_tapers(taper):
    """0.1 mm の面取り辺があると `MakeDraft` が Standard_OutOfRange で失敗し、潰れない角度まで断る。"""
    result = _new(_short_chamfer(), 10.0, taper)

    corner = 3.0 + 2.0 * math.tan(math.radians(22.5))
    perimeter = 20.0 + 9.9 + math.hypot(0.1, 0.1) + 19.9 + 10.0
    expected = _convex_polygon_volume(200.0 - 0.005, perimeter, corner, 10.0, taper)
    assert math.isclose(result["volume_after"], expected, rel_tol=1e-9)


def test_a_short_chamfer_edge_that_vanishes_is_refused():
    """細る向きの移動量 0.1745 mm は面取り辺が消える 0.1707 mm を越える。"""
    with pytest.raises(ValueError, match="collapses"):
        _new(_short_chamfer(), 10.0, -1.0)


# ======================================================================================
# sol レビューの指摘(5回目)
# ======================================================================================
def test_an_untapered_elliptic_cylinder_keeps_its_centroid():
    """押し出し面(SurfaceOfExtrusion)まで適応積分にすると、楕円柱の重心が 0.02 mm ずれ、既定の中心で回すと形が動く。"""
    face = _sketch_ellipse_face(120.0, 60.0)
    shape = _new(face, 10.0, 0.0)["shape"]

    assert kernel.volume(shape) == pytest.approx(math.pi * 60.0 * 30.0 * 10.0, rel=1e-12)
    assert kernel.centroid(shape) == pytest.approx([0.0, 0.0, 5.0], abs=1e-9)


def test_a_shape_mixing_extrusion_and_bspline_faces_measures_both_exactly():
    """押し出し面(既定の積分が正確)と傾けた B スプラインの面(適応積分が正確)を併せ持つ形の体積・重心。"""
    cylinder = _new(_sketch_ellipse_face(120.0, 60.0), 10.0, 0.0)["shape"]
    document = Document()
    sketch_id = sketch_create(document, "XY")
    add_ellipse(document, sketch_id, (200.0, 0.0), 12.0, 6.0)
    tapered = _regions_detailed(document, sketch_id)[0]["face"]

    joined = kernel_extrude.extrude_regions([tapered], 10.0, "join", UP, cylinder, taper=5.0)["shape"]

    expected = math.pi * 60.0 * 30.0 * 10.0 + _ellipse_parallel_volume(6.0, 3.0, 10.0, 5.0)
    assert kernel.volume(joined) == pytest.approx(expected, rel=1e-7)
    assert kernel.centroid(joined)[1] == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize("distance, taper", [(1.0, 5.0), (1.0, 0.3), (-1.0, 5.0), (10.0, 5.0)])
def test_a_very_thin_ellipse_grows(distance, taper):
    """幅120・高さ1 の楕円(先端の曲率半径 0.004 mm)は、継ぎ目が先端にあると MakeDraft が Standard_OutOfRange を投げる。"""
    result = _new(_sketch_ellipse_face(120.0, 1.0), distance, taper)

    expected = _ellipse_parallel_volume(60.0, 0.5, abs(distance), taper)
    assert math.isclose(result["volume_after"], expected, rel_tol=1e-6)


def test_a_very_thin_ellipse_refuses_to_shrink_past_its_tip():
    """細る向きの移動量 0.0052 mm は先端の曲率半径 0.0042 mm を越え、先端で輪郭が折り返す。"""
    with pytest.raises(ValueError, match="collapses"):
        _new(_sketch_ellipse_face(120.0, 1.0), 1.0, -0.3)


# ======================================================================================
# sol レビューの指摘(6回目)
# ======================================================================================
@pytest.mark.parametrize("lift, distance", [(100.0, 0.001), (1000.0, 0.1)])
def test_a_tapered_ellipse_far_from_the_origin_measures_its_volume_and_centroid(lift, distance):
    """面ごとの積分の基準点を原点に固定すると、原点から遠い小さな形で桁が落ち、体積が 1.3% ずれ重心が立体の外へ出る。"""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf, gp_Vec

    move = gp_Trsf()
    move.SetTranslation(gp_Vec(0.0, 0.0, lift))
    face = TopoDS.Face_s(BRepBuilderAPI_Transform(_sketch_ellipse_face(12.0, 6.0), move, True).Shape())

    result = _new(face, distance, 5.0)

    assert math.isclose(kernel.volume(result["shape"]), _ellipse_parallel_volume(6.0, 3.0, distance, 5.0), rel_tol=1e-5)
    centre = kernel.centroid(result["shape"])
    assert centre[0] == pytest.approx(0.0, abs=1e-6 * distance + 1e-9)
    assert abs(centre[2] - (lift + distance / 2.0)) < 0.01 * distance
