"""verb `import_svg` と図形種別 `bezier` の契約。"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from duocad import kernel, region, sketch as sketch_module, svg_import
from duocad.document import Document
from duocad.mcp_server import DocumentTools, McpServer
from document_support import replay_through_json
from mcp_support import call_error
from duocad.region import RegionRef
from duocad.server import Session
from duocad.verbs import import_svg
from duocad.verbs.extrude import extrude

#: 幅100mm・高さ50mmの座標系に、矩形1つと直線+3次ベジエの閉じた形を置いたSVG。
#: **物理単位を持つ**(`width="100mm"`)ので、取り込んだ寸法はそのままmmになる。
SVG_WITH_UNITS = """<svg xmlns="http://www.w3.org/2000/svg"
     width="100mm" height="50mm" viewBox="0 0 100 50">
  <rect x="5" y="5" width="30" height="20"/>
  <path d="M 50 40 C 60 20, 80 20, 90 40 Z"/>
</svg>
"""

#: 同じ形だが**物理単位を持たない**(viewBoxだけ)。1ユーザー単位=1mmで入る。
SVG_VIEWBOX_ONLY = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50">
  <rect x="5" y="5" width="30" height="20"/>
</svg>
"""

#: 円・楕円・2次ベジエ・group transform を混ぜたもの(種別の振り分けを見る)。
SVG_MIXED = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100">
  <circle cx="30" cy="30" r="10"/>
  <ellipse cx="90" cy="30" rx="20" ry="10"/>
  <path d="M 130 60 Q 150 20, 170 60 Z"/>
  <g transform="translate(10,10)"><rect x="0" y="70" width="20" height="20"/></g>
</svg>
"""


#: path の楕円弧(cx=90, cy=30, rx=20, ry=10 の上半分と下半分)。`<ellipse>` 要素は `ellipse` 図形になるので、近似はこちらで見る。
SVG_ELLIPTICAL_ARC = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100">
  <path d="M 70 30 A 20 10 0 0 1 110 30 A 20 10 0 0 1 70 30 Z"/>
</svg>
"""


def write_svg(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def new_sketch(document: Document, plane: str = "XY") -> str:
    """検査の下ごしらえ: **先にスケッチを作る**(import_svgはもうこれを作らない)。"""
    return sketch_module.sketch_create(document, plane)


def test_physical_units_come_in_as_millimetres(tmp_path: Path) -> None:
    """`width="100mm"` のSVGは、書いてある寸法がそのままmmで入る。

    落ちたら `svg_import.MM_PER_INCH`(ppi)が効いていない —— svgelementsの既定(96ppi)だと
    全長がpxへ正規化され、100mmが377.95という数値で入る(**3.78倍で静かに大きくなる**)。
    """
    _name, entities = svg_import.read_svg_entities(
        write_svg(tmp_path, "units.svg", SVG_WITH_UNITS)
    )
    widths = [
        abs(entity["params"]["x1"] - entity["params"]["x0"])
        for entity in entities
        if entity["kind"] == "line"
    ]
    assert any(width == pytest.approx(30.0, rel=1e-5) for width in widths), (
        f"矩形の幅30mmが見つからない: {sorted(widths)}"
    )


def test_viewbox_only_svg_is_one_unit_per_millimetre(tmp_path: Path) -> None:
    """物理単位を持たないSVGは1ユーザー単位=1mm。単位つきのものと同じ寸法で入る。"""
    _name, entities = svg_import.read_svg_entities(
        write_svg(tmp_path, "viewbox.svg", SVG_VIEWBOX_ONLY)
    )
    widths = {
        round(abs(entity["params"]["x1"] - entity["params"]["x0"]), 6)
        for entity in entities
        if entity["kind"] == "line"
    }
    assert 30.0 in widths


def test_the_y_axis_is_flipped(tmp_path: Path) -> None:
    """SVGのYは下向き、スケッチのvは上向き。**反転しないと上下が逆に入る**。"""
    _name, entities = svg_import.read_svg_entities(
        write_svg(tmp_path, "flip.svg", SVG_WITH_UNITS)
    )
    values = [
        value
        for entity in entities
        if entity["kind"] == "line"
        for key, value in entity["params"].items()
        if key.startswith("y")
    ]
    for expected in (25.0, 45.0):
        assert any(value == pytest.approx(expected, rel=1e-5) for value in values), (
            f"矩形の上下が v=25/45 に無い: {sorted(values)}"
        )


def test_scale_multiplies_the_resolved_millimetres(tmp_path: Path) -> None:
    """`scale` は解決後のmmへ掛ける(単位を持たないSVGの寸法を合わせるための逃げ道)。"""
    path = write_svg(tmp_path, "scale.svg", SVG_VIEWBOX_ONLY)
    _name, doubled = svg_import.read_svg_entities(path, scale=2.0)
    widths = {
        round(abs(entity["params"]["x1"] - entity["params"]["x0"]), 6)
        for entity in doubled
        if entity["kind"] == "line"
    }
    assert 60.0 in widths


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_scale_rejects_unusable_values(tmp_path: Path, bad: float) -> None:
    """0・負・NaN・infは弾く(通すと図形が潰れるか、NaNが同じスケッチの領域を全部消す)。"""
    path = write_svg(tmp_path, "scale-bad.svg", SVG_VIEWBOX_ONLY)
    with pytest.raises(ValueError):
        svg_import.read_svg_entities(path, scale=bad)


def test_shapes_map_to_the_expected_entity_kinds(tmp_path: Path) -> None:
    """真円は `arc`、`<ellipse>` は `ellipse`、2次ベジエは `bezier`、直線は `line`。"""
    _name, entities = svg_import.read_svg_entities(
        write_svg(tmp_path, "mixed.svg", SVG_MIXED)
    )
    kinds = {entity["kind"] for entity in entities}
    assert kinds == {"line", "arc", "bezier", "ellipse"}
    assert all(entity["kind"] in sketch_module.ENTITY_KINDS for entity in entities)


@pytest.mark.parametrize(
    "transform,angle",
    [("", 0.0), (' transform="rotate(30 90 30)"', -30.0)],
)
def test_an_ellipse_element_becomes_one_ellipse(tmp_path: Path, transform: str, angle: float) -> None:
    """落ちたら、`<ellipse>` が楕円図形にならないか、Y反転で向き(SVG の時計回りはスケッチの反時計回りの逆)を取り違えている。"""
    body = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100">
  <ellipse cx="90" cy="30" rx="20" ry="10"{transform}/>
</svg>
'''
    _name, entities = svg_import.read_svg_entities(write_svg(tmp_path, "one.svg", body))
    assert [entity["kind"] for entity in entities] == ["ellipse"]
    params = entities[0]["params"]
    assert (params["cx"], params["cy"], params["width"], params["height"]) == pytest.approx((90.0, 70.0, 40.0, 20.0), rel=1e-5)
    assert params["angle"] == pytest.approx(angle, abs=1e-6)


def test_a_skewed_ellipse_element_stays_curves(tmp_path: Path) -> None:
    """斜めに歪めた `<ellipse>` は軸が直交しないので、楕円図形にせず楕円弧(ベジエ近似)のまま取り込む。"""
    body = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100">
  <ellipse cx="90" cy="30" rx="20" ry="10" transform="skewX(20)"/>
</svg>
"""
    _name, entities = svg_import.read_svg_entities(write_svg(tmp_path, "skew.svg", body))
    assert "ellipse" not in {entity["kind"] for entity in entities}
    assert "bezier" in {entity["kind"] for entity in entities}


def test_the_extruded_side_of_a_curve_stays_one_face(tmp_path: Path) -> None:
    """ベジエの輪郭を押し出しても側面は1枚のまま。**これが折れ線化しない理由**。

    直線1本 + ベジエ1本で閉じた領域なので、側面2枚 + 上下2枚 = 4面が正しい。
    折れ線化していると側面が分割数ぶんの平面に割れる(実測で面数4対130)。
    """
    document = Document()
    sketch_id = new_sketch(document)
    import_svg(document, sketch_id, write_svg(tmp_path, "curve.svg", SVG_WITH_UNITS))
    regions = region.regions(document, sketch_id)
    curved = min(regions, key=lambda item: item.area)  # 矩形(600mm^2)ではない方
    extrude(document, [RegionRef(sketch=sketch_id, index=curved.index)], 5.0, "join")
    shape = document.bodies[-1].shape
    assert kernel.face_count(shape) == 4, (
        f"側面が割れている(面数 {kernel.face_count(shape)})"
    )


def test_an_elliptical_arc_is_within_a_tenth_of_a_percent(tmp_path: Path) -> None:
    """落ちたら、楕円を含むSVGの寸法が曲線の途中で黙ってずれている(標準の係数なら半径の0.027%)。"""
    _name, entities = svg_import.read_svg_entities(
        write_svg(tmp_path, "ellipse.svg", SVG_ELLIPTICAL_ARC)
    )
    # 楕円弧(cx=90, cy=30, rx=20, ry=10 / 高さ100 → v=70)由来のベジエ
    arcs = [
        entity["params"]
        for entity in entities
        if entity["kind"] == "bezier" and 69.0 <= entity["params"]["x0"] <= 111.0
    ]
    assert arcs, "楕円由来のベジエが見つからない"
    for params in arcs:
        points = [
            (params["x0"], params["y0"]), (params["cx1"], params["cy1"]),
            (params["cx2"], params["cy2"]), (params["x1"], params["y1"]),
        ]
        for t in (0.25, 0.5, 0.75):
            weights = ((1 - t) ** 3, 3 * t * (1 - t) ** 2, 3 * t**2 * (1 - t), t**3)
            u = sum(weight * x for weight, (x, _) in zip(weights, points))
            v = sum(weight * y for weight, (_, y) in zip(weights, points))
            # 楕円を単位円へ戻した半径。1からのずれが半径に対する誤差の比
            error = abs(math.hypot((u - 90.0) / 20.0, (v - 70.0) / 10.0) - 1.0)
            assert error < 0.001, f"t={t} で楕円から {error:.3%} ずれている"


def test_the_whole_import_is_one_log_entry(tmp_path: Path) -> None:
    """図形が何個あってもログは1行。**Ctrl+Z 1回で取り込みごと戻る**。

    落ちたら `add_line` をN回発行する形に戻っている —— ロゴ1枚が数百行のログになり、
    取り消すのに Ctrl+Z を数百回押すことになる。
    """
    document = Document()
    sketch_id = new_sketch(document)
    entity_ids = import_svg(document, sketch_id, write_svg(tmp_path, "log.svg", SVG_MIXED))
    # ログは2行(sketch_create + import_svg)。import_svg自体は1回しか積まない。
    assert len(document.log.entries) == 2
    assert document.log.entries[-1].verb == "import_svg"
    assert len(entity_ids) > 1
    assert len(sketch_module.get_sketch(document, sketch_id).entities) == len(entity_ids)

    document.log.undo(document)  # import_svgだけ戻る
    assert sketch_module.get_sketch(document, sketch_id).entities == []
    assert document.sketches != []  # スケッチ自体はimport_svgが作ったものではないので残る


def test_import_svg_adds_to_an_existing_sketch_without_replacing_it(tmp_path: Path) -> None:
    """**既にある図形を消さない**。取り込みは足すだけで、置き換えではない。"""
    document = Document()
    sketch_id = new_sketch(document)
    sketch_module.add_circle(document, sketch_id, [0.0, 0.0], 5.0)
    before = len(sketch_module.get_sketch(document, sketch_id).entities)

    entity_ids = import_svg(
        document, sketch_id, write_svg(tmp_path, "add.svg", SVG_MIXED)
    )

    after = sketch_module.get_sketch(document, sketch_id).entities
    assert len(after) == before + len(entity_ids)
    assert {e.id for e in after} >= set(entity_ids)


def test_undo_after_two_imports_removes_only_the_most_recent_batch(tmp_path: Path) -> None:
    """**このタスクが作った契約そのもの**: undoは「そのverb呼び出しが足した図形だけ」を消す。

    落ちたら `_undo_import_svg` がスケッチ全体を消す(古い設計)か、全図形を無差別に消す形へ
    戻っている —— どちらも「開いているスケッチへ足す」という今回の意味と両立しない。
    """
    document = Document()
    sketch_id = new_sketch(document)
    hand_drawn = sketch_module.add_circle(document, sketch_id, [0.0, 0.0], 5.0)

    first_ids = import_svg(
        document, sketch_id, write_svg(tmp_path, "first.svg", SVG_MIXED)
    )
    second_ids = import_svg(
        document, sketch_id, write_svg(tmp_path, "second.svg", SVG_VIEWBOX_ONLY)
    )
    assert set(first_ids).isdisjoint(second_ids), "1回目と2回目のIDが重なっている"

    document.log.undo(document)  # 2回目のimport_svgだけを戻す

    remaining = {e.id for e in sketch_module.get_sketch(document, sketch_id).entities}
    assert remaining == {hand_drawn, *first_ids}, (
        f"2回目の取り込み分だけが消えるはずが一致しない: {sorted(remaining)} != "
        f"{sorted({hand_drawn, *first_ids})}"
    )


def test_there_is_no_verb_that_adds_a_single_bezier() -> None:
    """**`add_bezier` は存在しない**(人もAIもベジエを1本ずつは足せない。2026-08-26本人裁定)。

    種別としては一人前(領域・押し出し・反復・保存は効く)が、作る口は取り込みだけ。
    落ちたら、UIかMCPにベジエを1本足す口ができている —— それ自体は悪くないが、
    人が制御点をクリックで置けるかを確かめないまま開けてはいけない。
    """
    from duocad import catalog
    from duocad.oplog import registered_verbs

    assert "bezier" in sketch_module.ENTITY_KINDS
    assert "add_bezier" not in registered_verbs()
    assert "add_bezier" not in catalog.tool_names()
    assert "add_bezier" not in {command.name for command in catalog.COMMANDS}


def test_replay_does_not_read_the_svg_file(tmp_path: Path) -> None:
    """**元のSVGを消しても**ログから同じ形が戻る(形はログの中にある)。"""
    path = write_svg(tmp_path, "gone.svg", SVG_MIXED)
    document = Document()
    sketch_id = new_sketch(document)
    import_svg(document, sketch_id, path)
    before = [
        (entity.kind, dict(entity.params))
        for entity in sketch_module.get_sketch(document, sketch_id).entities
    ]
    path.unlink()

    restored = replay_through_json(document)
    after = [
        (entity.kind, dict(entity.params))
        for entity in sketch_module.get_sketch(restored, sketch_id).entities
    ]
    assert after == before


def test_undo_and_redo_round_trip(tmp_path: Path) -> None:
    """undo→redo で図形が同じ数・同じIDで戻る(IDをログに記録している理由)。"""
    document = Document()
    sketch_id = new_sketch(document)
    import_svg(document, sketch_id, write_svg(tmp_path, "roundtrip.svg", SVG_MIXED))
    ids = [e.id for e in sketch_module.get_sketch(document, sketch_id).entities]
    document.log.undo(document)
    document.log.redo(document)
    assert [
        e.id for e in sketch_module.get_sketch(document, sketch_id).entities
    ] == ids


def test_imported_entities_do_not_create_named_parameters(tmp_path: Path) -> None:
    """取り込んだ図形は `d1` `d2` … を作らない。

    制御点の座標は人が打ち直す寸法ではない。名前つきの値に登録すると、ロゴ1枚で
    数千個がSketchPanelの図形一覧に並び、しかも `register_value` は1から線形に採番するので
    O(n^2)になる(小さなSVGで既に309個という実測がある)。
    """
    document = Document()
    sketch_id = new_sketch(document)
    import_svg(document, sketch_id, write_svg(tmp_path, "params.svg", SVG_MIXED))
    assert document.parameters == {}


def test_an_unknown_sketch_id_is_refused(tmp_path: Path) -> None:
    """`sketch_id` が実在しなければ拒否する。**doc.sketches にもログにも何も残さない**。"""
    document = Document()
    path = write_svg(tmp_path, "orphan.svg", SVG_MIXED)
    with pytest.raises(KeyError):
        import_svg(document, "sketch-does-not-exist", path)
    assert document.sketches == [] and document.log.entries == []


def test_a_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    document = Document()
    sketch_id = new_sketch(document)
    before = len(document.log.entries)
    with pytest.raises(FileNotFoundError):
        import_svg(document, sketch_id, tmp_path / "no-such-file.svg")
    assert sketch_module.get_sketch(document, sketch_id).entities == []
    assert len(document.log.entries) == before


def test_a_broken_file_raises_value_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.svg"
    path.write_text("<svg><this is not xml", encoding="utf-8")
    document = Document()
    sketch_id = new_sketch(document)
    before = len(document.log.entries)
    with pytest.raises(ValueError):
        import_svg(document, sketch_id, path)
    assert sketch_module.get_sketch(document, sketch_id).entities == []
    assert len(document.log.entries) == before


def test_an_svg_without_geometry_is_refused(tmp_path: Path) -> None:
    """文字だけのSVGは断る。**黙って空のスケッチを作らない**(何も出ない理由が分からない)。"""
    path = write_svg(
        tmp_path,
        "text.svg",
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        '<text x="1" y="5">hello</text></svg>',
    )
    document = Document()
    sketch_id = new_sketch(document)
    before = len(document.log.entries)
    with pytest.raises(ValueError, match="取り込める図形が無い"):
        import_svg(document, sketch_id, path)
    assert sketch_module.get_sketch(document, sketch_id).entities == []
    assert len(document.log.entries) == before


def test_too_many_entities_is_refused_not_truncated(tmp_path: Path) -> None:
    """上限超えは**断る**。黙って切り詰めると、欠けた輪郭で領域検出が別物を返す。"""
    segments = " ".join(
        f"M {index} 0 L {index} 1" for index in range(svg_import.MAX_ENTITIES + 10)
    )
    path = write_svg(
        tmp_path,
        "huge.svg",
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 6000 10">'
        f'<path d="{segments}"/></svg>',
    )
    document = Document()
    sketch_id = new_sketch(document)
    before = len(document.log.entries)
    with pytest.raises(ValueError, match="図形が多すぎる"):
        import_svg(document, sketch_id, path)
    assert sketch_module.get_sketch(document, sketch_id).entities == []
    assert len(document.log.entries) == before


def test_session_import_svg_reports_what_it_made(tmp_path: Path) -> None:
    session = Session(0.2, models_dir=tmp_path)
    sketch_id = session.sketch_create("XY")["sketch_id"]
    applied = session.import_svg(
        sketch_id, str(write_svg(tmp_path, "session.svg", SVG_MIXED))
    )
    assert applied["verb"] == "import_svg"
    assert applied["source_name"] == "session.svg"
    assert applied["entity_count"] > 1
    assert applied["sketch_id"] == sketch_id
    entity_ids = set(applied["entity_ids"])
    assert entity_ids
    assert entity_ids <= {e.id for e in session.document.sketches[-1].entities}


def test_the_state_feed_does_not_carry_every_entity(tmp_path: Path) -> None:
    """状態配信の射影で図形を伏せる。"""
    session = Session(0.2, models_dir=tmp_path)
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.import_svg(sketch_id, str(write_svg(tmp_path, "feed.svg", SVG_MIXED)))

    delivered = session.state_meta()["log"][-1]["args"]
    assert isinstance(delivered["entities"], str)
    assert "配信しない" in delivered["entities"]

    stored = session.document.log.entries[-1].args["entities"]
    assert isinstance(stored, list) and stored and isinstance(stored[0], dict)


def test_too_many_ellipse_elements_are_refused(tmp_path: Path) -> None:
    """落ちたら、`<ellipse>` だけのSVGが図形数の上限(`MAX_ENTITIES`)を素通りする。"""
    ellipses = "".join(
        f'<ellipse cx="{i % 100}" cy="{i // 100}" rx="0.4" ry="0.2"/>' for i in range(svg_import.MAX_ENTITIES + 1)
    )
    body = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100">{ellipses}</svg>'
    with pytest.raises(ValueError, match="多すぎる"):
        svg_import.read_svg_entities(write_svg(tmp_path, "many.svg", body))


# --------------------------------------------------------------------------------------
# 非相似な変換の下の円・楕円弧・角丸(輪郭が閉じ、面積が行列式どおりになる)
# --------------------------------------------------------------------------------------
def _stretch_after_rotation(sx: float, sy: float, degrees: float) -> tuple:
    """`scale(sx,sy) rotate(degrees)` の `matrix(a b c d e f)`。"""
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    return (sx * c, sy * s, -sx * s, sy * c, 0.0, 0.0)


#: svgelements の変換済み楕円弧(共役半径)が主軸に見えなくなる変換。
NON_SIMILARITY = {
    "skew": (1.0, 0.0, math.tan(math.radians(20.0)), 1.0, 0.0, 0.0),
    "stretch-after-rotation": _stretch_after_rotation(2.0, 1.0, 45.0),
    "stretch-after-rotation-2": _stretch_after_rotation(1.5, 0.7, 30.0),
    "shear": (1.0, 0.0, 0.6, 0.8, 10.0, 10.0),
    "general": (-0.5, 0.3, 0.4, 0.9, 80.0, 20.0),
}
#: 円は円のまま(相似)だが、反転は svgelements の t の向きを裏返す。
SIMILARITY = {
    "flip-y": (1.0, 0.0, 0.0, -1.0, 0.0, 200.0),
    "mirror-x": (-1.0, 0.0, 0.0, 1.0, 200.0, 0.0),
    "rotate": (math.cos(0.7), math.sin(0.7), -math.sin(0.7), math.cos(0.7), 20.0, 10.0),
    "mirror-scale": (-2.0, 0.0, 0.0, 2.0, 220.0, 0.0),
}


def _matrix_attribute(values: tuple) -> str:
    return "matrix(" + " ".join(repr(value) for value in values) + ")"


def _determinant(values: tuple) -> float:
    a, b, c, d = values[:4]
    return abs(a * d - b * c)


def _apply(values: tuple, x: float, y: float) -> tuple:
    a, b, c, d, e, f = values
    return (a * x + c * y + e, b * x + d * y + f)


def _import_regions(tmp_path: Path, inner: str, transform: tuple) -> list:
    """viewBox 200x200 の SVG に `inner` を変換つきの `<g>` で置いて取り込み、領域を返す。"""
    body = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">'
        f'<g transform="{_matrix_attribute(transform)}">{inner}</g></svg>'
    )
    document = Document()
    sketch_id = new_sketch(document)
    import_svg(document, sketch_id, write_svg(tmp_path, "affine.svg", body))
    return region.regions(document, sketch_id)


@pytest.mark.parametrize("name", sorted(NON_SIMILARITY))
@pytest.mark.parametrize(
    "shape,area",
    [
        ('<circle cx="30" cy="40" r="10"/>', math.pi * 100.0),
        ('<ellipse cx="30" cy="40" rx="20" ry="10"/>', math.pi * 200.0),
    ],
    ids=["circle", "ellipse"],
)
def test_a_circle_or_ellipse_under_a_non_similarity_transform_is_the_transformed_ellipse(
    tmp_path: Path, name: str, shape: str, area: float
) -> None:
    """落ちたら、円が真円として取り込まれて面積が合わない(`matrix(1 0 0.6 0.8 ..)` で約25%過大)か、輪郭が閉じず領域が出ない。"""
    matrix = NON_SIMILARITY[name]
    regions = _import_regions(tmp_path, shape, matrix)
    assert len(regions) == 1
    assert regions[0].area == pytest.approx(area * _determinant(matrix), rel=2e-3)
    center = _apply(matrix, 30.0, 40.0)
    assert regions[0].centroid[:2] == pytest.approx((center[0], 200.0 - center[1]), abs=0.05)


@pytest.mark.parametrize("name", sorted(SIMILARITY))
def test_a_circle_under_a_similarity_transform_stays_an_arc(tmp_path: Path, name: str) -> None:
    """相似変換(回転・等倍・反転)では円は円のまま `arc` で取り込む。"""
    matrix = SIMILARITY[name]
    body = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">'
        f'<g transform="{_matrix_attribute(matrix)}"><circle cx="30" cy="40" r="10"/></g></svg>'
    )
    _name, entities = svg_import.read_svg_entities(write_svg(tmp_path, "similar.svg", body))
    assert {entity["kind"] for entity in entities} == {"arc"}
    scale = math.sqrt(_determinant(matrix))
    for entity in entities:
        assert entity["params"]["radius"] == pytest.approx(10.0 * scale, rel=1e-6)
    regions = _import_regions(tmp_path, '<circle cx="30" cy="40" r="10"/>', matrix)
    assert [round(item.area, 3) for item in regions] == [round(math.pi * 100.0 * _determinant(matrix), 3)]


def _elliptical_arc(center: tuple, rx: float, ry: float, phi: float, start_t: float, sweep: float) -> tuple:
    """媒介変数 t の区間 [start_t, start_t + sweep] の楕円弧を `(始点, "A ...")` で返す(向きと大きさは解析的に決まる)。"""
    def point(t: float) -> tuple:
        x, y = rx * math.cos(t), ry * math.sin(t)
        return (
            center[0] + x * math.cos(phi) - y * math.sin(phi),
            center[1] + x * math.sin(phi) + y * math.cos(phi),
        )

    end = point(start_t + sweep)
    large = 1 if abs(sweep) > math.pi else 0
    positive = 1 if sweep > 0 else 0
    command = f"A {rx!r} {ry!r} {math.degrees(phi)!r} {large} {positive} {end[0]!r} {end[1]!r}"
    return point(start_t), command


ELLIPSE = {"center": (100.0, 90.0), "rx": 40.0, "ry": 15.0, "phi": math.radians(30.0)}


@pytest.mark.parametrize("name", sorted({**NON_SIMILARITY, **SIMILARITY}))
def test_an_ellipse_made_of_two_arcs_keeps_its_area_and_position(tmp_path: Path, name: str) -> None:
    """落ちたら、楕円弧の端点が変換後にずれて(せん断で最大数mm)隣の弧と離れ、輪郭が閉じず領域が出ない。"""
    matrix = {**NON_SIMILARITY, **SIMILARITY}[name]
    start, first = _elliptical_arc(**{**ELLIPSE, "start_t": 0.4, "sweep": 3.0})
    _start, second = _elliptical_arc(**{**ELLIPSE, "start_t": 3.4, "sweep": 2.0 * math.pi - 3.0})
    regions = _import_regions(tmp_path, f'<path d="M {start[0]!r} {start[1]!r} {first} {second} Z"/>', matrix)
    assert len(regions) == 1
    assert regions[0].area == pytest.approx(
        math.pi * ELLIPSE["rx"] * ELLIPSE["ry"] * _determinant(matrix), rel=2e-3
    )
    moved = _apply(matrix, *ELLIPSE["center"])
    assert regions[0].centroid[:2] == pytest.approx((moved[0], 200.0 - moved[1]), abs=0.1)


@pytest.mark.parametrize("name", ["skew", "general", "flip-y", "stretch-after-rotation"])
@pytest.mark.parametrize("start_t,sweep", [(0.3, 1.2), (0.3, 4.4), (0.3, -1.2), (1.0, -4.4)])
def test_an_elliptical_sector_keeps_its_sweep(tmp_path: Path, name: str, start_t: float, sweep: float) -> None:
    """落ちたら、弧の掃引の向きか大きさ(大弧か小弧か)が変換後に取り違えられている(扇形の面積が合わない)。"""
    matrix = {**NON_SIMILARITY, **SIMILARITY}[name]
    start, command = _elliptical_arc(**{**ELLIPSE, "start_t": start_t, "sweep": sweep})
    center = ELLIPSE["center"]
    d = f"M {center[0]!r} {center[1]!r} L {start[0]!r} {start[1]!r} {command} Z"
    regions = _import_regions(tmp_path, f'<path d="{d}"/>', matrix)
    assert len(regions) == 1
    expected = 0.5 * ELLIPSE["rx"] * ELLIPSE["ry"] * abs(sweep) * _determinant(matrix)
    assert regions[0].area == pytest.approx(expected, rel=2e-3)


@pytest.mark.parametrize("name", sorted({**NON_SIMILARITY, **SIMILARITY}))
def test_a_rounded_rectangle_with_unequal_radii_keeps_its_area(tmp_path: Path, name: str) -> None:
    """落ちたら、角丸 `<rect>`(rx≠ry の楕円弧)の輪郭が変換後に開く(反転グループ下で領域が出ない)。"""
    matrix = {**NON_SIMILARITY, **SIMILARITY}[name]
    rect = '<rect x="10" y="10" width="60" height="40" rx="12" ry="5"/>'
    regions = _import_regions(tmp_path, rect, matrix)
    assert len(regions) == 1
    expected = (60.0 * 40.0 - (4.0 - math.pi) * 12.0 * 5.0) * _determinant(matrix)
    assert regions[0].area == pytest.approx(expected, rel=2e-3)


# --------------------------------------------------------------------------------------
# 長さ0の曲線・半径0の弧
# --------------------------------------------------------------------------------------
def _triangle_with(segment: str) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        f'<path d="M 5 5 L 10 10 {segment} L 20 5 Z"/></svg>'
    )


@pytest.mark.parametrize("segment", ["c 0 0 0 0 0 0", "C 10 10 10 10 10 10", "q 0 0 0 0", "s 0 0 0 0", "t 0 0"])
def test_a_zero_length_curve_is_dropped_like_a_zero_length_line(tmp_path: Path, segment: str) -> None:
    """落ちたら、長さ0のベジエが1つあるだけでSVG全体の取り込みが ValueError になる(線分・弧は黙って捨てるのに)。"""
    document = Document()
    sketch_id = new_sketch(document)
    import_svg(document, sketch_id, write_svg(tmp_path, "zero.svg", _triangle_with(segment)))
    kinds = [entity.kind for entity in sketch_module.get_sketch(document, sketch_id).entities]
    assert kinds == ["line", "line", "line"]
    assert [round(item.area, 6) for item in region.regions(document, sketch_id)] == [37.5]


def test_a_closed_curve_with_distinct_control_points_is_kept(tmp_path: Path) -> None:
    """始点=終点でも制御点が離れていれば長さのある曲線(長さ0の判定は4点が全部同じ点のときだけ)。"""
    _name, entities = svg_import.read_svg_entities(
        write_svg(tmp_path, "loop.svg", _triangle_with("c 3 0 -3 0 0 0"))
    )
    assert [entity["kind"] for entity in entities] == ["line", "bezier", "line", "line"]


@pytest.mark.parametrize("radii", ["0 0", "0 5", "5 0"])
def test_a_zero_radius_arc_is_a_straight_line(tmp_path: Path, radii: str) -> None:
    """落ちたら、半径0の弧(SVG では端点を結ぶ直線)が捨てられて輪郭に穴が空く。"""
    body = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        f'<path d="M 10 10 A {radii} 0 0 1 50 10 L 50 50 Z"/></svg>'
    )
    document = Document()
    sketch_id = new_sketch(document)
    import_svg(document, sketch_id, write_svg(tmp_path, "zero-radius.svg", body))
    assert [entity.kind for entity in sketch_module.get_sketch(document, sketch_id).entities] == ["line"] * 3
    assert [round(item.area, 6) for item in region.regions(document, sketch_id)] == [800.0]


# --------------------------------------------------------------------------------------
# 描かれない定義
# --------------------------------------------------------------------------------------
SYMBOL = '<symbol id="s" viewBox="0 0 10 10"><rect x="0" y="0" width="10" height="10"/></symbol>'
VISIBLE = '<rect x="50" y="50" width="5" height="5"/>'
VISIBLE_CORNERS = {(50, 50), (55, 50), (55, 45), (50, 45)}


def _drawn_corners(tmp_path: Path, inner: str) -> set:
    """取り込んだ図形の始点 (x0, y0) の集合(丸め済み)。"""
    body = (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'viewBox="0 0 100 100">{inner}</svg>'
    )
    _name, entities = svg_import.read_svg_entities(write_svg(tmp_path, "defs.svg", body))
    assert {entity["kind"] for entity in entities} == {"line"}
    return {(round(entity["params"]["x0"]), round(entity["params"]["y0"])) for entity in entities}


def test_a_symbol_outside_defs_is_drawn_only_where_it_is_used(tmp_path: Path) -> None:
    """落ちたら、`<defs>` の外の `<symbol>` が定義位置でも図形になる(`<use>` の分と二重に入る)。"""
    use = '<use xlink:href="#s" x="30" y="30" width="10" height="10"/>'
    only_use = _drawn_corners(tmp_path, SYMBOL + use)
    assert only_use == {(30, 70), (40, 70), (40, 60), (30, 60)}
    assert _drawn_corners(tmp_path, f"<defs>{SYMBOL}</defs>" + use) == only_use


@pytest.mark.parametrize(
    "definition",
    [
        '<mask id="m"><rect x="0" y="0" width="10" height="10"/></mask>',
        '<marker id="k"><path d="M 0 0 L 5 5 L 0 5 Z"/></marker>',
        SYMBOL,
        '<g><mask id="m"><rect x="0" y="0" width="10" height="10"/></mask></g>',
    ],
)
def test_undrawn_definitions_add_no_shapes(tmp_path: Path, definition: str) -> None:
    """`<mask>` `<marker>` `<symbol>` の中身は、参照されない限り取り込まない(隣の描かれる図形だけが入る)。"""
    assert _drawn_corners(tmp_path, definition + VISIBLE) == VISIBLE_CORNERS


@pytest.mark.parametrize(
    "definitions",
    [
        # `<defs>` の中の `<g>` を `<use>` が展開するとき、その中の `<mask>` の図形まで平らに並んで描かれていた
        f'<defs><g id="t"><mask id="m"><rect x="0" y="0" width="10" height="10"/></mask>{VISIBLE}</g></defs>',
        # `<symbol>` の中の `<mask>`(defs の中でも外でも)
        f'<defs><symbol id="t"><mask id="m"><rect x="0" y="0" width="10" height="10"/></mask>{VISIBLE}</symbol></defs>',
        f'<symbol id="t"><mask id="m"><rect x="0" y="0" width="10" height="10"/></mask>{VISIBLE}</symbol>',
        f'<defs><g id="t"><symbol id="x"><rect x="0" y="0" width="9" height="9"/></symbol>{VISIBLE}</g></defs>',
    ],
)
def test_a_definition_inside_a_used_definition_is_not_drawn(tmp_path: Path, definitions: str) -> None:
    """`<use>` が参照する `<g>` `<symbol>` の中の、描かれない定義(`<mask>` など)は取り込まない。
    落ちたら、defs や symbol の中へ入れた mask の図形が、参照した分だけ余計に入る。"""
    corners = _drawn_corners(tmp_path, definitions + '<use xlink:href="#t"/>')
    assert corners == VISIBLE_CORNERS


def test_a_file_with_only_undrawn_definitions_is_refused(tmp_path: Path) -> None:
    """描かれる図形が無ければ、空のスケッチを作らず断る。"""
    body = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">{SYMBOL}</svg>'
    with pytest.raises(ValueError, match="取り込める図形が無い"):
        svg_import.read_svg_entities(write_svg(tmp_path, "only-symbol.svg", body))


@pytest.mark.parametrize(
    "switch",
    [
        # Illustrator の書き出し: foreignObject は requiredExtensions つきで選ばれず、絵は2つ目の <g>
        '<switch><foreignObject requiredExtensions="http://ns.adobe.com/AdobeIllustrator/10.0/" width="1" height="1"/>'
        f"<g>{VISIBLE}</g></switch>",
        f'<switch><g systemLanguage="ja"><rect x="5" y="5" width="9" height="9"/></g><g>{VISIBLE}</g></switch>',
        f"<switch><title>t</title><g>{VISIBLE}</g></switch>",
    ],
)
def test_a_switch_draws_only_its_first_unconditional_branch(tmp_path: Path, switch: str) -> None:
    """`<switch>` は描かれる枝が1つ。条件属性つきの枝は条件が評価できないので選ばない。
    落ちたら、選ばれない枝まで取り込む、または選ぶ枝を誤って絵が丸ごと消える(Illustrator の書き出し)。"""
    assert _drawn_corners(tmp_path, switch) == VISIBLE_CORNERS


# --------------------------------------------------------------------------------------
# Y反転の基準(viewport の高さ)
# --------------------------------------------------------------------------------------
def _lines(tmp_path: Path, svg_attributes: str, d: str = "M 0 0 L 200 0 L 200 100") -> list:
    body = f'<svg xmlns="http://www.w3.org/2000/svg" {svg_attributes}><path d="{d}"/></svg>'
    _name, entities = svg_import.read_svg_entities(write_svg(tmp_path, "viewport.svg", body))
    return [
        (entity["params"]["x0"], entity["params"]["y0"], entity["params"]["x1"], entity["params"]["y1"])
        for entity in entities
    ]


@pytest.mark.parametrize(
    "attributes",
    [
        'width="100mm" height="50mm" viewBox="0 0 200 100"',
        'width="100mm" viewBox="0 0 200 100"',
        'height="50mm" viewBox="0 0 200 100"',
        'width="100mm" height="100%" viewBox="0 0 200 100"',
    ],
)
def test_a_missing_side_follows_the_view_box_aspect_ratio(tmp_path: Path, attributes: str) -> None:
    """落ちたら、片方の辺だけ書かれた SVG が縦(横)にずれる(viewBox を縦横比の違う viewport に中央寄せしている)。"""
    got = [value for line in _lines(tmp_path, attributes) for value in line]
    assert got == pytest.approx([0.0, 50.0, 100.0, 50.0, 100.0, 50.0, 100.0, 0.0], rel=1e-5, abs=1e-4)


@pytest.mark.parametrize("attributes", ["", 'width="100mm"'])
def test_without_a_height_the_lowest_point_sits_on_v_zero(tmp_path: Path, attributes: str) -> None:
    """サイズも viewBox も無い SVG は高さの情報が無い。図形の下端(y 最大)を v=0 に置く(1000mm 離れない)。"""
    lines = _lines(tmp_path, attributes, "M 1 2 L 3 2 L 3 4 Z")
    assert lines == [(1.0, 2.0, 3.0, 2.0), (3.0, 2.0, 3.0, 0.0), (3.0, 0.0, 1.0, 2.0)]


@pytest.mark.parametrize("height", ["0", "-5", "auto"])
def test_an_unusable_height_is_refused_by_name(tmp_path: Path, height: str) -> None:
    """0以下・読めない高さは Y 反転の基準にできない。『図形が無い』ではなく height が原因だと断る。"""
    with pytest.raises(ValueError, match="height"):
        _lines(tmp_path, f'height="{height}" viewBox="0 0 200 100"')


def test_a_hidden_root_is_refused_as_value_error(tmp_path: Path) -> None:
    """ルートが描画されない(`display:none`)SVG は AttributeError ではなく ValueError で断る。"""
    with pytest.raises(ValueError, match="描画される"):
        _lines(tmp_path, 'style="display:none" viewBox="0 0 200 100"')


# --------------------------------------------------------------------------------------
# 失敗は何も残さない(領域が計算できない取り込み)
# --------------------------------------------------------------------------------------
HUGE_SCALE = 1.0e300


def test_an_import_whose_regions_cannot_be_computed_leaves_nothing(tmp_path: Path) -> None:
    """落ちたら、取り込んだ図形とログが残ったまま、以後 `regions()` が毎回失敗するスケッチになる。"""
    document = Document()
    sketch_id = new_sketch(document)
    sketch_module.add_circle(document, sketch_id, [0.0, 0.0], 5.0)
    entities_before = [e.id for e in sketch_module.get_sketch(document, sketch_id).entities]
    log_before = len(document.log.entries)
    parameters_before = dict(document.parameters)

    with pytest.raises(ValueError, match="領域を計算できない"):
        import_svg(document, sketch_id, write_svg(tmp_path, "huge.svg", SVG_MIXED), scale=HUGE_SCALE)

    assert [e.id for e in sketch_module.get_sketch(document, sketch_id).entities] == entities_before
    assert len(document.log.entries) == log_before
    assert document.parameters == parameters_before
    assert len(region.regions(document, sketch_id)) == 1  # 元のスケッチは今までどおり使える


def test_a_failed_mcp_import_leaves_the_document_untouched(session: Session, tmp_path: Path) -> None:
    """MCP の `import_svg` は verb の後で領域数を数える。失敗の封筒を返すなら文書は変わっていない。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    log_before = len(session.document.log.entries)
    server = McpServer(DocumentTools(session))

    error = call_error(
        server,
        "import_svg",
        {"sketch_id": sketch_id, "path": str(write_svg(tmp_path, "huge.svg", SVG_MIXED)), "scale": HUGE_SCALE},
    )

    assert error["code"] == "invalid_argument"
    assert len(session.document.log.entries) == log_before
    assert session.document.sketches[0].entities == []
