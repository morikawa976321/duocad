"""選択を測る —— 各項目の寸法と、**ちょうど2項目のあいだ**の距離/角度。

verb ではない(文書もログも触らない)。MCP の `measure` と WebSocket の `measure` の両方が
`measure_selection` を呼ぶので、AI が読む値と画面に出る値は同じ計算から出る。

## 2項目のあいだ(`between`)

ソリッドの頂点・直線の辺・平らな面と、スケッチの点(点の図形・端点・中心・制御点)・線分・領域を、点・直線・平面の
3種に還元して測る(スケッチは `region.plane_frame` で世界座標へ。issue #201)。**直線と平面は無限に延ばす**(有限の辺・面
どうしの最短距離ではない。平行な面どうしは重なっていなくても「面の間隔」が答え。issue #10)。スケッチの線分も辺と同じく
無限直線で、領域はそのスケッチの平面。

| 組 | 返すもの |
|---|---|
| 点-点 | 距離と Δx/Δy/Δz |
| 点-直線 / 点-平面 | 垂線の長さ(足が `to`) |
| 直線-直線 / 直線-平面 / 平面-平面 | 平行なら距離、平行でなければ**角度**(0〜90度。距離は無い) |

`from` / `to` は画面が線で描くための2点で、直線側は辺(線分)の中点・平面側は面(領域)の面積重心を起点にする。

平行の判定は `PARALLEL_SIN_TOLERANCE`(sin θ)。述語の `PARALLEL_TOLERANCE`(1e-3 ≈ 2.6度)は
粗すぎ、測定に使うと 2度傾いた面どうしの「距離」が出てしまう。

OCP / build123d を import しない(幾何は `kernel` / `query` から数値で受け取る)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Union

from . import kernel_sketch, meshbody, query, region, selection, sketch

from . import (
    brep_geometry as _brep_geometry,
)

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

#: 組の名前を種別の順で正規化するための順位(選択の順序に依らない)。
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


def _to_uv(frame: dict, point: Any) -> list:
    """ワールド座標の点をスケッチ座標 (u, v) へ(`sketch_create` が返す `frame` の逆: 3D = origin + u·u軸 + v·v軸)。"""
    offset = _sub(_as_vec(point), _as_vec(frame["origin"]))
    return [_dot(offset, _as_vec(frame["u"])), _dot(offset, _as_vec(frame["v"]))]


def _to_world(frame: dict, uv: Any) -> Vec:
    """スケッチ座標 (u, v) をワールド座標へ(`_to_uv` の逆)。"""
    origin = _as_vec(frame["origin"])
    return _add(_add(origin, _scale(_as_vec(frame["u"]), float(uv[0]))), _scale(_as_vec(frame["v"]), float(uv[1])))


def _body_centroid(body: Any) -> Union[list, None]:
    """ボディの体積重心(ワールド座標 [mm])。体積が0なら None。"""
    try:
        if body.mesh is not None:
            return meshbody.centroid(body.mesh)
        return [float(value) for value in _brep_geometry.centroid(body.shape)]
    except ValueError:
        return None


def _angle_deg(sin_value: float, cos_value: float) -> float:
    """sin と |cos| から 0〜90度の角(`atan2` なので 0 と 90 の近くでも桁落ちしない)。"""
    return math.degrees(math.atan2(min(1.0, sin_value), min(1.0, abs(cos_value))))


def _result(kind: str, **fields: Any) -> dict:
    """`between` の1件。キーの顔ぶれは組によらず同じ(無い値は None)。"""
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

    値は問い合わせ層のものをそのまま渡す(`query.describe_face` / `describe_edge` / `region.regions`。測り直すと述語と食い違いうる)。
    """
    if kind == "face":
        body = session.document.get_body(ref["body"])
        described = query.describe_face(body.shape, int(ref["index"]))
        item = {
            "kind": "face",
            "body": ref["body"],
            "index": ref["index"],
            "surface_kind": described["kind"],
            "area": described["area"],
            "normal": described["normal"],
            # `describe_faces` の `center`(曲面では UV 範囲の中点)ではなく面積重心を渡す。AI が重心の位置決めに読む値。
            "center": query.face_centroid(body.shape, int(ref["index"])),
        }
        if described["kind"] != "plane":
            return item, (
                f"面 {ref['index']} は平面ではない({described['kind']})。平面として測れるのは平らな面だけ"
            )
        centroid, normal = _brep_geometry.face_plane(body.shape, int(ref["index"]))
        return item, Plane(point=_as_vec(centroid), normal=_unit(_as_vec(normal)))

    if kind == "edge":
        body = session.document.get_body(ref["body"])
        described = query.describe_edge(body.shape, int(ref["index"]))
        item = {
            "kind": "edge",
            "body": ref["body"],
            "index": ref["index"],
            "length": described["length"],
            "start": described["start"],
            "end": described["end"],
            "edge_kind": described["kind"],
        }
        if described["kind"] == "circle":
            item["center"], item["radius"] = _brep_geometry.edge_circle(body.shape, int(ref["index"]))
        if described["kind"] != "line":
            return item, (
                f"辺 {ref['index']} は直線ではない({described['kind']})。直線として測れるのはまっすぐな辺だけ"
            )
        _, direction = _brep_geometry.edge_axis(body.shape, int(ref["index"]))
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
        center = [float(value) for value in described.centroid]
        frame = region.plane_frame(session.document, sketch_id)
        item = {
            "kind": "region",
            "sketch": sketch_id,
            "index": ref["index"],
            "area": described.area,
            "center": center,
            "center_uv": _to_uv(frame, center),
        }
        return item, Plane(point=_as_vec(center), normal=_unit(_as_vec(frame["normal"])))

    if kind == selection.BODY_KIND:
        # ソリッドを丸ごと指す種別(形状最適化の保持/除外)。面積にも長さにも足さない。
        # 値は `body_meta` から取る(体積はキャッシュ済み)。
        body = session.document.get_body(ref["body"])
        meta = session.body_meta(body)
        item = {
            "kind": selection.BODY_KIND,
            "body": ref["body"],
            "name": meta["name"],
            "volume": meta["volume"],
            "face_count": meta["face_count"],
            "center": _body_centroid(body),
        }
        return item, "ボディそのものは2項目の測定に使えない(頂点・辺・面を指す)"

    if kind == selection.SKETCH_ENTITY_KIND:
        # スケッチの図形。点なら (u, v) を、辺なら解決済みの `params` をそのまま返す(線分は長さ、楕円は周長も `length` に)。
        # 2項目の測定に使う点・線分は世界座標(`world` / `start` / `end`)も返す。
        sketch_id = ref["sketch"]
        entity_id = ref["entity"]
        target = sketch.get_entity(sketch.get_sketch(session.document, sketch_id), entity_id)
        resolved = sketch.entity_params(session.document, sketch_id, entity_id)
        frame = region.plane_frame(session.document, sketch_id)
        item = {
            "kind": selection.SKETCH_ENTITY_KIND,
            "sketch": sketch_id,
            "entity": entity_id,
            "entity_kind": target.kind,
            "point": ref.get("point"),
        }
        if ref.get("point") is not None:
            x_key, y_key = sketch.entity_point_pair(target.kind, str(ref["point"]))
            item["position"] = [resolved[x_key], resolved[y_key]]
            world = _to_world(frame, item["position"])
            item["world"] = list(world)
            return item, Point(point=world)
        item["params"] = resolved
        if target.kind == "ellipse":
            item["length"] = kernel_sketch.ellipse_perimeter(resolved)
        if target.kind != "line":
            return item, (
                f"スケッチの図形まるごとで測れるのは線分だけ(選んだのは {target.kind})。"
                "中心や端点は点として指せる"
            )
        item["length"] = math.hypot(resolved["x1"] - resolved["x0"], resolved["y1"] - resolved["y0"])
        start = _to_world(frame, (resolved["x0"], resolved["y0"]))
        end = _to_world(frame, (resolved["x1"], resolved["y1"]))
        item["start"], item["end"] = list(start), list(end)
        return item, Line(point=_scale(_add(start, end), 0.5), direction=_unit(_sub(end, start)))

    raise ValueError(f"unmeasurable selection kind: {kind!r}")


def inspect_selection(session: Any) -> Union[dict, None]:
    """選択が**ちょうど1項目**ならその項目(`measure_selection` の `items[0]` と同じ形)、それ以外は None。

    画面の選択情報の表示(issue #140)が選択の変わるたびに呼ぶので、2項目以上では何も測らない。
    """
    current = selection.get_selection(session.document)
    if len(current.items) != 1:
        return None
    ref = current.items[0]
    item, _ = _measure_item(session, ref, selection.ref_kind(ref))
    return item


def measure_selection(session: Any) -> dict:
    """いまの選択を測る。

    戻り値は `{"count", "items", "total_area", "total_length"}`。**ちょうど2項目のときだけ**
    `between`(`between()`)と `between_error`(測れない理由。測れたなら None)が加わる。
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
