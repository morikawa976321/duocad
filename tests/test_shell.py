"""シェル(中空化)の契約。開口の面と壁の厚みで中空になり、失敗は文書にもログにも残らない。

期待値の出どころ(20mm立方体、`kernel.shell_solid` の docstring と同じ実測):

    上面1枚を開口・厚み2        11面(すべて平面)  体積 8000 -> 3392 (= 8000 - 16*16*18)
    上面と下面を開口・厚み2      10面             体積 2880 (= 8000 - 16*16*20)
    穴(貫通・半径4)つき・上面開口・厚み2   14面
    厚み 10                     IsDone=True だが BRepCheck が偽 -> 例外
    厚み 11 / 全辺2mm面取り後の厚み1   形が変わらず返る -> 例外(黙って通さない)
    上辺4本 r=3 で丸めてから上面開口・厚み2   BRepCheck が偽だが ShapeFix で直る -> 受け取る(体積 3642.5)
"""
from __future__ import annotations

import json

import pytest

from duocad import query
from duocad.document import Body, Document
from duocad.region import region_ref
from duocad.sketch import add_circle, add_rectangle, base_plane, sketch_create
from duocad.verbs import chamfer, extrude, fillet, shell

CUBE_MM = 20.0
THICKNESS_OK = 2.0
#: 上面1枚を開口・厚み2 の実測(内側 16×16×18 が消える)
VOLUME_TOP_OPEN = CUBE_MM**3 - 16.0 * 16.0 * 18.0
#: 上面と下面を開口・厚み2 の実測(内側 16×16×20 が消える)
VOLUME_TOP_AND_BOTTOM_OPEN = CUBE_MM**3 - 16.0 * 16.0 * 20.0
#: 上辺4本を r=3 で丸めた立方体を上面開口・厚み2 で中空にした実測(ShapeFix 修復後も同じ)
VOLUME_ROUNDED_LIP = 3642.5


# --------------------------------------------------------------------------------------
# 道具(duocadのverbだけで素材を作る。生のOCPで組み立てない)
# --------------------------------------------------------------------------------------
def new_box(doc: Document, size: float = CUBE_MM) -> str:
    sketch_id = sketch_create(doc, base_plane("XY"))
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


def faces_facing(doc: Document, body_id: str, axis: int, sign: float) -> list:
    """外向き法線が `sign * e_axis` の面の添字(**決め打ちしない**)。"""
    return [
        face["index"]
        for face in query.describe_faces(body_of(doc, body_id).shape)
        if face["normal"][axis] * sign > 0.9
    ]


def top_face(doc: Document, body_id: str) -> int:
    (index,) = faces_facing(doc, body_id, 2, +1.0)
    return index


def edges_at_z(doc: Document, body_id: str, z: float) -> list:
    return [
        edge["index"]
        for edge in query.describe_edges(body_of(doc, body_id).shape)
        if abs(edge["start"][2] - z) < 1e-6 and abs(edge["end"][2] - z) < 1e-6
    ]


# --------------------------------------------------------------------------------------
# 成功する形
# --------------------------------------------------------------------------------------
def test_shell_opens_the_top_and_leaves_a_wall():
    """上面を開口にすると外側5面+縁1面+内側5面の11面(すべて平面)になり、内側の体積だけ消える。"""
    doc = Document()
    body_id = new_box(doc)
    opening = top_face(doc, body_id)
    volume_before = query.volume(body_of(doc, body_id).shape)

    operation = shell(doc, [opening], THICKNESS_OK, body_id=body_id)

    assert kinds_of(doc, body_id) == {"plane": 11}
    assert query.volume(body_of(doc, body_id).shape) == pytest.approx(VOLUME_TOP_OPEN, rel=1e-6)
    assert volume_before == pytest.approx(CUBE_MM**3, rel=1e-6)
    assert operation.verb == "shell"
    assert operation.args == {"faces": [opening], "thickness": THICKNESS_OK, "body_id": body_id}
    assert doc.log.entries[-1] is operation


def test_two_openings_make_a_tube():
    """上面と下面を開口にすると内側が貫通し、10面・体積 2880 になる。"""
    doc = Document()
    body_id = new_box(doc)
    openings = faces_facing(doc, body_id, 2, +1.0) + faces_facing(doc, body_id, 2, -1.0)
    assert len(openings) == 2

    shell(doc, openings, THICKNESS_OK, body_id=body_id)

    assert kinds_of(doc, body_id) == {"plane": 10}
    assert query.volume(body_of(doc, body_id).shape) == pytest.approx(
        VOLUME_TOP_AND_BOTTOM_OPEN, rel=1e-6
    )


def test_shell_keeps_a_wall_around_a_through_hole():
    """穴つきの箱を上面開口で中空にすると、穴の円筒の周りにも壁が残る(14面)。"""
    doc = Document()
    body_id = new_box_with_hole(doc)
    volume_before = query.volume(body_of(doc, body_id).shape)

    shell(doc, [top_face(doc, body_id)], THICKNESS_OK, body_id=body_id)

    described = query.describe_shape(body_of(doc, body_id).shape)
    assert described["face_count"] == 14
    assert 0 < query.volume(body_of(doc, body_id).shape) < volume_before


def test_provenance_is_json_serialisable_and_maps_the_opening_to_its_rim():
    """由来はJSON化でき、開口の面は縁の面1枚へ、他の外側の面は自分自身へ写る。内側の5面は新規。"""
    doc = Document()
    body_id = new_box(doc)
    opening = top_face(doc, body_id)

    operation = shell(doc, [opening], THICKNESS_OK, body_id=body_id)

    provenance = operation.provenance
    json.dumps(provenance)
    assert provenance["verb"] == "shell"
    assert provenance["faces"] == [opening]
    assert provenance["thickness"] == THICKNESS_OK
    assert provenance["repaired"] is False
    assert len(provenance["face_map"][str(opening)]) == 1, "開口の面は縁の面1枚になる"
    for index in range(1, 7):
        if index != opening:
            assert provenance["face_map"][str(index)], f"外側の面 {index} が消えたことになっている"
    assert provenance["deleted"] == []
    assert len(provenance["new_faces"]) == 5, "内側の5面が新規"


def test_shell_is_logged_and_undoable():
    """ログに1行積まれ、undoで元の形状(6面・体積8000)へ戻り、redoで中空に戻る。"""
    doc = Document()
    body_id = new_box(doc)
    shape_before = body_of(doc, body_id).shape
    log_before = len(doc.log.entries)

    shell(doc, [top_face(doc, body_id)], THICKNESS_OK, body_id=body_id)
    assert len(doc.log.entries) == log_before + 1

    doc.log.undo(doc)
    assert body_of(doc, body_id).shape is shape_before
    assert kinds_of(doc, body_id) == {"plane": 6}

    doc.log.redo(doc)
    assert kinds_of(doc, body_id) == {"plane": 11}
    assert query.volume(body_of(doc, body_id).shape) == pytest.approx(VOLUME_TOP_OPEN, rel=1e-6)


# --------------------------------------------------------------------------------------
# 失敗は文書にもログにも残らない
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("thickness", [0.0, -2.0, 10.0, 11.0, 30.0])
def test_a_failing_shell_raises_and_leaves_nothing_behind(thickness: float):
    """厚み0以下・半辺に届く厚み(BRepCheck偽)・形が変わらない厚み はすべて例外で、文書もログも変えない。"""
    doc = Document()
    body_id = new_box(doc)
    shape_before = body_of(doc, body_id).shape
    log_before = len(doc.log.entries)

    with pytest.raises(ValueError):
        shell(doc, [top_face(doc, body_id)], thickness, body_id=body_id)

    assert body_of(doc, body_id).shape is shape_before, "失敗したのに形状が置き換わった"
    assert len(doc.log.entries) == log_before, "失敗したのにログへ積まれた"


def test_opening_every_face_is_rejected():
    """6面すべてを開口にすると OCCT は入力をそのまま返す(実測)。壁が残らないので拒否する。"""
    doc = Document()
    body_id = new_box(doc)
    with pytest.raises(ValueError):
        shell(doc, [1, 2, 3, 4, 5, 6], THICKNESS_OK, body_id=body_id)
    assert kinds_of(doc, body_id) == {"plane": 6}


@pytest.mark.parametrize("faces", [[], [0], [7], [1, 1]])
def test_bad_face_lists_are_rejected(faces: list):
    doc = Document()
    body_id = new_box(doc)
    log_before = len(doc.log.entries)
    with pytest.raises(ValueError):
        shell(doc, faces, THICKNESS_OK, body_id=body_id)
    assert len(doc.log.entries) == log_before, "失敗したのにログへ積まれた"


def test_shell_after_a_full_chamfer_is_rejected_instead_of_silently_doing_nothing():
    """全辺を面取りした箱は MakeThickSolid が入力をそのまま返す(実測)。体積検査が捕まえて例外にする。"""
    doc = Document()
    body_id = new_box(doc)
    all_edges = [edge["index"] for edge in query.describe_edges(body_of(doc, body_id).shape)]
    chamfer(doc, all_edges, 2.0, body_id=body_id)
    shape_before = body_of(doc, body_id).shape

    with pytest.raises(ValueError, match="unchanged"):
        shell(doc, [top_face(doc, body_id)], 1.0, body_id=body_id)

    assert body_of(doc, body_id).shape is shape_before


# --------------------------------------------------------------------------------------
# フィレット済み(口の縁が丸い)は ShapeFix で修復して受け取る
# --------------------------------------------------------------------------------------
def test_a_rounded_lip_is_repaired_and_accepted():
    """上辺4本を丸めた箱を上面開口で中空にすると BRepCheck が偽になる(実測)が、面1枚の向きだけの
    問題なので ShapeFix で直して受け取る。体積は修復前後で同じ 3642.5。"""
    doc = Document()
    body_id = new_box(doc)
    lip = edges_at_z(doc, body_id, CUBE_MM)
    assert len(lip) == 4
    fillet(doc, lip, 3.0, body_id=body_id)

    operation = shell(doc, [top_face(doc, body_id)], THICKNESS_OK, body_id=body_id)

    assert operation.provenance["repaired"] is True
    assert query.volume(body_of(doc, body_id).shape) == pytest.approx(VOLUME_ROUNDED_LIP, rel=1e-3)
    assert query.describe_shape(body_of(doc, body_id).shape)["face_count"] == 20


def test_a_thickness_reaching_the_lip_fillet_is_rejected():
    """厚みがフィレット半径(3)に届くと OCCT は入力をそのまま返す(実測)。黙って通さない。"""
    doc = Document()
    body_id = new_box(doc)
    fillet(doc, edges_at_z(doc, body_id, CUBE_MM), 3.0, body_id=body_id)
    with pytest.raises(ValueError):
        shell(doc, [top_face(doc, body_id)], 3.0, body_id=body_id)
