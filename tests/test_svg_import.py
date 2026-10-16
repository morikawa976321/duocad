"""verb `import_svg` と図形種別 `bezier` の契約。"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from duocad import kernel, region, sketch as sketch_module, svg_import
from duocad.document import Document
from document_support import replay_through_json
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
    session = Session(None, 0.2, models_dir=tmp_path)
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
    session = Session(None, 0.2, models_dir=tmp_path)
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.import_svg(sketch_id, str(write_svg(tmp_path, "feed.svg", SVG_MIXED)))

    delivered = session.state_meta()["log"][-1]["args"]
    assert isinstance(delivered["entities"], str)
    assert "配信しない" in delivered["entities"]

    stored = session.document.log.entries[-1].args["entities"]
    assert isinstance(stored, list) and stored and isinstance(stored[0], dict)
