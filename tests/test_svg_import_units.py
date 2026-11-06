"""SVG取り込みの単位換算・非表示図形の扱い・取り込み結果の応答。"""
from __future__ import annotations

from pathlib import Path

import pytest

from duocad import svg_import
from duocad.server import Session


def _svg(width: str, height: str, view_box: str, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="{view_box}">{body}</svg>'
    )


def _write(directory: Path, body: str, name: str = "t.svg") -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def _rect_width(entities: list) -> float:
    xs = [value for entity in entities if entity["kind"] == "line" for value in (entity["params"]["x0"], entity["params"]["x1"])]
    return max(xs) - min(xs)


@pytest.mark.parametrize(
    ("width", "height", "view_box", "expected_mm"),
    [
        ("72pt", "72pt", "0 0 72 72", 25.4),
        ("6pc", "6pc", "0 0 6 6", 25.4),
        ("1in", "1in", "0 0 1 1", 25.4),
        ("100mm", "100mm", "0 0 1 1", 100.0),
    ],
)
def test_physical_units_of_the_root_become_millimetres(
    tmp_path: Path, width: str, height: str, view_box: str, expected_mm: float
) -> None:
    """落ちたら、ルートの width/height の `pt` `pc` が svgelements の決め打ち(px の 4/3・16 倍)で解かれ、約3.78倍で入る。"""
    box = float(view_box.split()[2])
    body = _svg(width, height, view_box, f'<rect x="0" y="0" width="{box}" height="{box}"/>')
    _name, entities = svg_import.read_svg_entities(_write(tmp_path, body))
    assert _rect_width(entities) == pytest.approx(expected_mm, rel=1e-4)


def test_typographic_units_with_one_side_missing_follow_the_aspect_ratio(tmp_path: Path) -> None:
    """落ちたら、片方だけが `pt` のときの縦横比からの補完が、換算前の値で行われている。"""
    body = '<svg xmlns="http://www.w3.org/2000/svg" width="144pt" viewBox="0 0 100 50"><rect x="0" y="0" width="100" height="50"/></svg>'
    _name, entities = svg_import.read_svg_entities(_write(tmp_path, body))
    ys = [value for entity in entities for value in (entity["params"]["y0"], entity["params"]["y1"])]
    assert _rect_width(entities) == pytest.approx(50.8, rel=1e-4)
    assert max(ys) - min(ys) == pytest.approx(25.4, rel=1e-4)


def test_hidden_shapes_are_dropped_and_counted(tmp_path: Path) -> None:
    """落ちたら、`visibility` が hidden / collapse の図形が取り込まれる、または継承と `visible` の上書きが効かない。"""
    body = _svg(
        "100mm",
        "50mm",
        "0 0 100 50",
        '<rect x="1" y="1" width="5" height="5"/>'
        '<rect x="10" y="1" width="5" height="5" visibility="hidden"/>'
        '<rect x="20" y="1" width="5" height="5" style="visibility:collapse"/>'
        '<g visibility="hidden"><rect x="30" y="1" width="5" height="5"/>'
        '<rect x="40" y="1" width="5" height="5" visibility="visible"/></g>'
        '<rect x="50" y="1" width="5" height="5" opacity="0"/>'
        '<text x="1" y="30">hi</text><image href="a.png" width="3" height="3"/>',
    )
    _name, entities, skipped = svg_import.read_svg_report(_write(tmp_path, body))
    xs = {round(entity["params"][key]) for entity in entities if entity["kind"] == "line" for key in ("x0", "x1")}
    assert {1, 6, 40, 45, 50, 55} == xs
    assert skipped == {"text": 1, "image": 1, "hidden": 3}
    assert svg_import.describe_skipped(skipped) == "文字 1 個・画像 1 個・非表示の図形 3 個"


def test_an_svg_of_only_hidden_shapes_says_why(tmp_path: Path) -> None:
    """落ちたら、全部が非表示のSVGの断りが、落とした数を言わない。"""
    body = _svg("10mm", "10mm", "0 0 10 10", '<rect width="5" height="5" visibility="hidden"/>')
    with pytest.raises(ValueError, match="非表示の図形 1 個"):
        svg_import.read_svg_entities(_write(tmp_path, body))


def test_import_svg_reports_size_regions_and_skipped(tmp_path: Path) -> None:
    """落ちたら、取り込みの応答が大きさ・領域数・落とした要素を載せていない(単位違いに気づけない)。"""
    session = Session(0.2, models_dir=tmp_path)
    sketch_id = session.sketch_create("XY")["sketch_id"]
    body = _svg(
        "72pt", "36pt", "0 0 72 36", '<rect x="0" y="0" width="72" height="36"/><text x="1" y="1">a</text>'
    )
    applied = session.import_svg(sketch_id, str(_write(tmp_path, body)))
    assert applied["size_mm"] == pytest.approx([25.4, 12.7], rel=1e-4)
    assert applied["region_count"] == 1
    assert applied["skipped"] == "文字 1 個"


def test_import_svg_of_an_open_line_has_no_region(tmp_path: Path) -> None:
    session = Session(0.2, models_dir=tmp_path)
    sketch_id = session.sketch_create("XY")["sketch_id"]
    body = _svg("10mm", "10mm", "0 0 10 10", '<path d="M 1 1 L 9 9" fill="none"/>')
    applied = session.import_svg(sketch_id, str(_write(tmp_path, body)))
    assert applied["region_count"] == 0
    assert applied["skipped"] == ""


def test_entities_size_covers_curves() -> None:
    circle_arc = {"kind": "arc", "params": {"cx": 0.0, "cy": 0.0, "radius": 5.0, "start_deg": 0.0, "end_deg": 180.0}}
    assert svg_import.entities_size([circle_arc]) == pytest.approx([10.0, 5.0], rel=1e-3)
    assert svg_import.entities_size([]) == [0.0, 0.0]


def _extent(entities: list) -> tuple[float, float]:
    xs = [params[key] for entity in entities for params in [entity["params"]] for key in ("x0", "x1", "cx") if key in params]
    ys = [params[key] for entity in entities for params in [entity["params"]] for key in ("y0", "y1", "cy") if key in params]
    return max(xs) - min(xs), max(ys) - min(ys)


def test_typographic_units_on_shape_attributes_become_millimetres(tmp_path: Path) -> None:
    """落ちたら、図形の長さの属性の `pt` `pc` が svgelements の決め打ち(px の 4/3・16 倍)で解かれ、ルートが mm でも約3.78倍で入る。"""
    body = '<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm"><rect width="72pt" height="6pc"/></svg>'
    _name, entities = svg_import.read_svg_entities(_write(tmp_path, body))
    assert _extent(entities) == pytest.approx((25.4, 25.4), rel=1e-4)


def test_a_circle_radius_in_pt_and_a_line_end_in_pc_become_millimetres(tmp_path: Path) -> None:
    """落ちたら、円の `r` や線の `x2` のような寸法以外の長さの属性が `pt` `pc` のまま解かれる。"""
    circle = '<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm"><circle cx="50" cy="50" r="36pt"/></svg>'
    line = '<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm"><line x1="0" y1="5" x2="6pc" y2="5"/></svg>'
    _name, circle_entities = svg_import.read_svg_entities(_write(tmp_path, circle, "c.svg"))
    _name, line_entities = svg_import.read_svg_entities(_write(tmp_path, line, "l.svg"))
    radius = next(entity["params"]["radius"] for entity in circle_entities if entity["kind"] == "arc")
    assert radius == pytest.approx(12.7, rel=1e-4)
    assert _extent(line_entities)[0] == pytest.approx(25.4, rel=1e-4)


@pytest.mark.parametrize(
    ("unit", "expected"),
    [("96px", 96.0), ("96", 96.0), ("1in", 25.4), ("25.4mm", 25.4), ("2.54cm", 25.4)],
)
def test_other_units_on_shape_attributes_are_left_to_svgelements(tmp_path: Path, unit: str, expected: float) -> None:
    """落ちたら、`pt` `pc` の書き換えが px・単位なし・in・mm・cm の解釈を変えている。"""
    body = f'<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm"><rect width="{unit}" height="{unit}"/></svg>'
    _name, entities = svg_import.read_svg_entities(_write(tmp_path, body))
    assert _extent(entities)[0] == pytest.approx(expected, rel=1e-4)


def test_typographic_units_on_shapes_follow_the_viewbox_once_like_inches(tmp_path: Path) -> None:
    """落ちたら、viewBox の倍率が `pt` に二重に掛かる、または `in` と違う大きさで入る(同じ長さなら同じ値になる)。"""
    def width_of(unit: str) -> float:
        body = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm" viewBox="0 0 200 200">'
            f'<rect width="{unit}" height="{unit}"/></svg>'
        )
        _name, entities = svg_import.read_svg_entities(_write(tmp_path, body, f"{unit}.svg"))
        return _rect_width(entities)

    assert width_of("72pt") == pytest.approx(width_of("1in"), rel=1e-4)
    assert width_of("6pc") == pytest.approx(width_of("1in"), rel=1e-4)


def test_a_body_with_nothing_to_wrap_is_returned_untouched() -> None:
    """落ちたら、直す箇所が無い SVG まで再直列化され、他の癖が入る。"""
    data = b'<svg xmlns="http://www.w3.org/2000/svg" width="10mm" height="10mm"><rect  width="72pt" height="5"/></svg>'
    assert svg_import._prepare(data)[0] is data


#: `pt` `pc` が入る経路ごとの本文と、図形の外接箱 (x 最小, y 最小, x 最大, y 最大)。ルートは 100mm 角で viewBox 無し。
_TYPOGRAPHIC_ROUTES = {
    "attribute": ('<rect width="72pt" height="6pc"/>', (0.0, 0.0, 25.4, 25.4)),
    "upper-case": ('<rect width="72PT" height="6PC"/>', (0.0, 0.0, 25.4, 25.4)),
    "inline-style": ('<rect style="width:72pt;height:6pc"/>', (0.0, 0.0, 25.4, 25.4)),
    "style-rule": ("<style>rect{width:72pt;height:6pc}</style><rect/>", (0.0, 0.0, 25.4, 25.4)),
    "style-class": ('<style>.a{width:72pt;height:6pc}</style><rect class="a"/>', (0.0, 0.0, 25.4, 25.4)),
    "circle-style": ('<circle style="r:36pt;cx:72pt;cy:6pc"/>', (12.7, 12.7, 38.1, 38.1)),
    "transform": ('<rect width="10" height="10" transform="translate(72pt, 6pc)"/>', (25.4, 25.4, 35.4, 35.4)),
    "style-transform": (
        '<rect width="10" height="10" style="transform:translate(72pt, 6pc)"/>',
        (25.4, 25.4, 35.4, 35.4),
    ),
    "group-transform": (
        '<g transform="translate(72pt)"><rect width="10" height="10"/></g>',
        (25.4, 0.0, 35.4, 10.0),
    ),
    "use-offset": (
        '<defs><rect id="r" width="10" height="10"/></defs><use href="#r" x="72pt" y="6pc"/>',
        (25.4, 25.4, 35.4, 35.4),
    ),
    "percent-of-nested-pt": (
        '<svg width="72pt" height="6pc"><rect width="50%" height="50%"/></svg>',
        (0.0, 0.0, 12.7, 12.7),
    ),
}


def _box(entities: list) -> tuple[float, float, float, float]:
    """図形の外接箱を SVG の向き(y 下向き、原点は左上)で返す。ルートの高さは 100mm。"""
    xs: list[float] = []
    ys: list[float] = []
    for entity in entities:
        params = entity["params"]
        if entity["kind"] == "line":
            xs += [params["x0"], params["x1"]]
            ys += [params["y0"], params["y1"]]
        else:
            xs += [params["cx"] - params["radius"], params["cx"] + params["radius"]]
            ys += [params["cy"] - params["radius"], params["cy"] + params["radius"]]
    return min(xs), 100.0 - max(ys), max(xs), 100.0 - min(ys)


@pytest.mark.parametrize("route", sorted(_TYPOGRAPHIC_ROUTES))
def test_typographic_units_become_millimetres_whichever_way_they_come_in(tmp_path: Path, route: str) -> None:
    """落ちたら、`pt` `pc` がその経路では svgelements の決め打ち(px の 4/3・16 倍)で解かれ、約3.78倍で入る。"""
    inner, expected = _TYPOGRAPHIC_ROUTES[route]
    body = f'<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm">{inner}</svg>'
    _name, entities = svg_import.read_svg_entities(_write(tmp_path, body))
    assert _box(entities) == pytest.approx(expected, rel=1e-4, abs=1e-4)


@pytest.mark.parametrize(
    ("corner", "radius"),
    [
        ('rx="1pt"', 25.4 / 72),
        ('ry="1pt"', 25.4 / 72),
        ('rx="1PT"', 25.4 / 72),
        ('rx="0.5pc"', 25.4 / 12),
        ('style="rx:1pt"', 25.4 / 72),
    ],
)
def test_a_corner_radius_in_typographic_units_stays_a_corner(tmp_path: Path, corner: str, radius: float) -> None:
    """落ちたら、`pt` `pc` の角丸の半径が幅の半分より大きいと取り違えられ、角丸の長方形が円になる。"""
    body = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm">'
        f'<rect width="10" height="10" {corner}/></svg>'
    )
    _name, entities = svg_import.read_svg_entities(_write(tmp_path, body))
    assert sorted(entity["kind"] for entity in entities) == ["arc"] * 4 + ["line"] * 4
    radii = [entity["params"]["radius"] for entity in entities if entity["kind"] == "arc"]
    assert radii == pytest.approx([radius] * 4, rel=1e-4)


@pytest.mark.parametrize(
    ("rect", "size"),
    [
        ('<rect width="10mm" height="10mm" rx="100pt"/>', [10.0, 10.0]),
        ('<rect width="10mm" height="10mm" rx="100"/>', [10.0, 10.0]),
        ('<rect width="10" height="10" rx="100mm"/>', [10.0, 10.0]),
        ('<rect width="10mm" height="20mm" rx="100" ry="3"/>', [10.0, 20.0]),
        ('<rect width="20mm" height="10mm" rx="3" ry="100pt"/>', [20.0, 10.0]),
    ],
)
def test_a_corner_radius_is_limited_to_half_the_side_whatever_the_units(tmp_path: Path, rect: str, size: list) -> None:
    """落ちたら、角丸の半径と幅・高さの単位が違うとき、半径が辺の半分に抑えられず、長方形より大きい輪郭が入る。"""
    body = f'<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm">{rect}</svg>'
    _name, entities = svg_import.read_svg_entities(_write(tmp_path, body))
    assert svg_import.entities_size(entities) == pytest.approx(size, rel=1e-4)


def test_reading_an_svg_leaves_the_parser_as_it_was(tmp_path: Path) -> None:
    """落ちたら、`pt` `pc` のための差し替えが取り込みの後(失敗した後も)に svgelements へ残る。"""
    from svgelements import Length

    before = Length.value
    body = '<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm"><rect style="width:72pt;height:6pc"/></svg>'
    svg_import.read_svg_entities(_write(tmp_path, body))
    assert Length.value is before
    with pytest.raises(ValueError):
        svg_import.read_svg_entities(_write(tmp_path, "<svg", "broken.svg"))
    assert Length.value is before
    assert Length("72pt").value(ppi=25.4) == pytest.approx(96.0)


@pytest.mark.parametrize(
    ("hidden", "count"),
    [
        ('<rect x="10" y="1" width="5" height="5" display="none"/>', 1),
        ('<rect x="10" y="1" width="5" height="5" style="display:none"/>', 1),
        ('<style>.off{display:none}</style><rect class="off" x="10" y="1" width="5" height="5"/>', 1),
        ('<g style="display:none"><rect x="10" y="1" width="5" height="5"/><circle cx="20" cy="3" r="2"/></g>', 2),
        ('<g display="none"><rect x="10" y="1" width="5" height="5" display="inline"/></g>', 1),
        (
            '<defs><g id="pair"><rect width="2" height="2"/><rect x="3" width="2" height="2"/></g></defs>'
            '<use href="#pair" x="10" display="none"/>',
            2,
        ),
        ('<rect x="10" y="1" width="5" height="5" display="none"/><rect x="20" y="1" width="5" height="5" visibility="hidden"/>', 2),
    ],
)
def test_shapes_under_display_none_are_counted_as_hidden(tmp_path: Path, hidden: str, count: int) -> None:
    """落ちたら、`display:none` で入らなかった図形の数が応答に出ない(隠したレイヤの形が黙って消える)。"""
    body = _svg("100mm", "50mm", "0 0 100 50", '<rect x="1" y="1" width="5" height="5"/>' + hidden)
    _name, entities, skipped = svg_import.read_svg_report(_write(tmp_path, body))
    assert len(entities) == 4
    assert skipped == {"hidden": count}


def test_the_hidden_count_holds_when_the_second_reading_stops_early(tmp_path: Path) -> None:
    """落ちたら、`display:none` の入れ子の `<svg>` の幅が 0 のとき(読み直しが途中で終わる)に、非表示の図形の数が負で出る。"""
    body = _svg(
        "100mm",
        "100mm",
        "0 0 100 100",
        '<svg display="none" width="0" height="0" viewBox="0 0 10 10"><rect width="4" height="4"/></svg>'
        '<rect x="10" width="2" height="2"/><rect x="20" width="2" height="2" visibility="hidden"/>',
    )
    _name, entities, skipped = svg_import.read_svg_report(_write(tmp_path, body))
    assert len(entities) == 4
    assert skipped == {"hidden": 1}


def test_an_svg_of_only_display_none_shapes_says_why(tmp_path: Path) -> None:
    """落ちたら、全部が `display:none` のSVGの断りが、落とした数を言わない。"""
    body = _svg("10mm", "10mm", "0 0 10 10", '<g style="display:none"><rect width="5" height="5"/></g>')
    with pytest.raises(ValueError, match="非表示の図形 1 個"):
        svg_import.read_svg_entities(_write(tmp_path, body))
