"""スケッチ。

**座標はスケッチ平面上の2D**(u, v)。基準平面のときは (x, y) / (x, z) / (y, z) に対応する。
`sketch_create` の `plane` は基準平面の名前("XY"/"XZ"/"YZ")か、`face_plane(body_id,
face_index)` が返す既存ソリッドの**平面状の**面への参照(平面でない面は ValueError)。

**verbの戻り値の流儀**: 新しいものを作るverbは新しいIDを返し(`sketch_create` と同じ)、
既存の形状を変えるverbは `Operation` を返す(`extrude_face` と同じ)。スケッチのverbは前者、
`extrude` は後者。**`import_svg`(2026-08-28〜)はどちらでもない** —— 新しいスケッチを作らず
既存のスケッチへ図形を足すだけなので、戻り値は追加した entity_id のリストである
(`duocad/verbs/import_svg.py` のdocstring)。

**OCP / build123d を直接importしない**。領域検出の幾何は `duocad.region` 経由で
`duocad.kernel` が持つ。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Tuple
from uuid import uuid4

from . import kernel
from . import parameters
from .document import BodyId, Document, unknown_id_message
from .oplog import Operation, register_verb

SketchId = str
EntityId = str
PatternId = str

#: 基準平面の名前(この3つだけ。実装計画書§1「平面(基準3面)」)
BASE_PLANES = ("XY", "XZ", "YZ")

#: 反復の種別(矩形・円形。これでv1の反復verbが揃う)
PATTERN_KINDS = ("rectangular", "circular")


# --------------------------------------------------------------------------------------
# 図形種別ごとの純幾何演算(表) — kind分岐のテーブル化
# --------------------------------------------------------------------------------------
#
# **図形種別ごとに1行**(`EntityKindOps`)へ検査・平行移動・回転・中心をまとめる。
#
# OCPに依存する処理(辺の生成・2D bbox)はここに置けない(import境界。`kernel.py` だけが
# OCPをimportしてよい)。そちらは `kernel.py` 内に姉妹の表を持つ。**表は2つに分かれるが、
# 新しい種別を足すときに触るのは「ここへ1行」と「kernel.pyへ1行」の2箇所だけ**(+TS側1箇所)。
#
# **表に無い種別は即時 ValueError**(`entity_kind_ops` が投げる)。silent defaultは無い。
def _validate_line(params: Dict[str, float]) -> None:
    if (params["x0"], params["y0"]) == (params["x1"], params["y1"]):
        raise ValueError("line endpoints must differ (長さ0の線分は領域を割らない)")


def _validate_circle(params: Dict[str, float]) -> None:
    if not params["radius"] > 0:
        raise ValueError(f"circle radius must be positive, got {params['radius']!r}")


def _validate_rectangle(params: Dict[str, float]) -> None:
    if not (params["width"] > 0 and params["height"] > 0):
        raise ValueError(
            f"rectangle width/height must be positive, got "
            f"{params['width']!r} x {params['height']!r}"
        )


def _validate_arc(params: Dict[str, float]) -> None:
    if not params["radius"] > 0:
        raise ValueError(f"arc radius must be positive, got {params['radius']!r}")
    if params["start_deg"] == params["end_deg"]:
        raise ValueError("arc start_deg and end_deg must differ (角度0の円弧は辺にならない)")


def _validate_bezier(params: Dict[str, float]) -> None:
    """3次ベジエ。**始点と終点が一致していてよい**(閉じた1本の曲線はSVGに普通にある)。

    弾くのは制御点4つが1点に潰れた場合だけ —— そのときだけ長さ0の辺になり、以後
    `regions()` が毎回失敗する。
    """
    points = _bezier_points(params)
    if all(point == points[0] for point in points[1:]):
        raise ValueError("bezier control points must not all coincide (長さ0の曲線は辺にならない)")


def _translate_line(params: Dict[str, float], dx: float, dy: float) -> Dict[str, float]:
    return {
        "x0": params["x0"] + dx,
        "y0": params["y0"] + dy,
        "x1": params["x1"] + dx,
        "y1": params["y1"] + dy,
    }


def _translate_circle(params: Dict[str, float], dx: float, dy: float) -> Dict[str, float]:
    moved = dict(params)
    moved["cx"] = params["cx"] + dx
    moved["cy"] = params["cy"] + dy
    return moved


def _translate_arc(params: Dict[str, float], dx: float, dy: float) -> Dict[str, float]:
    moved = dict(params)
    moved["cx"] = params["cx"] + dx
    moved["cy"] = params["cy"] + dy
    return moved


def _translate_rectangle(params: Dict[str, float], dx: float, dy: float) -> Dict[str, float]:
    moved = dict(params)
    moved["x"] = params["x"] + dx
    moved["y"] = params["y"] + dy
    return moved


def _translate_bezier(params: Dict[str, float], dx: float, dy: float) -> Dict[str, float]:
    return _bezier_params(
        [(px + dx, py + dy) for px, py in _bezier_points(params)]
    )


def _rotate_point(x: float, y: float, cx: float, cy: float, angle_deg: float) -> Tuple[float, float]:
    """点 (x, y) を中心 (cx, cy) まわりに angle_deg 度だけ反時計回りに回す。"""
    rad = math.radians(angle_deg)
    cos_a = math.cos(rad)
    sin_a = math.sin(rad)
    dx = x - cx
    dy = y - cy
    return (cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a)


def _rectangle_as_rotated_lines(params: Dict[str, float], cx: float, cy: float, angle_deg: float) -> List[dict]:
    """軸平行の長方形を、4隅を回した4本の線分へ落とす(長方形の語彙は回転を表せないため)。"""
    x = params["x"]
    y = params["y"]
    width = params["width"]
    height = params["height"]
    corners = [
        (x, y),
        (x + width, y),
        (x + width, y + height),
        (x, y + height),
    ]
    rotated = [_rotate_point(px, py, cx, cy, angle_deg) for px, py in corners]
    lines: List[dict] = []
    for index in range(4):
        x0, y0 = rotated[index]
        x1, y1 = rotated[(index + 1) % 4]
        lines.append({"kind": "line", "params": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}})
    return lines


def _rotate_line(
    params: Dict[str, float], cx: float, cy: float, angle_deg: float, rotate_copies: bool
) -> List[dict]:
    if rotate_copies:
        x0, y0 = _rotate_point(params["x0"], params["y0"], cx, cy, angle_deg)
        x1, y1 = _rotate_point(params["x1"], params["y1"], cx, cy, angle_deg)
        return [{"kind": "line", "params": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}]
    mx = (params["x0"] + params["x1"]) / 2.0
    my = (params["y0"] + params["y1"]) / 2.0
    nmx, nmy = _rotate_point(mx, my, cx, cy, angle_deg)
    return [{"kind": "line", "params": _translate_line(params, nmx - mx, nmy - my)}]


def _rotate_circle(
    params: Dict[str, float], cx: float, cy: float, angle_deg: float, rotate_copies: bool
) -> List[dict]:
    nx, ny = _rotate_point(params["cx"], params["cy"], cx, cy, angle_deg)
    return [{"kind": "circle", "params": {"cx": nx, "cy": ny, "radius": params["radius"]}}]


def _rotate_arc(
    params: Dict[str, float], cx: float, cy: float, angle_deg: float, rotate_copies: bool
) -> List[dict]:
    nx, ny = _rotate_point(params["cx"], params["cy"], cx, cy, angle_deg)
    start_deg = params["start_deg"]
    end_deg = params["end_deg"]
    if rotate_copies:
        start_deg = start_deg + angle_deg
        end_deg = end_deg + angle_deg
    return [
        {
            "kind": "arc",
            "params": {"cx": nx, "cy": ny, "radius": params["radius"], "start_deg": start_deg, "end_deg": end_deg},
        }
    ]


def _rotate_rectangle(
    params: Dict[str, float], cx: float, cy: float, angle_deg: float, rotate_copies: bool
) -> List[dict]:
    if rotate_copies:
        # 長方形の `x, y, width, height` は軸平行しか表せないので、回転した複製は
        # 種別が変わる(4本の線分になる) — `rotate` の戻り値を種別横断の
        # `[{"kind":.., "params":..}, ...]` にしてあるのはこの1ケースのため。
        return _rectangle_as_rotated_lines(params, cx, cy, angle_deg)
    ecx = params["x"] + params["width"] / 2.0
    ecy = params["y"] + params["height"] / 2.0
    ncx, ncy = _rotate_point(ecx, ecy, cx, cy, angle_deg)
    return [{"kind": "rectangle", "params": _translate_rectangle(params, ncx - ecx, ncy - ecy)}]


def _rotate_bezier(
    params: Dict[str, float], cx: float, cy: float, angle_deg: float, rotate_copies: bool
) -> List[dict]:
    """**ベジエはアフィン不変**なので、制御点を回すだけで曲線が厳密に回る。

    長方形(`_rotate_rectangle`)のように種別が崩れて線分4本に化けることが無い唯一の曲線で、
    戻り値は常に1要素のまま。
    """
    if rotate_copies:
        rotated = [_rotate_point(px, py, cx, cy, angle_deg) for px, py in _bezier_points(params)]
        return [{"kind": "bezier", "params": _bezier_params(rotated)}]
    mx, my = _center_bezier(params)
    nmx, nmy = _rotate_point(mx, my, cx, cy, angle_deg)
    return [{"kind": "bezier", "params": _translate_bezier(params, nmx - mx, nmy - my)}]


def _center_line(params: Dict[str, float]) -> Tuple[float, float]:
    return ((params["x0"] + params["x1"]) / 2.0, (params["y0"] + params["y1"]) / 2.0)


def _center_circle(params: Dict[str, float]) -> Tuple[float, float]:
    return (float(params["cx"]), float(params["cy"]))


def _center_arc(params: Dict[str, float]) -> Tuple[float, float]:
    return (float(params["cx"]), float(params["cy"]))


def _center_rectangle(params: Dict[str, float]) -> Tuple[float, float]:
    return (params["x"] + params["width"] / 2.0, params["y"] + params["height"] / 2.0)


def _center_bezier(params: Dict[str, float]) -> Tuple[float, float]:
    """**制御点4つの重心**。曲線上の点ではないが、平行移動・回転の基準として一貫していれば
    足りる(`_center_line` が線分の中点を返すのと同じ役割)。"""
    points = _bezier_points(params)
    return (
        sum(point[0] for point in points) / 4.0,
        sum(point[1] for point in points) / 4.0,
    )


#: 3次ベジエの `params` のキー(**始点・制御点1・制御点2・終点の順**)。
#: `_bezier_points` / `_bezier_params` の往復がこの順序を唯一の正とする。
BEZIER_KEYS = ("x0", "y0", "cx1", "cy1", "cx2", "cy2", "x1", "y1")


def _bezier_points(params: Dict[str, float]) -> List[Tuple[float, float]]:
    """`params` を制御点4つの (x, y) へ。"""
    return [
        (float(params["x0"]), float(params["y0"])),
        (float(params["cx1"]), float(params["cy1"])),
        (float(params["cx2"]), float(params["cy2"])),
        (float(params["x1"]), float(params["y1"])),
    ]


def _bezier_params(points: List[Tuple[float, float]]) -> Dict[str, float]:
    """制御点4つの (x, y) を `params` へ(`_bezier_points` の逆)。"""
    flat = [value for point in points for value in point]
    return {key: float(value) for key, value in zip(BEZIER_KEYS, flat)}


@dataclass(frozen=True)
class EntityKindOps:
    """図形1種別ぶんの純幾何演算(OCPなし)。**表の1行 = その種別の全処理**。

    `rotate` は同じ種別に閉じない(長方形の `rotate_copies=True` は4本の線分になる)ので、
    戻り値は常に `[{"kind":.., "params":..}, ...]` の**リスト**にしてある(他の種別は1要素)。
    """

    validate: Callable[[Dict[str, float]], None]
    translate: Callable[[Dict[str, float], float, float], Dict[str, float]]
    rotate: Callable[[Dict[str, float], float, float, float, bool], List[dict]]
    center_2d: Callable[[Dict[str, float]], Tuple[float, float]]


_ENTITY_KIND_TABLE: Dict[str, EntityKindOps] = {
    "line": EntityKindOps(_validate_line, _translate_line, _rotate_line, _center_line),
    "circle": EntityKindOps(_validate_circle, _translate_circle, _rotate_circle, _center_circle),
    "rectangle": EntityKindOps(_validate_rectangle, _translate_rectangle, _rotate_rectangle, _center_rectangle),
    "arc": EntityKindOps(_validate_arc, _translate_arc, _rotate_arc, _center_arc),
    "bezier": EntityKindOps(_validate_bezier, _translate_bezier, _rotate_bezier, _center_bezier),
}

#: 図形の種別。**`_ENTITY_KIND_TABLE` のキーが唯一の正**(新種別は表に1行足すだけでよい)。
ENTITY_KINDS = tuple(_ENTITY_KIND_TABLE.keys())


def entity_kind_ops(kind: str) -> EntityKindOps:
    """種別ごとの純幾何演算(検査・平行移動・回転・中心)を引く。

    表に無い種別は**その場で ValueError**(黙ってdefaultへ落とすと「反復だけ位置がずれる」型の
    静かなバグになる)。
    """
    try:
        return _ENTITY_KIND_TABLE[kind]
    except KeyError:
        raise ValueError(f"unknown entity kind: {kind!r} (one of {list(ENTITY_KINDS)})") from None


VERB_SKETCH_CREATE = "sketch_create"
VERB_ADD_LINE = "add_line"
VERB_ADD_CIRCLE = "add_circle"
VERB_ADD_RECTANGLE = "add_rectangle"
VERB_ADD_ARC = "add_arc"


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

    面の添字は形状変更で静かにずれるので、`provenance` を day1 で空けておく
    (後から足すと全操作の書き直しになる)。
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

    `params` は**その図形が自分で持つ名前つきの数値**で、値は名前つきの値への参照
    (`parameters.register_value` が返す名前)。**解決済みの数値を返すのは `entity_params()` の
    役目**であり、内部表現が参照であることは呼び出し側から隠される。

    キーは図形ごとに固定する(スカラー1個 = 将来のパラメータ1個になるよう平らに持つ):

        line      : x0, y0, x1, y1
        circle    : cx, cy, radius
        rectangle : x, y, width, height
        arc       : cx, cy, radius, start_deg, end_deg   (度・反時計回り・X軸から。0度をまたいでよい)
        bezier    : x0, y0, cx1, cy1, cx2, cy2, x1, y1   (始点・制御点1・制御点2・終点)

    **bezier だけは値が素の数値のまま**でありうる。これを作るのは `import_svg` だけで、
    取り込んだ曲線の制御点座標は「人が打ち直す寸法」ではないため名前つきの値に登録しない
    (ロゴ1枚で数千個の `d1` が並び、SketchPanelの図形一覧が埋まる)。
    `parameters.resolve` は素の数値をそのまま返すので、この形は元から通る。
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
    """図形の反復。

    **1つの反復は1つのオブジェクト**であり、nx×ny 個の図形へ展開して持たない(展開すると
    個数や間隔を後から変えられなくなり、反復の存在理由が消える)。展開は幾何を作るときだけ。

    `params` のキーは `dx` / `dy` / `nx` / `ny` で、値は `Entity.params` と同じく**名前つきの
    値への参照**。格子は (0,0) を含み、そこが `sources` の元の位置になる。
    """

    id: PatternId
    sources: List[EntityId] = field(default_factory=list)
    params: Dict[str, Any] = field(default_factory=dict)
    #: 反復の種類(`Body.kind` / `Entity.kind` と同じ語彙)。既定は "rectangular"。
    kind: str = "rectangular"
    #: 寸法ではない設定(例: 円形反復の `rotate_copies`)。**`params` に混ぜない** —
    #: `params` は `set_parameter` で編集できる名前つきの数値の置き場であり、真偽の
    #: 切り替えを入れると `0.5` を書き込める余地ができる。
    options: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in PATTERN_KINDS:
            raise ValueError(
                f"kind must be one of {list(PATTERN_KINDS)!r}, got {self.kind!r}"
            )


@dataclass
class Sketch:
    """1つのスケッチ。`plane` は PlaneRef、`entities` は追加順の図形。

    **領域(`Region`)は持たない** —— 領域は描くものではなく計算されるものなので、正は
    `duocad.region.regions(doc, sketch_id)` の戻り値である。

    `patterns` は反復のリストを持つ(反復は図形ではなく、図形を参照する別のもの)。
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
    raise KeyError(
        unknown_id_message("sketch", sketch_id, [s.id for s in doc.sketches])
    )


# --------------------------------------------------------------------------------------
# verb
# --------------------------------------------------------------------------------------
def sketch_create(doc: Document, plane: Any) -> SketchId:
    """スケッチを作って SketchId を返す。

    面を指定する場合、その面が**平面でなければ ValueError**(円筒面などに貼らせない ——
    黙って通すと以後の領域検出が意味を失う)。失敗時は `doc.sketches` にもログにも残さない。
    """
    plane_ref = resolve_plane(doc, plane)
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


def add_arc(
    doc: Document,
    sketch_id: SketchId,
    center: Any,
    radius: float,
    start_deg: float,
    end_deg: float,
) -> EntityId:
    """円弧を足して EntityId を返す。

    **中心・半径・角度で持つ**(3点で受け取らない) —— 半径・角度を後から編集できる形に
    しておかないと、円弧だけが「数値を持たない図形」になり `set_parameter` の外側に落ちる。
    `params` のキーは `cx` / `cy` / `radius` / `start_deg` / `end_deg` の5つ。角度は**度**・
    反時計回り・スケッチ平面のX軸から測る。
    """
    params = {
        "cx": float(center[0]),
        "cy": float(center[1]),
        "radius": float(radius),
        "start_deg": float(start_deg),
        "end_deg": float(end_deg),
    }
    return _add_entity(doc, sketch_id, "arc", params, VERB_ADD_ARC)


def entity_params(
    doc: Document, sketch_id: SketchId, entity_id: EntityId
) -> Dict[str, float]:
    """図形の数値パラメータを**解決済みの素の数値**で返す。

    `Entity.params` の中身は名前つきの値への参照なので、この関数を挟んで内部表現を隠す。
    """
    sketch = get_sketch(doc, sketch_id)
    for entity in sketch.entities:
        if entity.id == entity_id:
            return {
                key: parameters.resolve(doc, ref)
                for key, ref in entity.params.items()
            }
    raise KeyError(
        unknown_id_message(
            "entity",
            entity_id,
            [e.id for e in sketch.entities],
            where=f"sketch {sketch_id!r}",
        )
    )


def resolve_plane(doc: Document, plane: Any) -> dict:
    """引数の平面指定を PlaneRef のdictへ正規化する。失敗時は文書を触らない。

    **verb層からも呼ぶ**(`verbs/import_svg.py`)ので公開している —— 平面の解決を2箇所に
    持つと、面が平面かどうかの検査が片方だけ抜ける。
    """
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

    **数値は先に検査する**。非有限値・半径0以下・長さ0の線分・辺0の長方形はその場で
    `ValueError` にし、文書にもログにも残さない。黙って受け取ると、以後 `regions()` が毎回
    失敗する(図形の削除verbが無いのでundo以外に復旧手段が無い)か、NaN が混ざって**同じ
    スケッチ上の正常な領域まで黙って消える**うえ、`OpLog.to_json()` が裸の NaN を吐いて
    JSONとして壊れる。

    **モデルとログには別のdictを渡す**。同じ参照を共有すると、図形のパラメータを書き換えた
    瞬間に**唯一の正であるログ**が黙って書き換わる。`Entity.params` の中身は名前つきの値への
    参照であり、ログの `args["params"]` には打った数値をそのまま入れるので、モデルとログでは
    持つ値の形自体が違う。
    """
    require_valid_params(kind, params)
    sketch = get_sketch(doc, sketch_id)
    entity_id = f"entity-{uuid4().hex[:12]}"
    refs = {
        key: parameters.register_value(doc, value) for key, value in params.items()
    }
    sketch.entities.append(Entity(id=entity_id, kind=kind, params=refs))
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


def require_valid_params(kind: str, params: Dict[str, float]) -> None:
    """図形の数値が使える値かを検査する(壊れた図形をスケッチへ入れない)。

    **作成時と `set_parameter` の両方から呼ぶ**。作成時だけ検査していると、同じ値を
    後から書き込む経路が素通りし、`add_circle` が拒否する半径0やNaNが文書に入る
    (NaNは同じスケッチの正常な領域まで黙って消す)。
    """
    for name, value in params.items():
        if not math.isfinite(value):
            raise ValueError(f"{kind}.{name} must be a finite number, got {value!r}")
    entity_kind_ops(kind).validate(params)


# --------------------------------------------------------------------------------------
# verbの登録
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
    sketch = next(
        (candidate for candidate in document.sketches if candidate.id == sketch_id),
        None,
    )
    if sketch is not None:
        for entity in sketch.entities:
            parameters.release_param_refs(document, entity.params)
        for pattern in sketch.patterns:
            parameters.release_param_refs(document, pattern.params)
    document.sketches = [s for s in document.sketches if s.id != sketch_id]


def _do_add_entity(document: Document, operation: Operation) -> None:
    """3つの図形追加verbで共有する再適用(記録された種別とparamsで図形を作り直す)。

    ログの `params` は素の数値。再生時に `register_value` し直し、Entity には参照を入れる。
    """
    args = operation.args
    sketch = get_sketch(document, args["sketch_id"])
    entity_id = args["entity_id"]
    if any(entity.id == entity_id for entity in sketch.entities):
        raise ValueError(f"entity id already exists: {entity_id!r}")
    raw_params = dict(args["params"])
    refs = {
        key: parameters.register_value(document, float(value))
        for key, value in raw_params.items()
    }
    sketch.entities.append(
        Entity(id=entity_id, kind=args["kind"], params=refs)
    )


def _undo_add_entity(document: Document, operation: Operation) -> None:
    args = operation.args
    sketch = get_sketch(document, args["sketch_id"])
    entity_id = args["entity_id"]
    remaining = []
    for entity in sketch.entities:
        if entity.id == entity_id:
            parameters.release_param_refs(document, entity.params)
        else:
            remaining.append(entity)
    sketch.entities = remaining


register_verb(VERB_SKETCH_CREATE, do=_do_sketch_create, undo=_undo_sketch_create)
for _verb in (VERB_ADD_LINE, VERB_ADD_CIRCLE, VERB_ADD_RECTANGLE, VERB_ADD_ARC):
    # 3つとも「スケッチへ図形を1つ足す」で、do/undo の実体は args["kind"] で分かれない
    # (追加する図形の種別は args が持つ)。verb名を分けているのは操作ログとMCPの粒度のため。
    register_verb(_verb, do=_do_add_entity, undo=_undo_add_entity)
del _verb
