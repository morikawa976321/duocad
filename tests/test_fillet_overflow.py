"""はみ出しを吸収するフィレット(`kernel_overflow`)。OCCT の `MakeFillet` が断る・別の形を返す半径でも、Fusion 360 と同じ形になるか。

期待値は「障害物の無い形で OCCT が丸めてから、障害物を足す/はみ出しを切る」で作った形(issue #208 の「後で結合」)の体積。
"""
from __future__ import annotations

import pytest
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Fuse
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.Bnd import Bnd_Box
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from duocad import kernel, query
from duocad.document import Document
from duocad.region import region_ref
from duocad.sketch import add_circle, base_plane, sketch_create
from duocad.verbs import extrude, fillet
from document_support import assert_matches_replay


def _cylinder(x: float, z: float, radius: float, height: float):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, 0.0, z), gp_Dir(0.0, 0.0, 1.0)), radius, height).Shape()


def _union(*shapes):
    return kernel.boolean_bodies(shapes[0], list(shapes[1:]), "union")["shape"]


def _boolean(builder, first, second):
    made = builder(first, second)
    made.Build()
    return made.Shape()


def _circle_edge(shape, radius: float, z: float, x: float = 0.0) -> int:
    for edge in query.describe_edges(shape):
        if edge["kind"] != "circle" or abs(edge["start"][2] - z) > 1e-6:
            continue
        circle = BRepAdaptor_Curve(TopoDS.Edge_s(kernel.edge_of(shape, edge["index"]))).Circle()
        if abs(circle.Radius() - radius) < 1e-6 and abs(circle.Location().X() - x) < 1e-6:
            return edge["index"]
    raise AssertionError(f"no circle r={radius} at z={z}")


def _native(shape, index: int, radius: float):
    """OCCT の `MakeFillet` だけで丸めた形。断れば None。"""
    maker = BRepFilletAPI_MakeFillet(shape)
    maker.Add(radius, TopoDS.Edge_s(kernel.edge_of(shape, index)))
    maker.Build()
    if not maker.IsDone() or not BRepCheck_Analyzer(maker.Shape()).IsValid():
        return None
    return maker.Shape()


def _bounds(shape):
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    return box.Get()


def _post_on_disc(disc_radius: float):
    """半径 `disc_radius`・厚さ 8 の円板の中央に、半径 9・高さ 60 の柱。"""
    return _union(_cylinder(0.0, 0.0, disc_radius, 8.0), _cylinder(0.0, 8.0, 9.0, 60.0))


@pytest.mark.parametrize("radius", [21.0, 21.25, 30.0])
def test_a_radius_wider_than_the_flange_is_cut_where_the_flange_ends(radius):
    """円板の縁(柱から 21)を越える半径で丸まり、縁の外へはみ出す分は円板の側面の延長で切れる。
    落ちたら、はみ出しの吸収が働いていない(OCCT は 21 以上で断る)か、はみ出した肉の切り方が変わった。"""
    shape = _post_on_disc(30.0)
    index = _circle_edge(shape, 9.0, 8.0)
    assert _native(shape, index, radius) is None, "前提: OCCT はこの半径を断る"

    rounded = kernel.fillet_edges(shape, {index: radius})

    # 期待値: 十分広い円板で OCCT が丸め、円板の半径 30 の円柱の内側だけを残した形
    wide = _post_on_disc(100.0)
    expected = _boolean(
        BRepAlgoAPI_Common, _native(wide, _circle_edge(wide, 9.0, 8.0), radius), _cylinder(0.0, 0.0, 30.0, 100.0)
    )
    assert rounded["volume_after"] == pytest.approx(kernel.volume(expected), rel=1e-7)
    assert BRepCheck_Analyzer(rounded["shape"]).IsValid()
    assert _bounds(rounded["shape"]) == pytest.approx(_bounds(shape), abs=1e-6), "肉が円板の外へはみ出した"


def _two_posts(gap: float):
    """issue #208 の形: 円板(半径 20・厚さ 3)の上に、柱 A(半径 5・中心 0)と柱 B(半径 4・A との隙間 `gap`)。"""
    disc, post_a = _cylinder(0.0, 0.0, 20.0, 3.0), _cylinder(0.0, 3.0, 5.0, 20.0)
    post_b = _cylinder(5.0 + gap + 4.0, 3.0, 4.0, 20.0)
    return _union(disc, post_a, post_b), _union(disc, post_a), post_b


@pytest.mark.parametrize(
    "gap, radius",
    [(2.0, 1.0), (2.0, 2.0), (2.0, 3.0), (2.0, 5.0), (2.0, 8.0), (0.5, 1.0), (0.5, 3.0), (0.5, 5.0), (0.5, 8.0), (0.5, 12.0)],
)
def test_a_fillet_that_reaches_another_post_passes_through_it(gap, radius):
    """柱 A の根元の丸めが柱 B に当たっても、B を貫通した形(A だけで丸めてから B を足した形)になる。
    OCCT は B に当たる半径で、断る(0.5/5・2/8)、壊れた形を返す(2/2)、B にも接する別の形を返す(2/3・2/5)のどれか。
    落ちたら、別の形を返したときに吸収へ回す判定(`kernel._blend_reaches_other_faces`)か、吸収そのものが変わった。"""
    shape, without_b, post_b = _two_posts(gap)
    index = _circle_edge(shape, 5.0, 3.0)

    rounded = kernel.fillet_edges(shape, {index: radius})

    alone = _native(without_b, _circle_edge(without_b, 5.0, 3.0), radius)
    expected = kernel.volume(_boolean(BRepAlgoAPI_Fuse, alone, post_b))
    assert rounded["volume_after"] == pytest.approx(expected, rel=1e-7)
    assert BRepCheck_Analyzer(rounded["shape"]).IsValid()


def test_a_fillet_that_reaches_nothing_else_is_occt_s_own():
    """障害物にもはみ出しにも当たらない半径は OCCT の `MakeFillet` の形のまま(面の数まで同じ)。"""
    shape, _, _ = _two_posts(2.0)
    index = _circle_edge(shape, 5.0, 3.0)

    rounded = kernel.fillet_edges(shape, {index: 1.0})["shape"]

    native = _native(shape, index, 1.0)
    assert kernel.volume(rounded) == pytest.approx(kernel.volume(native), rel=1e-12)
    assert kernel.face_count(rounded) == kernel.face_count(native)


def _l_bracket():
    """床 40×30×5 と壁 5×30×25 の L 字。内側の凹の辺は x=5・z=5 で、床の残りの幅 35・壁の残りの高さ 20。"""
    floor = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(40, 30, 5)).Shape()
    wall = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(5, 30, 25)).Shape()
    return _union(floor, wall)


def _line_edge_at(shape, x: float, z: float) -> int:
    (found,) = [
        edge["index"] for edge in query.describe_edges(shape)
        if edge["kind"] == "line" and all(abs(p[0] - x) < 1e-6 and abs(p[2] - z) < 1e-6 for p in (edge["start"], edge["end"]))
    ]
    return found


def test_a_straight_fillet_taller_than_the_wall_is_cut_at_the_wall_top():
    """L 字の内側の辺を、壁の残りの高さ(20)を越える半径 22 で丸めると、壁の上面の高さで切れ、辺の両端は側面で切れる。"""
    shape = _l_bracket()
    index = _line_edge_at(shape, 5.0, 5.0)
    assert _native(shape, index, 22.0) is None, "前提: OCCT はこの半径を断る"

    rounded = kernel.fillet_edges(shape, {index: 22.0})

    # 断面は「22 角の正方形 − 四分円」のうち壁の上面(z=25)より下。z を 5..25 で積分した面積 × 奥行き 30
    import math

    def area_below(height):  # 角(0,0)から高さ h までの「正方形 − 四分円」の面積(中心は (22, 22))
        r = 22.0
        y = height - r
        circle = (y * math.sqrt(r * r - y * y) + r * r * math.asin(y / r)) / 2 - (-r * r * math.pi / 4)
        return r * height - circle

    expected = kernel.volume(shape) + 30.0 * area_below(20.0)
    assert rounded["volume_after"] == pytest.approx(expected, rel=1e-7)
    assert _bounds(rounded["shape"]) == pytest.approx(_bounds(shape), abs=1e-6)


def test_a_convex_fillet_wider_than_the_plate_is_cut_at_the_bottom():
    """厚さ 3 の板の上の角を半径 5 で丸めると、丸めが板の下面より下へ出る分は捨てられ、板は小さくなるだけ。"""
    plate = BRepPrimAPI_MakeBox(gp_Pnt(-20, -20, 0), gp_Pnt(20, 20, 3)).Shape()
    index = _line_edge_at(plate, 20.0, 3.0)
    assert _native(plate, index, 5.0) is None, "前提: OCCT はこの半径を断る"

    rounded = kernel.fillet_edges(plate, {index: 5.0})

    import math

    # 断面: 角(20,3)から 5 角の正方形 − 四分円(中心 (15, -2))のうち z>=0 の部分を、長さ 40 だけ削る
    r, depth = 5.0, 3.0
    y0 = -2.0
    # z∈[0,3] の範囲で、x が円の外側(x > 15 + sqrt(r²-(z-y0)²))の幅を積分
    def width(z):
        return 5.0 - math.sqrt(r * r - (z - y0) ** 2)
    steps = 20000
    removed = sum(width((i + 0.5) * depth / steps) for i in range(steps)) * depth / steps
    assert rounded["volume_after"] == pytest.approx(kernel.volume(plate) - 40.0 * removed, rel=1e-6)
    assert rounded["volume_after"] < kernel.volume(plate)


def test_absorbed_fillet_is_logged_undone_and_replayed():
    """吸収で丸めた操作も、ログ・undo・redo・JSON からの再生で同じ形になる(verb の契約は変わらない)。"""
    doc = Document()
    sketch_id = sketch_create(doc, base_plane("XY"))
    add_circle(doc, sketch_id, (0.0, 0.0), 60.0)
    body_id = extrude(doc, [region_ref(sketch_id, 1)], 8.0, mode="join").args["body_id"]
    post = sketch_create(doc, base_plane("XY"))
    add_circle(doc, post, (0.0, 0.0), 18.0)
    extrude(doc, [region_ref(post, 1)], 68.0, mode="join", body_id=body_id)
    shape = doc.get_body(body_id).shape
    before = query.volume(shape)
    index = _circle_edge(shape, 9.0, 8.0)

    fillet(doc, [index], 21.25, body_id=body_id)
    after = query.volume(doc.get_body(body_id).shape)
    assert after > before

    doc.log.undo(doc)
    assert query.volume(doc.get_body(body_id).shape) == pytest.approx(before)
    doc.log.redo(doc)
    assert query.volume(doc.get_body(body_id).shape) == pytest.approx(after)
    assert_matches_replay(doc)


def test_rounding_every_edge_of_a_cube_past_its_limit_is_still_refused():
    """角で連なりが出会う選び方は吸収しない(辺ごとの肉の和では Fusion の角にならない)。OCCT の失敗の理由をそのまま返す。"""
    cube = BRepPrimAPI_MakeBox(20.0, 20.0, 20.0).Shape()
    every = {edge["index"]: 10.0 for edge in query.describe_edges(cube)}

    with pytest.raises(ValueError, match="IsDone is false.*meet at a corner"):
        kernel.fillet_edges(cube, every)


def _box(x0, y0, z0, x1, y1, z1):
    return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), gp_Pnt(x1, y1, z1)).Shape()


def test_overflow_cut_off_at_the_edge_end_is_dropped_even_if_it_touches_another_bump():
    """L 字の辺の端の先へ伸びた肉が、床の上の別の突起に一部重なっても捨てられる(重なりで残すと、外接箱を大きく越えた
    形になった。Codex レビューの再現)。"""
    shape = _union(_box(0, 0, 0, 40, 35, 5), _box(0, 0, 0, 5, 30, 25), _box(10, 32, 5, 12, 35, 8))
    index = _line_edge_at(shape, 5.0, 5.0)

    rounded = kernel.fillet_edges(shape, {index: 22.0})

    assert _bounds(rounded["shape"]) == pytest.approx(_bounds(shape), abs=1e-6)


def test_another_selected_edge_does_not_hide_a_fillet_that_hits_a_post():
    """柱 A の根元(半径 3、柱 B に当たる)と柱 B の上縁(半径 1)を同時に丸めても、A の丸めは B を貫通する。
    B の側面は B の上縁の頂点に触れるので、選んだ辺全部の頂点でまとめて見ると当たりを見逃した(Codex レビューの再現)。"""
    shape, without_b, post_b = _two_posts(2.0)
    root = _circle_edge(shape, 5.0, 3.0)
    top_b = _circle_edge(shape, 4.0, 23.0, x=11.0)

    rounded = kernel.fillet_edges(shape, {root: 3.0, top_b: 1.0})

    alone = _native(without_b, _circle_edge(without_b, 5.0, 3.0), 3.0)
    b_rounded = _native(post_b, _circle_edge(post_b, 4.0, 23.0, x=11.0), 1.0)
    expected = kernel.volume(_boolean(BRepAlgoAPI_Fuse, alone, b_rounded))
    assert rounded["volume_after"] == pytest.approx(expected, rel=1e-7)


def test_independent_edges_between_the_same_two_faces_keep_their_own_radii():
    """同じ2面の間にある別々の辺(半円柱の2本の縦辺)は、それぞれの半径で丸まる(最初の辺の半径で両方を丸めていた。
    Codex レビューの再現)。吸収へ入れるため、別の柱の根元をはみ出す半径で一緒に丸める。"""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common as _Common

    half = _boolean(_Common, _cylinder(0.0, 3.0, 10.0, 30.0), _box(-20, 0, 3, 20, 20, 40))
    shape = _union(_box(-20, -20, 0, 130, 20, 3), half, _cylinder(110.0, 3.0, 9.0, 60.0))
    verticals = sorted(
        (edge for edge in query.describe_edges(shape)
         if edge["kind"] == "line" and abs(edge["start"][0]) > 9.9 and abs(edge["start"][0]) < 10.1
         and abs(edge["start"][2] - edge["end"][2]) > 1.0),
        key=lambda edge: edge["start"][0],
    )
    assert len(verticals) == 2
    root = _circle_edge(shape, 9.0, 3.0, x=110.0)

    volumes = [
        kernel.fillet_edges(shape, {verticals[0]["index"]: 3.0, verticals[1]["index"]: second, root: 22.0})["volume_after"]
        for second in (3.0, 4.0)
    ]

    assert volumes[0] != pytest.approx(volumes[1], rel=1e-9)


def test_concave_and_convex_rounds_that_cancel_out_in_volume_are_accepted():
    """凹の辺に足す肉と凸の辺から引く肉の体積が打ち消し合っても、形は変わっているので確定できる(体積が同じなのを
    「何も変わらない」と見なして断っていた。Codex レビューの再現)。"""
    shape = _union(_box(0, 0, 0, 20, 30, 3), _box(0, 0, 0, 3, 30, 6))
    inner, outer = _line_edge_at(shape, 3.0, 3.0), _line_edge_at(shape, 20.0, 3.0)

    rounded = kernel.fillet_edges(shape, {inner: 5.0, outer: 5.0})

    assert BRepCheck_Analyzer(rounded["shape"]).IsValid()


def test_a_radius_wider_than_a_thin_pin_is_refused_before_building_any_material():
    """細いピン(半径 1)の上縁を半径 3 で丸めると、球がピンの中に入らないので吸収も断る(理由つきのエラー)。
    PC ケースの部品で同じ組(半径 0.56 のピンに半径 12)を吸収させると、見当違いの肉を統合するところで仮想メモリを
    使い尽くし PC ごと落ちた。落ちたら、断る前に肉を作ろうとしている。"""
    shape = _union(_box(-10, -10, 0, 10, 10, 3), _cylinder(0.0, 3.0, 1.0, 10.0))
    top = _circle_edge(shape, 1.0, 13.0)

    with pytest.raises(ValueError, match="does not fit inside a curved face of radius 1"):
        kernel.fillet_edges(shape, {top: 3.0})


def test_rounding_a_convex_edge_next_to_a_curved_face_removes_all_of_the_material():
    """半円柱(半径 10)の縦辺を半径 4 で丸める肉は、曲面の側の接点まで(平面から 6.67)届く。吸収で作っても OCCT と
    同じ体積になる(柱の厚みを接点までの距離で絞ったときは 6.18 mm³ 残った。Codex レビューの反例)。"""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common as _Common

    half = _boolean(_Common, _cylinder(0.0, 3.0, 10.0, 30.0), _box(-20, 0, 3, 20, 20, 40))
    shape = _union(_box(-20, -20, 0, 20, 20, 3), half)
    verticals = [
        edge["index"] for edge in query.describe_edges(shape)
        if edge["kind"] == "line" and 9.9 < abs(edge["start"][0]) < 10.1 and abs(edge["start"][2] - edge["end"][2]) > 1.0
    ]
    radii = dict(zip(verticals, (3.0, 4.0)))

    from duocad import kernel_overflow

    absorbed = kernel_overflow.absorb(shape, kernel._edge_map(shape), radii)

    maker = BRepFilletAPI_MakeFillet(shape)
    for index, radius in radii.items():
        maker.Add(radius, TopoDS.Edge_s(kernel.edge_of(shape, index)))
    maker.Build()
    assert kernel.volume(absorbed.shape) == pytest.approx(kernel.volume(maker.Shape()), rel=1e-7)


def test_a_through_hole_next_to_the_post_stays_open():
    """柱の根元の丸めが脇の貫通穴にかかっても、穴の上の肉は穴の壁の延長で切れ、穴は塞がらない
    (支える面の内側の輪を全部塞いだときは、穴の上に肉が残った。Codex レビューの反例)。"""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.TopAbs import TopAbs_OUT

    disc_and_post = _union(_cylinder(0.0, 0.0, 20.0, 3.0), _cylinder(0.0, 3.0, 5.0, 20.0))
    hole = _cylinder(11.0, -1.0, 2.0, 30.0)
    shape = kernel.boolean_bodies(disc_and_post, [hole], "cut")["shape"]
    index = _circle_edge(shape, 5.0, 3.0)

    rounded = kernel.fillet_edges(shape, {index: 6.0})

    alone = _native(disc_and_post, _circle_edge(disc_and_post, 5.0, 3.0), 6.0)
    expected = kernel.volume(_boolean(BRepAlgoAPI_Cut, alone, hole))
    assert rounded["volume_after"] == pytest.approx(expected, rel=1e-7)
    inside_the_hole = BRepClass3d_SolidClassifier(rounded["shape"], gp_Pnt(9.5, 0.0, 3.1), 1e-7)
    assert inside_the_hole.State() == TopAbs_OUT


def test_many_far_away_selections_do_not_hide_a_fillet_that_hits_a_post():
    """柱 A・B(隙間 2)の当たりを、離れた 11 本の柱の上縁を一緒に選んでも見逃さない(連なりの数で検査を打ち切ったときは
    13 本で見逃した。Codex レビューの反例)。"""
    disc, post_a = _cylinder(0.0, 0.0, 60.0, 3.0), _cylinder(0.0, 3.0, 5.0, 20.0)
    post_b = _cylinder(11.0, 3.0, 4.0, 20.0)
    far = [BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(-35.0 + 7.0 * i, -35.0, 3.0), gp_Dir(0, 0, 1)), 2.0, 10.0).Shape() for i in range(11)]
    shape = _union(disc, post_a, post_b, *far)
    radii = {_circle_edge(shape, 5.0, 3.0): 3.0, _circle_edge(shape, 4.0, 23.0, x=11.0): 1.0}
    for i in range(11):
        radii[_circle_edge(shape, 2.0, 13.0, x=-35.0 + 7.0 * i)] = 0.25

    rounded = kernel.fillet_edges(shape, radii)

    without_b = _union(disc, post_a, *far)
    pieces = BRepFilletAPI_MakeFillet(without_b)
    pieces.Add(3.0, TopoDS.Edge_s(kernel.edge_of(without_b, _circle_edge(without_b, 5.0, 3.0))))
    for i in range(11):
        pieces.Add(0.25, TopoDS.Edge_s(kernel.edge_of(without_b, _circle_edge(without_b, 2.0, 13.0, x=-35.0 + 7.0 * i))))
    pieces.Build()
    b_rounded = _native(post_b, _circle_edge(post_b, 4.0, 23.0, x=11.0), 1.0)
    expected = kernel.volume(_boolean(BRepAlgoAPI_Fuse, pieces.Shape(), b_rounded))
    assert rounded["volume_after"] == pytest.approx(expected, rel=1e-7)
