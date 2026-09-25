"""スケッチ。

**座標はスケッチ平面上の2D**(u, v)。基準平面のときは (x, y) / (x, z) / (y, z) に対応する。
`sketch_create` の `plane` は基準平面の名前("XY"/"XZ"/"YZ")か、`face_plane(body_id,
face_index)` が返す既存ソリッドの**平面状の**面への参照(平面でない面は ValueError)。

**verbの戻り値の流儀**: 新しいものを作るverbは新しいIDを返し(`sketch_create` と同じ)、
既存の形状を変えるverbは `Operation` を返す(`extrude_face` と同じ)。スケッチのverbは前者、
`extrude` は後者。**`import_svg` はどちらでもない** —— 新しいスケッチを作らず
既存のスケッチへ図形を足すだけなので、戻り値は追加した entity_id のリストである
(`duocad/verbs/import_svg.py` のdocstring)。

**OCP / build123d を直接importしない**。領域検出の幾何は `duocad.region` 経由で
`duocad.kernel_sketch` が持つ。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4

from . import kernel_sketch
from . import parameters
from .document import BodyId, Document, unknown_id_message
from .oplog import Operation, register_verb

SketchId = str
EntityId = str
PatternId = str


def new_entity_id() -> EntityId:
    """新しい図形のID(**再生時は記録済みのIDをそのまま使う**のでここは通らない)。"""
    return f"entity-{uuid4().hex[:12]}"


#: 基準平面の名前(この3つだけ。実装計画書§1「平面(基準3面)」)
BASE_PLANES = ("XY", "XZ", "YZ")

#: 反復の種別(矩形・円形)
PATTERN_KINDS = ("rectangular", "circular")


# --------------------------------------------------------------------------------------
# 図形種別ごとの純幾何演算(表) — kind分岐のテーブル化
# --------------------------------------------------------------------------------------
#
# **図形種別ごとに1行**(`EntityKindOps`)へ検査・平行移動・回転・中心をまとめる。
#
# OCPに依存する処理(辺の生成・2D bbox)はここに置けない(import境界。カーネル層だけが
# OCPをimportしてよい)。そちらは `kernel_sketch.py` 内に姉妹の表を持つ。**表は2つに分かれるが、
# 新しい種別を足すときに触るのは「ここへ1行」と「kernel_sketch.pyへ1行」の2箇所だけ**(+TS側1箇所)。
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


def _translate_cxcy(params: Dict[str, float], dx: float, dy: float) -> Dict[str, float]:
    """中心 `cx`/`cy` だけで位置が決まる種別(円・円弧)の平行移動。"""
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


def rotate_point(x: float, y: float, cx: float, cy: float, angle_deg: float) -> Tuple[float, float]:
    """点 (x, y) を中心 (cx, cy) まわりに angle_deg 度だけ反時計回りに回す。

    **2D回転の式はこの1本しかない。** 種別の表(`EntityKindOps.rotate`)は「図形1つを回す」単位で、
    点1つを回す `move_sketch_entities` もここを使う。**別に式を書き起こさない**(符号や度/ラジアンの
    取り違えが、円形反復と移動で食い違う形で出る)。画面側は `web/src/entityKinds.ts` の `rotatePoint`。
    """
    rad = math.radians(angle_deg)
    cos_a = math.cos(rad)
    sin_a = math.sin(rad)
    dx = x - cx
    dy = y - cy
    return (cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a)


def _rotate_line(
    params: Dict[str, float], cx: float, cy: float, angle_deg: float, rotate_copies: bool
) -> Dict[str, float]:
    if rotate_copies:
        x0, y0 = rotate_point(params["x0"], params["y0"], cx, cy, angle_deg)
        x1, y1 = rotate_point(params["x1"], params["y1"], cx, cy, angle_deg)
        return {"x0": x0, "y0": y0, "x1": x1, "y1": y1}
    mx = (params["x0"] + params["x1"]) / 2.0
    my = (params["y0"] + params["y1"]) / 2.0
    nmx, nmy = rotate_point(mx, my, cx, cy, angle_deg)
    return _translate_line(params, nmx - mx, nmy - my)


def _rotate_circle(
    params: Dict[str, float], cx: float, cy: float, angle_deg: float, rotate_copies: bool
) -> Dict[str, float]:
    nx, ny = rotate_point(params["cx"], params["cy"], cx, cy, angle_deg)
    return {"cx": nx, "cy": ny, "radius": params["radius"]}


def _rotate_arc(
    params: Dict[str, float], cx: float, cy: float, angle_deg: float, rotate_copies: bool
) -> Dict[str, float]:
    nx, ny = rotate_point(params["cx"], params["cy"], cx, cy, angle_deg)
    start_deg = params["start_deg"]
    end_deg = params["end_deg"]
    if rotate_copies:
        start_deg = start_deg + angle_deg
        end_deg = end_deg + angle_deg
    return {
        "cx": nx,
        "cy": ny,
        "radius": params["radius"],
        "start_deg": start_deg,
        "end_deg": end_deg,
    }


def _rotate_rectangle(
    params: Dict[str, float], cx: float, cy: float, angle_deg: float, rotate_copies: bool
) -> Dict[str, float]:
    if rotate_copies:
        nx, ny = rotate_point(params["x"], params["y"], cx, cy, angle_deg)
        turned = dict(params)
        turned["x"] = nx
        turned["y"] = ny
        turned["angle"] = kernel_sketch.rectangle_angle(params) + angle_deg
        return turned
    ecx, ecy = _center_rectangle(params)
    ncx, ncy = rotate_point(ecx, ecy, cx, cy, angle_deg)
    return _translate_rectangle(params, ncx - ecx, ncy - ecy)


def _rotate_bezier(
    params: Dict[str, float], cx: float, cy: float, angle_deg: float, rotate_copies: bool
) -> Dict[str, float]:
    """**ベジエはアフィン不変**なので、制御点を回すだけで曲線が厳密に回る。"""
    if rotate_copies:
        rotated = [rotate_point(px, py, cx, cy, angle_deg) for px, py in _bezier_points(params)]
        return _bezier_params(rotated)
    mx, my = _center_bezier(params)
    nmx, nmy = rotate_point(mx, my, cx, cy, angle_deg)
    return _translate_bezier(params, nmx - mx, nmy - my)


def _mirror_point(
    x: float, y: float, ax: float, ay: float, ux: float, uy: float
) -> Tuple[float, float]:
    """点 (x, y) を、点 (ax, ay) を通り単位方向 (ux, uy) の直線に対して鏡映する。

    `t = dot(Q-A, u)`; `foot = A + t*u`; `mirrored = 2*foot - Q`(モジュールのタスク仕様どおり)。
    """
    t = (x - ax) * ux + (y - ay) * uy
    foot_x = ax + t * ux
    foot_y = ay + t * uy
    return (2.0 * foot_x - x, 2.0 * foot_y - y)


def _mirror_line(
    params: Dict[str, float], ax: float, ay: float, ux: float, uy: float
) -> Dict[str, float]:
    x0, y0 = _mirror_point(params["x0"], params["y0"], ax, ay, ux, uy)
    x1, y1 = _mirror_point(params["x1"], params["y1"], ax, ay, ux, uy)
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1}


def _mirror_circle(
    params: Dict[str, float], ax: float, ay: float, ux: float, uy: float
) -> Dict[str, float]:
    cx, cy = _mirror_point(params["cx"], params["cy"], ax, ay, ux, uy)
    return {"cx": cx, "cy": cy, "radius": params["radius"]}


def _mirror_arc(
    params: Dict[str, float], ax: float, ay: float, ux: float, uy: float
) -> Dict[str, float]:
    """円弧の鏡映。**鏡映は向きを反転する**ので、始点・終点を入れ替えてから角度を映す
    (`new_end - new_start == end_deg - start_deg` を保つ。CCWの掃引量をそのまま保存する)。
    """
    cx, cy = _mirror_point(params["cx"], params["cy"], ax, ay, ux, uy)
    axis_angle_deg = math.degrees(math.atan2(uy, ux))
    start_deg = 2.0 * axis_angle_deg - params["end_deg"]
    end_deg = 2.0 * axis_angle_deg - params["start_deg"]
    return {
        "cx": cx,
        "cy": cy,
        "radius": params["radius"],
        "start_deg": start_deg,
        "end_deg": end_deg,
    }


def _mirror_rectangle(
    params: Dict[str, float], ax: float, ay: float, ux: float, uy: float
) -> Dict[str, float]:
    """長方形の鏡映。軸の角度を φ とすると向きは `2φ - angle` になる。

    **新しい基準の角は、元の「height 側の隣の角」の鏡像**である。鏡映は向きを裏返すので、
    元の基準の角をそのまま基準にすると `width`/`height` が負の側へ伸びる形になり、
    正の幅高さで同じ4隅を張れない。
    """
    corners = kernel_sketch.rectangle_corners(params)
    bx, by = _mirror_point(corners[3][0], corners[3][1], ax, ay, ux, uy)
    axis_angle_deg = math.degrees(math.atan2(uy, ux))
    mirrored = dict(params)
    mirrored["x"] = bx
    mirrored["y"] = by
    mirrored["angle"] = 2.0 * axis_angle_deg - kernel_sketch.rectangle_angle(params)
    return mirrored


def _mirror_bezier(
    params: Dict[str, float], ax: float, ay: float, ux: float, uy: float
) -> Dict[str, float]:
    """**ベジエはアフィン不変**なので、制御点を鏡映するだけで曲線が厳密に鏡映される
    (`_rotate_bezier` と同じ理由)。"""
    mirrored = [_mirror_point(px, py, ax, ay, ux, uy) for px, py in _bezier_points(params)]
    return _bezier_params(mirrored)


def _center_line(params: Dict[str, float]) -> Tuple[float, float]:
    return ((params["x0"] + params["x1"]) / 2.0, (params["y0"] + params["y1"]) / 2.0)


def _center_cxcy(params: Dict[str, float]) -> Tuple[float, float]:
    return (float(params["cx"]), float(params["cy"]))


def _center_rectangle(params: Dict[str, float]) -> Tuple[float, float]:
    corners = kernel_sketch.rectangle_corners(params)
    return (
        (corners[0][0] + corners[2][0]) / 2.0,
        (corners[0][1] + corners[2][1]) / 2.0,
    )


def _center_bezier(params: Dict[str, float]) -> Tuple[float, float]:
    """**制御点4つの重心**。曲線上の点ではないが、平行移動・回転の基準として一貫していれば
    足りる(`_center_line` が線分の中点を返すのと同じ役割)。"""
    points = _bezier_points(params)
    return (
        sum(point[0] for point in points) / 4.0,
        sum(point[1] for point in points) / 4.0,
    )


#: 3次ベジエの `params` のキー(**始点・制御点1・制御点2・終点の順**)。正は `kernel_sketch.BEZIER_POINT_KEYS`。
BEZIER_KEYS = tuple(key for pair in kernel_sketch.BEZIER_POINT_KEYS for key in pair)


def _bezier_points(params: Dict[str, float]) -> List[Tuple[float, float]]:
    """`params` を制御点4つの (x, y) へ。"""
    return [(float(params[key_x]), float(params[key_y])) for key_x, key_y in kernel_sketch.BEZIER_POINT_KEYS]


def _bezier_params(points: List[Tuple[float, float]]) -> Dict[str, float]:
    """制御点4つの (x, y) を `params` へ(`_bezier_points` の逆)。"""
    flat = [value for point in points for value in point]
    return {key: float(value) for key, value in zip(BEZIER_KEYS, flat)}


@dataclass(frozen=True)
class EntityKindOps:
    """図形1種別ぶんの純幾何演算(OCPなし)。**表の1行 = その種別の全処理**。

    **`translate` / `rotate` / `mirror` はどれも同じ種別に閉じる** —— 戻すのはその種別の
    `params` 1つで、種別は呼び出し側が持っているものから変わらない。

    `points` は**動かせる点**の (xキー, yキー) の組(`move_sketch_entities` の「点」の定義)。
    寸法(半径・幅高さ・角度)は点ではないので入れない —— 点を動かしても形は変わらず、
    寸法を動かすと形が変わる(寸法の編集は今回スコープ外)。長方形は基準の角1つだけで、
    残り3隅は `width`/`height`/`angle` から**導出される値**なので独立に動かせない。
    """

    validate: Callable[[Dict[str, float]], None]
    translate: Callable[[Dict[str, float], float, float], Dict[str, float]]
    rotate: Callable[[Dict[str, float], float, float, float, bool], Dict[str, float]]
    center_2d: Callable[[Dict[str, float]], Tuple[float, float]]
    #: 軸(点+単位方向)に対する鏡映。
    mirror: Callable[[Dict[str, float], float, float, float, float], Dict[str, float]]
    #: 動かせる点の (xキー, yキー)。**xキーがその点の名前**(選択の参照が持つ値)。
    points: Tuple[Tuple[str, str], ...] = ()


_ENTITY_KIND_TABLE: Dict[str, EntityKindOps] = {
    "line": EntityKindOps(
        _validate_line, _translate_line, _rotate_line, _center_line, _mirror_line,
        points=(("x0", "y0"), ("x1", "y1")),
    ),
    "circle": EntityKindOps(
        _validate_circle, _translate_cxcy, _rotate_circle, _center_cxcy, _mirror_circle,
        points=(("cx", "cy"),),
    ),
    "rectangle": EntityKindOps(
        _validate_rectangle, _translate_rectangle, _rotate_rectangle, _center_rectangle,
        _mirror_rectangle,
        points=(("x", "y"),),
    ),
    "arc": EntityKindOps(
        _validate_arc, _translate_cxcy, _rotate_arc, _center_cxcy, _mirror_arc,
        points=(("cx", "cy"),),
    ),
    "bezier": EntityKindOps(
        _validate_bezier, _translate_bezier, _rotate_bezier, _center_bezier, _mirror_bezier,
        points=kernel_sketch.BEZIER_POINT_KEYS,
    ),
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


def entity_point_keys(kind: str) -> Tuple[Tuple[str, str], ...]:
    """その種別が持つ**動かせる点**の (xキー, yキー) の組。表に無い種別は ValueError。

    `move_sketch_entities` の選択参照は「xキー」で点を名指しする(`{sketch, entity, point}`)。
    y は必ずこの表から引く —— 参照に2つ書くと、片方だけ書き換わった参照が作れてしまう。
    """
    return entity_kind_ops(kind).points


def entity_point_pair(kind: str, point: str) -> Tuple[str, str]:
    """点の名前(xキー)から (xキー, yキー) を引く。その種別に無い名前は ValueError。"""
    for x_key, y_key in entity_point_keys(kind):
        if x_key == point:
            return (x_key, y_key)
    raise ValueError(
        f"{kind} has no movable point {point!r} "
        f"(one of {[x for x, _ in entity_point_keys(kind)]})"
    )


def get_entity(sketch: "Sketch", entity_id: EntityId) -> "Entity":
    """スケッチの図形をIDで引く。無ければ KeyError(黙って None を返さない)。"""
    for entity in sketch.entities:
        if entity.id == entity_id:
            return entity
    raise KeyError(
        unknown_id_message(
            "entity",
            entity_id,
            [e.id for e in sketch.entities],
            where=f"sketch {sketch.id!r}",
        )
    )


VERB_SKETCH_CREATE = "sketch_create"
VERB_SKETCH_CREATE_OFFSET = "sketch_create_offset"
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

    **これは `sketch_create` へ渡す引数の形**であって、スケッチが持つ形ではない。
    `resolve_plane` を通すと解決済みの姿勢(`frame`)が足され、以後**幾何の正はその枠**になる
    (`body`/`index` は「元はどのボディのどの面か」の由来表示に残るだけで、引き直しには
    使わない)。面の添字は形状変更で静かにずれるため。
    """
    return {
        "kind": "face",
        "body": body_id,
        "index": int(face_index),
        "provenance": None,
    }


def offset_plane(base: Any, distance: float) -> dict:
    """基準平面(base)から法線方向に `distance` だけ離れたオフセット平面への参照。

    `{"kind": "offset", "base": <base|face のPlaneRef>, "distance": float, "provenance": None}`。
    `base_plane`/`face_plane` と同じく**これは `sketch_create` へ渡す引数の形**で、姿勢は
    `resolve_plane` が焼き付ける。**`base` は `resolve_plane` にそのまま渡せる形**なら何でもよい
    (基準平面名の文字列 `"XY"` でも `base_plane(...)`/`face_plane(...)` の dict でもよく、
    ここでは正規化しない —— 正規化は `resolve_plane` 側の再帰呼び出しが1箇所で行う)。

    **`base` は "base" か "face" だけ**(offsetのoffsetは作れない。`resolve_plane` が弾く)。
    寸法を積み重ねたいだけなら `distance` を1つの値にまとめれば済み、多段の入れ子は
    「どの段がどの向きか」を人が覚える負担を増やすだけで得るものが無い。
    """
    return {
        "kind": "offset",
        "base": base,
        "distance": float(distance),
        "provenance": None,
    }


def plane_log_args(plane_ref: dict) -> dict:
    """PlaneRef から**操作ログへ載せる形**(解決済みの姿勢を落とした素の参照)を作る。

    **`frame` はログへ入れない**。ログは唯一の正であり、そこへ解決結果を焼き込むと
    `set_parameter` の `rebuild()`(履歴を最初から再生し直す)で古い姿勢が復活し、
    「スケッチ作成**より前**のパラメータ編集には追随する」が壊れる。姿勢は再生のたびに
    `_do_sketch_create` がその履歴時点の形状から解決し直す(`resolve_plane`)。
    """
    return {key: value for key, value in plane_ref.items() if key != "frame"}


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
        rectangle : x, y, width, height, angle           (度・反時計回り。基準の角 (x,y) まわり)
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
    #: 表示/非表示。`Body.visible` と同じ扱い —— 選択機構の一部で、**操作ログに載らず
    #: 保存もされず undo でも戻らない**(切り替えは `duocad.selection.set_sketch_visible`)。
    #: 開き直した文書は全スケッチが可視で始まる。
    visible: bool = True


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
def sketch_create(doc: Document, plane: Any, *, verb: str = VERB_SKETCH_CREATE) -> SketchId:
    """スケッチを作って SketchId を返す。

    面を指定する場合、その面が**平面でなければ ValueError**(円筒面などに貼らせない ——
    黙って通すと以後の領域検出が意味を失う)。失敗時は `doc.sketches` にもログにも残さない。

    **`verb` はログへ積む名前だけを差し替える**(`_add_entity` が3つのadd_*で共有している
    のと同じ形)。`sketch_create_offset`(オフセット平面に貼る経路)は貼る先の解決が
    `offset_plane()` で作った `plane` に変わるだけで、do/undoの実体もこの関数もそのまま
    共有する —— UI側のEnter要否は verb 名ごとに1つ(`tests/test_ui_command_wiring.py`)なので、
    `base_plane`/`distance` を同じ `sketch_create` に足すと即適用だった既存の挙動まで
    Enter必須に変わってしまう。それを避けるための別名である。
    """
    plane_ref = resolve_plane(doc, plane)
    sketch_id = f"sketch-{uuid4().hex[:12]}"
    # モデルとログには別のdictを渡す(共有すると、貼った面の由来スロットを後から埋めた
    # 瞬間に、唯一の正であるログが黙って書き換わる)。**モデルは解決済みの姿勢つき、
    # ログは素の参照だけ**(`plane_log_args`)。
    doc.sketches.append(Sketch(id=sketch_id, plane=dict(plane_ref)))
    doc.log.append(
        verb,
        {"sketch_id": sketch_id, "plane": plane_log_args(plane_ref)},
    )
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
    doc: Document,
    sketch_id: SketchId,
    corner: Any,
    width: float,
    height: float,
    angle: float = 0.0,
) -> EntityId:
    """`corner` を基準の角とする幅 `width`・高さ `height` の長方形を足して EntityId を返す。

    `angle`(度・反時計回り)は基準の角まわりの傾き。**`angle` は params の最後に置くこと**
    —— `_add_entity` はこの順に `register_value` を呼ぶので、順序を変えると同じログを
    再生したときに `d<n>` の割り当てが変わる。
    """
    params = {
        "x": float(corner[0]),
        "y": float(corner[1]),
        "width": float(width),
        "height": float(height),
        "angle": float(angle),
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

    **0度をまたいでよい**(例: `start_deg=350, end_deg=10` は20度の円弧)。掃引量は
    `sweep = (end_deg - start_deg) % 360` で解決される(`kernel_sketch` 側の実装)ので、常に
    `start_deg` から反時計回りに `end_deg` まで進む弧として扱われる。
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
            return parameters.resolve_all(doc, entity.params)
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

    **面参照・offset参照はここで姿勢まで解決して `frame` に焼き付ける**
    (`kernel_sketch.face_frame` / `kernel_sketch.offset_plane_frame`)。これが「姿勢をライブに引き直さない」
    の唯一の入口であり、解決を2箇所に持つと平面かどうかの検査も焼き付けも片方だけ抜ける。
    offset参照は `base` を再帰的にこの関数で解決してから平行移動するので、base側が面参照
    であれば面の平面性検査もそのままここを通る。

    **`doc` を渡すのは「いまこの瞬間の形状」から解決するため**である。新規作成では
    `sketch_create` が、再生では `_do_sketch_create` が呼ぶので、`rebuild()` はその履歴時点の
    形状に対して姿勢を解き直す(＝スケッチ作成**より前**のパラメータ編集には追随し、
    **より後**の面取り・フィレットには一切動かされない)。
    """
    if isinstance(plane, str):
        return base_plane(plane)
    if isinstance(plane, dict) and plane.get("kind") == "base":
        return base_plane(plane["name"])
    if isinstance(plane, dict) and plane.get("kind") == "face":
        body_id = plane["body"]
        face_index = int(plane["index"])
        body = doc.get_body(body_id)
        if body.mesh is not None:
            # **名指しの面参照なので断る**(断り文は `meshbody.reject` へ一元化)。
            from . import meshbody

            meshbody.reject(body, "スケッチを貼る面にする")
        if body.shape is None:
            raise ValueError(f"body {body_id!r} has no shape")
        return {
            "kind": "face",
            "body": body_id,
            "index": face_index,
            "provenance": plane.get("provenance"),
            "frame": kernel_sketch.face_frame(body.shape, face_index),
        }
    if isinstance(plane, dict) and plane.get("kind") == "offset":
        base_ref = resolve_plane(doc, plane["base"])
        if base_ref.get("kind") == "offset":
            # **多段のoffsetは作らせない**(`offset_plane` のdocstring参照)。ここで弾かないと
            # 「基準からの合計距離」が入れ子の深さぶん暗黙に積み上がり、`distance` 1つを見ても
            # 実際の位置が分からなくなる。
            raise ValueError("offset plane base must not itself be an offset plane")
        distance = float(plane["distance"])
        frame = kernel_sketch.offset_plane_frame(kernel_sketch.sketch_plane_frame(base_ref), distance)
        return {
            "kind": "offset",
            "base": {key: value for key, value in base_ref.items() if key != "frame"},
            "distance": distance,
            "provenance": plane.get("provenance"),
            "frame": frame,
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

    **`entity_id` もログの `args` に記録する**(`uuid4` 由来で非決定的なため。記録が無いと
    再生のたびに別のIDになり、保存した選択集合が指す先を失う)。
    """
    require_valid_params(kind, params)
    sketch = get_sketch(doc, sketch_id)
    entity_id = new_entity_id()
    refs = parameters.register_all(doc, params)
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

    **平面は `resolve_plane` で解き直す**。ログが持つのは素の参照(body/index)だけで、姿勢は
    「この操作を適用するその瞬間の形状」から取る。`rebuild()` は履歴を頭から再生するので、
    ここで解くと**スケッチより前**のパラメータ編集は反映され、**スケッチより後**の面取り・
    フィレットはまだ適用されていない —— それが「作成時点で固定する」の実体である。
    """
    args = operation.args
    sketch_id = args["sketch_id"]
    if any(sketch.id == sketch_id for sketch in document.sketches):
        raise ValueError(f"sketch id already exists: {sketch_id!r}")
    plane_ref = resolve_plane(document, args["plane"])
    document.sketches.append(Sketch(id=sketch_id, plane=dict(plane_ref)))


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


def add_entities(document: Document, planned: Sequence[tuple]) -> List[EntityId]:
    """図形を足す。`planned` は `(Sketch, EntityId, 種別, 解決済みの params)` の並びで、作った ID の並びを返す。

    **全部の ID を先に検査してから**(既存との重複と、並びの中での重複の両方)、1件ずつ名前つきの値を
    登録(`parameters.register_all`。キーの並びが `d<n>` の振り順)して足す —— 足しながら検査すると、
    2件目で重複したときに1件目だけ書いた文書が残る。
    """
    seen: set = set()
    for sketch, entity_id, _kind, _values in planned:
        if entity_id in seen or any(existing.id == entity_id for existing in sketch.entities):
            raise ValueError(f"entity id already exists: {entity_id!r}")
        seen.add(entity_id)
    created: List[EntityId] = []
    for sketch, entity_id, kind, values in planned:
        refs = parameters.register_all(document, dict(values))
        sketch.entities.append(Entity(id=entity_id, kind=kind, params=refs))
        created.append(entity_id)
    return created


def remove_entities(document: Document, entity_ids, *, sketch_id: Optional[SketchId] = None) -> None:
    """図形を取り除き、名前つきの値を手放す(`parameters.release_param_refs`)。

    **手放すのを忘れると leak する** —— 値が誰にも参照されないまま文書に残り、`d1 d2 …` の連番が
    飛ぶ(=保存して再生したときに `set_parameter` の記録した名前が別の値を指す)。素の数値のまま持つ
    図形(`import_svg` のベジエ)は手放すものが無いので、そのまま通る。
    `sketch_id` があればそのスケッチだけ(無ければ KeyError)、無ければ全スケッチを走査する。
    """
    wanted = set(entity_ids)
    sketches = document.sketches if sketch_id is None else [get_sketch(document, sketch_id)]
    for sketch in sketches:
        remaining = []
        for entity in sketch.entities:
            if entity.id in wanted:
                parameters.release_param_refs(document, entity.params)
            else:
                remaining.append(entity)
        sketch.entities = remaining


def _do_add_entity(document: Document, operation: Operation) -> None:
    """3つの図形追加verbで共有する再適用(記録された種別とparamsで図形を作り直す)。

    ログの `params` は素の数値。再生時に登録し直し、Entity には参照を入れる。
    """
    args = operation.args
    sketch = get_sketch(document, args["sketch_id"])
    add_entities(document, [(sketch, args["entity_id"], args["kind"], dict(args["params"]))])


def _undo_add_entity(document: Document, operation: Operation) -> None:
    args = operation.args
    remove_entities(document, [args["entity_id"]], sketch_id=args["sketch_id"])


register_verb(VERB_SKETCH_CREATE, do=_do_sketch_create, undo=_undo_sketch_create)
# `sketch_create_offset` は貼る先の解決が違うだけで、do/undoの実体は `plane["kind"]` を
# 見ない(`sketch_create` のdocstring参照)。3つのadd_*と同じ「別名・同じ実体」の形。
register_verb(VERB_SKETCH_CREATE_OFFSET, do=_do_sketch_create, undo=_undo_sketch_create)
for _verb in (VERB_ADD_LINE, VERB_ADD_CIRCLE, VERB_ADD_RECTANGLE, VERB_ADD_ARC):
    # 3つとも「スケッチへ図形を1つ足す」で、do/undo の実体は args["kind"] で分かれない
    # (追加する図形の種別は args が持つ)。verb名を分けているのは操作ログとMCPの粒度のため。
    register_verb(_verb, do=_do_add_entity, undo=_undo_add_entity)
del _verb
