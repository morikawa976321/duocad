"""選択を測る —— 各項目の寸法と、**ちょうど2項目のあいだ**の距離/角度。

verb ではない(文書もログも触らない)。MCP の `measure` と WebSocket の `measure` の両方が
`measure_selection` を呼ぶので、AI が読む値と画面に出る値は同じ計算から出る。

## 2項目のあいだ(`between`)

頂点・直線の辺・平らな面を、点・直線・平面の3種に還元して測る。**直線と平面は無限に
延ばす** —— 有限の辺・面どうしの最短距離ではない(平行な面どうしは、面が重なっていなくても
「面の間隔」が答え。issue #10 の「平行な面同士の距離」はこれ)。

| 組 | 返すもの |
|---|---|
| 点-点 | 距離と Δx/Δy/Δz |
| 点-直線 / 点-平面 | 垂線の長さ(足が `to`) |
| 直線-直線 / 直線-平面 / 平面-平面 | 平行なら距離、平行でなければ**角度**(0〜90度。距離は無い) |

`from` / `to` は「どこからどこまでの距離か」を画面が線で描くための2点で、直線側は
辺の中点・平面側は面の面積重心を起点にする(どちらも指した実物の上にある)。

平行の判定は `PARALLEL_SIN_TOLERANCE`(単位ベクトルの外積の大きさ = sin θ)。述語の
`PARALLEL_TOLERANCE`(1e-3 ≈ 2.6度)は「だいたい平行な辺を拾う」ための粗さで、測定に
使うと 2度傾いた面どうしの「距離」が出てしまう。

OCP / build123d を import しない(幾何は `kernel` / `query` から数値で受け取る)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Union

from . import kernel, query, region, selection, sketch

#: 2本の単位ベクトルを「平行」とみなす sin θ の上限(1e-6 rad ≈ 0.00006度)。
PARALLEL_SIN_TOLERANCE = 1e-6

Vec = tuple[float, float, float]


@dataclass(frozen=True)
class Point:
    point: Vec


@dataclass(frozen=True)
class Line:
    """無限直線。`point` は辺の中点(描画の起点)、`direction` は単位ベクトル。"""

    point: Vec
    direction: Vec


@dataclass(frozen=True)
class Plane:
    """無限平面。`point` は面の面積重心(描画の起点)、`normal` は単位ベクトル。"""

    point: Vec
    normal: Vec


Measurable = Union[Point, Line, Plane]

#: 組の名前を「点-直線」のように**種別の順で正規化する**ための順位(選択の順序に依らない)。
_RANK = {Point: 0, Line: 1, Plane: 2}
_NAME = {Point: "point", Line: "line", Plane: "plane"}


# --------------------------------------------------------------------------------------
# ベクトルの道具(3成分のタプルだけ。numpy を持ち込むほどの量ではない)
# --------------------------------------------------------------------------------------
def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a: Vec, s: float) -> Vec:
    return (a[0] * s, a[1] * s, a[2] * s)


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec, b: Vec) -> Vec:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _norm(a: Vec) -> float:
    return math.sqrt(_dot(a, a))


def _unit(a: Vec) -> Vec:
    length = _norm(a)
    if length <= 0.0:
        raise ValueError("zero-length vector cannot be normalized")
    return _scale(a, 1.0 / length)


def _as_vec(values: Any) -> Vec:
    return (float(values[0]), float(values[1]), float(values[2]))


def _angle_deg(sin_value: float, cos_value: float) -> float:
    """sin と |cos| から 0〜90度の角(`atan2` なので 0 と 90 の近くでも桁落ちしない)。"""
    return math.degrees(math.atan2(min(1.0, sin_value), min(1.0, abs(cos_value))))


def _result(kind: str, **fields: Any) -> dict:
    """`between` の1件。**キーの顔ぶれは組によらず同じ**(無い値は None) ——
    AI も画面も、組ごとにキーの有無を覚えずに済む。"""
    base = {
        "kind": kind,
        "parallel": None,
        "distance": None,
        "angle_deg": None,
        "from": None,
        "to": None,
        "delta": None,
    }
    base.update(fields)
    return base


# --------------------------------------------------------------------------------------
# 2項目のあいだ
# --------------------------------------------------------------------------------------
def between(first: Measurable, second: Measurable) -> dict:
    """2つの幾何のあいだの距離(平行でない線・面どうしは角度)。組の並びは種別の順に正規化する。"""
    a, b = sorted((first, second), key=lambda item: _RANK[type(item)])  # 同順位なら選択の順のまま
    kind = f"{_NAME[type(a)]}-{_NAME[type(b)]}"

    if isinstance(a, Point) and isinstance(b, Point):
        delta = _sub(b.point, a.point)
        return _result(
            kind, distance=_norm(delta), delta=list(delta), **{"from": list(a.point), "to": list(b.point)}
        )

    if isinstance(a, Point) and isinstance(b, Line):
        along = _dot(_sub(a.point, b.point), b.direction)
        foot = _add(b.point, _scale(b.direction, along))
        return _result(kind, distance=_norm(_sub(a.point, foot)), **{"from": list(a.point), "to": list(foot)})

    if isinstance(a, Point) and isinstance(b, Plane):
        height = _dot(_sub(a.point, b.point), b.normal)
        foot = _sub(a.point, _scale(b.normal, height))
        return _result(kind, distance=abs(height), **{"from": list(a.point), "to": list(foot)})

    if isinstance(a, Line) and isinstance(b, Line):
        sin_value = _norm(_cross(a.direction, b.direction))
        if sin_value >= PARALLEL_SIN_TOLERANCE:
            return _result(
                kind, parallel=False, angle_deg=_angle_deg(sin_value, _dot(a.direction, b.direction))
            )
        along = _dot(_sub(a.point, b.point), b.direction)
        foot = _add(b.point, _scale(b.direction, along))
        return _result(
            kind,
            parallel=True,
            distance=_norm(_sub(a.point, foot)),
            **{"from": list(a.point), "to": list(foot)},
        )

    if isinstance(a, Line) and isinstance(b, Plane):
        # 直線と平面の角は「方向と法線の余角」なので、sin と cos が入れ替わる。
        sin_value = abs(_dot(a.direction, b.normal))
        if sin_value >= PARALLEL_SIN_TOLERANCE:
            cos_value = _norm(_cross(a.direction, b.normal))
            return _result(kind, parallel=False, angle_deg=_angle_deg(sin_value, cos_value))
        height = _dot(_sub(a.point, b.point), b.normal)
        foot = _sub(a.point, _scale(b.normal, height))
        return _result(kind, parallel=True, distance=abs(height), **{"from": list(a.point), "to": list(foot)})

    if isinstance(a, Plane) and isinstance(b, Plane):
        sin_value = _norm(_cross(a.normal, b.normal))
        if sin_value >= PARALLEL_SIN_TOLERANCE:
            return _result(kind, parallel=False, angle_deg=_angle_deg(sin_value, _dot(a.normal, b.normal)))
        height = _dot(_sub(b.point, a.point), a.normal)
        foot = _add(a.point, _scale(a.normal, height))
        return _result(kind, parallel=True, distance=abs(height), **{"from": list(a.point), "to": list(foot)})

    raise TypeError(f"unmeasurable pair: {type(first).__name__} / {type(second).__name__}")


# --------------------------------------------------------------------------------------
# 選択の各項目
# --------------------------------------------------------------------------------------
def _measure_item(session: Any, ref: dict, kind: str) -> tuple[dict, Union[Measurable, str]]:
    """1項目の寸法と、2項目測定に使う幾何(使えない種別ならその理由の文字列)。

    値は**問い合わせ層のもの**をそのまま渡す(`query.describe_faces` / `describe_edges` /
    `region.regions`)。測り直すと述語が使う値と食い違いうる。
    """
    if kind == "face":
        body = session.document.get_body(ref["body"])
        described = next(face for face in query.describe_faces(body.shape) if face["index"] == ref["index"])
        item = {
            "kind": "face",
            "body": ref["body"],
            "index": ref["index"],
            "area": described["area"],
            "normal": described["normal"],
            "center": described["center"],
        }
        if described["kind"] != "plane":
            return item, (
                f"面 {ref['index']} は平面ではない({described['kind']})。平面として測れるのは平らな面だけ"
            )
        centroid, normal = kernel.face_plane(body.shape, int(ref["index"]))
        return item, Plane(point=_as_vec(centroid), normal=_unit(_as_vec(normal)))

    if kind == "edge":
        body = session.document.get_body(ref["body"])
        described = next(edge for edge in query.describe_edges(body.shape) if edge["index"] == ref["index"])
        item = {
            "kind": "edge",
            "body": ref["body"],
            "index": ref["index"],
            "length": described["length"],
            "start": described["start"],
            "end": described["end"],
            "edge_kind": described["kind"],
        }
        if described["kind"] != "line":
            return item, (
                f"辺 {ref['index']} は直線ではない({described['kind']})。直線として測れるのはまっすぐな辺だけ"
            )
        _, direction = kernel.edge_axis(body.shape, int(ref["index"]))
        midpoint = _scale(_add(_as_vec(described["start"]), _as_vec(described["end"])), 0.5)
        return item, Line(point=midpoint, direction=_unit(_as_vec(direction)))

    if kind == "vertex":
        body = session.document.get_body(ref["body"])
        described = next(
            vertex for vertex in query.describe_vertices(body.shape) if vertex["index"] == ref["index"]
        )
        item = {"kind": "vertex", "body": ref["body"], "index": ref["index"], "point": described["point"]}
        return item, Point(point=_as_vec(described["point"]))

    if kind == selection.REGION_KIND:
        sketch_id = ref["sketch"]
        described = next(
            reg for reg in region.regions(session.document, sketch_id) if reg.index == ref["index"]
        )
        item = {"kind": "region", "sketch": sketch_id, "index": ref["index"], "area": described.area}
        return item, "領域は2項目の測定に使えない(測れるのはソリッドの頂点・直線の辺・平らな面)"

    if kind == selection.BODY_KIND:
        # **ソリッドを丸ごと指す種別**(形状最適化の保持/除外)。面積にも長さにも
        # 足さない —— ボディの「大きさ」は体積であって、面の面積の合計ではない。
        # 値は `body_meta` から取る(体積はキャッシュ済み。測り直さない)。
        body = session.document.get_body(ref["body"])
        meta = session.body_meta(body)
        item = {
            "kind": selection.BODY_KIND,
            "body": ref["body"],
            "name": meta["name"],
            "volume": meta["volume"],
            "face_count": meta["face_count"],
        }
        return item, "ボディそのものは2項目の測定に使えない(頂点・辺・面を指す)"

    if kind == selection.SKETCH_ENTITY_KIND:
        # **スケッチの図形**。点なら (u, v) を、辺(図形全体)なら解決済みの `params` を
        # そのまま返す —— `move_sketch_entities` の dx, dy を決めるのに要るのは
        # 「いまどこにあるか」だけである。
        sketch_id = ref["sketch"]
        entity_id = ref["entity"]
        target = sketch.get_entity(sketch.get_sketch(session.document, sketch_id), entity_id)
        resolved = sketch.entity_params(session.document, sketch_id, entity_id)
        item = {
            "kind": selection.SKETCH_ENTITY_KIND,
            "sketch": sketch_id,
            "entity": entity_id,
            "entity_kind": target.kind,
            "point": ref.get("point"),
        }
        if ref.get("point") is None:
            item["params"] = resolved
        else:
            x_key, y_key = sketch.entity_point_pair(target.kind, str(ref["point"]))
            item["position"] = [resolved[x_key], resolved[y_key]]
        return item, "スケッチの図形は2項目の測定に使えない(測れるのはソリッドの頂点・直線の辺・平らな面)"

    raise ValueError(f"unmeasurable selection kind: {kind!r}")


def measure_selection(session: Any) -> dict:
    """いまの選択(提案のままでよい)を測る。

    `confirmed_items` を通さないのは意図 —— AIが自分の提案を確かめてから確定する往復が
    ここで閉じる。確定済みに限ると、AIは中身を見ずに commit する動機を持ってしまう。

    戻り値は `{"count", "origin", "items", "total_area", "total_length"}`。**ちょうど2項目
    のときだけ** `between`(組の距離/角度。`between()`)と `between_error`(測れない理由。
    測れたなら None)が加わる —— 1項目や3項目のときに「2つではない」と言い続けるのは
    各項目の寸法を読みたい側には雑音でしかない。
    """
    current = selection.get_selection(session.document)
    items: list[dict] = []
    measurables: list[Union[Measurable, str]] = []
    total_area = 0.0
    total_length = 0.0
    for ref in current.items:
        kind = selection.ref_kind(ref)
        item, measurable = _measure_item(session, ref, kind)
        items.append(item)
        measurables.append(measurable)
        if kind in ("face", selection.REGION_KIND):
            total_area += item["area"]
        elif kind == "edge":
            total_length += item["length"]
    result: dict = {
        "count": len(items),
        "origin": current.origin,
        "items": items,
        "total_area": total_area,
        "total_length": total_length,
    }
    if len(items) == 2:
        reasons = [reason for reason in measurables if isinstance(reason, str)]
        if reasons:
            result["between"] = None
            result["between_error"] = reasons[0]
        else:
            first, second = measurables
            assert not isinstance(first, str) and not isinstance(second, str)
            result["between"] = between(first, second)
            result["between_error"] = None
    return result
