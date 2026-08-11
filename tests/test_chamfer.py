"""3verb前倒しの契約(2) — 面取り。項番8〜11（2026-08-11）。

**2026-08-11に実装が入って全件緑になった**。書かれた当初はスタブに対して全件赤
（`NotImplementedError`）で、それが正常な状態だった。契約は書いた時点から変えていない。

## タスクファイルの項番8を実測で訂正した（重要）

タスクファイルは項番8を「**箱のエッジに面取りをかけると円錐面ができる**こと」と書いているが、
2026-08-11に測ったところ**そうならない**:

    箱のまっすぐな辺を面取り（距離2mm） -> IsDone=True / plane 7 / 面7   ← **平面が1枚増えるだけ**
    円筒の円い縁を面取り（距離1mm）     -> IsDone=True / plane 2 + cylinder_full 1 + **cone 1** / 面4

円錐は**円い辺**の面取りから生まれる。まっすぐな辺の面取りは平面になる（当然で、
まっすぐな辺に沿って平らに削るのだから面も平ら）。タスクファイル自身が
「数値の期待値を推測で書かない。実測値以外が要るなら、その場で測ってから書く」と指示して
いるので、**実測どおりに書いた**。項番8の意図（面取りが新しい面を作ることの確認）は
`test_chamfering_a_straight_edge_makes_a_plane` が、項番9の意図（実物の円錐が作れること）は
`test_chamfering_a_circular_edge_makes_a_cone` が引き受ける。

## 項番9 — 「実物が1つ作れるようになる」

`PC_case_feet.step`（PCケースの脚）の実測（2026-08-11）:

    5面 = plane 3 + cylinder_full 1 + cone 1
    体積 11538.02 mm^3 / 境界箱 25.0 x 25.0 x 29.0 mm
    内訳: 底の円板(r=12.5, z=0) / 円錐(z=0〜22) / 円環の平面(z=22) / 円筒(r=8.5, z=22〜29) / 上面(z=29)

**面取りが入って初めて円錐が作れる**ようになる、というのがこのタスクの意義そのもの。
ただし脚と**寸法まで同じ**形を組むことは受入テストの再定義にあたり、本人の判断事項なので
ここでは行わない（次々タスクの引継ぎに「受入テスト再定義待ち」と書く）。ここで固定するのは
**必要な面種（平面・全周円筒・円錐）がverbで作れるようになること**と、
**脚が要求している構成が何か**の2つ。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from duocad import kernel, query
from duocad.document import Body, Document
from duocad.region import region_ref
from duocad.sketch import add_circle, add_rectangle, base_plane, sketch_create
from duocad.verbs import chamfer, extrude

MODELS = Path(r"C:\Users\tm976\mf\3d-models")
FEET = MODELS / "mATX_PC_case_20251122" / "PC_case_feet.step"

#: PCケースの脚の実測（2026-08-11）
FEET_KINDS = {"plane": 3, "cylinder_full": 1, "cone": 1}
FEET_FACE_COUNT = 5

#: 円筒の円い縁を1つ面取りしたときの実測（2026-08-11）
CYLINDER_CHAMFER_KINDS = {"plane": 2, "cylinder_full": 1, "cone": 1}


def body_of(doc: Document, body_id: str) -> Body:
    return next(body for body in doc.bodies if body.id == body_id)


def kinds_of(doc: Document, body_id: str) -> dict:
    described = query.describe_shape(body_of(doc, body_id).shape)
    return {kind: count for kind, count in described["faces_by_type"].items() if count}


def new_box(doc: Document, size: float = 20.0) -> str:
    sketch_id = sketch_create(doc, base_plane("XY"))
    add_rectangle(doc, sketch_id, (0.0, 0.0), size, size)
    return extrude(doc, [region_ref(sketch_id, 1)], size, mode="join").args["body_id"]


def new_cylinder(doc: Document, radius: float = 10.0, height: float = 5.0) -> str:
    """円を押し出して円筒を作る（duocadのverbだけで作れる ＝ 生のOCPを使わない）。"""
    sketch_id = sketch_create(doc, base_plane("XY"))
    add_circle(doc, sketch_id, (0.0, 0.0), radius)
    return extrude(doc, [region_ref(sketch_id, 1)], height, mode="join").args["body_id"]


def edges_of_kind(doc: Document, body_id: str, kind: str) -> list:
    return [
        edge["index"]
        for edge in query.describe_edges(body_of(doc, body_id).shape)
        if edge["kind"] == kind
    ]


# --------------------------------------------------------------------------------------
# 項番8（実測で訂正）: まっすぐな辺の面取りは平面／円い辺の面取りが円錐
# --------------------------------------------------------------------------------------
def test_chamfering_a_straight_edge_makes_a_plane():
    """箱のまっすぐな辺を面取りすると**平面が1枚増える**（2026-08-11実測: 6面 -> 平面7）。

    タスクファイルの項番8は「円錐面ができる」と書いているが、まっすぐな辺では円錐にならない。
    円錐は円い辺から生まれる（次のテスト）。
    """
    doc = Document()
    body_id = new_box(doc)
    assert kinds_of(doc, body_id) == {"plane": 6}
    straight = edges_of_kind(doc, body_id, "line")
    assert len(straight) == 12

    chamfer(doc, straight[:1], 2.0, body_id=body_id)

    assert kinds_of(doc, body_id) == {"plane": 7}
    assert "cone" not in kinds_of(doc, body_id)


def test_chamfering_a_circular_edge_makes_a_cone():
    """**円い辺**を面取りすると円錐ができる（2026-08-11実測: 円筒3面 -> 平面2+円筒1+円錐1）。

    これが「面取りが入って初めて実物（PCケースの脚）が作れるようになる」の実体。
    """
    doc = Document()
    body_id = new_cylinder(doc)
    assert kinds_of(doc, body_id) == {"plane": 2, "cylinder_full": 1}
    circular = edges_of_kind(doc, body_id, "circle")
    assert len(circular) == 2, "円筒の円い縁は上下の2本"

    chamfer(doc, circular[:1], 1.0, body_id=body_id)

    assert kinds_of(doc, body_id) == CYLINDER_CHAMFER_KINDS


# --------------------------------------------------------------------------------------
# 項番9: 実物（PCケースの脚）が要求する構成
# --------------------------------------------------------------------------------------
def test_the_real_foot_needs_a_cone():
    """`PC_case_feet.step` は 平面3 + 全周円筒1 + 円錐1 = 5面（2026-08-11実測）。

    **実装に依存しないので最初から緑**。この構成の円錐は現行の7verbでは作れず、
    面取りが入って初めて到達可能になる、という到達点の記録。
    """
    if not FEET.exists():
        pytest.skip(f"参照STEPが無い: {FEET}")
    described = query.describe_shape(kernel.read_step(str(FEET)))
    assert described["face_count"] == FEET_FACE_COUNT
    assert {k: v for k, v in described["faces_by_type"].items() if v} == FEET_KINDS


def test_how_close_the_verbs_get_to_the_real_foot():
    """**脚にどこまで近づけるか**を厳密な期待値で固定し、残る差を明示する。

    脚に一番近い「verbだけで作れる形」は円筒の縁を1回面取りしたもので、実測は
    平面2 + 全周円筒1 + 円錐1 = **4面**。実物は平面3 + 全周円筒1 + 円錐1 = **5面** で、
    差は**平面1枚（z=22の円環）**である。

    **なぜ埋まらないか**: 実物の円錐は半径12.5→8.5・高さ22mmで、これは面取りの2距離が
    非対称（半径方向4mm・軸方向22mm）ということ。今回のAPIは距離1つ（45度）だけを取り、
    **面取りの角度指定はこの前倒しの範囲外**とタスクファイルが明記している。したがって
    「脚と寸法まで同じ形」は今回の到達点ではない。

    面種の**集合の包含**で通す書き方（`set(FEET_KINDS) <= produced`）にしていた版は、
    4面でも5面でも緑になり「実物が作れる」と誤って主張できたので、実測値そのものに落とした
    （2026-08-11のQA指摘）。受入テストの再定義は本人の判断事項。
    """
    doc = Document()
    body_id = new_cylinder(doc, radius=12.5, height=29.0)
    circular = edges_of_kind(doc, body_id, "circle")

    chamfer(doc, circular[:1], 4.0, body_id=body_id)

    produced = kinds_of(doc, body_id)
    assert produced == CYLINDER_CHAMFER_KINDS, "verbだけで作れる形が実測と違う"
    assert set(FEET_KINDS) == set(produced), "脚に要る面種は揃っている（種類としては到達）"
    assert produced["plane"] == FEET_KINDS["plane"] - 1, (
        "残る差は平面1枚（非対称な面取り＝角度指定が要る。今回の範囲外）"
    )


# --------------------------------------------------------------------------------------
# 項番10: 失敗を黙って通さない
# --------------------------------------------------------------------------------------
def test_a_failing_chamfer_raises_and_leaves_nothing_behind():
    """`IsDone()=False` を例外にし、文書もログも変えない（フィレットと同じ契約）。

    `BRepFilletAPI_MakeChamfer` はフィレットと同系統で、**例外を投げずに失敗する**。
    ここでは形状に対して明らかに大きすぎる距離（20mm立方体の辺に対して 50mm）を与える。
    """
    doc = Document()
    body_id = new_box(doc)
    shape_before = body_of(doc, body_id).shape
    kinds_before = kinds_of(doc, body_id)
    log_before = len(doc.log.entries)
    straight = edges_of_kind(doc, body_id, "line")

    # 例外の型まで縛る（`Exception` だと `IsDone()` を見ない実装でも緑になる。QA指摘）
    with pytest.raises(ValueError):
        chamfer(doc, straight, 50.0, body_id=body_id)

    assert body_of(doc, body_id).shape is shape_before
    assert kinds_of(doc, body_id) == kinds_before
    assert len(doc.log.entries) == log_before, "失敗したのにログへ積まれた"


def test_chamfer_rejects_an_unknown_edge_index():
    """範囲外の辺の添字は ValueError（黙って無視して「成功」にしない）。"""
    doc = Document()
    body_id = new_box(doc)
    log_before = len(doc.log.entries)
    with pytest.raises(ValueError):
        chamfer(doc, [999], 1.0, body_id=body_id)
    assert len(doc.log.entries) == log_before


# --------------------------------------------------------------------------------------
# 項番11: 操作ログに載り、undoできる
# --------------------------------------------------------------------------------------
def test_chamfer_is_logged_and_undoable():
    doc = Document()
    body_id = new_cylinder(doc)
    kinds_before = kinds_of(doc, body_id)
    volume_before = query.volume(body_of(doc, body_id).shape)
    circular = edges_of_kind(doc, body_id, "circle")

    operation = chamfer(doc, circular[:1], 1.0, body_id=body_id)

    assert operation.verb == "chamfer"
    assert operation.args["edges"] == circular[:1]
    assert operation.args["distance"] == 1.0
    assert operation.args["body_id"] == body_id

    doc.log.undo(doc)
    assert kinds_of(doc, body_id) == kinds_before
    assert query.volume(body_of(doc, body_id).shape) == pytest.approx(volume_before)

    doc.log.redo(doc)
    assert kinds_of(doc, body_id) == CYLINDER_CHAMFER_KINDS


def test_chamfer_records_provenance_through_the_normalizing_adapter():
    """面取りもフィレットと同じく `History()` を持たない側。由来は正規化アダプタ経由。"""
    doc = Document()
    body_id = new_cylinder(doc)
    circular = edges_of_kind(doc, body_id, "circle")

    operation = chamfer(doc, circular[:1], 1.0, body_id=body_id)

    assert operation.provenance is not None
    assert operation.provenance["verb"] == "chamfer"
    # フィレットと同じキーを要求する（片方だけ緩いと、面取りだけ由来が空でも緑になる。QA指摘）
    assert "face_map" in operation.provenance
    doc.log.to_json()  # JSON化できない値が混ざっていればここで落ちる
