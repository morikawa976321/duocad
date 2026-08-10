"""スケッチ(実装計画書 §3.3「スケッチと領域」・§1のスケッチverb)。M2で追加。

M2(2026-08-11)で実装済み。データモデル(`Sketch` / `Entity` / PlaneRef と、その day1 の
由来スロット)はテスト先行の段階で先に置き、verbはこのタスクで埋めた(tests/README.md 参照)。

契約:
    sketch_create(doc, plane) -> SketchId
        `plane` は基準平面の名前("XY"/"XZ"/"YZ")か、`face_plane(body_id, face_index)` が返す
        既存ソリッドの**平面状の**面への参照。平面でない面(円筒面など)は ValueError。
    add_line(doc, sketch_id, p0, p1) -> EntityId
    add_circle(doc, sketch_id, center, radius) -> EntityId
    add_rectangle(doc, sketch_id, corner, width, height) -> EntityId
    entity_params(doc, sketch_id, entity_id) -> dict[str, float]

**座標はスケッチ平面上の2D**(u, v)。基準平面のときは (x, y) / (x, z) / (y, z) に対応する。

**verbの戻り値の流儀**(既存に合わせる): 新しいものを作るverbは新しいIDを返し
(`make_editable_copy` と同じ)、既存の形状を変えるverbは `Operation` を返す
(`extrude_face` と同じ)。スケッチのverbは前者、`extrude` は後者。

**なぜ verbs/ ではなくここに置くか**: スケッチの4 verbは `Sketch`/`Entity` のデータモデルと
不可分で、別モジュールへ切り出すと相互参照になる。`verbs/` に置くのは3Dのverb
(`extrude_face` / `extrude`)。タスクファイルが指定する成果物のパスもこの分け方である。

**OCP / build123d を直接importしない**(tests/test_boundaries.py が再帰的に検査する)。
領域検出の幾何は `duocad.region` 経由で `duocad.kernel` が持つ。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List
from uuid import uuid4

from . import kernel
from .document import BodyId, Document
from .oplog import Operation, register_verb

SketchId = str
EntityId = str
PatternId = str

#: 基準平面の名前(この3つだけ。実装計画書§1「平面(基準3面)」)
BASE_PLANES = ("XY", "XZ", "YZ")

#: 図形の種別(M2の3つ。円弧はv1.2、円形反復はv1.1で範囲外)
ENTITY_KINDS = ("line", "circle", "rectangle")

VERB_SKETCH_CREATE = "sketch_create"
VERB_ADD_LINE = "add_line"
VERB_ADD_CIRCLE = "add_circle"
VERB_ADD_RECTANGLE = "add_rectangle"


# --------------------------------------------------------------------------------------
# PlaneRef — スケッチを貼る平面への参照(day1の由来スロット対象。§3.3・§6予防2)
# --------------------------------------------------------------------------------------
def base_plane(name: str) -> dict:
    """基準平面への参照。`{"kind": "base", "name": "XY", "provenance": None}`。

    JSON化できる素のdictで持つ(操作ログの `args` に入るため。§3.6)。
    """
    if name not in BASE_PLANES:
        raise ValueError(f"unknown base plane: {name!r} (one of {list(BASE_PLANES)})")
    return {"kind": "base", "name": name, "provenance": None}


def face_plane(body_id: BodyId, face_index: int) -> dict:
    """既存ソリッドの面への参照。`{"kind": "face", "body":.., "index":.., "provenance": None}`。

    **これが「壊れやすい参照」の2つ目**(実装計画書§6「由来スロットの適用先は3つ」の
    「スケッチを貼った面」)。面の添字は形状変更で静かにずれるので、
    `provenance` を day1 で空けておく — 後から足すと全操作の書き直しになる。
    """
    return {
        "kind": "face",
        "body": body_id,
        "index": int(face_index),
        "provenance": None,
    }


# --------------------------------------------------------------------------------------
# データモデル(実装済み)
# --------------------------------------------------------------------------------------
@dataclass
class Entity:
    """スケッチ上の1図形。

    `params` は**その図形が自分で持つ名前つきの数値**(実装計画書§3.4
    「パラメトリック・プリミティブ」)。M2では素の float が入る。M3で名前つきの値への
    参照に変わるが、**解決済みの数値を返す `entity_params()` の戻り値は変わらない**
    (だからM3はM2のテストを壊さない)。

    キーは図形ごとに固定する(スカラー1個 = 将来のパラメータ1個になるよう平らに持つ):

        line      : x0, y0, x1, y1
        circle    : cx, cy, radius
        rectangle : x, y, width, height
    """

    id: EntityId
    kind: str
    params: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in ENTITY_KINDS:
            raise ValueError(
                f"kind must be one of {list(ENTITY_KINDS)!r}, got {self.kind!r}"
            )


@dataclass
class Pattern:
    """図形の矩形反復(M3で追加。実装計画書§1の `pattern_rectangular`)。

    **1つの反復は1つのオブジェクト**であり、nx×ny 個の図形へ展開して持たない
    (展開すると個数や間隔を後から変えられなくなり、反復の存在理由が消える。§2「verbの粒度」)。
    展開は幾何を作るときだけ行う導出物。

    `params` のキーは `dx` / `dy` / `nx` / `ny` で、値は `Entity.params` と同じく
    **名前つきの値への参照**(§6予防5)。格子は (0,0) を含み、そこが `sources` の
    元の位置になる。
    """

    id: PatternId
    sources: List[EntityId] = field(default_factory=list)
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Sketch:
    """1つのスケッチ。`plane` は PlaneRef、`entities` は追加順の図形。

    **領域(`Region`)は持たない** — 領域は描くものではなく計算されるもの(§3.3)なので、
    正は `duocad.region.regions(doc, sketch_id)` の戻り値であり、キャッシュを持つか
    どうかは実装の自由にしてある(tests/README.md「M2の決定」)。

    `patterns` はM3で追加(反復は図形ではなく、図形を参照する別のもの)。
    """

    id: SketchId
    plane: dict
    entities: List[Entity] = field(default_factory=list)
    patterns: List["Pattern"] = field(default_factory=list)


def get_sketch(doc: Document, sketch_id: SketchId) -> Sketch:
    """IDでスケッチを引く。存在しなければ KeyError(黙って None を返さない)。"""
    for sketch in doc.sketches:
        if sketch.id == sketch_id:
            return sketch
    raise KeyError(f"no such sketch: {sketch_id!r}")


# --------------------------------------------------------------------------------------
# verb
# --------------------------------------------------------------------------------------
def sketch_create(doc: Document, plane: Any) -> SketchId:
    """スケッチを作って SketchId を返す。

    `plane` は "XY"/"XZ"/"YZ" か `face_plane(body_id, face_index)`。
    面を指定する場合、その面が**平面でなければ ValueError**(円筒面などに貼らせない。
    黙って通すと以後の領域検出が意味を失う。§6予防6)。失敗時は `doc.sketches` にも
    ログにも変更を残さないこと。
    """
    plane_ref = _resolve_plane(doc, plane)
    sketch_id = f"sketch-{uuid4().hex[:12]}"
    # モデルとログには別のdictを渡す(共有すると、貼った面の由来スロットを後から埋めた
    # 瞬間に、唯一の正であるログが黙って書き換わる)。
    doc.sketches.append(Sketch(id=sketch_id, plane=dict(plane_ref)))
    doc.log.append(VERB_SKETCH_CREATE, {"sketch_id": sketch_id, "plane": dict(plane_ref)})
    return sketch_id


def add_line(doc: Document, sketch_id: SketchId, p0: Any, p1: Any) -> EntityId:
    """スケッチ平面上の2点 `p0`/`p1` を結ぶ線分を足して EntityId を返す。"""
    params = {
        "x0": float(p0[0]),
        "y0": float(p0[1]),
        "x1": float(p1[0]),
        "y1": float(p1[1]),
    }
    return _add_entity(doc, sketch_id, "line", params, VERB_ADD_LINE)


def add_circle(
    doc: Document, sketch_id: SketchId, center: Any, radius: float
) -> EntityId:
    """スケッチ平面上の `center` を中心とする半径 `radius` の円を足して EntityId を返す。"""
    params = {
        "cx": float(center[0]),
        "cy": float(center[1]),
        "radius": float(radius),
    }
    return _add_entity(doc, sketch_id, "circle", params, VERB_ADD_CIRCLE)


def add_rectangle(
    doc: Document, sketch_id: SketchId, corner: Any, width: float, height: float
) -> EntityId:
    """`corner` を左下とする幅 `width`・高さ `height` の長方形を足して EntityId を返す。"""
    params = {
        "x": float(corner[0]),
        "y": float(corner[1]),
        "width": float(width),
        "height": float(height),
    }
    return _add_entity(doc, sketch_id, "rectangle", params, VERB_ADD_RECTANGLE)


def entity_params(
    doc: Document, sketch_id: SketchId, entity_id: EntityId
) -> Dict[str, float]:
    """図形の数値パラメータを**解決済みの素の数値**で返す。

    M2では `Entity.params` の値がそのまま数値なので素通しでよい。**この関数を挟んである
    理由はM3にある** — M3で数値を図形に焼き込まず名前つきの値として別に持つ形へ移すとき、
    `Entity.params` の中身は参照に変わるが、ここの戻り値(数値)は変わらない。
    テストがこの関数越しに読んでいれば、M3の変更でM2の契約は壊れない。
    """
    sketch = get_sketch(doc, sketch_id)
    for entity in sketch.entities:
        if entity.id == entity_id:
            return dict(entity.params)
    raise KeyError(f"no such entity: {entity_id!r}")


def _resolve_plane(doc: Document, plane: Any) -> dict:
    """引数の平面指定を PlaneRef のdictへ正規化する。失敗時は文書を触らない。"""
    if isinstance(plane, str):
        return base_plane(plane)
    if isinstance(plane, dict) and plane.get("kind") == "base":
        return base_plane(plane["name"])
    if isinstance(plane, dict) and plane.get("kind") == "face":
        body_id = plane["body"]
        face_index = int(plane["index"])
        body = next((candidate for candidate in doc.bodies if candidate.id == body_id), None)
        if body is None:
            raise KeyError(f"no such body: {body_id!r}")
        if body.shape is None:
            raise ValueError(f"body {body_id!r} has no shape")
        kernel.require_planar_face(body.shape, face_index)
        return {
            "kind": "face",
            "body": body_id,
            "index": face_index,
            "provenance": plane.get("provenance"),
        }
    raise ValueError(f"invalid sketch plane: {plane!r}")


def _add_entity(
    doc: Document,
    sketch_id: SketchId,
    kind: str,
    params: Dict[str, float],
    verb: str,
) -> EntityId:
    """図形を1つ足してログに積む(3つの add_* で共有)。

    **数値は先に検査する**(§6予防6)。非有限値・半径0以下・長さ0の線分・辺0の長方形は
    その場で `ValueError` にし、文書にもログにも残さない。黙って受け取ると、以後
    `regions()` が毎回失敗する(M2に図形の削除verbは無いのでundo以外に復旧手段が無い)か、
    NaN が混ざって**同じスケッチ上の正常な領域まで黙って消える**うえ、
    `OpLog.to_json()` が裸の NaN を吐いてJSONとして壊れる(2026-08-11のQAで実測)。

    **モデルとログには別のdictを渡す**。同じ参照を共有すると、図形のパラメータを
    書き換えた瞬間に**唯一の正であるログ**(§3.1)が黙って書き換わる。M3は
    `Entity.params` の中身を変える予定なので、ここを共有したままにはできない。
    """
    _require_finite(kind, params)
    sketch = get_sketch(doc, sketch_id)
    entity_id = f"entity-{uuid4().hex[:12]}"
    sketch.entities.append(Entity(id=entity_id, kind=kind, params=dict(params)))
    doc.log.append(
        verb,
        {
            "sketch_id": sketch_id,
            "entity_id": entity_id,
            "kind": kind,
            "params": dict(params),
        },
    )
    return entity_id


def _require_finite(kind: str, params: Dict[str, float]) -> None:
    """図形の数値が使える値かを検査する(壊れた図形をスケッチへ入れない)。"""
    for name, value in params.items():
        if not math.isfinite(value):
            raise ValueError(f"{kind}.{name} must be a finite number, got {value!r}")
    if kind == "circle" and not params["radius"] > 0:
        raise ValueError(f"circle radius must be positive, got {params['radius']!r}")
    if kind == "line" and (params["x0"], params["y0"]) == (params["x1"], params["y1"]):
        raise ValueError("line endpoints must differ (長さ0の線分は領域を割らない)")
    if kind == "rectangle" and not (params["width"] > 0 and params["height"] > 0):
        raise ValueError(
            f"rectangle width/height must be positive, got "
            f"{params['width']!r} x {params['height']!r}"
        )


# --------------------------------------------------------------------------------------
# verbの登録(oplog は verb名の分岐を持たない。登録は verb を定義したこちら側)
# --------------------------------------------------------------------------------------
def _do_sketch_create(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された SketchId と平面でスケッチを作り直す。

    同じIDが既にあるなら `ValueError`(`Document.add_body` と同じ理由 — 黙って重複させると
    `get_sketch` がどちらを返すかで以後の図形追加・領域参照の指す先が静かに決まる)。
    """
    args = operation.args
    sketch_id = args["sketch_id"]
    if any(sketch.id == sketch_id for sketch in document.sketches):
        raise ValueError(f"sketch id already exists: {sketch_id!r}")
    document.sketches.append(Sketch(id=sketch_id, plane=dict(args["plane"])))


def _undo_sketch_create(document: Document, operation: Operation) -> None:
    sketch_id = operation.args["sketch_id"]
    document.sketches = [sketch for sketch in document.sketches if sketch.id != sketch_id]


def _do_add_entity(document: Document, operation: Operation) -> None:
    """3つの図形追加verbで共有する再適用(記録された種別とparamsで図形を作り直す)。"""
    args = operation.args
    sketch = get_sketch(document, args["sketch_id"])
    entity_id = args["entity_id"]
    if any(entity.id == entity_id for entity in sketch.entities):
        raise ValueError(f"entity id already exists: {entity_id!r}")
    sketch.entities.append(
        Entity(id=entity_id, kind=args["kind"], params=dict(args["params"]))
    )


def _undo_add_entity(document: Document, operation: Operation) -> None:
    args = operation.args
    sketch = get_sketch(document, args["sketch_id"])
    entity_id = args["entity_id"]
    sketch.entities = [entity for entity in sketch.entities if entity.id != entity_id]


register_verb(VERB_SKETCH_CREATE, do=_do_sketch_create, undo=_undo_sketch_create)
for _verb in (VERB_ADD_LINE, VERB_ADD_CIRCLE, VERB_ADD_RECTANGLE):
    # 3つとも「スケッチへ図形を1つ足す」で、do/undo の実体は args["kind"] で分かれない
    # (追加する図形の種別は args が持つ)。verb名を分けているのは操作ログとMCPの粒度のため。
    register_verb(_verb, do=_do_add_entity, undo=_undo_add_entity)
del _verb
