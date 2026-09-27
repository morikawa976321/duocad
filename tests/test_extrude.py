"""M2契約: 領域の押し出し(`duocad.verbs.extrude`)。

同じ verb の面を受ける側(面群)は `test_extrude_faces.py`。こちらは**スケッチの領域**を
`join`(足す)/`cut`(引く)する。

検算はすべて**実測または算術で決まる値**:

    片側だけの押し出し   : 体積 = その領域の面積 × 距離
    40×30の板を10mm     : 体積 12000
    半径3の穴を貫通      : 体積 12000 − π·3²·10 = 11717.2566611769…
    そのときの面の構成    : 平面6 + 全周円筒1 = 7面(OCPで直接実測)
"""
from __future__ import annotations

import json
import math

import pytest

# 参照ボディ(読み取り専用)を作るためだけにOCPを使う(境界検査は `duocad/` 配下だけが対象)
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import kernel, query
from duocad.document import Body, Document
from document_support import replay_through_json
from duocad.region import region_ref, regions
from duocad.sketch import add_circle, add_line, add_rectangle, sketch_create
from duocad.verbs import extrude, extrude_faces

#: 板＋穴の構成を最小で作るための寸法
PLATE_WIDTH = 40.0
PLATE_HEIGHT = 30.0
PLATE_THICKNESS = 10.0
PLATE_VOLUME = PLATE_WIDTH * PLATE_HEIGHT * PLATE_THICKNESS

HOLE_RADIUS = 3.0
HOLE_CENTER = (20.0, 15.0)
#: 板(z=0..10)より深く取り、止まり穴にならないようにする。
#: 貫通したことは体積では決まらない(止まり穴でも除去は板厚ぶん)ので、
#: 面の構成(平面6 + 全周円筒1 = 7面)で確かめる —
#: test_a_plate_with_a_hole_has_only_planes_and_a_full_cylinder
HOLE_DEPTH = 20.0
HOLE_VOLUME = math.pi * HOLE_RADIUS**2 * PLATE_THICKNESS

#: 体積比較の相対許容差(M1の面押し出しテストと同じ理由・同じ桁)
VOLUME_REL_TOL = 1.0e-9
#: 実測値(小数3桁)と突き合わせる面積の許容差(tests/test_region.py と同じ)
AREA_REL_TOL = 1.0e-6
#: y=5 の線分で半径20の円を割ったときの大きい方の面積(実測)
LARGER_HALF_AREA = 826.215


def split_circle_sketch(document: Document) -> str:
    sketch_id = sketch_create(document, "XY")
    add_circle(document, sketch_id, (0.0, 0.0), 20.0)
    add_line(document, sketch_id, (-30.0, 5.0), (30.0, 5.0))
    return sketch_id


def plate_document() -> Document:
    document = Document()
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (0.0, 0.0), PLATE_WIDTH, PLATE_HEIGHT)
    plate = regions(document, sketch_id)
    assert len(plate) == 1, "長方形1つのスケッチの領域は1個"
    extrude(document, [region_ref(sketch_id, plate[0].index)], PLATE_THICKNESS, mode="join")
    return document


def drill(document: Document) -> None:
    sketch_id = sketch_create(document, "XY")
    add_circle(document, sketch_id, HOLE_CENTER, HOLE_RADIUS)
    hole = regions(document, sketch_id)
    assert len(hole) == 1
    extrude(document, [region_ref(sketch_id, hole[0].index)], HOLE_DEPTH, mode="cut")


# --------------------------------------------------------------------------------------
# 押し出し — 面積 × 距離で検算する
# --------------------------------------------------------------------------------------
def test_extruding_one_half_of_a_split_circle_gives_area_times_distance():
    """**分割した領域の片方だけ**を10mm押し出すと、体積 = その領域の面積 × 10 になる。

    領域の片方だけを対象にできていなければ、体積は円全体の押し出し(=1256.637×10)に
    なって落ちる。
    """
    document = Document()
    sketch_id = split_circle_sketch(document)
    target = max(regions(document, sketch_id), key=lambda region: region.area)
    assert math.isclose(target.area, LARGER_HALF_AREA, rel_tol=AREA_REL_TOL)

    extrude(document, [region_ref(sketch_id, target.index)], 10.0, mode="join")

    body = document.active_body()
    assert body is not None, "join は対象が無ければ新しいソリッドを作る"
    assert document.is_editable(body.id) is True, "作ったボディは kind='solid'"
    assert math.isclose(query.volume(body.shape), target.area * 10.0, rel_tol=AREA_REL_TOL)


def test_a_tilted_rectangle_gives_a_region_and_extrudes():
    """**実測**: 傾いた長方形からも領域が1つ出て、面積×距離ぶんの体積に押し出せる。

    落ちたら、`kernel_sketch.rectangle_corners` が張る辺か `_bbox_rectangle`(領域を割る補助面の
    大きさ)のどちらかが `angle` を見落としている。
    """
    document = Document()
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (5.0, -7.0), PLATE_WIDTH, PLATE_HEIGHT, angle=31.0)

    found = regions(document, sketch_id)
    assert len(found) == 1, "傾いていても長方形1つのスケッチの領域は1個"
    assert math.isclose(found[0].area, PLATE_WIDTH * PLATE_HEIGHT, rel_tol=AREA_REL_TOL)

    extrude(document, [region_ref(sketch_id, found[0].index)], PLATE_THICKNESS, mode="join")

    body = document.active_body()
    assert body is not None
    assert math.isclose(query.volume(body.shape), PLATE_VOLUME, rel_tol=VOLUME_REL_TOL)
    assert BRepCheck_Analyzer(body.shape).IsValid() is True


def test_join_fuses_into_the_existing_body():
    """対象ボディがあるときの `join` は、そこへ肉を足す(2つ目のソリッドを作らない)。"""
    document = plate_document()
    body = document.active_body()
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (10.0, 10.0), 10.0, 10.0)
    boss = regions(document, sketch_id)

    extrude(document, [region_ref(sketch_id, boss[0].index)], 15.0, mode="join")

    assert len(document.bodies) == 1, "join で2つ目のボディが増えている"
    assert document.active_body().id == body.id
    assert math.isclose(
        query.volume(document.active_body().shape),
        PLATE_VOLUME + 10.0 * 10.0 * 5.0,
        rel_tol=VOLUME_REL_TOL,
    )


# --------------------------------------------------------------------------------------
# `new` — 「対象があっても融合しない」種別
# --------------------------------------------------------------------------------------
def test_new_makes_a_second_body_instead_of_fusing():
    """`mode="new"` は対象ボディがあっても融合せず、**独立したソリッドを1つ足す**。

    差が出なければ、既存ボディがいると2つ目の板が作れない状態に `new` が `join` へ
    落ちている。
    """
    document = plate_document()
    plate = document.active_body()
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (10.0, 10.0), 10.0, 10.0)
    boss = regions(document, sketch_id)

    extrude(document, [region_ref(sketch_id, boss[0].index)], 15.0, mode="new")

    assert len(document.bodies) == 2, "new が既存ボディへ融合している"
    assert math.isclose(query.volume(plate.shape), PLATE_VOLUME, rel_tol=VOLUME_REL_TOL), (
        "new が元の板の形を変えている"
    )
    created = document.active_body()
    assert created is not None and created.id != plate.id, "作ったボディがactiveになっていない"
    assert document.is_editable(created.id) is True, "作ったボディは kind='solid'"
    assert math.isclose(
        query.volume(created.shape), 10.0 * 10.0 * 15.0, rel_tol=VOLUME_REL_TOL
    )


def test_new_refuses_a_body_id():
    """`new` に行き先を渡したら ValueError。文書にもログにも何も残さない。"""
    document = plate_document()
    plate = document.active_body()
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (10.0, 10.0), 10.0, 10.0)
    boss = regions(document, sketch_id)
    entries_before = len(document.log.entries)

    with pytest.raises(ValueError):
        extrude(
            document,
            [region_ref(sketch_id, boss[0].index)],
            15.0,
            mode="new",
            body_id=plate.id,
        )

    assert len(document.bodies) == 1
    assert len(document.log.entries) == entries_before
    assert math.isclose(query.volume(plate.shape), PLATE_VOLUME, rel_tol=VOLUME_REL_TOL)


def test_new_is_undoable_and_replays():
    """`new` で作ったボディは undo で消え、保存したログの再生でも同じ2ボディに戻る。

    再生の分岐が `join`(「無ければ作る」)と別なので、ここを通さないと**開くと
    ボディが1つに融合している**という形で表面化する。
    """
    document = plate_document()
    plate_id = document.active_body().id
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (10.0, 10.0), 10.0, 10.0)
    boss = regions(document, sketch_id)
    operation = extrude(
        document, [region_ref(sketch_id, boss[0].index)], 15.0, mode="new"
    )
    created_id = operation.args["body_id"]
    assert created_id != plate_id

    document.log.undo(document)
    assert [body.id for body in document.bodies] == [plate_id], "undoで新規ボディが残っている"

    document.log.redo(document)
    assert [body.id for body in document.bodies] == [plate_id, created_id]

    replayed = replay_through_json(document)

    assert [body.id for body in replayed.bodies] == [plate_id, created_id]
    assert math.isclose(
        query.volume(replayed.get_body(created_id).shape),
        10.0 * 10.0 * 15.0,
        rel_tol=VOLUME_REL_TOL,
    )


def test_extrude_is_logged_and_undoable():
    """押し出しが操作ログに1行で載り、undoで戻る。

    `args["regions"]` は `RegionRef` をそのまま入れると `to_json()` で落ちる ——
    JSON化できる素のdictであること。作ったボディのIDを `args["body_id"]` に記録することも
    固定する — 記録が無いと再生で別のIDになり、保存した選択集合が指す先を失う。
    """
    document = Document()
    sketch_id = split_circle_sketch(document)
    target = max(regions(document, sketch_id), key=lambda region: region.area)
    reference = region_ref(sketch_id, target.index)

    operation = extrude(document, [reference], 10.0, mode="join")

    body = document.active_body()
    assert operation.verb == "extrude"
    assert operation.args == {
        "regions": [reference.to_dict()],
        "distance": 10.0,
        "mode": "join",
        "body_id": body.id,
    }
    json.loads(document.log.to_json())

    undone = document.log.undo(document)

    assert undone is not None and undone.verb == "extrude"
    assert document.bodies == [], "join が作ったボディが undo で消えていない"

    document.log.redo(document)
    assert math.isclose(
        query.volume(document.active_body().shape), target.area * 10.0, rel_tol=AREA_REL_TOL
    )


def test_undo_restores_the_shape_for_fuse_and_cut():
    """既存ボディへの `join`(fuse)と `cut` も undo で元の形状に戻る(「作ったものを
    消す」経路とは別に「形状を戻す」経路があり、こちらはその検証)。
    """
    document = plate_document()
    body_id = document.active_body().id

    drill(document)
    assert query.describe_shape(document.active_body().shape)["faces_by_type"]["cylinder_full"] == 1

    document.log.undo(document)  # cut を取り消す

    assert len(document.bodies) == 1 and document.active_body().id == body_id
    assert math.isclose(
        query.volume(document.active_body().shape), PLATE_VOLUME, rel_tol=VOLUME_REL_TOL
    )
    assert query.describe_shape(document.active_body().shape)["faces_by_type"]["cylinder_full"] == 0

    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (10.0, 10.0), 10.0, 10.0)
    boss = regions(document, sketch_id)
    extrude(document, [region_ref(sketch_id, boss[0].index)], 15.0, mode="join")

    document.log.undo(document)  # fuse を取り消す

    assert len(document.bodies) == 1
    assert math.isclose(
        query.volume(document.active_body().shape), PLATE_VOLUME, rel_tol=VOLUME_REL_TOL
    )


# --------------------------------------------------------------------------------------
# 失敗を黙って通さない(§6予防6・§7)
# --------------------------------------------------------------------------------------
def test_extrude_without_an_existing_region_fails_and_changes_nothing():
    """押し出し対象の領域が存在しない場合は失敗し、文書が変更されない。"""
    document = plate_document()
    sketch_id = sketch_create(document, "XY")
    add_circle(document, sketch_id, HOLE_CENTER, HOLE_RADIUS)
    before_volume = query.volume(document.active_body().shape)
    entries_before = len(document.log.entries)
    bodies_before = len(document.bodies)

    # 添字は1始まり(region.py の契約)なので、0 と負数も範囲外である
    for target in ([], [region_ref(sketch_id, 99)], [region_ref(sketch_id, 0)],
                   [region_ref(sketch_id, -1)]):
        with pytest.raises(ValueError):
            extrude(document, target, 5.0, mode="cut")

    assert query.volume(document.active_body().shape) == before_volume
    assert len(document.log.entries) == entries_before
    assert len(document.bodies) == bodies_before


@pytest.mark.parametrize(
    ("distance", "mode"),
    [(0.0, "join"), (5.0, "sculpt")],
)
def test_extrude_rejects_a_zero_distance_and_an_unknown_mode(distance, mode):
    """距離0(形状が変わらない)と未知のmodeは ValueError。ログにも文書にも残さない。"""
    document = Document()
    sketch_id = split_circle_sketch(document)
    target = max(regions(document, sketch_id), key=lambda region: region.area)
    entries_before = len(document.log.entries)

    with pytest.raises(ValueError):
        extrude(document, [region_ref(sketch_id, target.index)], distance, mode=mode)

    assert document.bodies == []
    assert len(document.log.entries) == entries_before


def test_cut_without_a_target_body_fails():
    """引く相手が無い `cut` は ValueError(何も無いところから削れたことにしない)。"""
    document = Document()
    sketch_id = split_circle_sketch(document)
    target = max(regions(document, sketch_id), key=lambda region: region.area)

    with pytest.raises(ValueError):
        extrude(document, [region_ref(sketch_id, target.index)], 10.0, mode="cut")

    assert document.bodies == []


# --------------------------------------------------------------------------------------
# 受入テストへの前進 — 棚取付具の「板と穴」
# --------------------------------------------------------------------------------------
def test_a_plate_with_a_hole_has_only_planes_and_a_full_cylinder():
    """長方形を押し出して板にし、円を `cut` で押し出して穴を開けられる。

    受入対象の `shelf_attachment_GD.step` は56面 = 平面34 + 全周円筒22(穴)、トーラス0・
    円錐0・spline0(実測)。ここではその最小形(平面6 + 全周円筒1 = 7面)を作れることを
    確かめる。面の構成と体積はOCPで直接実測した値(box40×30×10からr=3の円柱を引き、
    `query.describe_shape` で数えた)。体積が穴の分だけ減ることも見る(面の構成だけだと、
    半径違いの穴でも7面のまま通ってしまう)。
    """
    document = plate_document()
    assert math.isclose(
        query.volume(document.active_body().shape), PLATE_VOLUME, rel_tol=VOLUME_REL_TOL
    )

    drill(document)

    shape = document.active_body().shape
    assert math.isclose(
        query.volume(shape), PLATE_VOLUME - HOLE_VOLUME, rel_tol=VOLUME_REL_TOL
    )
    info = query.describe_shape(shape)
    assert info["faces_by_type"] == {
        "plane": 6,
        "cylinder_full": 1,
        "cylinder_partial": 0,
        "torus": 0,
        "cone": 0,
        "bspline": 0,
        "other": 0,
    }
    assert info["face_count"] == 7
    assert math.isclose(
        query.volume(shape), PLATE_VOLUME - HOLE_VOLUME, rel_tol=VOLUME_REL_TOL
    )


def test_replay_reproduces_the_split_circle_extrusion():
    """円＋線分のスケッチと片側だけの押し出しが、保存したログから再生できる。

    下の板と穴の再生は長方形と円しか通らないので、`add_line` の再生はこちらでしか
    検査されない。
    """
    document = Document()
    sketch_id = split_circle_sketch(document)
    target = max(regions(document, sketch_id), key=lambda region: region.area)
    extrude(document, [region_ref(sketch_id, target.index)], 10.0, mode="join")

    replayed = replay_through_json(document)

    body = replayed.active_body()
    assert body is not None
    assert math.isclose(query.volume(body.shape), target.area * 10.0, rel_tol=AREA_REL_TOL)
    assert len(replayed.sketches) == 1
    assert regions(replayed, sketch_id) == regions(document, sketch_id)


def test_replay_reproduces_the_plate_with_the_hole():
    """保存したログを再生すると同じ板と穴になる(形状はログからの導出物)。"""
    document = plate_document()
    drill(document)
    expected_volume = query.volume(document.active_body().shape)
    expected_faces = query.describe_shape(document.active_body().shape)

    replayed = replay_through_json(document)

    body = replayed.active_body()
    assert body is not None
    assert math.isclose(query.volume(body.shape), expected_volume, rel_tol=VOLUME_REL_TOL)
    assert query.describe_shape(body.shape) == expected_faces
    assert len(replayed.sketches) == 2


# ---------------------------------------------------------------------------------
# 面の統合(面を受ける `extrude`)。実STEPを使わない唯一の押し出し検査。
# ---------------------------------------------------------------------------------

def test_extrude_faces_adopt_face_merging_when_it_helps():
    """統合が効く場合は採用され、面が増えっぱなしにならない(20mm立方体の上面を+10)。

    実測: 統合なし10面 / 統合後6面。**統合を採用する経路**(`BRepTools_History` を通した
    由来の射影)はこの形でしか通らない — 受入対象の部品では統合の利得が無く採用されないため、
    そちらのテストだけでは履歴経由の射影が一度も実行されない。
    """

    document = Document()
    document.add_body(
        Body(id="box", name="box", kind="solid", shape=BRepPrimAPI_MakeBox(20.0, 20.0, 20.0).Shape())
    )
    body = document.active_body()
    assert body is not None
    top = next(
        face
        for face in kernel.tessellate(body.shape)["faces"]
        if face["normal"][2] > 0.999 and face["centroid"][2] > 19.9
    )

    operation = extrude_faces(document, [{"body": "box", "index": top["index"]}], 10.0)

    assert math.isclose(query.volume(body.shape), 12000.0, rel_tol=1e-9)
    assert kernel.face_count(body.shape) == 6, "同一平面の面が統合されていない"
    assert operation.provenance["unified"] is True
    assert isinstance(operation.provenance["after_face_indices"][0], int)
    assert BRepCheck_Analyzer(body.shape).IsValid()
    # 履歴経由でも旧添字が新添字へ張れている(押した面以外の5面が「消えた」になっていない)
    assert sum(1 for new in operation.provenance["face_map"].values() if new) == 5
