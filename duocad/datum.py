"""原点の7パーツ(原点・X/Y/Z軸・XY/XZ/YZ平面)を、ボディやスケッチの点・線・面と同じく指すための参照。

文書には持たない(どの文書にも常にある、動かない基準)。参照の形は

    DatumRef : {"datum": "origin"|"X"|"Y"|"Z"|"XY"|"XZ"|"YZ", "provenance": None}

で、選択集合にもピックの引数(軸・中心・鏡映面・収束点)にもそのまま入る。どの verb も、指された原点パーツを
点・直線・平面のどれかへ還元して使う(`point_of` / `line_of` / `plane_of`)。平面の向きは `sketch_create` の
基準平面と同じ枠から取る(鏡映・分割の `base_plane` と同じ側が正になる)。

スケッチの中で使うときは、そのスケッチ平面の座標(u, v)へ落とす: 点は平面へ垂直に落とし(回転の中心は法線方向の
直線なので、落とした点まわりの回転が原点を通る軸まわりの回転と同じ)、軸は**平面の上にあるものだけ**を通す
(回転体の軸を平面へ落とすと別の立体ができる)。
"""
from __future__ import annotations

import math
from typing import Any

#: 原点の点
ORIGIN = "origin"

#: 軸の名前 → 向き(どれも原点を通る)
AXES = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}

#: 平面の名前(`sketch.BASE_PLANES` と同じ綴り)
PLANES = ("XY", "XZ", "YZ")

NAMES = (ORIGIN,) + tuple(AXES) + PLANES

#: 種別(点・直線・平面)
POINT, LINE, PLANE = "point", "line", "plane"

#: 原点パーツがスケッチ平面の上にあるとみなす距離[mm]と sin θ
_ON_PLANE_MM = 1e-6
_ON_PLANE_SIN = 1e-9

Vec = tuple[float, float, float]


def is_ref(ref: Any) -> bool:
    """原点パーツの参照の形か(名前の正しさは見ない。見分けだけ)。"""
    return isinstance(ref, dict) and "datum" in ref


def name_of(ref: Any, *, what: str = "datum") -> str:
    """参照から名前を取る。知らない名前は ValueError。"""
    if not is_ref(ref):
        raise ValueError(f'{what} must be a datum reference like {{"datum": "X"}}, got {ref!r}')
    name = ref["datum"]
    if not isinstance(name, str) or name not in NAMES:
        raise ValueError(f"{what}: unknown datum {name!r} (one of {list(NAMES)})")
    return name


def make_ref(name: str) -> dict:
    """選択集合へ入れる形(`selection._coerce_ref` が返す正規形)。"""
    if name not in NAMES:
        raise ValueError(f"unknown datum {name!r} (one of {list(NAMES)})")
    return {"datum": name, "provenance": None}


def kind_of(name: str) -> str:
    """点・直線・平面のどれか。"""
    if name == ORIGIN:
        return POINT
    return LINE if name in AXES else PLANE


def point_of(ref: Any, *, what: str) -> Vec:
    """点として指された原点パーツ(原点だけ)の座標。"""
    name = name_of(ref, what=what)
    if name != ORIGIN:
        raise ValueError(f"{what}: {name} is not a point (点として指せる原点パーツは origin だけ)")
    return (0.0, 0.0, 0.0)


def line_of(ref: Any, *, what: str) -> tuple[Vec, Vec]:
    """直線として指された原点パーツ(X/Y/Z軸)の `(原点, 単位方向)`。"""
    name = name_of(ref, what=what)
    if name not in AXES:
        raise ValueError(f"{what}: {name} is not an axis (直線として指せる原点パーツは X / Y / Z)")
    return (0.0, 0.0, 0.0), AXES[name]


def plane_name_of(ref: Any, *, what: str) -> str:
    """平面として指された原点パーツ(XY/XZ/YZ)の名前(`sketch.base_plane` へそのまま渡せる)。"""
    name = name_of(ref, what=what)
    if name not in PLANES:
        raise ValueError(f"{what}: {name} is not a plane (平面として指せる原点パーツは XY / XZ / YZ)")
    return name


def plane_frame(name: str) -> dict:
    """平面の枠 `{origin, u, v, normal}`(`sketch_create` の基準平面と同じ)。"""
    from . import kernel_sketch, sketch

    return kernel_sketch.sketch_plane_frame(sketch.base_plane(name))


def describe(name: str) -> dict:
    """測定・選択の表示に出す1項目(`measure._measure_item` の形)。"""
    kind = kind_of(name)
    item: dict = {"kind": "datum", "datum": name, "datum_kind": kind}
    if kind == POINT:
        item["point"] = [0.0, 0.0, 0.0]
    elif kind == LINE:
        item["point"], item["direction"] = [0.0, 0.0, 0.0], list(AXES[name])
    else:
        frame = plane_frame(name)
        item["point"], item["normal"] = [0.0, 0.0, 0.0], [float(value) for value in frame["normal"]]
    return item


# --------------------------------------------------------------------------------------
# スケッチ平面の座標へ
# --------------------------------------------------------------------------------------
def _dot(a: Any, b: Any) -> float:
    return float(a[0]) * float(b[0]) + float(a[1]) * float(b[1]) + float(a[2]) * float(b[2])


def _uv(frame: dict, point: Vec) -> tuple[float, float]:
    origin = frame["origin"]
    offset = (point[0] - float(origin[0]), point[1] - float(origin[1]), point[2] - float(origin[2]))
    return (_dot(offset, frame["u"]), _dot(offset, frame["v"]))


def point_in_sketch(frame: dict, ref: Any, *, what: str) -> tuple[float, float]:
    """原点をスケッチ平面へ垂直に落とした (u, v)。"""
    return _uv(frame, point_of(ref, what=what))


def line_in_sketch(frame: dict, ref: Any, *, what: str) -> tuple[tuple[float, float], tuple[float, float]]:
    """スケッチ平面の上にある軸の `((u, v), 単位方向)`。平面の上にない軸は ValueError。"""
    point, direction = line_of(ref, what=what)
    origin = frame["origin"]
    off_plane = abs(_dot([point[i] - float(origin[i]) for i in range(3)], frame["normal"]))
    tilt = abs(_dot(direction, frame["normal"]))
    if off_plane > _ON_PLANE_MM or tilt > _ON_PLANE_SIN:
        raise ValueError(
            f"{what}: {ref['datum']} 軸はこのスケッチの平面の上に無い(平面の上にある軸だけを使える。"
            f"平面までの距離 {off_plane:.6g} mm、向きと法線の内積 {tilt:.6g})"
        )
    du, dv = _dot(direction, frame["u"]), _dot(direction, frame["v"])
    length = math.hypot(du, dv)
    return _uv(frame, point), (du / length, dv / length)
