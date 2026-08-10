"""M1契約: `extrude_face` / `make_editable_copy` / `kernel.tessellate`。

このファイルは**M1のAPI契約**(M0の契約は tests/README.md と他のテストが持つ。ここは追加分で、
既存のテストには一切触っていない)。実物の `shelf_attachment_GD.step`(読み取りのみ)を使い、
自己申告ではなく**体積の実測**で通す(実装計画書§8「各マイルストーンに検証手段を付ける。
自己申告ではなく実測で通す」)。

平らな面を距離dで押し出したときの体積の増分は **面積 × d** になる。これが成り立つかを
見るのが本体で、面の添字・undo・再生・失敗の扱いはその周りを固める。
"""
from __future__ import annotations

import json
import math

import pytest

# 検算のためだけにOCPを使う(境界検査は `duocad/` 配下だけを対象にしており、テスト側は対象外。
# 形状が妥当かどうかは duocad の外から独立に見たいので、ここは意図的に直接使う)。
from OCP.BRepCheck import BRepCheck_Analyzer

from duocad import io_step, kernel, query
from duocad.document import Body, Document
from duocad.oplog import OpLog
from duocad.verbs import extrude_face, make_editable_copy

STEP_PATH = r"C:\Users\tm976\mf\3d-models\shelf_attachment_GD.step"

#: 体積比較の相対許容差。M0が実測した「STEP往復で1ULP級の丸めが出る」のと同じ理由で `==` は使わない
#: (tests/README.md「STEP往復の体積は bit-exact ではない」)。ブーリアンの積分順序でも同程度の差が出る。
VOLUME_REL_TOL = 1.0e-9


def _reference_document() -> Document:
    """参照STEPを読み(分解せず単一形状のまま)、固定ID "reference" の参照Bodyを持つ文書を作る。

    M3.5でimport_stepはソリッドごとに分解するが(shelf_attachment_GD.stepは17ソリッドに
    分かれ、1ソリッドあたり最大4面)、このファイルはextrude_face verb自体の振る舞い
    (56面の複合形状に対する複数平面の連続押し出し・曲面の拒否・面の統合)を検証するもの
    なので、分解を経由せず kernel.read_step を直接使って56面のままの参照Bodyを1つ作る。

    **この参照Bodyの作成はログの外で行う**(import_stepを経由しないため)。したがって
    再生系のテストは、素の `Document()` へ `OpLog.replay` するのではなく、この関数で
    同じ土台を作ってからログを再生すること(`tests/test_m2_regressions.py::box_document`
    と同じ流儀)。
    """
    document = Document()
    shape = kernel.read_step(STEP_PATH)
    document.add_body(Body(id="reference", name="shelf", kind="reference", shape=shape))
    return document


@pytest.fixture()
def session():
    """参照STEPを読み(分解せず単一形状のまま)、編集用のソリッドをアクティブにした文書。"""
    document = _reference_document()
    reference_id = "reference"
    solid_id = make_editable_copy(document, reference_id)
    return document, reference_id, solid_id


def largest_plane(shape) -> dict:
    """面積が最大の平面(押し出しの検算がしやすい面)。"""
    tessellation = kernel.tessellate(shape)
    planes = [face for face in tessellation["faces"] if face["kind"] == "plane"]
    assert planes, "平面が1つも無い(検算に使えない)"
    return max(planes, key=lambda face: face["area"])


# --------------------------------------------------------------------------------------
# make_editable_copy
# --------------------------------------------------------------------------------------
def test_make_editable_copy_creates_an_editable_solid(session):
    """参照ボディ(読み取り専用)から編集できるソリッドを作り、それがアクティブになる。"""
    document, reference_id, solid_id = session
    assert len(document.bodies) == 2
    assert document.is_editable(reference_id) is False
    assert document.is_editable(solid_id) is True
    active = document.active_body()
    assert active is not None and active.id == solid_id
    assert query.describe_shape(active.shape)["face_count"] == 56


def test_make_editable_copy_is_logged(session):
    """操作ログに載る(文書の組み立てもログから再生できる。§3.1)。

    このファイルの参照Body自体はログの外で作る(`_reference_document` 参照。import_step経由だと
    56面のままの単一Bodyを作れないため)ので、ログに載るのは `make_editable_copy` だけになる。
    """
    document, reference_id, solid_id = session
    verbs = [operation.verb for operation in document.log.entries]
    assert verbs == ["make_editable_copy"]
    args = document.log.entries[-1].args
    assert args["source_body_id"] == reference_id
    assert args["body_id"] == solid_id


# --------------------------------------------------------------------------------------
# extrude_face — 体積で検算する
# --------------------------------------------------------------------------------------
def test_extrude_face_adds_area_times_distance(session):
    """正の距離は面の外向き法線方向へ肉を足す(増分 = 面積 × 距離)。"""
    document, _, _ = session
    body = document.active_body()
    target = largest_plane(body.shape)
    before = query.volume(body.shape)

    extrude_face(document, target["index"], 2.0)

    after = query.volume(body.shape)
    assert math.isclose(
        after - before, target["area"] * 2.0, rel_tol=1.0e-6
    ), f"増分 {after - before} が 面積×2 = {target['area'] * 2.0} と合わない"


def test_extrude_face_negative_distance_removes_material(session):
    """負の距離は同じ向きへ削る(減少 = 面積 × |距離|)。"""
    document, _, _ = session
    body = document.active_body()
    target = largest_plane(body.shape)
    before = query.volume(body.shape)

    extrude_face(document, target["index"], -1.5)

    after = query.volume(body.shape)
    assert after < before
    assert math.isclose(before - after, target["area"] * 1.5, rel_tol=1.0e-6)


def test_extrude_face_does_not_touch_the_reference_body(session):
    """編集用コピーを押し出しても、読み込んだ参照ボディの形状は変わらない。

    `make_editable_copy` は形状を共有する(深いコピーをしない)ので、verbが形状を
    **その場で書き換えていない**ことがこの検査で担保される。
    """
    document, reference_id, _ = session
    reference = next(body for body in document.bodies if body.id == reference_id)
    reference_volume_before = query.volume(reference.shape)

    body = document.active_body()
    extrude_face(document, largest_plane(body.shape)["index"], 2.0)

    assert query.volume(reference.shape) == reference_volume_before
    assert query.describe_shape(reference.shape)["face_count"] == 56


# --------------------------------------------------------------------------------------
# ログ・由来・undo・再生
# --------------------------------------------------------------------------------------
def test_extrude_face_logs_args_and_json_serialisable_provenance(session):
    """ログに verb/args が載り、`provenance` が **JSONにできる**射影であること。

    M0のQAが申し送った穴(`kernel.normalize_provenance` の戻りは TopoDS_Shape を含み
    `to_json()` で落ちる)を、M1で実際に由来を積むこのverbが埋めているかを固定する。
    """
    document, _, solid_id = session
    body = document.active_body()
    target = largest_plane(body.shape)

    operation = extrude_face(document, target["index"], 2.0)

    assert operation.verb == "extrude_face"
    assert operation.args == {
        "body_id": solid_id,
        "face_index": target["index"],
        "distance": 2.0,
    }
    assert operation.selection == [
        {"body": solid_id, "kind": "face", "index": target["index"]}
    ]
    provenance = operation.provenance
    assert provenance["verb"] == "extrude_face"
    assert target["index"] in provenance["deleted"] or provenance["face_map"][str(target["index"])]
    assert isinstance(provenance["new_faces"], list)
    json.dumps(provenance)  # ここで例外が出たら永続化できていない
    restored = OpLog.from_json(document.log.to_json())
    assert restored.entries[-1].provenance == provenance


def test_extrude_face_undo_restores_the_shape(session):
    """undoで押し出す前の形状(体積・面数)に戻る。"""
    document, _, _ = session
    body = document.active_body()
    before_volume = query.volume(body.shape)
    before_faces = kernel.face_count(body.shape)

    extrude_face(document, largest_plane(body.shape)["index"], 2.0)
    assert kernel.face_count(body.shape) != before_faces

    undone = document.log.undo(document)

    assert undone is not None and undone.verb == "extrude_face"
    assert query.volume(body.shape) == before_volume
    assert kernel.face_count(body.shape) == before_faces
    assert document.log.can_redo is True


def test_extrude_face_redo_reapplies_the_same_extrusion(session):
    """redoで同じ押し出しが戻る(操作を二重に記録しない)。"""
    document, _, _ = session
    body = document.active_body()
    extrude_face(document, largest_plane(body.shape)["index"], 2.0)
    extruded_volume = query.volume(body.shape)
    entries_before = len(document.log.entries)

    document.log.undo(document)
    document.log.redo(document)

    assert math.isclose(query.volume(body.shape), extruded_volume, rel_tol=VOLUME_REL_TOL)
    assert len(document.log.entries) == entries_before


def test_replay_reproduces_the_extruded_shape(session):
    """保存したログを再生すると同じ形状になる(形状はログからの導出物。§3.1)。"""
    document, _, _ = session
    body = document.active_body()
    target = largest_plane(body.shape)
    extrude_face(document, target["index"], 2.0)
    expected_volume = query.volume(body.shape)
    expected_faces = kernel.face_count(body.shape)

    # 参照Bodyはログの外で作っているので、素の Document() ではなく同じ土台
    # (_reference_document()) を作ってからログを再生する(_reference_document のdocstring参照)。
    replayed = _reference_document()
    replayed.log = OpLog.from_json(document.log.to_json())
    replayed.log.replay(replayed)

    active = replayed.active_body()
    assert active is not None
    assert math.isclose(query.volume(active.shape), expected_volume, rel_tol=VOLUME_REL_TOL)
    assert kernel.face_count(active.shape) == expected_faces
    assert len(replayed.bodies) == 2


def test_after_face_index_allows_chained_extrusion(session):
    """押し出した面の後継が分かるので、同じ面を続けて押し出せる。

    これが無いと計測の手続き(「同じ面を10回押し出す」)が毎回の再ピックを含んでしまう。
    由来マップ(`Modified`)はブーリアンでは空を返すため、幾何で突き合わせている。
    """
    document, _, _ = session
    body = document.active_body()
    index = largest_plane(body.shape)["index"]
    volumes = [query.volume(body.shape)]

    for _ in range(3):
        operation = extrude_face(document, index, 1.0)
        index = operation.provenance["after_face_index"]
        assert isinstance(index, int), "押し出した面の後継が見つからない"
        volumes.append(query.volume(body.shape))

    assert volumes == sorted(volumes), f"体積が単調に増えていない: {volumes}"

    # 連なった操作もログから再生できる(1回だけの再生では連鎖の取りこぼしが見えない)。
    # 参照Bodyはログの外で作っているので、同じ土台(_reference_document())から再生する。
    replayed = _reference_document()
    replayed.log = OpLog.from_json(document.log.to_json())
    replayed.log.replay(replayed)
    replayed_body = replayed.active_body()
    assert replayed_body is not None
    assert math.isclose(query.volume(replayed_body.shape), volumes[-1], rel_tol=VOLUME_REL_TOL)


def test_extrude_face_result_is_a_valid_solid(session):
    """押し出した結果が `BRepCheck_Analyzer` で妥当であること。

    実測(2026-08-10): この部品の面36・43を+2mmすると、ブーリアンの結果は妥当なのに
    `ShapeUpgrade_UnifySameDomain` を通した形状だけ `IsValid()` が偽になる
    (面数・体積は同じなので統合の利得も無い)。統合を無条件に採ると、正常な押し出しが
    **壊れた形状として文書に残る**。`kernel._unify_same_domain` の妥当性検査がその防波堤で、
    このテストがそれを固定する。
    """
    document, _, _ = session
    body = document.active_body()
    assert BRepCheck_Analyzer(body.shape).IsValid(), "入力の形状がすでに妥当でない"

    extrude_face(document, largest_plane(body.shape)["index"], 2.0)

    assert BRepCheck_Analyzer(body.shape).IsValid(), "押し出した形状が妥当でない"


def test_extrude_face_adopts_face_merging_when_it_helps():
    """統合が効く場合は採用され、面が増えっぱなしにならない(20mm立方体の上面を+10)。

    実測: 統合なし10面 / 統合後6面。**統合を採用する経路**(`BRepTools_History` を通した
    由来の射影)はこの形でしか通らない — 受入対象の部品では統合の利得が無く採用されないため、
    そちらのテストだけでは履歴経由の射影が一度も実行されない。
    """
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

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

    operation = extrude_face(document, top["index"], 10.0)

    assert math.isclose(query.volume(body.shape), 12000.0, rel_tol=1e-9)
    assert kernel.face_count(body.shape) == 6, "同一平面の面が統合されていない"
    assert operation.provenance["unified"] is True
    assert isinstance(operation.provenance["after_face_index"], int)
    assert BRepCheck_Analyzer(body.shape).IsValid()
    # 履歴経由でも旧添字が新添字へ張れている(全部が「消えた」になっていない)
    assert any(operation.provenance["face_map"].values())


def test_extrude_face_rejects_curved_faces(session):
    """曲面(穴の円筒面)は押し出せない。通すと**成功したまま壊れた形状**が残る(実測)。

    実測(2026-08-10): この部品の穴の円筒面(面1)を+1mmすると `IsDone()` は真・体積も変わるが
    `BRepCheck_Analyzer.IsValid()` が偽になる。-1mmでは体積が 91018→38597 と激減する
    「有効だが無意味な」形状になる。曲面の押し出し(オフセット)はM1の範囲外。
    """
    document, _, _ = session
    body = document.active_body()
    curved = [
        face for face in kernel.tessellate(body.shape)["faces"] if face["kind"] != "plane"
    ]
    assert curved, "曲面が無い(この部品は穴の円筒面を22持つ)"
    before_volume = query.volume(body.shape)
    entries_before = len(document.log.entries)

    with pytest.raises(ValueError, match="平面の面だけ"):
        extrude_face(document, curved[0]["index"], 1.0)

    assert query.volume(body.shape) == before_volume
    assert len(document.log.entries) == entries_before


# --------------------------------------------------------------------------------------
# 失敗を黙って通さない(§6予防6・§7)
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("face_index", "distance"),
    [(9999, 1.0), (0, 1.0), (-1, 1.0)],
)
def test_extrude_face_rejects_bad_face_index(session, face_index, distance):
    """範囲外の面の添字は ValueError。ログにも文書にも変更を残さない。"""
    document, _, _ = session
    body = document.active_body()
    before_volume = query.volume(body.shape)
    entries_before = len(document.log.entries)

    with pytest.raises(ValueError):
        extrude_face(document, face_index, distance)

    assert query.volume(body.shape) == before_volume
    assert len(document.log.entries) == entries_before


def test_extrude_face_rejects_zero_distance(session):
    """距離0は形状を変えないので ValueError(黙って空振りさせない)。"""
    document, _, _ = session
    body = document.active_body()
    entries_before = len(document.log.entries)

    with pytest.raises(ValueError):
        extrude_face(document, largest_plane(body.shape)["index"], 0.0)

    assert len(document.log.entries) == entries_before


def test_extrude_face_rejects_reference_body(session):
    """参照ボディ(kind="reference")は押し出せない(M0の read-only 契約)。"""
    document, reference_id, _ = session
    body = document.active_body()
    target = largest_plane(body.shape)
    entries_before = len(document.log.entries)

    with pytest.raises(ValueError, match="not editable"):
        extrude_face(document, target["index"], 2.0, body_id=reference_id)

    assert len(document.log.entries) == entries_before


# --------------------------------------------------------------------------------------
# tessellate — 画面に出る形の元
# --------------------------------------------------------------------------------------
def test_tessellate_returns_triangles_for_every_face(session):
    """56面すべてに三角形が付き、添字は面の添字と一致する。"""
    document, _, _ = session
    body = document.active_body()

    tessellation = kernel.tessellate(body.shape)

    assert tessellation["face_count"] == 56
    assert tessellation["untriangulated"] == []
    assert [face["index"] for face in tessellation["faces"]] == list(range(1, 57))
    for face in tessellation["faces"]:
        assert len(face["positions"]) % 3 == 0
        assert len(face["indices"]) % 3 == 0
        assert face["indices"], f"面 {face['index']} に三角形が無い"
        assert max(face["indices"]) < len(face["positions"]) // 3
        assert len(face["normal"]) == 3
        assert math.isclose(sum(value * value for value in face["normal"]), 1.0, rel_tol=1e-9)
        assert face["area"] > 0


def test_tessellate_is_json_serialisable_and_bounded(session):
    """JSONにできる素の値だけで、境界箱が実物の寸法と合っている。"""
    document, _, _ = session
    body = document.active_body()

    tessellation = kernel.tessellate(body.shape)

    json.dumps(tessellation)  # OCPのオブジェクトが混ざっていたらここで落ちる
    bbox = tessellation["bbox"]
    assert bbox["diagonal"] > 0
    for axis in range(3):
        assert bbox["min"][axis] < bbox["max"][axis]
        assert math.isclose(
            bbox["center"][axis], (bbox["min"][axis] + bbox["max"][axis]) / 2.0, rel_tol=1e-12
        )


def test_tessellate_finer_deviation_yields_more_triangles(session):
    """偏差を小さくすると三角形が増える(絶対偏差として効いていることの確認)。

    M0で `isRelative=True` だと偏差を変えてもメッシュが変わらない落とし穴を実測しているので、
    同じ間違いをテセレーション側でしていないことを固定する。
    """
    document, _, _ = session
    body = document.active_body()

    def triangles(deviation: float) -> int:
        tessellation = kernel.tessellate(body.shape, deviation)
        return sum(len(face["indices"]) // 3 for face in tessellation["faces"])

    assert triangles(0.05) > triangles(0.4)