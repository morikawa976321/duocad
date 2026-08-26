"""SVGをスケッチの図形へ翻訳する。**OCP / build123d を直接importしない**(import境界)。

`import_svg` verb の材料をここが作る。返すのは `[{"kind":.., "params":{..}}, ...]` という素の
dictのリストで、`duocad.sketch` の `Entity` と同じ語彙・同じキーである。

## 決めごと(なぜこの形か)

**単位は `ppi=25.4` で解決する。** svgelementsは既定(96ppi)だと全長をpxへ正規化するので、
`width="100mm"` のSVGが377.95という数値で出てくる。25.4を渡すと2つの都合が同時に揃う ——
物理単位を持つSVGは**その寸法がそのままmm**になり(100mm → 100.0)、`viewBox` しか無いSVGは
**1ユーザー単位 = 1mm** で素通しになる。duocadは単位mm固定なので分岐が要らない。

**物理単位のSVGには 0.54ppm の系統的なずれがある**(実測)。svgelementsがmm→pxに
CSSの丸めた定数(3.7795296。厳密には 96/25.4 = 3.77952755…)を使うため、`width="100mm"` は
100.000054 になる。100mmで54ナノメートルなので実用上は無視してよいが、**取り込んだ寸法が
ぴったり一致しない**ことは知っておくこと(テストも近似で比べている)。

**Y軸を反転する。** SVGのYは下向き、スケッチのvは上向き。反転しないと上下が逆に入る。
反転は向きも裏返すので、**真円の弧は start/end を入れ替える**必要がある(中点の角度で判定)。

**曲線は曲線のまま渡す。** 3次ベジエ(C/S)は1:1、2次ベジエ(Q/T)は次数上げで**厳密に**3次へ。
楕円弧(rx≠ry)だけはベジエで厳密に表せないので90度以下に割って近似する
(**最大誤差は半径の0.027%**。半径50mmで13.6um)。折れ線には落とさない —— 落とすと
押し出した側面が線分の数だけ平面に割れる(`kernel._edges_bezier` のdocstring)。

**見た目は捨てる。** fill / stroke / 色 / text / image / clip-path はスケッチに意味が無い。
`<text>` を図形として取り込みたいなら、SVG側でアウトライン化してから読ませること。

失敗は黙って通さない: パス無し=FileNotFoundError / 壊れている=ValueError /
図形が1つも無い=ValueError / 多すぎる=ValueError(`MAX_ENTITIES`)。いずれもdoc/logは無傷。
"""
from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Any, List, Tuple, Union

PathLike = Union[str, Path]

#: 単位解決に渡すppi。25.4 = 1インチ25.4 → **ユーザー単位がmmで出る**(モジュールdocstring)。
MM_PER_INCH = 25.4

#: 1回の取り込みで作ってよい図形の上限。画像をトレースしたSVGは数万セグメントになりうる。
#: 黙って切り詰めず、**その場で断る**(切り詰めると欠けた輪郭で領域検出が別物を返す)。
MAX_ENTITIES = 5000

#: 真円とみなす rx/ry の相対差。これ以下なら `arc` に、超えたらベジエ近似に振る。
_CIRCULAR_TOLERANCE = 1.0e-9


def read_svg_entities(path: PathLike, scale: float = 1.0) -> Tuple[str, List[dict]]:
    """SVGを読み、`(source_name, entities)` を返す。`source_name` は**ファイル名だけ**。

    `entities` は `[{"kind":.., "params":{..}}, ...]`。`scale` は解決後のmmへさらに掛ける倍率で、
    寸法の合わないSVG(単位を持たず1ユーザー単位=1mmでは大きすぎる/小さすぎる図)を
    取り込むときだけ使う。
    """
    scale = float(scale)
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError(f"scale must be a positive finite number, got {scale!r}")

    data = Path(path).read_bytes()  # 無ければ FileNotFoundError
    source_name = Path(path).name
    svg = _parse(data, source_name)
    height = _height_of(svg)

    entities: List[dict] = []
    for element in _shapes(svg):
        for segment in _segments(element):
            entities.extend(_translate_segment(segment, height, scale))
            if len(entities) > MAX_ENTITIES:
                raise ValueError(
                    f"{source_name}: 図形が多すぎる(上限 {MAX_ENTITIES})。"
                    "画像をトレースしたSVGはこの経路では扱えない"
                    " — SVG側で単純化してから読ませること"
                )
    if not entities:
        raise ValueError(
            f"{source_name}: 取り込める図形が無い"
            "(text / image はスケッチに取り込めない。文字はアウトライン化してから読ませること)"
        )
    return source_name, entities


# --------------------------------------------------------------------------------------
# 解析(svgelements)
# --------------------------------------------------------------------------------------
def _parse(data: bytes, source_name: str) -> Any:
    """SVG本文をパースする。壊れていれば ValueError(パーサの例外をそのまま投げない)。"""
    from svgelements import SVG

    try:
        return SVG.parse(io.BytesIO(data), ppi=MM_PER_INCH)
    except Exception as error:  # パーサ側の例外種別は版で変わりうるので束ねる
        raise ValueError(
            f"{source_name}: SVGとして読めない: {type(error).__name__}: {error}"
        ) from error


def _height_of(svg: Any) -> float:
    """Y反転の基準にする高さ(mm)。取れなければ図形のbboxの上端で代用する。"""
    try:
        value = float(getattr(svg, "height", None))
    except (TypeError, ValueError):
        value = 0.0
    if math.isfinite(value) and value > 0:
        return value
    box = svg.bbox() if hasattr(svg, "bbox") else None
    return float(box[3]) if box else 0.0


def _shapes(svg: Any) -> List[Any]:
    """スケッチに取り込める要素(= 幾何を持つもの)だけを拾う。text / image はここで落ちる。"""
    from svgelements import Shape

    return [element for element in svg.elements() if isinstance(element, Shape)]


def _segments(element: Any) -> List[Any]:
    """1要素を Move / Line / Arc / CubicBezier / QuadraticBezier / Close の列へ。

    `transform` はsvgelementsが解決済みの座標で返すので、こちらで掛け直さない。
    """
    from svgelements import Path as SvgPath

    try:
        return list(SvgPath(element).segments())
    except Exception:
        return []  # 幾何に落とせない要素(空のpath等)は黙って飛ばす


# --------------------------------------------------------------------------------------
# 翻訳(1セグメント -> 0個以上の図形)
# --------------------------------------------------------------------------------------
def _translate_segment(segment: Any, height: float, scale: float) -> List[dict]:
    from svgelements import Arc, Close, CubicBezier, Line, Move, QuadraticBezier

    if isinstance(segment, Move):
        return []
    if isinstance(segment, (Line, Close)):  # Close は Line の一種
        return _line_entity(segment, height, scale)
    if isinstance(segment, QuadraticBezier):
        return [_quadratic_as_cubic(segment, height, scale)]
    if isinstance(segment, CubicBezier):
        return [
            _bezier_entity(
                _point(segment.start, height, scale),
                _point(segment.control1, height, scale),
                _point(segment.control2, height, scale),
                _point(segment.end, height, scale),
            )
        ]
    if isinstance(segment, Arc):
        return _arc_entities(segment, height, scale)
    return []  # 未知のセグメント種別は落とす(パーサの版が増やしても壊れない)


def _point(value: Any, height: float, scale: float) -> Tuple[float, float]:
    """SVGの点をスケッチの (u, v) へ。**ここだけがY反転と倍率を知っている**。"""
    return (float(value.x) * scale, (height - float(value.y)) * scale)


def _line_entity(segment: Any, height: float, scale: float) -> List[dict]:
    start = _point(segment.start, height, scale)
    end = _point(segment.end, height, scale)
    if math.dist(start, end) < 1.0e-9:
        return []  # 長さ0の線分は領域を割らない(`sketch._validate_line` が弾く値)
    return [
        {
            "kind": "line",
            "params": {"x0": start[0], "y0": start[1], "x1": end[0], "y1": end[1]},
        }
    ]


def _bezier_entity(
    start: Tuple[float, float],
    control1: Tuple[float, float],
    control2: Tuple[float, float],
    end: Tuple[float, float],
) -> dict:
    return {
        "kind": "bezier",
        "params": {
            "x0": start[0],
            "y0": start[1],
            "cx1": control1[0],
            "cy1": control1[1],
            "cx2": control2[0],
            "cy2": control2[1],
            "x1": end[0],
            "y1": end[1],
        },
    }


def _quadratic_as_cubic(segment: Any, height: float, scale: float) -> dict:
    """2次ベジエを3次へ。**次数上げは厳密**で、近似ではない。

        c1 = p0 + 2/3 (q - p0),  c2 = p1 + 2/3 (q - p1)
    """
    start = _point(segment.start, height, scale)
    control = _point(segment.control, height, scale)
    end = _point(segment.end, height, scale)

    def lifted(anchor: Tuple[float, float]) -> Tuple[float, float]:
        return (
            anchor[0] + 2.0 / 3.0 * (control[0] - anchor[0]),
            anchor[1] + 2.0 / 3.0 * (control[1] - anchor[1]),
        )

    return _bezier_entity(start, lifted(start), lifted(end), end)


def _arc_entities(segment: Any, height: float, scale: float) -> List[dict]:
    """円弧。真円なら `arc`、楕円ならベジエ近似(90度以下に割る)。"""
    radius_x = abs(float(segment.rx))
    radius_y = abs(float(segment.ry))
    if radius_x <= 0 or radius_y <= 0:
        return []
    if abs(radius_x - radius_y) <= _CIRCULAR_TOLERANCE * max(radius_x, radius_y):
        return _circular_arc_entity(segment, height, scale, radius_x)
    return _elliptical_arc_entities(segment, height, scale)


def _circular_arc_entity(
    segment: Any, height: float, scale: float, radius: float
) -> List[dict]:
    """真円の弧を `arc`(中心・半径・開始角・終了角)へ。

    **Y反転で回転の向きが裏返る**ので、start/end をそのまま使うと弧が「残りの側」になる。
    元の弧の中点が反時計回りに start→end の間へ入るかを見て、入らなければ入れ替える。
    """
    center = _point(segment.center, height, scale)
    start = _point(segment.start, height, scale)
    end = _point(segment.end, height, scale)
    middle = _point(segment.point(0.5), height, scale)

    def angle_of(point: Tuple[float, float]) -> float:
        return math.degrees(
            math.atan2(point[1] - center[1], point[0] - center[0])
        ) % 360.0

    start_deg, end_deg, middle_deg = angle_of(start), angle_of(end), angle_of(middle)
    if not (middle_deg - start_deg) % 360.0 < (end_deg - start_deg) % 360.0:
        start_deg, end_deg = end_deg, start_deg
    if start_deg == end_deg:
        return []  # `sketch._validate_arc` が弾く値(角度0の弧は辺にならない)
    return [
        {
            "kind": "arc",
            "params": {
                "cx": center[0],
                "cy": center[1],
                "radius": radius * scale,
                "start_deg": start_deg,
                "end_deg": end_deg,
            },
        }
    ]


def _elliptical_arc_entities(segment: Any, height: float, scale: float) -> List[dict]:
    """楕円弧を90度以下のベジエへ。制御点は**接ベクトル**から作るので回転楕円もそのまま扱える。

    `prx` / `pry` は中心から主軸・副軸の端点なので、その差ベクトルが楕円の2軸そのものになる。
    楕円上の点は `P(t) = 中心 + cos(t)・軸x + sin(t)・軸y`。
    """
    center = segment.center
    axis_x = (
        float(segment.prx.x) - float(center.x),
        float(segment.prx.y) - float(center.y),
    )
    axis_y = (
        float(segment.pry.x) - float(center.x),
        float(segment.pry.y) - float(center.y),
    )
    start_t = math.radians(float(segment.theta))
    sweep = math.radians(float(segment.delta))
    if abs(sweep) < 1.0e-12:
        return []
    count = max(1, math.ceil(abs(sweep) / (math.pi / 2)))
    step = sweep / count
    # 90度以下の弧を3次ベジエで近似するときの接ベクトルの係数(標準形)。
    alpha = math.sin(step) * (math.sqrt(4.0 + 3.0 * math.tan(step / 2.0) ** 2) - 1.0) / 3.0

    def position(t: float) -> Tuple[float, float]:
        return (
            float(center.x) + math.cos(t) * axis_x[0] + math.sin(t) * axis_y[0],
            float(center.y) + math.cos(t) * axis_x[1] + math.sin(t) * axis_y[1],
        )

    def tangent(t: float) -> Tuple[float, float]:
        return (
            -math.sin(t) * axis_x[0] + math.cos(t) * axis_y[0],
            -math.sin(t) * axis_x[1] + math.cos(t) * axis_y[1],
        )

    def flip(point: Tuple[float, float]) -> Tuple[float, float]:
        return (point[0] * scale, (height - point[1]) * scale)

    entities: List[dict] = []
    for index in range(count):
        t0 = start_t + index * step
        t1 = t0 + step
        p0, p1 = position(t0), position(t1)
        d0, d1 = tangent(t0), tangent(t1)
        entities.append(
            _bezier_entity(
                flip(p0),
                flip((p0[0] + alpha * d0[0], p0[1] + alpha * d0[1])),
                flip((p1[0] - alpha * d1[0], p1[1] - alpha * d1[1])),
                flip(p1),
            )
        )
    return entities
