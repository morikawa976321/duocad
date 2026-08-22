"""スケッチの契約(`sketch_create` / `add_line` / `add_circle` / `add_rectangle`)。

領域は `tests/test_region.py`、押し出しは `tests/test_extrude.py` が持つ。

**座標はスケッチ平面上の2D**。基準平面 "XY" なら (x, y) がそのまま使える。
"""
from __future__ import annotations

import dataclasses
import json

import pytest

# 立体の面にスケッチを貼るテストのために、テスト側から直接ソリッドを作る
# (境界検査は `duocad/` 配下だけを対象にしており、テストは対象外。M1の
# かつて test_extrude_face.py が BRepPrimAPI_MakeBox を使っていたのと同じ)。
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder

# 境界検査の実体はM0のテストが持つ。M2の新規ファイルにも同じ検査が効いていることを
# 見るために、そこから直接借りる(検査ロジックを写経すると片方だけ腐る)。
from test_boundaries import DUOCAD_DIR, _imported_module_roots, _source_files

from duocad import kernel
from duocad.document import Body, Document
from duocad.sketch import (
    BASE_PLANES,
    Entity,
    add_circle,
    add_line,
    add_rectangle,
    base_plane,
    entity_params,
    face_plane,
    get_sketch,
    sketch_create,
)

#: M2のソース(`duocad/` からの相対パス)。境界検査の対象に入っていることを固定する
M2_MODULES = ("sketch.py", "region.py", "verbs/extrude.py")


@pytest.fixture()
def box():
    """20mm立方体のソリッドを1つ持つ文書(6面とも平面)。"""
    document = Document()
    document.add_body(
        Body(
            id="box",
            name="box",
            kind="solid",
            shape=BRepPrimAPI_MakeBox(20.0, 20.0, 20.0).Shape(),
        )
    )
    return document


@pytest.fixture()
def cylinder():
    """円柱のソリッドを1つ持つ文書(側面が曲面なので「平面でない面」の実物になる)。"""
    document = Document()
    document.add_body(
        Body(
            id="cyl",
            name="cyl",
            kind="solid",
            shape=BRepPrimAPI_MakeCylinder(10.0, 20.0).Shape(),
        )
    )
    return document


def first_face_index(shape, kind: str) -> int:
    """種別が `kind` の面のうち最初の添字(M1の `kernel.tessellate` が返す分類を使う)。"""
    for face in kernel.tessellate(shape)["faces"]:
        if face["kind"] == kind:
            return face["index"]
    raise AssertionError(f"{kind} の面が無い(この形状ではテストが成立しない)")


# --------------------------------------------------------------------------------------
# データモデル — day1で払う予防(素で通る)
# --------------------------------------------------------------------------------------
def test_base_planes_are_the_three_reference_planes():
    """基準平面は3つだけ(実装計画書§1)。未知の名前は ValueError で弾く。"""
    assert BASE_PLANES == ("XY", "XZ", "YZ")
    assert base_plane("XY") == {"kind": "base", "name": "XY", "provenance": None}
    with pytest.raises(ValueError):
        base_plane("ZZ")


def test_entity_kind_is_validated():
    """図形の種別は決められたものだけ。知らない種別を黙って受け取らない(§6予防6)。

    円弧(`arc`)は有効な種別 — 理由は `tests/test_arc.py` のモジュールdocstring
    (実物のアロマ拡散器が部分円筒6面を持ち、代用では作れない)。このテストが見ているのは
    「登録されていない種別を拒否すること」であって、特定の綴りではない。
    """
    assert Entity(id="e1", kind="circle").params == {}
    assert Entity(id="e2", kind="arc").params == {}, "円弧は2026-08-11から有効な種別"
    # 必須フィールドの包含で見る(補助フィールドの追加まで禁じない)
    assert {"id", "kind", "params"} <= {f.name for f in dataclasses.fields(Entity)}
    with pytest.raises(ValueError):
        Entity(id="e3", kind="spline")  # 円形反復・スプラインはv1.1以降で今も範囲外


def test_m2_modules_are_covered_by_the_import_boundary_scan():
    """M2の新規ファイルにもOCP/build123dのimport境界が効いていること。

    `_source_files()` は再帰的に走査するので新規ファイルは自動的に対象になるが、
    **対象に入っていること自体**は誰も固定していない(M0の
    `test_boundary_scan_covers_every_source_file` はM0の6ファイルだけを見る)。
    M2で `duocad/verbs/` にもう1つverbが増えるので、ここで固定する。
    """
    scanned = {rel for rel, _ in _source_files()}
    assert set(M2_MODULES) <= scanned, f"走査から漏れている: {sorted(set(M2_MODULES) - scanned)}"
    for rel in M2_MODULES:
        roots = _imported_module_roots(DUOCAD_DIR / rel)
        assert "OCP" not in roots, f"{rel} がOCPを直接importしている(kernel.py越しに呼ぶこと)"
        assert "build123d" not in roots, f"{rel} がbuild123dを直接importしている(query.py越し)"


# --------------------------------------------------------------------------------------
# sketch_create — 基準平面と既存ソリッドの面
# --------------------------------------------------------------------------------------
#: 3平面は `base_plane()` の同じ return を通るだけで分岐が無い(`sketch.py`)。
#: 3値の網羅は `test_base_planes_are_the_three_reference_planes` が別に固定している。
@pytest.mark.parametrize("name", BASE_PLANES[:1])
def test_sketch_create_on_each_base_plane(name):
    """基準3面のどれにもスケッチを作れる。作ったスケッチは文書が持つ。"""
    document = Document()

    sketch_id = sketch_create(document, name)

    assert len(document.sketches) == 1
    created = get_sketch(document, sketch_id)
    assert created.plane == base_plane(name)
    assert created.entities == []


def test_sketch_create_rejects_an_unknown_base_plane():
    """知らない平面名は ValueError。文書にもログにも変更を残さない。"""
    document = Document()

    with pytest.raises(ValueError):
        sketch_create(document, "ZZ")

    assert document.sketches == []
    assert document.log.entries == []


def test_sketch_create_on_a_planar_face_of_an_existing_solid(box):
    """既存ソリッドの**平面状の面**にスケッチを貼れる(実装計画書§1のsketch_createの引数)。"""
    body = box.active_body()
    index = first_face_index(body.shape, "plane")

    sketch_id = sketch_create(box, face_plane(body.id, index))

    plane = get_sketch(box, sketch_id).plane
    assert plane["kind"] == "face"
    assert plane["body"] == body.id
    assert plane["index"] == index
    assert plane["provenance"] is None, "貼った面への参照に由来スロットが無い"


def test_sketch_create_rejects_a_non_planar_face(cylinder):
    """平面でない面(円筒面)を指定したら失敗する。**黙って通さない**(§6予防6)。

    通してしまうと、その上の領域検出・押し出しが全部意味を失った状態で成功する。
    """
    body = cylinder.active_body()
    index = first_face_index(body.shape, "cylinder_full")

    with pytest.raises(ValueError):
        sketch_create(cylinder, face_plane(body.id, index))

    assert cylinder.sketches == []
    assert cylinder.log.entries == []


# --------------------------------------------------------------------------------------
# 図形の追加
# --------------------------------------------------------------------------------------
def test_line_circle_and_rectangle_are_kept_in_the_sketch():
    """線・円・長方形を追加でき、スケッチが追加順に図形を保持する。

    数値は `entity_params()` 越しに読む。M3で数値を図形へ焼き込まない形
    (名前つきの値への参照)へ移しても、この関数の戻り(素の数値)は変わらないため、
    M3の変更でこの契約が壊れない(duocad/sketch.py の `entity_params` docstring)。
    """
    document = Document()
    sketch_id = sketch_create(document, "XY")

    line = add_line(document, sketch_id, (0.0, 0.0), (40.0, 0.0))
    circle = add_circle(document, sketch_id, (20.0, 15.0), 3.0)
    rectangle = add_rectangle(document, sketch_id, (0.0, 0.0), 40.0, 30.0)

    entities = get_sketch(document, sketch_id).entities
    assert [entity.kind for entity in entities] == ["line", "circle", "rectangle"]
    assert [entity.id for entity in entities] == [line, circle, rectangle]
    assert len({line, circle, rectangle}) == 3, "図形のIDが重複している"
    assert entity_params(document, sketch_id, line) == {
        "x0": 0.0, "y0": 0.0, "x1": 40.0, "y1": 0.0,
    }
    assert entity_params(document, sketch_id, circle) == {
        "cx": 20.0, "cy": 15.0, "radius": 3.0,
    }
    assert entity_params(document, sketch_id, rectangle) == {
        "x": 0.0, "y": 0.0, "width": 40.0, "height": 30.0,
    }


def test_adding_an_entity_to_an_unknown_sketch_fails():
    """存在しないスケッチへの図形追加は KeyError(黙って新しいスケッチを作らない)。"""
    document = Document()

    with pytest.raises(KeyError):
        add_circle(document, "sketch-does-not-exist", (0.0, 0.0), 5.0)

    assert document.log.entries == []


# --------------------------------------------------------------------------------------
# 操作ログとundo(verbの粒度: undo1回で消える単位＝ログ1行＝MCP1呼び出し。§2)
# --------------------------------------------------------------------------------------
def test_sketch_and_entities_are_logged_and_undoable():
    """スケッチの作成・図形の追加が操作ログに1行ずつ載り、undoで1つずつ戻る。

    ログが唯一の正(§3.1)なので、args だけで再生できる形になっていること
    (作られたIDを記録していないと、再生した文書の選択が別物を指す)まで見る。
    **3種類の図形すべての args の形を固定する** — 1種類だけ見ていると、
    「`Entity` は正しいがログの形だけ別」という実装が緑のまま残る。
    """
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle_id = add_circle(document, sketch_id, (0.0, 0.0), 20.0)
    line_id = add_line(document, sketch_id, (-30.0, 5.0), (30.0, 5.0))
    rectangle_id = add_rectangle(document, sketch_id, (0.0, 0.0), 40.0, 30.0)

    entries = document.log.entries
    assert [operation.verb for operation in entries] == [
        "sketch_create", "add_circle", "add_line", "add_rectangle",
    ]
    assert entries[0].args == {"sketch_id": sketch_id, "plane": base_plane("XY")}
    assert entries[1].args == {
        "sketch_id": sketch_id,
        "entity_id": circle_id,
        "kind": "circle",
        "params": {"cx": 0.0, "cy": 0.0, "radius": 20.0},
    }
    assert entries[2].args == {
        "sketch_id": sketch_id,
        "entity_id": line_id,
        "kind": "line",
        "params": {"x0": -30.0, "y0": 5.0, "x1": 30.0, "y1": 5.0},
    }
    assert entries[3].args == {
        "sketch_id": sketch_id,
        "entity_id": rectangle_id,
        "kind": "rectangle",
        "params": {"x": 0.0, "y": 0.0, "width": 40.0, "height": 30.0},
    }
    json.loads(document.log.to_json())  # 保存形式はJSON(§3.6)

    for expected_verb in ("add_rectangle", "add_line", "add_circle"):
        assert document.log.undo(document).verb == expected_verb
    assert get_sketch(document, sketch_id).entities == []
    assert document.log.undo(document).verb == "sketch_create"
    assert document.sketches == []

    for _ in range(4):
        document.log.redo(document)
    assert [entity.id for entity in get_sketch(document, sketch_id).entities] == [
        circle_id, line_id, rectangle_id,
    ]
    assert len(document.log.entries) == 4, "redoが操作を二重に記録している"
