"""SVGをスケッチの図形へ翻訳する。**OCP / build123d を直接importしない**(import境界)。

`import_svg` verb の材料をここが作る。返すのは `[{"kind":.., "params":{..}}, ...]` という素の
dictのリストで、`duocad.sketch` の `Entity` と同じ語彙・同じキーである。

## 決めごと

**単位は `ppi=25.4` で解決する。** svgelementsは既定(96ppi)だと全長をpxへ正規化し、
`width="100mm"` が377.95になる。25.4を渡すと物理単位のSVGは**寸法がそのままmm**、
`viewBox` だけのSVGは**1ユーザー単位 = 1mm** になる。

**物理単位のSVGには 0.54ppm の系統的なずれがある**(実測)。svgelementsがmm→pxにCSSの丸めた
定数(3.7795296。厳密には 96/25.4)を使うため `width="100mm"` は100.000054になる。
取り込んだ寸法はぴったり一致しない(テストも近似で比べている)。

**Y軸を反転する。** SVGのYは下向き、スケッチのvは上向き。反転は向きも裏返すので、
**真円の弧は start/end を入れ替える**(中点の角度で判定)。

**曲線は曲線のまま渡す。** 3次ベジエ(C/S)は1:1、2次ベジエ(Q/T)は次数上げで**厳密に**3次へ。
`<ellipse>` 要素は1つの `ellipse` 図形にする(斜めに歪めた変換が掛かって軸が直交しないものは下の楕円弧の扱い)。
楕円弧(rx≠ry)だけは90度以下に割ってベジエ近似する(**最大誤差は半径の0.027%**。半径50mmで
13.6um)。折れ線には落とさない —— 押し出した側面が線分の数だけ平面に割れる
(`kernel_sketch._edges_bezier` のdocstring)。

**見た目は捨てる。** fill / stroke / 色 / text / image / clip-path はスケッチに意味が無い。
`<text>` はSVG側でアウトライン化してから読ませること。

失敗は黙って通さない: パス無し=FileNotFoundError / 壊れている=ValueError /
図形が1つも無い=ValueError / 多すぎる=ValueError(`MAX_ENTITIES`)。いずれもdoc/logは無傷。
"""
from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Any, List, Tuple, Union

PathLike = Union[str, Path]

#: 単位解決に渡すppi(モジュールdocstring)。
MM_PER_INCH = 25.4

#: 1回の取り込みで作ってよい図形の上限。黙って切り詰めず断る(欠けた輪郭で領域検出が別物を返す)。
MAX_ENTITIES = 5000

#: 真円とみなす rx/ry の相対差。これ以下なら `arc` に、超えたらベジエ近似に振る。
_CIRCULAR_TOLERANCE = 1.0e-9


def read_svg_entities(path: PathLike, scale: float = 1.0) -> Tuple[str, List[dict]]:
    """SVGを読み、`(source_name, entities)` を返す。`source_name` は**ファイル名だけ**。

    `entities` は `[{"kind":.., "params":{..}}, ...]`。`scale` は解決後のmmへさらに掛ける倍率。
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
        whole = _ellipse_entity(element, height, scale)
        pieces = [[whole]] if whole is not None else (
            _translate_segment(segment, height, scale) for segment in _segments(element)
        )
        for piece in pieces:
            entities.extend(piece)
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
    """SVG本文をパースする。壊れていれば ValueError。"""
    from svgelements import SVG

    try:
        return SVG.parse(io.BytesIO(data), ppi=MM_PER_INCH)
    except Exception as error:  # パーサ側の例外種別は版で変わりうるので束ねる
        raise ValueError(
            f"{source_name}: SVGとして読めない: {type(error).__name__}: {error}"
        ) from error


def _height_of(svg: Any) -> float:
    """Y反転の基準にする高さ(mm)。取れなければ図形のbboxの上端で代用。"""
    try:
        value = float(getattr(svg, "height", None))
    except (TypeError, ValueError):
        value = 0.0
    if math.isfinite(value) and value > 0:
        return value
    box = svg.bbox() if hasattr(svg, "bbox") else None
    return float(box[3]) if box else 0.0


def _shapes(svg: Any) -> List[Any]:
    """幾何を持つ要素だけを拾う。text / image はここで落ちる。"""
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


def _ellipse_entity(element: Any, height: float, scale: float) -> Any:
    """`<ellipse>` 要素を `ellipse` 図形1つへ。`<ellipse>` でないか、変換で軸が直交しなくなったものは None。

    svgelements の `Circle` は `Ellipse` の派生ではないので `<circle>` はここを通らない(今までどおり円弧になる)。
    """
    from svgelements import Arc, Ellipse

    if not isinstance(element, Ellipse):
        return None
    arcs = [segment for segment in _segments(element) if isinstance(segment, Arc)]
    if not arcs:
        return None
    center = _point(arcs[0].center, height, scale)
    end_x = _point(arcs[0].prx, height, scale)
    end_y = _point(arcs[0].pry, height, scale)
    axis_x = (end_x[0] - center[0], end_x[1] - center[1])
    axis_y = (end_y[0] - center[0], end_y[1] - center[1])
    width = 2.0 * math.hypot(*axis_x)
    height_mm = 2.0 * math.hypot(*axis_y)
    if width <= 0 or height_mm <= 0:
        return None
    if abs(axis_x[0] * axis_y[0] + axis_x[1] * axis_y[1]) > _CIRCULAR_TOLERANCE * width * height_mm:
        return None
    return {
        "kind": "ellipse",
        "params": {
            "cx": center[0],
            "cy": center[1],
            "width": width,
            "height": height_mm,
            "angle": math.degrees(math.atan2(axis_x[1], axis_x[0])),
        },
    }


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


def _flip(x: float, y: float, height: float, scale: float) -> Tuple[float, float]:
    """SVGの (x, y) をスケッチの (u, v) へ。Y反転と倍率はここだけが知っている。"""
    return (x * scale, (height - y) * scale)


def _point(value: Any, height: float, scale: float) -> Tuple[float, float]:
    """svgelements の点(`.x` / `.y`)を `_flip` へ。"""
    return _flip(float(value.x), float(value.y), height, scale)


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
    """2次ベジエを3次へ(次数上げは厳密)。

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
    """真円の弧を `arc` へ。Y反転で回転の向きが裏返るので、中点が反時計回りに
    start→end の間へ入らなければ start/end を入れ替える。
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
    """楕円弧を90度以下のベジエへ。制御点は接ベクトルから作るので回転楕円も扱える。

    `prx` / `pry` は中心から主軸・副軸の端点で、その差ベクトルが楕円の2軸。
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
    # 円弧の3次ベジエ近似の標準係数(90度で半径の0.027%)。Maisonobe の式 sin·(√(4+3tan²)−1)/3 は同条件で0.196%(実測)。
    alpha = 4.0 / 3.0 * math.tan(step / 4.0)

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
        return _flip(point[0], point[1], height, scale)

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
