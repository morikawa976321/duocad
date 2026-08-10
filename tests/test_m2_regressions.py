"""M2の回帰テスト — **契約のテストが通っていても壊れていた経路**(2026-08-11のQAで発見)。

`tests/test_sketch.py` / `test_region.py` / `test_extrude.py` がM2のAPI契約で、
このファイルは契約ではなく**再発防止**である。M2のロジック実装のQAで、97件が全部緑のまま
次の4つが壊れている(または黙って壊れる)ことが実測された。**契約側のテストは1行も変えていない**
（契約が誤っていたのではなく、契約が触れていない経路に穴があった）。

1. 面に貼ったスケッチは、`sketch_create` までしか通らず `regions()` で必ず例外になっていた
   （`gp_Ax3(gp_Ax3)` というコンストラクタが無い／`ReverseZ` はOCCTでは `ZReverse`）。
   実装計画書§3.3の「基準平面 **または既存ソリッドの平面状の面**」の後半が一度も動いていない。
2. 別スケッチの領域を混ぜて押し出すと、全部を最初のスケッチの法線で押して
   **体積0の退化ソリッド**を黙って作っていた（`IsDone()` も `BRepCheck_Analyzer` も通る）。
3. 壊れた数値（NaN・半径0・辺0）がスケッチとログに永続化され、NaNの場合は
   **同じスケッチ上の正常な領域まで黙って消えた**うえ `to_json()` が裸のNaNを吐いた。
4. 参照STEPを読んだ直後の文書（アクティブが `kind="reference"`）では、`join` が
   最初の板を作れず ValueError になっていた。受入対象そのものの経路。
"""
from __future__ import annotations

import json
import math

import pytest

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import io_step, kernel, query
from duocad.document import Body, Document
from duocad.oplog import OpLog
from duocad.region import region_ref, regions
from duocad.sketch import add_circle, add_rectangle, face_plane, sketch_create
from duocad.verbs import extrude

STEP_PATH = r"C:\Users\tm976\mf\3d-models\shelf_attachment_GD.step"

BOX_EDGE = 20.0
BOX_VOLUME = BOX_EDGE**3


def box_document() -> Document:
    """20mm立方体のソリッドを1つ持つ文書。"""
    document = Document()
    document.add_body(
        Body(
            id="box",
            name="box",
            kind="solid",
            shape=BRepPrimAPI_MakeBox(BOX_EDGE, BOX_EDGE, BOX_EDGE).Shape(),
        )
    )
    return document


def face_index_by_normal_z(shape, sign: float) -> int:
    """法線のZ成分が `sign` 向きの面の添字(上面 / 底面を選ぶ)。

    立方体の底面は `TopAbs_REVERSED` で入っており、平面の `gp_Ax3` をそのまま使うと
    法線が内側を向く。押し出しが外向きになるかを見るために両方を試す。
    """
    for face in kernel.tessellate(shape)["faces"]:
        if face["normal"][2] * sign > 0.999:
            return face["index"]
    raise AssertionError("該当する面が無い")


@pytest.mark.parametrize("sign", [1.0, -1.0], ids=["top_face", "bottom_face"])
def test_sketch_on_a_face_can_be_extruded_and_replayed(sign):
    """面に貼ったスケッチが、領域検出 → 押し出し → 再生まで通る。

    5×5の長方形を4mm押し出すので、体積の増分は 100 で**面の向きによらない**
    (外向きに押せていなければ、立方体の内側を押して増分が0になるか形状が壊れる)。
    """
    document = box_document()
    index = face_index_by_normal_z(document.active_body().shape, sign)

    sketch_id = sketch_create(document, face_plane("box", index))
    add_rectangle(document, sketch_id, (0.0, 0.0), 5.0, 5.0)
    found = regions(document, sketch_id)

    assert len(found) == 1
    assert math.isclose(found[0].area, 25.0, rel_tol=1e-9)

    extrude(document, [region_ref(sketch_id, found[0].index)], 4.0, mode="join")

    volume = query.volume(document.active_body().shape)
    assert math.isclose(volume, BOX_VOLUME + 100.0, rel_tol=1e-9)

    # 再生: 立方体はログの外で作った参照物なので、同じ土台を持つ文書へ再生する
    replayed = box_document()
    replayed.log = OpLog.from_json(document.log.to_json())
    replayed.log.replay(replayed)
    assert math.isclose(query.volume(replayed.active_body().shape), volume, rel_tol=1e-9)


def test_extrude_rejects_regions_from_two_different_sketches():
    """別平面のスケッチの領域を混ぜたら ValueError(体積0の退化ソリッドを黙って作らない)。

    実測(2026-08-11): XY平面とYZ平面の10×10を同時に押し出すと、例外なしで体積500
    (期待1000)の形状ができ、退化した面が2枚残っていた。
    """
    document = Document()
    first = sketch_create(document, "XY")
    add_rectangle(document, first, (0.0, 0.0), 10.0, 10.0)
    second = sketch_create(document, "YZ")
    add_rectangle(document, second, (0.0, 0.0), 10.0, 10.0)
    refs = [
        region_ref(first, regions(document, first)[0].index),
        region_ref(second, regions(document, second)[0].index),
    ]
    entries_before = len(document.log.entries)

    with pytest.raises(ValueError):
        extrude(document, refs, 5.0, mode="join")

    assert document.bodies == []
    assert len(document.log.entries) == entries_before


@pytest.mark.parametrize(
    "add",
    [
        lambda doc, sk: add_circle(doc, sk, (0.0, 0.0), float("nan")),
        lambda doc, sk: add_circle(doc, sk, (0.0, 0.0), 0.0),
        lambda doc, sk: add_circle(doc, sk, (0.0, 0.0), -5.0),
        lambda doc, sk: add_rectangle(doc, sk, (0.0, 0.0), 0.0, 5.0),
        lambda doc, sk: add_rectangle(doc, sk, (float("inf"), 0.0), 5.0, 5.0),
    ],
    ids=["nan_radius", "zero_radius", "negative_radius", "zero_width", "inf_corner"],
)
def test_broken_numbers_never_enter_the_sketch(add):
    """使えない数値はその場で ValueError。スケッチにもログにも残さない(§6予防6)。

    残すと後戻りできない: M2に図形の削除verbは無いので、壊れた図形が入ったスケッチは
    undo以外に直す手段が無い。NaNの場合はさらに悪く、**同じスケッチ上の正常な領域まで
    黙って消え**(バウンディングボックスがNaNになる)、`to_json()` が裸のNaNを吐いて
    JSONとして壊れる(2026-08-11に実測)。
    """
    document = Document()
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (0.0, 0.0), 40.0, 30.0)
    entries_before = len(document.log.entries)

    with pytest.raises(ValueError):
        add(document, sketch_id)

    assert len(document.log.entries) == entries_before
    assert len(regions(document, sketch_id)) == 1, "正常な領域が巻き添えで消えている"
    json.loads(document.log.to_json())  # 裸のNaNが混ざっていたらここで落ちる


def test_join_makes_the_first_solid_next_to_a_reference_body():
    """参照STEPを読んだ直後(アクティブが read-only)でも、`join` が最初の板を作れる。

    `import_step` が作るボディは `kind="reference"` で、`add_body` の契約により
    **常にアクティブになる**。ここで「アクティブが編集不可だから ValueError」にすると、
    受入対象の経路(棚取付具を参照として読み、その隣に板を作る)が塞がる。
    明示的に参照ボディを `body_id` で指定した場合は今までどおり ValueError
    (`test_extrude.py::test_extrude_rejects_a_reference_body`)。
    """
    document = Document()
    reference_id = io_step.import_step(document, STEP_PATH)
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (0.0, 0.0), 40.0, 30.0)

    operation = extrude(
        document, [region_ref(sketch_id, regions(document, sketch_id)[0].index)], 10.0
    )

    created = next(body for body in document.bodies if body.id == operation.args["body_id"])
    assert created.kind == "solid"
    assert created.id != reference_id
    assert len(document.bodies) == 2
    assert math.isclose(query.volume(created.shape), 12000.0, rel_tol=1e-9)
