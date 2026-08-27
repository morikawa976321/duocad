"""verb `import_svg` と図形種別 `bezier` の契約。

## このファイルが固定する6つ

1. **単位はmm** — 物理単位を持つSVG(`width="100mm"`)はその寸法で、`viewBox` しか無いSVGは
   1ユーザー単位=1mmで入る。**Yは反転する**(SVGは下向き、スケッチのvは上向き)。
2. **曲線は曲線のまま** — ベジエは折れ線に落ちない。押し出した側面が線分の数だけ平面に
   割れないことを面数で見る。
3. **ログ1行・undo1回** — ロゴ1枚が数百行にならない。`add_bezier` というverbは**無い**。
4. **再生は外部ファイルを読まない** — SVGを消してもログから同じ形が戻る。
5. **取り込んだ図形は名前つきの値を持たない** — SketchPanelの図形一覧がロゴ1枚で埋まらない。
6. **貼る先は「いま開いているスケッチ」(`sketch_id`)だけ** — 新しいスケッチは作らない
   (2026-08-28。それまでは「新しいスケッチを1枚作って全部そこへ入れる」形だった)。
   `sketch_id` が実在しなければ拒否し、既存の図形は消えず、新しく足した分だけが増える。
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from duocad import kernel, region, sketch as sketch_module, svg_import
from duocad.document import Document
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


def write_svg(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def new_sketch(document: Document, plane: str = "XY") -> str:
    """検査の下ごしらえ: **先にスケッチを作る**(import_svgはもうこれを作らない)。"""
    return sketch_module.sketch_create(document, plane)


# --------------------------------------------------------------------------------------
# 1: 単位と向き
# --------------------------------------------------------------------------------------
def test_physical_units_come_in_as_millimetres(tmp_path: Path) -> None:
    """`width="100mm"` のSVGは、書いてある寸法がそのままmmで入る。

    落ちたら `svg_import.MM_PER_INCH`(ppi)が効いていない —— svgelementsの既定(96ppi)だと
    全長がpxへ正規化され、100mmが377.95という数値で入る(**3.78倍で静かに大きくなる**)。

    **厳密一致では比べない**。svgelementsのmm→pxが丸めた定数を使うので0.54ppmずれる
    (`svg_import` のモジュールdocstring)。ここで見たいのは3.78倍かどうかである。
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
    """SVGのYは下向き、スケッチのvは上向き。**反転しないと上下が逆に入る**。

    高さ50の座標系で y=5..25 に置いた矩形は、v=25..45 に入るのが正しい。反転していなければ
    v=5..25 のままになる。
    """
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


# --------------------------------------------------------------------------------------
# 2: 種別の振り分けと、曲線が曲線のままであること
# --------------------------------------------------------------------------------------
def test_shapes_map_to_the_expected_entity_kinds(tmp_path: Path) -> None:
    """真円は `arc`、楕円と2次ベジエは `bezier`、直線は `line`。

    2次ベジエ(Q)は**次数上げで厳密に**3次になるので、近似の話ではない。
    """
    _name, entities = svg_import.read_svg_entities(
        write_svg(tmp_path, "mixed.svg", SVG_MIXED)
    )
    kinds = {entity["kind"] for entity in entities}
    assert kinds == {"line", "arc", "bezier"}
    assert all(entity["kind"] in sketch_module.ENTITY_KINDS for entity in entities)


def test_a_cubic_bezier_becomes_one_entity_not_a_polyline(tmp_path: Path) -> None:
    """3次ベジエ1本が図形1つになる(折れ線に落ちない)。

    落ちたら曲線が平坦化されている —— 図形数・パラメータ数・押し出し後の面数が
    まとめて膨らむ(実測で面数4対130)。
    """
    _name, entities = svg_import.read_svg_entities(
        write_svg(tmp_path, "curve.svg", SVG_WITH_UNITS)
    )
    assert [entity["kind"] for entity in entities].count("bezier") == 1


def test_the_extruded_side_of_a_curve_stays_one_face(tmp_path: Path) -> None:
    """ベジエの輪郭を押し出しても側面は1枚のまま。**これが折れ線化しない理由**。

    直線1本 + ベジエ1本で閉じた領域なので、側面2枚 + 上下2枚 = 4面が正しい。
    折れ線化していると側面が分割数ぶんの平面に割れる。
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
    """楕円弧はベジエ近似(厳密にならない)。**誤差は半径の0.1%以内**であること。

    ここが緩むと、楕円を含むSVGの寸法が黙ってずれる。標準の90度分割で実測0.027%。
    """
    _name, entities = svg_import.read_svg_entities(
        write_svg(tmp_path, "ellipse.svg", SVG_MIXED)
    )
    # 楕円(cx=90, cy=30, rx=20, ry=10 / 高さ100 → v=70)の制御点が乗る箱を見る。
    beziers = [entity for entity in entities if entity["kind"] == "bezier"]
    xs = [entity["params"][key] for entity in beziers for key in ("x0", "x1")]
    on_ellipse = [x for x in xs if 69.0 <= x <= 111.0]
    assert on_ellipse, "楕円由来のベジエが見つからない"
    for x in on_ellipse:
        # 端点は必ず楕円の上にある(近似されるのは端点の間だけ)。
        assert min(abs(x - 70.0), abs(x - 90.0), abs(x - 110.0)) < 20.0 * 0.001


# --------------------------------------------------------------------------------------
# 3: ログの粒度と、ベジエを1本ずつ足すverbが無いこと
# --------------------------------------------------------------------------------------
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


# --------------------------------------------------------------------------------------
# 4: 再生(保存して開く)
# --------------------------------------------------------------------------------------
def test_replay_does_not_read_the_svg_file(tmp_path: Path) -> None:
    """**元のSVGを消しても**ログから同じ形が戻る(形はログの中にある)。

    SVG本文を埋めて再生時にパースし直す形だと、svgelementsの版が変わった日に
    同じログから別の形が出てくる。ここはそれを禁じている。
    """
    path = write_svg(tmp_path, "gone.svg", SVG_MIXED)
    document = Document()
    sketch_id = new_sketch(document)
    import_svg(document, sketch_id, path)
    before = [
        (entity.kind, dict(entity.params))
        for entity in sketch_module.get_sketch(document, sketch_id).entities
    ]
    path.unlink()

    restored = Document()
    restored.log = type(document.log).from_json(document.log.to_json())
    restored.log.replay(restored)
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


# --------------------------------------------------------------------------------------
# 5: パラメータを増やさないこと
# --------------------------------------------------------------------------------------
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


def test_imported_entities_still_resolve_and_draw(tmp_path: Path) -> None:
    """素の数値のままでも `entity_params` は解決でき、領域も出る(名前つきでなくても一人前)。"""
    document = Document()
    sketch_id = new_sketch(document)
    import_svg(document, sketch_id, write_svg(tmp_path, "draw.svg", SVG_MIXED))
    sketch = sketch_module.get_sketch(document, sketch_id)
    for entity in sketch.entities:
        values = sketch_module.entity_params(document, sketch_id, entity.id)
        assert all(math.isfinite(value) for value in values.values())
    assert region.regions(document, sketch_id)


# --------------------------------------------------------------------------------------
# 6: 貼る先は「開いているスケッチ」だけ
# --------------------------------------------------------------------------------------
def test_an_unknown_sketch_id_is_refused(tmp_path: Path) -> None:
    """`sketch_id` が実在しなければ拒否する。**doc.sketches にもログにも何も残さない**。"""
    document = Document()
    path = write_svg(tmp_path, "orphan.svg", SVG_MIXED)
    with pytest.raises(KeyError):
        import_svg(document, "sketch-does-not-exist", path)
    assert document.sketches == [] and document.log.entries == []


# --------------------------------------------------------------------------------------
# 失敗の契約
# --------------------------------------------------------------------------------------
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
    with pytest.raises(ValueError, match="取り込める図形が無い"):
        import_svg(document, sketch_id, path)
    assert sketch_module.get_sketch(document, sketch_id).entities == []


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
    with pytest.raises(ValueError, match="図形が多すぎる"):
        import_svg(document, sketch_id, path)
    assert sketch_module.get_sketch(document, sketch_id).entities == []


# --------------------------------------------------------------------------------------
# Session(WS/MCPが通る経路)
# --------------------------------------------------------------------------------------
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


def test_session_import_svg_rejects_an_unknown_sketch(tmp_path: Path) -> None:
    """WS/MCPが通る経路でも、実在しない `sketch_id` は拒否する。"""
    session = Session(None, 0.2, models_dir=tmp_path)
    path = str(write_svg(tmp_path, "orphan.svg", SVG_MIXED))
    with pytest.raises(KeyError):
        session.import_svg("sketch-does-not-exist", path)
    assert session.document.sketches == [] and session.document.log.entries == []


def test_the_state_feed_does_not_carry_every_entity(tmp_path: Path) -> None:
    """状態配信の射影で図形を伏せる。

    `args["entities"]` はロゴ1枚で数百件になり、`state_meta()` は**操作のたびに全接続へ
    配られる**。素通しにすると1操作ごとにそれが流れる(STEP本文で踏んだのと同じ罠)。
    **ログそのものは無傷**であること(保存は図形を書く)も併せて見る。
    """
    session = Session(None, 0.2, models_dir=tmp_path)
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.import_svg(sketch_id, str(write_svg(tmp_path, "feed.svg", SVG_MIXED)))

    delivered = session.state_meta()["log"][-1]["args"]
    assert isinstance(delivered["entities"], str)
    assert "配信しない" in delivered["entities"]

    stored = session.document.log.entries[-1].args["entities"]
    assert isinstance(stored, list) and stored and isinstance(stored[0], dict)
