"""3verb前倒しの契約(3) — 円弧。項番12〜15。

**未実装のうちは赤で終わるのが正常。**

## なぜ円弧を前倒すのか

領域検出があれば「円を線で分けて片側だけ押し出す」で多くは代用できる。ただし
**アロマ拡散器**（実測: 352面 = 平面338 + 全周円筒8 + **部分円筒6**）のように
**部分円筒面**を持つ実物があり、代用では作れない。

## 期待値の出どころ

半円（半径5mm・180°）と弦の線分で閉じた領域を、法線方向へ3mm押し出したときの実測:

    部分円筒 1（U範囲 180.0度）+ 平面 3   計4面

「部分円筒」は `kernel.classify_face` が U範囲 350度以下の円筒に付ける種別で、
**全周円筒（穴）と区別される**。円弧を押し出して全周円筒になったら、それは円を
押し出しているのと同じ＝円弧が効いていない、ということ。

## パラメータの持ち方（この契約で決めたこと）

`add_arc(doc, sketch_id, center, radius, start_deg, end_deg)` — **中心・半径・角度**で持つ。
3点（始点・中点・終点）で受け取らないのは、実装計画書§3.4「パラメトリック・プリミティブ」と
§6予防5に従い**半径・角度を後から編集できる形**にするため。3点で持つと、円弧だけが
「数値を持たない図形」になり `set_parameter` の外側へ落ちる。
`params` のキーは `cx` / `cy` / `radius` / `start_deg` / `end_deg`（平らに持つ）。
角度は**度**・反時計回り・スケッチ平面のX軸から測る。
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from duocad import kernel, query
from duocad.document import Body, Document
from duocad.parameters import set_parameter
from duocad.region import region_ref, regions
from duocad.sketch import ENTITY_KINDS, add_arc, add_line, base_plane, entity_params, get_sketch, sketch_create
from duocad.verbs import extrude

from models import MODELS  # テスト共通のSTEPの置き場(tests/models.py)

#: 半円（r=5・180°）+ 弦を3mm押し出したときの実測
HALF_DISC_PRISM_KINDS = {"cylinder_partial": 1, "plane": 3}

ARC_RADIUS = 5.0
ARC_START_DEG = 0.0
ARC_END_DEG = 180.0
EXTRUDE_MM = 3.0


def body_of(doc: Document, body_id: str) -> Body:
    return next(body for body in doc.bodies if body.id == body_id)


def kinds_of(doc: Document, body_id: str) -> dict:
    described = query.describe_shape(body_of(doc, body_id).shape)
    return {kind: count for kind, count in described["faces_by_type"].items() if count}


def half_disc_sketch(doc: Document) -> str:
    """半円（円弧）と弦（線分）で閉じた領域を持つスケッチを作って sketch_id を返す。"""
    sketch_id = sketch_create(doc, base_plane("XY"))
    add_arc(doc, sketch_id, (0.0, 0.0), ARC_RADIUS, ARC_START_DEG, ARC_END_DEG)
    add_line(doc, sketch_id, (-ARC_RADIUS, 0.0), (ARC_RADIUS, 0.0))
    return sketch_id


# --------------------------------------------------------------------------------------
# 項番12: スケッチに円弧を追加できる
# --------------------------------------------------------------------------------------
def test_arc_is_a_sketch_entity_kind():
    """円弧が図形の種別に入っていること（データモデル側なので**最初から緑**）。"""
    assert "arc" in ENTITY_KINDS


def test_add_arc_stores_named_numbers():
    """円弧は**中心・半径・角度**を名前つきの数値として持つ（後から編集できる形）。

    3点で持つと円弧だけが `set_parameter` の外側へ落ちる。
    """
    doc = Document()
    sketch_id = sketch_create(doc, base_plane("XY"))

    entity_id = add_arc(doc, sketch_id, (1.0, 2.0), ARC_RADIUS, ARC_START_DEG, ARC_END_DEG)

    params = entity_params(doc, sketch_id, entity_id)
    assert set(params) == {"cx", "cy", "radius", "start_deg", "end_deg"}
    assert params["cx"] == 1.0 and params["cy"] == 2.0
    assert params["radius"] == ARC_RADIUS
    assert params["start_deg"] == ARC_START_DEG and params["end_deg"] == ARC_END_DEG


def test_add_arc_is_logged_and_undoable():
    """他の図形と同じく操作ログに載り、undoで消える。"""
    doc = Document()
    sketch_id = sketch_create(doc, base_plane("XY"))

    add_arc(doc, sketch_id, (0.0, 0.0), ARC_RADIUS, ARC_START_DEG, ARC_END_DEG)

    assert doc.log.entries[-1].verb == "add_arc"
    assert len(get_sketch(doc, sketch_id).entities) == 1
    doc.log.undo(doc)
    assert get_sketch(doc, sketch_id).entities == []


def test_arc_radius_can_be_edited_afterwards():
    """**半径・角度を後から編集できる**こと（これが3点で持たない理由そのもの）。"""
    doc = Document()
    sketch_id = sketch_create(doc, base_plane("XY"))
    entity_id = add_arc(doc, sketch_id, (0.0, 0.0), ARC_RADIUS, ARC_START_DEG, ARC_END_DEG)
    name = get_sketch(doc, sketch_id).entities[0].params["radius"]
    assert isinstance(name, str), "M3の規律どおり、値ではなく名前つきの値への参照を持つ"

    set_parameter(doc, name, 8.0)

    assert entity_params(doc, sketch_id, entity_id)["radius"] == 8.0


@pytest.mark.parametrize(
    "radius,start_deg,end_deg",
    [(0.0, 0.0, 180.0), (-1.0, 0.0, 90.0), (5.0, 30.0, 30.0), (float("nan"), 0.0, 90.0)],
)
def test_broken_arcs_are_refused(radius: float, start_deg: float, end_deg: float):
    """半径0・負・角度差0・NaNは拒否する（壊れた図形をスケッチへ入れない）。

    角度差0の円弧は辺にならず、NaNは**同じスケッチの正常な領域まで黙って消す**
    （実測済みの挙動）。
    """
    doc = Document()
    sketch_id = sketch_create(doc, base_plane("XY"))
    with pytest.raises(ValueError):
        add_arc(doc, sketch_id, (0.0, 0.0), radius, start_deg, end_deg)
    assert get_sketch(doc, sketch_id).entities == []


# --------------------------------------------------------------------------------------
# 項番13: 円弧を含む閉領域が検出できる
# --------------------------------------------------------------------------------------
def test_a_region_closed_by_an_arc_and_a_line_is_detected():
    """線分＋円弧で囲んだ領域が検出できること。

    面積は半円なので πr²/2 = π·25/2 ≒ 39.27 mm²（幾何から導けるので実測不要）。
    """
    doc = Document()
    sketch_id = half_disc_sketch(doc)

    found = regions(doc, sketch_id)

    assert len(found) == 1, "半円の領域が1つだけ検出されること"
    assert found[0].area == pytest.approx(3.141592653589793 * ARC_RADIUS**2 / 2, rel=1e-6)


def test_an_open_arc_alone_does_not_close_a_region():
    """円弧だけでは閉じない（弦が無ければ領域は生まれない）。黙って何か作らないこと。"""
    doc = Document()
    sketch_id = sketch_create(doc, base_plane("XY"))
    add_arc(doc, sketch_id, (0.0, 0.0), ARC_RADIUS, ARC_START_DEG, ARC_END_DEG)

    assert regions(doc, sketch_id) == []


# --------------------------------------------------------------------------------------
# 項番14: 押し出すと部分円筒面ができる（全周円筒ではない）
# --------------------------------------------------------------------------------------
def test_extruding_an_arc_region_makes_a_partial_cylinder():
    """円弧を含む領域を押し出すと**部分円筒**になる（実測: 部分円筒1 + 平面3）。

    全周円筒になったら円弧が効いていない（＝円を押し出しているのと同じ）。
    `cylinder_partial` と `cylinder_full` の区別は U範囲350度のしきい値（`kernel` の
    `FULL_CYLINDER_DEGREES`）で、**この区別があるからこそ実物の部分円筒6面（アロマ拡散器）を
    作れたと言える**。
    """
    doc = Document()
    sketch_id = half_disc_sketch(doc)

    operation = extrude(doc, [region_ref(sketch_id, 1)], EXTRUDE_MM, mode="join")
    body_id = operation.args["body_id"]

    assert kinds_of(doc, body_id) == HALF_DISC_PRISM_KINDS
    assert "cylinder_full" not in kinds_of(doc, body_id), "全周円筒が出たら円弧が効いていない"


def test_the_partial_cylinder_spans_the_arc_angle():
    """できた形に**指定した角度の円弧がそのまま残っている**こと（長さで見る）。

    面の種別だけを見ていると「たまたま部分円筒になった」を通してしまう。円弧の長さは
    r·θ で角度と1対1なので、長さを見れば角度を見たことになる（U範囲を直接読む問い合わせは
    持っていないため、`describe_edges` の `length` で代用する）。
    """
    doc = Document()
    sketch_id = half_disc_sketch(doc)
    operation = extrude(doc, [region_ref(sketch_id, 1)], EXTRUDE_MM, mode="join")
    shape = body_of(doc, operation.args["body_id"]).shape

    spans = [
        edge
        for edge in query.describe_edges(shape)
        if edge["kind"] == "circle"
    ]
    assert spans, "円い辺が1本も無いなら円弧が形に残っていない"
    # 円弧の長さ = r * θ(ラジアン)。180度なら πr。
    assert spans[0]["length"] == pytest.approx(3.141592653589793 * ARC_RADIUS, rel=1e-6)
    # 面の側も部分円筒が1枚だけ（全周円筒に化けていない）
    assert kinds_of(doc, operation.args["body_id"]) == HALF_DISC_PRISM_KINDS


def test_the_aroma_diffuser_needs_partial_cylinders():
    """代用が効かない実物の記録（**実装に依存しないので最初から緑**）。

    アロマ拡散器は部分円筒6面を持つ。「円を線で分けて片側だけ押し出す」では
    この面は作れない — これが円弧を前倒した理由そのもの。
    """
    diffuser = MODELS / "aroma_oil_diffuser.step"
    if not diffuser.exists():
        pytest.skip(f"参照STEPが無い: {diffuser}")
    described = query.describe_shape(kernel.read_step(str(diffuser)))
    assert described["face_count"] == 352
    assert described["faces_by_type"]["cylinder_partial"] == 6


# --------------------------------------------------------------------------------------
# 項番15: 操作ログに載り、undoできる（押し出しまで通したとき）
# --------------------------------------------------------------------------------------
def test_arc_survives_a_log_round_trip():
    """円弧を含むスケッチが保存・再生で完全に復元できること。

    円弧の数値（半径・角度）が `args` に載っていなければ再生で別の形になる。
    """
    doc = Document()
    sketch_id = half_disc_sketch(doc)
    extrude(doc, [region_ref(sketch_id, 1)], EXTRUDE_MM, mode="join")
    saved = doc.log.to_json()

    from duocad.oplog import OpLog

    restored = Document()
    log = OpLog.from_json(saved)
    log.replay(restored)
    restored.log = log

    body_id = [body for body in restored.bodies if body.kind == "solid"][-1].id
    assert kinds_of(restored, body_id) == HALF_DISC_PRISM_KINDS


# --------------------------------------------------------------------------------------
# 実物に近い角度と、角度の決め方
# --------------------------------------------------------------------------------------
def test_a_ninety_degree_sector_also_makes_a_partial_cylinder():
    """**実物に近いのは半円ではなく扇形**（アロマ拡散器の部分円筒は75〜97°）。

    90度の扇形（円弧 + 半径の線分2本）を押し出したときの実測:
    平面4 / 部分円筒1 = 5面。半円（180°）だけを契約にしていると、
    「半円のときだけ通る」実装を止められない。
    """
    doc = Document()
    sketch_id = sketch_create(doc, base_plane("XY"))
    add_arc(doc, sketch_id, (0.0, 0.0), ARC_RADIUS, 0.0, 90.0)
    add_line(doc, sketch_id, (0.0, ARC_RADIUS), (0.0, 0.0))
    add_line(doc, sketch_id, (0.0, 0.0), (ARC_RADIUS, 0.0))

    operation = extrude(doc, [region_ref(sketch_id, 1)], EXTRUDE_MM, mode="join")

    assert kinds_of(doc, operation.args["body_id"]) == {"plane": 4, "cylinder_partial": 1}


def test_angles_run_counter_clockwise_and_may_cross_zero():
    """角度は**反時計回りに start_deg から end_deg へ**。0度をまたいでよい。

    `start_deg=350, end_deg=10` は20度の円弧（350度の円弧ではない）。ここを決めておかないと、
    実装者が「大きい方から小さい方へは引き算」と解釈して**340度の円弧**を作る余地が残る。
    円弧の長さ = r·θ なので、期待値は幾何から導ける（実測不要）。
    """
    doc = Document()
    sketch_id = sketch_create(doc, base_plane("XY"))
    add_arc(doc, sketch_id, (0.0, 0.0), ARC_RADIUS, 350.0, 10.0)
    add_line(
        doc,
        sketch_id,
        (ARC_RADIUS * math.cos(math.radians(350)), ARC_RADIUS * math.sin(math.radians(350))),
        (ARC_RADIUS * math.cos(math.radians(10)), ARC_RADIUS * math.sin(math.radians(10))),
    )
    operation = extrude(doc, [region_ref(sketch_id, 1)], EXTRUDE_MM, mode="join")
    shape = body_of(doc, operation.args["body_id"]).shape

    arcs = [edge for edge in query.describe_edges(shape) if edge["kind"] == "circle"]
    assert arcs, "円い辺が残っていない"
    assert arcs[0]["length"] == pytest.approx(
        ARC_RADIUS * math.radians(20.0), rel=1e-6
    ), "0度をまたぐ円弧が20度ではなく340度になっている"
