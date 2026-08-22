"""3verb前倒しの契約(1) — フィレット。項番1〜7・16〜17。

## なぜ前倒したのか

本人の実際の設計は、どれも現行の7verbでは作れない（実測）:

| モデル | ソリッド | 面 | 足りないもの |
|---|---|---|---|
| PCケースの脚 | 1 | 5（平面3 + 全周円筒1 + 円錐1） | 面取りだけ |
| 積層プレート | 1 | 74（平面18 + 部分円筒28 + 円錐8 + トーラス12 + その他8） | 面取り・フィレット |
| アロマ拡散器 | 4 | 352（平面338 + 全周円筒8 + 部分円筒6） | 円弧 |
| PCケース | 27 | 2835（部分円筒646 + トーラス124 + 球43） | フィレット |

この3verbが入ると**受入テストを実物に戻せる**。

## 期待値の出どころ（推測で書かない）

タスクファイルが与える実測値に加え、**duocadの語彙で書くために
測り直した**（`kernel.classify_face` は球を独立の種別に持たず `other` に落とすため、
「円筒12 + 球8 + 平面6」はそのままでは書けない）。20mm立方体・全辺フィレットの実測:

    r=3.0   IsDone=True   plane 6 / cylinder_partial 12 / other(球) 8   計26面
    r=10.0  IsDone=False  （例外なしで黙って失敗する）
    r=10.5  IsDone=True だが **BRepCheck.IsValid=False**（＝「歪」の正体。duocadは受け取らない）
            OCCTが作る面構成は plane 6 / cylinder_partial 12 / bspline 7 / other(球) 1  計26面
    r=30.0  IsDone=False
    半径バラバラ(1.0〜3.75) IsDone=True  plane 6 / cylinder_partial 12 / bspline 8  計26面

穴（貫通・半径4mm）の円い辺2本に r=1.5:

    plane 6 / cylinder_full 1 / torus 2   計9面（フィレット前は plane 6 / cylinder_full 1 の7面）

**球が `other` に落ちること自体が契約**（`FACE_KINDS` に球を足すのはこのタスクの範囲外。
足すと既存の内訳の意味が変わる）。
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from duocad import kernel, query
from duocad.document import Body, Document
from duocad.sketch import add_circle, base_plane, sketch_create
from duocad.verbs import extrude, fillet
from duocad.region import region_ref

#: 20mm立方体・全辺フィレットの実測（上のdocstring参照）
CUBE_MM = 20.0
RADIUS_OK = 3.0
RADIUS_FAILS_LOW = 10.0
RADIUS_ODD_BUT_OK = 10.5
RADIUS_FAILS_HIGH = 30.0

FILLET_R3_KINDS = {"plane": 6, "cylinder_partial": 12, "other": 8}
#: 半径を 1.0 + 0.25*i でバラバラにしたときの実測
VARIED_RADII_KINDS = {"plane": 6, "cylinder_partial": 12, "bspline": 8}
HOLE_FILLET_KINDS = {"plane": 6, "cylinder_full": 1, "torus": 2}


# --------------------------------------------------------------------------------------
# 道具（duocadのverbだけで素材を作る。生のOCPで組み立てない）
# --------------------------------------------------------------------------------------
def new_box(doc: Document, size: float = CUBE_MM) -> str:
    """スケッチ（正方形）→ 押し出しで立方体を1つ作り、body_id を返す。"""
    sketch_id = sketch_create(doc, base_plane("XY"))
    from duocad.sketch import add_rectangle

    add_rectangle(doc, sketch_id, (0.0, 0.0), size, size)
    operation = extrude(doc, [region_ref(sketch_id, 1)], size, mode="join")
    return operation.args["body_id"]


def new_box_with_hole(doc: Document, size: float = CUBE_MM, radius: float = 4.0) -> str:
    """立方体に貫通穴を1つ開けて body_id を返す（項番3・16で使う）。"""
    body_id = new_box(doc, size)
    sketch_id = sketch_create(doc, base_plane("XY"))
    add_circle(doc, sketch_id, (size / 2, size / 2), radius)
    extrude(doc, [region_ref(sketch_id, 1)], size, mode="cut", body_id=body_id)
    return body_id


def body_of(doc: Document, body_id: str) -> Body:
    return next(body for body in doc.bodies if body.id == body_id)


def kinds_of(doc: Document, body_id: str) -> dict:
    """`faces_by_type` から**0でないものだけ**を取り出す（期待値を読みやすくするため）。"""
    described = query.describe_shape(body_of(doc, body_id).shape)
    return {kind: count for kind, count in described["faces_by_type"].items() if count}


def edges_of_kind(doc: Document, body_id: str, kind: str) -> list:
    """`query.describe_edges` で種別の合う辺の添字を返す（辺の選択は問い合わせ層越し）。"""
    return [
        edge["index"]
        for edge in query.describe_edges(body_of(doc, body_id).shape)
        if edge["kind"] == kind
    ]


# --------------------------------------------------------------------------------------
# 項番1: 箱の縦エッジにフィレット（同一半径なら splineなし）
# --------------------------------------------------------------------------------------
def test_fillet_on_a_box_makes_cylinders_spheres_and_planes():
    """全辺・同一半径なら **円筒 + 球 + 平面** になり、B-splineは出ない。

    B-splineは「フィレットの副産物」であって、ロフト等の別機能ではない（項番2で固定する）。
    球は duocad の分類では `other`（`FACE_KINDS` に球は無い）。
    """
    doc = Document()
    body_id = new_box(doc)
    all_edges = [edge["index"] for edge in query.describe_edges(body_of(doc, body_id).shape)]
    assert len(all_edges) == 12, "立方体の辺は12本（重複除去済み）"

    fillet(doc, all_edges, RADIUS_OK, body_id=body_id)

    assert kinds_of(doc, body_id) == FILLET_R3_KINDS
    assert "bspline" not in kinds_of(doc, body_id), "同一半径でsplineが出たら副産物の理解が違う"


def test_fillet_on_the_vertical_edges_only():
    """縦4辺だけを丸める（全部を丸めるのとは別に、選んだ辺だけに効くことを固定する）。

    丸めた縦辺のぶんだけ部分円筒が4枚できる。上面・底面は平面のまま残るので、
    **面数は 6 + 4 = 10**（角の球は生じない＝球は「3辺が集まる角」を丸めたときに出る）。
    """
    doc = Document()
    body_id = new_box(doc)
    # **端点で選ぶ**（添字の決め打ちにしない）。`describe_edges` が端点を返すのはこのため。
    vertical = [
        edge["index"]
        for edge in query.describe_edges(body_of(doc, body_id).shape)
        if edge["kind"] == "line" and edge["start"][2] != edge["end"][2]
    ]
    assert len(vertical) == 4, "立方体の縦辺は4本"

    fillet(doc, vertical, RADIUS_OK, body_id=body_id)

    kinds = kinds_of(doc, body_id)
    assert kinds.get("cylinder_partial") == 4
    assert kinds.get("plane") == 6
    assert kinds.get("other", 0) == 0, "縦辺だけなら角の球は出ない"


# --------------------------------------------------------------------------------------
# 項番2: 半径をバラバラにすると B-spline が出る（副産物であることの固定）
# --------------------------------------------------------------------------------------
def test_varied_radii_produce_bspline_faces():
    """**半径をバラバラにすると B-spline が出る**（実測: bspline 8）。

    トーラスも同時に出る半径の組み合わせが観測されている。どちらにせよ
    要点は「splineはフィレットの副産物であり、B-splineが出たこと自体は異常ではない」。
    """
    doc = Document()
    body_id = new_box(doc)
    all_edges = [edge["index"] for edge in query.describe_edges(body_of(doc, body_id).shape)]
    radii = [1.0 + index * 0.25 for index in range(len(all_edges))]

    fillet(doc, all_edges, radii, body_id=body_id)

    # **実測どおり厳密に**（`bspline > 0` だけだと面構成を取り違えても緑になる）
    assert kinds_of(doc, body_id) == VARIED_RADII_KINDS


def test_radius_list_must_match_the_edges():
    """半径のリストが辺の数と合わなければ ValueError（黙って足りない分を補わない）。"""
    doc = Document()
    body_id = new_box(doc)
    with pytest.raises(ValueError):
        fillet(doc, [1, 2, 3], [1.0, 2.0], body_id=body_id)


# --------------------------------------------------------------------------------------
# 項番3: 穴の円い辺へのフィレットで トーラスが出る
# --------------------------------------------------------------------------------------
def test_fillet_on_a_circular_edge_produces_torus():
    """曲がった辺（穴の縁）を丸めると**トーラス**になる（実測: torus 2）。

    まっすぐな辺は円筒、円い辺はトーラス。**辺の種別が結果の面の種別を決める**ので、
    `query.describe_edges` の `kind` は選択の一部である。
    """
    doc = Document()
    body_id = new_box_with_hole(doc)
    assert kinds_of(doc, body_id) == {"plane": 6, "cylinder_full": 1}

    circular = edges_of_kind(doc, body_id, "circle")
    assert len(circular) == 2, "貫通穴の円い辺は上下の2本"

    fillet(doc, circular, 1.5, body_id=body_id)

    assert kinds_of(doc, body_id) == HOLE_FILLET_KINDS


# --------------------------------------------------------------------------------------
# 項番4: 失敗を黙って通さない（最重要）
# --------------------------------------------------------------------------------------
#: 失敗する半径は2つ実測してあるが、どちらも `kernel._blend_edges` の同じ `IsDone()` 判定を
#: 通るだけなので、低い方1つで足りる(2026-08-22に高い方を落とした)。
@pytest.mark.parametrize("radius", [RADIUS_FAILS_LOW])
def test_a_failing_fillet_raises_and_leaves_nothing_behind(radius: float):
    """`IsDone()=False` を**例外**にし、文書もログも変えない（ログにも積まない）。

    OCCTのフィレットは例外を投げずに失敗する。ここを素通しすると、
    **「操作したのに何も起きていない」がログ上は成功として残る**（再生すると別の形になる）。
    r=10.0 と r=30.0 はどちらも20mm立方体の全辺で `IsDone()=False`（実測）。
    """
    doc = Document()
    body_id = new_box(doc)
    shape_before = body_of(doc, body_id).shape
    kinds_before = kinds_of(doc, body_id)
    log_before = len(doc.log.entries)
    all_edges = [edge["index"] for edge in query.describe_edges(shape_before)]

    # **例外の型まで縛る**。`Exception` で受けると、`IsDone()` を
    # 一度も見ない実装が引数検査の TypeError で落ちても緑になり、契約が空になる。
    # 既存の `extrude` 系と同じく ValueError に揃える。
    with pytest.raises(ValueError):
        fillet(doc, all_edges, radius, body_id=body_id)

    assert body_of(doc, body_id).shape is shape_before, "失敗したのに形状が置き換わった"
    assert kinds_of(doc, body_id) == kinds_before
    assert len(doc.log.entries) == log_before, "失敗したのにログへ積まれた"


#--------------------------------------------------------------------------------------
# 項番5: r=10.5 は「成功するが歪」
# --------------------------------------------------------------------------------------
def test_radius_10_5_is_reported_as_done_but_is_actually_broken():
    """r=10.5 の「成功するが歪」の**正体**は `BRepCheck` が偽になることだった（実測）。

        r=3.0   IsDone=True  BRepCheck.IsValid=True   volume 7572.619
        r=10.5  IsDone=True  BRepCheck.IsValid=False  volume 9246.464

    **歪さは機械判定できる**(上の実測)ので、docstringの注意書きではなく契約として
    固定する(項番5)。duocadは `BRepCheck` が偽の形状を**文書に入れない** —
    「例外を投げずに黙って失敗する」を通さないのがこのリポジトリの一番の規律であり
    （§6予防6・§7）、`extrude_face_on_shape` も同じく `BRepCheck` を最後の関門にしている。
    OCCTが出す面構成（平面6 / 部分円筒12 / B-spline 7 / 球1 = `FILLET_R105_KINDS`）は
    記録として残すが、**その形状は受け取らない**。

    r=10.5 は立方体の半分（10mm）をわずかに超える半径で、フィレット同士が干渉しはじめる領域。
    """
    doc = Document()
    body_id = new_box(doc)
    shape_before = body_of(doc, body_id).shape
    log_before = len(doc.log.entries)
    all_edges = [edge["index"] for edge in query.describe_edges(shape_before)]

    with pytest.raises(ValueError) as error:
        fillet(doc, all_edges, RADIUS_ODD_BUT_OK, body_id=body_id)

    assert "BRepCheck" in str(error.value), (
        "r=10.5 は IsDone では止まらない。止めているのが BRepCheck であること自体が契約"
    )
    assert body_of(doc, body_id).shape is shape_before
    assert len(doc.log.entries) == log_before


# --------------------------------------------------------------------------------------
# 項番6: 操作ログに載り、undoできる
# --------------------------------------------------------------------------------------
def test_fillet_is_logged_and_undoable():
    """フィレットは操作ログに1行積まれ、undoで元の形状へ戻る。"""
    doc = Document()
    body_id = new_box(doc)
    kinds_before = kinds_of(doc, body_id)
    volume_before = query.volume(body_of(doc, body_id).shape)
    all_edges = [edge["index"] for edge in query.describe_edges(body_of(doc, body_id).shape)]

    operation = fillet(doc, all_edges, RADIUS_OK, body_id=body_id)

    assert operation.verb == "fillet"
    assert operation.args["edges"] == all_edges
    assert operation.args["radius"] == RADIUS_OK
    assert operation.args["body_id"] == body_id
    assert doc.log.entries[-1] is operation

    doc.log.undo(doc)
    assert kinds_of(doc, body_id) == kinds_before
    assert query.volume(body_of(doc, body_id).shape) == pytest.approx(volume_before)

    doc.log.redo(doc)
    assert kinds_of(doc, body_id) == FILLET_R3_KINDS


# --------------------------------------------------------------------------------------
# 項番7: 由来（provenance）が回収されている
# --------------------------------------------------------------------------------------
def test_fillet_records_provenance_through_the_normalizing_adapter():
    """フィレットは `History()` を持たない。**正規化アダプタ経由**で由来が埋まること。

    `BRepFilletAPI_MakeFillet` で実測: `History` 無し / `Modified`・
    `Generated`・`IsDeleted` 有り。`kernel.normalize_provenance` はこの差を吸収するために
    存在するので（ブーリアンは `History()` を持つ）、verb側に分岐を書かないことも契約。

    `Operation.provenance` は**JSON化できる射影**であること（`OpLog.to_json()` が通る）。
    生の `TopoDS_Shape` を入れると保存時に落ちる。
    """
    doc = Document()
    body_id = new_box(doc)
    all_edges = [edge["index"] for edge in query.describe_edges(body_of(doc, body_id).shape)]

    operation = fillet(doc, all_edges, RADIUS_OK, body_id=body_id)

    assert operation.provenance is not None, "由来が空のまま通ったら回収できていない"
    assert operation.provenance["verb"] == "fillet"
    assert "face_map" in operation.provenance
    doc.log.to_json()  # JSON化できない値が混ざっていればここで落ちる


# --------------------------------------------------------------------------------------
# 項番17: フィレット後は面の添字がずれる（生の添字を保存してはいけない）
# --------------------------------------------------------------------------------------
def test_fillet_shifts_face_indices():
    """**同じ面を追跡すると添字が変わりうる**ことを実行可能な形で残す。

    実測(20mm立方体の1辺に r=2 のフィレット)で、面の添字は
    1→1 / 2→7 / 3→6 / 4→4 / 5→5 / 6→2 と入れ替わった(別の観測では i=5→9 だったこともある)。
    穴の径変更・穴の追加・ポケット・面取りでは動かなかったので、**散発的に静かに間違う**。

    これは設計前提「**生の添字を保存してはいけない**」（実装計画書§7）の根拠であり、
    由来（`face_map`）を通して追跡しなければならない理由そのもの。
    """
    doc = Document()
    body_id = new_box(doc)
    before = query.describe_shape(body_of(doc, body_id).shape)
    assert before["face_count"] == 6
    all_edges = [edge["index"] for edge in query.describe_edges(body_of(doc, body_id).shape)]

    operation = fillet(doc, all_edges[:1], 2.0, body_id=body_id)

    face_map = operation.provenance["face_map"]
    moved = [
        (int(old), new)
        for old, new in face_map.items()
        if new and int(old) != new[0]
    ]
    assert moved, "1面も添字が動かないなら、この実測（および§7の前提）と食い違う"


# --------------------------------------------------------------------------------------
# 選択と対象の拒否
# --------------------------------------------------------------------------------------
def test_fillet_rejects_an_unknown_edge_index():
    """範囲外の辺の添字は ValueError（黙って無視して「成功」にしない）。"""
    doc = Document()
    body_id = new_box(doc)
    log_before = len(doc.log.entries)
    with pytest.raises(ValueError):
        fillet(doc, [999], 1.0, body_id=body_id)
    assert len(doc.log.entries) == log_before


def test_fillet_refuses_a_reference_body():
    """参照ボディ（読み込んだSTEPの原本）は編集できない（既存verbと同じ契約）。"""
    doc = Document()
    doc.add_body(Body(id="ref-1", name="original", kind="reference", shape=None))
    with pytest.raises(ValueError):
        fillet(doc, [1], 1.0, body_id="ref-1")
    assert doc.log.entries == []


def test_fillet_needs_at_least_one_edge():
    """辺を1本も選ばずに呼んだら ValueError（何も起きないのを成功にしない）。"""
    doc = Document()
    body_id = new_box(doc)
    with pytest.raises(ValueError):
        fillet(doc, [], 1.0, body_id=body_id)


# --------------------------------------------------------------------------------------
# 面取りとフィレットの合成
# --------------------------------------------------------------------------------------
def test_chamfer_and_fillet_compose_on_the_same_body():
    """**同じボディに面取りとフィレットを順にかけられる**こと。

    本命の受入対象（積層プレート）は円錐8とトーラス12を**同時に**持つので、片方だけ動く
    実装では実物に届かない。32件を緑にしてもこの合成が壊れていれば意味がない、というのが
    このテストの存在理由。

    実測（20mm立方体＋貫通穴r=4、穴の縁を面取り1mm → もう一方の縁をフィレット1mm）:

        面取り後      平面6 / 全周円筒1 / 円錐1        = 8面
        フィレット後  平面6 / 全周円筒1 / 円錐1 / トーラス1 = 9面
    """
    from duocad.verbs import chamfer

    doc = Document()
    body_id = new_box_with_hole(doc)
    circular = edges_of_kind(doc, body_id, "circle")
    assert len(circular) == 2

    chamfer(doc, circular[:1], 1.0, body_id=body_id)
    assert kinds_of(doc, body_id) == {"plane": 6, "cylinder_full": 1, "cone": 1}

    # 面取りで辺の添字は変わりうるので、**選び直す**（生の添字を持ち回らない。§7）
    remaining = edges_of_kind(doc, body_id, "circle")
    fillet(doc, remaining[-1:], 1.0, body_id=body_id)

    assert kinds_of(doc, body_id) == {
        "plane": 6,
        "cylinder_full": 1,
        "cone": 1,
        "torus": 1,
    }
    assert len(doc.log.entries) >= 2
    doc.log.undo(doc)
    assert kinds_of(doc, body_id) == {"plane": 6, "cylinder_full": 1, "cone": 1}
