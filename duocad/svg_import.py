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
**真円の弧は start/end を入れ替える**(中点の角度で判定)。反転の基準は viewport の高さ(`<svg height>`)。
未指定の辺は、もう一辺(絶対長)と viewBox の縦横比から決める(ブラウザの auto と同じ。svgelements のまま
だと viewBox を縦横比の違う viewport に中央寄せして図形が動く)。サイズも viewBox も無ければ高さの
情報が無いので、図形の下端(y 最大)を v=0 に置く。

**曲線は曲線のまま渡す。** 3次ベジエ(C/S)は1:1、2次ベジエ(Q/T)は次数上げで**厳密に**3次へ。
`<ellipse>` 要素は1つの `ellipse` 図形にする(斜めに歪めた変換が掛かって軸が直交しないものは下の楕円弧の扱い)。
楕円弧(rx≠ry、または変換で円でなくなったもの)は90度以下に割ってベジエ近似する(**最大誤差は半径の0.027%**。
半径50mmで13.6um)。折れ線には落とさない —— 押し出した側面が線分の数だけ平面に割れる
(`kernel_sketch._edges_bezier` のdocstring)。半径0の弧・潰れた弧は端点を結ぶ線分(SVG 1.1 付録 F.6.2)。
長さ0の線分・ベジエ・弧は黙って捨てる(領域を割らない。`sketch._validate_bezier` が弾く値)。

**見た目は捨てる。** fill / stroke / 色 / text / image / clip-path はスケッチに意味が無い。
`<text>` はSVG側でアウトライン化してから読ませること。

**描画されない定義は取り込まない。** `<symbol>` `<mask>` `<marker>` は `<use>` などで参照されて初めて意味を持つ。
`<switch>` は描かれる枝が1つだけ(条件属性 `requiredFeatures` / `requiredExtensions` / `systemLanguage` を
持たない最初の子。条件は評価できないので満たされないものとして扱う)。`<defs>` `<clipPath>` `<pattern>` の
中身と同じく、図形としては数えない(`<use>` が呼ぶ分だけが入る)。

失敗は黙って通さない: パス無し=FileNotFoundError / 壊れている・高さが使えない(0以下)=ValueError /
図形が1つも無い=ValueError / 多すぎる=ValueError(`MAX_ENTITIES`)。いずれもdoc/logは無傷。
"""
from __future__ import annotations

import io
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union

PathLike = Union[str, Path]

#: 単位解決に渡すppi(モジュールdocstring)。
MM_PER_INCH = 25.4

#: 1回の取り込みで作ってよい図形の上限。黙って切り詰めず断る(欠けた輪郭で領域検出が別物を返す)。
MAX_ENTITIES = 5000

#: 真円とみなす相対差(半径の差・軸の内積)。これ以下なら `arc` に、超えたらベジエ近似に振る。
#: 長さ0・潰れた弧の判定(軸の外積)にも使う。
_CIRCULAR_TOLERANCE = 1.0e-9

#: これより短い線分・ベジエは長さ0として捨てる(mm。`sketch._validate_line` が弾く値より少し広い)。
_ZERO_LENGTH = 1.0e-9

_SVG_NAMESPACE = "http://www.w3.org/2000/svg"

#: `<use>` などから呼ばれる定義。`<defs>` の外に書かれていても、そこでは描かれない。
_UNDRAWN_DEFINITIONS = frozenset({"symbol", "mask", "marker"})

#: svgelements が最初から描画対象にしない入れ物(中身を調べる必要が無い)。
_NON_RENDERING = frozenset({"defs", "clipPath", "pattern"})

#: `<switch>` が選ぶ候補になる要素(`<title>` `<desc>` などは候補にならない)。
_SWITCH_CANDIDATES = frozenset(
    {"a", "circle", "ellipse", "foreignObject", "g", "image", "line", "path", "polygon",
     "polyline", "rect", "svg", "switch", "text", "use"}
)
_CONDITIONAL_ATTRIBUTES = ("requiredFeatures", "requiredExtensions", "systemLanguage")


def read_svg_entities(path: PathLike, scale: float = 1.0) -> Tuple[str, List[dict]]:
    """SVGを読み、`(source_name, entities)` を返す。`source_name` は**ファイル名だけ**。

    `entities` は `[{"kind":.., "params":{..}}, ...]`。`scale` は解決後のmmへさらに掛ける倍率。
    """
    scale = float(scale)
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError(f"scale must be a positive finite number, got {scale!r}")

    data = Path(path).read_bytes()  # 無ければ FileNotFoundError
    source_name = Path(path).name
    data, width_hint, height_hint = _prepare(data)
    svg = _parse(data, source_name, width_hint, height_hint)
    shapes = _shapes(svg)
    height = _flip_height(svg, shapes)

    entities: List[dict] = []
    for element in shapes:
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
            "(text / image / 描かれない定義はスケッチに取り込めない。文字はアウトライン化してから読ませること)"
        )
    return source_name, entities


# --------------------------------------------------------------------------------------
# 解析(svgelements)
# --------------------------------------------------------------------------------------
def _parse(
    data: bytes, source_name: str, width: Optional[float] = None, height: Optional[float] = None
) -> Any:
    """SVG本文をパースする。壊れていれば ValueError。

    `width` / `height`(mm)は `<svg>` に書かれていない辺の大きさ(svgelements は viewBox の寸法を使う)。
    """
    from svgelements import SVG

    try:
        svg = SVG.parse(io.BytesIO(data), ppi=MM_PER_INCH, width=width, height=height)
    except Exception as error:  # パーサ側の例外種別は版で変わりうるので束ねる
        raise ValueError(
            f"{source_name}: SVGとして読めない: {type(error).__name__}: {error}"
        ) from error
    if not isinstance(svg, SVG):  # ルートが <svg> でない / display:none だと None などが返る
        raise ValueError(f"{source_name}: SVGとして読めない: ルートが描画される <svg> ではない")
    return svg


def _local_name(tag: str) -> str:
    """svgelements が見るタグ名。SVG名前空間は外し、他の名前空間は空にする。"""
    prefix = "{" + _SVG_NAMESPACE + "}"
    if tag.startswith(prefix):
        return tag[len(prefix):]
    return "" if tag.startswith("{") else tag


def _prepare(data: bytes) -> Tuple[bytes, Optional[float], Optional[float]]:
    """svgelements に渡す前の下ごしらえ: `(本文, width の既定, height の既定)`。

    svgelements は `<symbol>` `<mask>` `<marker>` `<switch>` の子を親の列へ平らに並べるので、読んだ後では
    描かれない図形を見分けられない。`<defs>` で包んでおけば `<use>` からの参照(id)は生きたまま、
    そこでは描かれない。**直す箇所が無ければ元の本文をそのまま返す**(再直列化で他の癖を持ち込まない)。
    """
    try:
        root = ET.fromstring(data)
    except Exception:  # 壊れた本文はそのまま渡して、svgelements 側で ValueError にする
        return data, None, None
    if _local_name(root.tag) != "svg":
        return data, None, None
    undrawn = _undrawn_children(root)
    for parent, child in undrawn:
        namespace = child.tag[: child.tag.index("}") + 1] if child.tag.startswith("{") else ""
        wrapper = ET.Element(namespace + "defs")
        wrapper.tail, child.tail = child.tail, None
        wrapper.append(child)
        parent[list(parent).index(child)] = wrapper
    if undrawn:
        data = ET.tostring(root, encoding="utf-8")
    width, height = _viewport_defaults(root)
    return data, width, height


def _undrawn_children(root: Any) -> List[Tuple[Any, Any]]:
    """描かれない定義・選ばれなかった `<switch>` の枝を `(親, 子)` で集める(最も外側だけ)。"""
    found: List[Tuple[Any, Any]] = []
    stack = [root]
    while stack:
        parent = stack.pop()
        children = list(parent)
        in_switch = _local_name(parent.tag) == "switch"
        selected = next(
            (
                child
                for child in children
                if in_switch
                and _local_name(child.tag) in _SWITCH_CANDIDATES
                and not any(name in child.attrib for name in _CONDITIONAL_ATTRIBUTES)
            ),
            None,
        )
        for child in children:
            name = _local_name(child.tag)
            if name in _NON_RENDERING:
                continue
            passed_over = in_switch and name in _SWITCH_CANDIDATES and child is not selected
            if name in _UNDRAWN_DEFINITIONS or passed_over:
                found.append((parent, child))
            else:
                stack.append(child)
    return found


def _absolute_length(text: Optional[str]) -> Optional[float]:
    """絶対長(mm)。無い・百分率・em など単位から決まらない・0以下は None。"""
    from svgelements import Length

    if text is None:
        return None
    try:
        value = Length(text).value(ppi=MM_PER_INCH)
    except Exception:
        return None
    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
        return float(value)
    return None


def _viewport_defaults(root: Any) -> Tuple[Optional[float], Optional[float]]:
    """`(width, height)`: viewBox があり、片方だけが絶対長のとき、もう片方を縦横比から決めた値(mm)。

    svgelements は書かれていない辺を viewBox の寸法で埋めるため、縦横比の違う viewport に viewBox を
    中央寄せ(xMidYMid)して図形が縦横にずれる。ブラウザは auto を縦横比で決める。
    """
    numbers = re.split(r"[\s,]+", (root.get("viewBox") or "").strip())
    try:
        box_width, box_height = float(numbers[2]), float(numbers[3])
    except (IndexError, ValueError):
        return None, None
    if not (math.isfinite(box_width) and math.isfinite(box_height) and box_width > 0 and box_height > 0):
        return None, None
    width, height = _absolute_length(root.get("width")), _absolute_length(root.get("height"))
    if width is not None and height is None:
        return None, width * box_height / box_width
    if height is not None and width is None:
        return height * box_width / box_height, None
    return None, None


def _flip_height(svg: Any, shapes: List[Any]) -> float:
    """Y反転の基準にする viewport の高さ(mm)。高さの情報が無ければ図形の下端(y 最大)。"""
    raw = svg.values.get("height")
    if raw is None or str(raw).strip().endswith("%"):
        if svg.viewbox is not None:
            return float(svg.height)  # viewBox の高さ、または `_viewport_defaults` が決めた値
        boxes = [box for box in (shape.bbox() for shape in shapes) if box is not None]
        return float(max(box[3] for box in boxes)) if boxes else 0.0
    height = float(svg.height)
    if not (math.isfinite(height) and height > 0):
        raise ValueError(f"<svg> の height={raw!r} が使えない(0以下か、数値として読めない)")
    return height


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
        return _quadratic_as_cubic(segment, height, scale)
    if isinstance(segment, CubicBezier):
        return _bezier_entity(
            _point(segment.start, height, scale),
            _point(segment.control1, height, scale),
            _point(segment.control2, height, scale),
            _point(segment.end, height, scale),
        )
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
    if math.dist(start, end) < _ZERO_LENGTH:
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
) -> List[dict]:
    """3次ベジエ。制御点が全部同じ点なら長さ0で、線分と同じく捨てる(`c 0 0 0 0 0 0` / `s 0 0 0 0` / `t 0 0`)。"""
    if all(math.dist(start, point) < _ZERO_LENGTH for point in (control1, control2, end)):
        return []
    return [
        {
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
    ]


def _quadratic_as_cubic(segment: Any, height: float, scale: float) -> List[dict]:
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
    """円弧。真円なら `arc`、楕円ならベジエ近似(90度以下に割る)。半径0・潰れた弧は端点を結ぶ線分。"""
    frame = _arc_frame(segment)
    if frame is None:
        return _line_entity(segment, height, scale)
    _center, axis_x, axis_y, _start_t, sweep = frame
    if abs(sweep) < 1.0e-12:
        return []
    length_x, length_y = math.hypot(*axis_x), math.hypot(*axis_y)
    dot = axis_x[0] * axis_y[0] + axis_x[1] * axis_y[1]
    if (
        abs(length_x - length_y) <= _CIRCULAR_TOLERANCE * max(length_x, length_y)
        and abs(dot) <= _CIRCULAR_TOLERANCE * length_x * length_y
    ):
        return _circular_arc_entity(segment, frame, length_x, height, scale)
    return _elliptical_arc_entities(segment, frame, height, scale)


def _arc_frame(segment: Any) -> Any:
    """変換済みの Arc を `(center, axis_x, axis_y, start_t, sweep)` へ。弧は
    `P(t) = center + cos t * axis_x + sin t * axis_y`(t は start_t から start_t + sweep まで)。潰れた弧は None。

    svgelements は Arc を center / prx / pry(元の楕円の t=0・π/2 の点)と sweep で持ち、変換は3点と
    start / end にそのまま掛かるので、上の式はどんなアフィン変換の後でも元の弧になる(軸は主軸とは限らない
    共役半径)。使わないもの: `theta` `rx` `ry` `get_rotation` は prx ⊥ pry を仮定して3点から作り直す値で、
    せん断や回転後の非等倍スケールでは主軸が入らず、反転では t の向きまで裏返る(端点が最大数mmずれる)。
    start_t は start から解く。sweep は大きさだけ svgelements のものを使い、向きは end に着く方を選ぶ。
    """
    center = segment.center
    if center is None or segment.prx is None or segment.pry is None:
        return None
    cx, cy = float(center.x), float(center.y)
    axis_x = (float(segment.prx.x) - cx, float(segment.prx.y) - cy)
    axis_y = (float(segment.pry.x) - cx, float(segment.pry.y) - cy)
    cross = axis_x[0] * axis_y[1] - axis_x[1] * axis_y[0]
    length_x, length_y = math.hypot(*axis_x), math.hypot(*axis_y)
    if length_x == 0 or length_y == 0 or abs(cross) <= _CIRCULAR_TOLERANCE * length_x * length_y:
        return None

    def parameter_of(point: Any) -> float:
        dx, dy = float(point.x) - cx, float(point.y) - cy
        return math.atan2(
            (axis_x[0] * dy - axis_x[1] * dx) / cross,
            (dx * axis_y[1] - dy * axis_y[0]) / cross,
        )

    start_t = parameter_of(segment.start)
    ahead = (parameter_of(segment.end) - start_t) % math.tau
    # svgelements は行列式が負のとき sweep の符号を裏返す(外積の符号が行列式の符号)。半周ちょうどは大きさで
    # 決まらないので、その向きで選ぶ。
    expected = float(segment.sweep) * (1.0 if cross > 0 else -1.0)
    sweep = min(
        (ahead, ahead - math.tau),
        key=lambda value: (round(abs(abs(value) - abs(expected)), 9), 0 if value * expected > 0 else 1),
    )
    return (cx, cy), axis_x, axis_y, start_t, sweep


def _circular_arc_entity(
    segment: Any, frame: Any, radius: float, height: float, scale: float
) -> List[dict]:
    """真円の弧を `arc` へ。Y反転で回転の向きが裏返るので、中点が反時計回りに
    start→end の間へ入らなければ start/end を入れ替える。
    """
    (cx, cy), axis_x, axis_y, start_t, sweep = frame
    middle_t = start_t + sweep / 2.0
    center = _flip(cx, cy, height, scale)
    start = _point(segment.start, height, scale)
    end = _point(segment.end, height, scale)
    middle = _flip(
        cx + math.cos(middle_t) * axis_x[0] + math.sin(middle_t) * axis_y[0],
        cy + math.cos(middle_t) * axis_x[1] + math.sin(middle_t) * axis_y[1],
        height,
        scale,
    )

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


def _elliptical_arc_entities(segment: Any, frame: Any, height: float, scale: float) -> List[dict]:
    """楕円弧を90度以下のベジエへ。制御点は接ベクトルから作るので回転楕円・共役半径も扱える。

    ベジエ近似はアフィン変換で不変なので、t の刻みを90度以下にすれば変換後の楕円でも誤差は変わらない。
    両端は svgelements の start / end をそのまま使う(隣の線・弧と一致させる)。
    """
    (cx, cy), axis_x, axis_y, start_t, sweep = frame
    count = max(1, math.ceil(abs(sweep) / (math.pi / 2)))
    step = sweep / count
    # 円弧の3次ベジエ近似の標準係数(90度で半径の0.027%)。Maisonobe の式 sin·(√(4+3tan²)−1)/3 は同条件で0.196%(実測)。
    alpha = 4.0 / 3.0 * math.tan(step / 4.0)

    def position(t: float) -> Tuple[float, float]:
        return (
            cx + math.cos(t) * axis_x[0] + math.sin(t) * axis_y[0],
            cy + math.cos(t) * axis_x[1] + math.sin(t) * axis_y[1],
        )

    def tangent(t: float) -> Tuple[float, float]:
        return (
            -math.sin(t) * axis_x[0] + math.cos(t) * axis_y[0],
            -math.sin(t) * axis_x[1] + math.cos(t) * axis_y[1],
        )

    def flip(point: Tuple[float, float]) -> Tuple[float, float]:
        return _flip(point[0], point[1], height, scale)

    entities: List[dict] = []
    p0 = (float(segment.start.x), float(segment.start.y))
    for index in range(count):
        t0 = start_t + index * step
        t1 = t0 + step
        p1 = (float(segment.end.x), float(segment.end.y)) if index == count - 1 else position(t1)
        d0, d1 = tangent(t0), tangent(t1)
        entities.extend(
            _bezier_entity(
                flip(p0),
                flip((p0[0] + alpha * d0[0], p0[1] + alpha * d0[1])),
                flip((p1[0] - alpha * d1[0], p1[1] - alpha * d1[1])),
                flip(p1),
            )
        )
        p0 = p1
    return entities
