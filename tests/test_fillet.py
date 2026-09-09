"""フィレットの契約。半径によって成功/失敗/「成功だが歪」に分岐する境目が壊れていないか。

期待値の出どころ(20mm立方体・全辺フィレットの実測):

    r=3.0   IsDone=True   plane 6 / cylinder_partial 12 / other(球) 8   計26面
    r=10.0  IsDone=False  (例外なしで黙って失敗する)
    r=10.5  IsDone=True だが **BRepCheck.IsValid=False**(＝「歪」の正体。duocadは受け取らない)
    r=30.0  IsDone=False
    半径バラバラ(1.0〜3.75) IsDone=True  plane 6 / cylinder_partial 12 / bspline 8  計26面

穴(貫通・半径4mm)の円い辺2本に r=1.5 で plane 6 / cylinder_full 1 / torus 2 の計9面。

**球が `other` に落ちること自体が契約**(`kernel.classify_face` は球を独立の種別に持たない)。
"""
from __future__ import annotations


import pytest

from duocad import query
from duocad.document import Document
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
    sketch_id = sketch_create(doc, base_plane("XY"))
    from duocad.sketch import add_rectangle

    add_rectangle(doc, sketch_id, (0.0, 0.0), size, size)
    operation = extrude(doc, [region_ref(sketch_id, 1)], size, mode="join")
    return operation.args["body_id"]


def new_box_with_hole(doc: Document, size: float = CUBE_MM, radius: float = 4.0) -> str:
    body_id = new_box(doc, size)
    sketch_id = sketch_create(doc, base_plane("XY"))
    add_circle(doc, sketch_id, (size / 2, size / 2), radius)
    extrude(doc, [region_ref(sketch_id, 1)], size, mode="cut", body_id=body_id)
    return body_id


def body_of(doc: Document, body_id: str) -> Body:
    return next(body for body in doc.bodies if body.id == body_id)


def kinds_of(doc: Document, body_id: str) -> dict:
    described = query.describe_shape(body_of(doc, body_id).shape)
    return {kind: count for kind, count in described["faces_by_type"].items() if count}


def edges_of_kind(doc: Document, body_id: str, kind: str) -> list:
    return [
        edge["index"]
        for edge in query.describe_edges(body_of(doc, body_id).shape)
        if edge["kind"] == kind
    ]


# --------------------------------------------------------------------------------------
# 箱の縦エッジにフィレット（同一半径なら splineなし）
# --------------------------------------------------------------------------------------
def test_fillet_on_a_box_makes_cylinders_spheres_and_planes():
    """全辺・同一半径なら**円筒 + 球 + 平面**になり、B-splineは出ない(球は `other`)。"""
    doc = Document()
    body_id = new_box(doc)
    all_edges = [edge["index"] for edge in query.describe_edges(body_of(doc, body_id).shape)]
    assert len(all_edges) == 12, "立方体の辺は12本（重複除去済み）"

    fillet(doc, all_edges, RADIUS_OK, body_id=body_id)

    assert kinds_of(doc, body_id) == FILLET_R3_KINDS
    assert "bspline" not in kinds_of(doc, body_id), "同一半径でsplineが出たら副産物の理解が違う"


def test_fillet_on_the_vertical_edges_only():
    """縦辺だけのフィレットでは角の球ができない(実測: 部分円筒4枚+平面6枚)。"""
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
# 半径をバラバラにすると B-spline が出る（副産物であることの固定）
# --------------------------------------------------------------------------------------
def test_varied_radii_produce_bspline_faces():
    """**半径をバラバラにすると B-spline が出る**(splineはフィレットの副産物であり異常ではない)。"""
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
# 穴の円い辺へのフィレットで トーラスが出る
# --------------------------------------------------------------------------------------
def test_fillet_on_a_circular_edge_produces_torus():
    """曲がった辺(穴の縁)を丸めると**トーラス**になる —— **辺の種別が結果の面の種別を決める**。"""
    doc = Document()
    body_id = new_box_with_hole(doc)
    assert kinds_of(doc, body_id) == {"plane": 6, "cylinder_full": 1}

    circular = edges_of_kind(doc, body_id, "circle")
    assert len(circular) == 2, "貫通穴の円い辺は上下の2本"

    fillet(doc, circular, 1.5, body_id=body_id)

    assert kinds_of(doc, body_id) == HOLE_FILLET_KINDS


# --------------------------------------------------------------------------------------
# 失敗を黙って通さない（最重要）
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("radius", [RADIUS_FAILS_LOW])
def test_a_failing_fillet_raises_and_leaves_nothing_behind(radius: float):
    """`IsDone()=False` を例外にし、文書もログも変えない。"""
    doc = Document()
    body_id = new_box(doc)
    shape_before = body_of(doc, body_id).shape
    kinds_before = kinds_of(doc, body_id)
    log_before = len(doc.log.entries)
    all_edges = [edge["index"] for edge in query.describe_edges(shape_before)]

    # 例外の型まで縛る: `Exception` で受けると、`IsDone()` を一度も見ない実装が
    # 引数検査の TypeError で落ちても緑になり、この検査が空になる。
    with pytest.raises(ValueError):
        fillet(doc, all_edges, radius, body_id=body_id)

    assert body_of(doc, body_id).shape is shape_before, "失敗したのに形状が置き換わった"
    assert kinds_of(doc, body_id) == kinds_before
    assert len(doc.log.entries) == log_before, "失敗したのにログへ積まれた"


#--------------------------------------------------------------------------------------
# r=10.5 は「成功するが歪」
# --------------------------------------------------------------------------------------
def test_radius_10_5_is_reported_as_done_but_is_actually_broken():
    """r=10.5は`IsDone()`は真だが`BRepCheck.IsValid=False`(実測) —— 立方体の半辺(10mm)を
    わずかに超えてフィレット同士が干渉し始める境界値。duocadはこの形状を文書に入れない。
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
# 操作ログに載り、undoできる
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
# 由来（provenance）が回収されている
# --------------------------------------------------------------------------------------
def test_fillet_records_provenance_through_the_normalizing_adapter():
    """由来(`Operation.provenance`)が正規化アダプタ経由で埋まり、JSON化できること。"""
    doc = Document()
    body_id = new_box(doc)
    all_edges = [edge["index"] for edge in query.describe_edges(body_of(doc, body_id).shape)]

    operation = fillet(doc, all_edges, RADIUS_OK, body_id=body_id)

    assert operation.provenance is not None, "由来が空のまま通ったら回収できていない"
    assert operation.provenance["verb"] == "fillet"
    assert "face_map" in operation.provenance
    doc.log.to_json()  # JSON化できない値が混ざっていればここで落ちる


# --------------------------------------------------------------------------------------
# フィレット後は面の添字がずれる（生の添字を保存してはいけない）
# --------------------------------------------------------------------------------------
def test_fillet_shifts_face_indices():
    """同じ面でも添字は形状変更で変わりうる(実測: 20mm立方体の1辺にr=2で2→7/3→6/6→2)。
    穴の径変更・追加・ポケット・面取りでは動かなかった —— 散発的に静かに間違う。
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
    """同じボディに面取りとフィレットを順にかけられること。"""
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
