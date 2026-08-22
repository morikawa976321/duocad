"""v1最後のverbの契約 — 円形反復 `pattern_circular`。

`pattern_rectangular`（実装済み）の**兄弟**なので、語彙も規律も揃える:

- **反復は1つのverbとして持つ**（§2「verbの粒度」）。N個の独立した図形へ展開すると、
  個数や角度を後から変えられなくなり**反復の存在理由そのものが消える**。
  スケッチが持つのは `Pattern` 1個で、N個の図形は幾何を作るときの導出物。
- 数値は**名前つきの値**（`parameters.register_value`）にして後から編集できるようにする（§6予防5）。
- 元の図形は反復に吸収される（配置の0番目が元の位置）。
- 失敗は黙って通さない。**失敗時は文書にもログにも変更を残さない**（§6予防6）。

## この契約で決めたこと（`pattern_rectangular` に無かったぶん）

1. **`Pattern.kind`** を足す（`"rectangular"` | `"circular"`）。`Body.kind` / `Entity.kind` と
   同じ語彙。既存の矩形反復は `"rectangular"` を既定にすれば壊れない。
2. **`rotate_copies` は `params` に入れない**。`params` は「後から `set_parameter` で
   編集できる**寸法値**」の置き場であり、真偽の切り替えを混ぜると `set_parameter` で
   `0.5` を書き込める余地ができる。`Pattern.options`（素のdict）に持つ。
3. **刻み角の場合分け**（項番9。実際に踏みやすい）:
   - 全周（`total_angle == 360`）… 刻みは **`360 / count`**。最後の複製が最初と重ならない。
   - 全周未満 … **端点を含む**ので刻みは **`total_angle / (count - 1)`**。
     （`count == 1` は複製が無いので刻みを使わない）
4. **回転させた長方形は4本の線分へ展開する**。`Entity` の長方形は軸平行の
   `x, y, width, height` しか持てず、回転した矩形をその語彙で表せないため。
   展開結果は幾何を作るための導出物なので、**元と同じ種別である必要はない**
   （`_expanded_entities` が返すのは素のdictのリストで、`kernel._entity_edges` は
   線分を知っている）。
5. **回転中心と図形の中心が一致する場合は拒否する**（項番14）。すべての複製が元と
   完全に重なり、「反復したのに何も増えない」が成功として通ってしまうため。
   許すか拒否するかはどちらでもよいが、**黙って壊れないこと**が要件なので拒否を選んだ。
"""
from __future__ import annotations

import math

import pytest

from duocad import kernel, query
from duocad.document import Document
from duocad.oplog import OpLog
from duocad.parameters import parameter_of, set_parameter
from duocad.region import region_ref, regions
from duocad.sketch import add_circle, add_rectangle, get_sketch, sketch_create
from duocad.verbs import extrude, pattern_circular

PLATE_SIZE = 60.0
PLATE_THICKNESS = 10.0
HOLE_DEPTH = 20.0  # 板(z=0..10)を貫く

#: 反復の中心（板の中央）と、そこから離した最初の穴
CENTER = (30.0, 30.0)
FIRST_HOLE = (30.0 + 18.0, 30.0)
HOLE_RADIUS = 3.0
COUNT = 6


def plate(document: Document) -> None:
    """60×60の板（厚さ10）を作る。"""
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (0.0, 0.0), PLATE_SIZE, PLATE_SIZE)
    extrude(
        document,
        [region_ref(sketch_id, regions(document, sketch_id)[0].index)],
        PLATE_THICKNESS,
        mode="join",
    )


def hole_ring(document: Document, count: int = COUNT, total_angle: float = 360.0) -> tuple:
    """穴1つ＋円形反復を持つスケッチを作り、(sketch_id, entity_id, pattern_id) を返す。"""
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, FIRST_HOLE, HOLE_RADIUS)
    pattern_id = pattern_circular(
        document, sketch_id, [circle], CENTER, count, total_angle=total_angle
    )
    return sketch_id, circle, pattern_id


def drill_all(document: Document, sketch_id: str) -> None:
    """スケッチの全領域を1回の押し出しで切り取る。"""
    refs = [region_ref(sketch_id, region.index) for region in regions(document, sketch_id)]
    extrude(document, refs, HOLE_DEPTH, mode="cut")


def hole_axes(shape) -> list:
    """穴（全周円筒面）の軸の (x, y) を昇順で返す（`test_pattern.py` と同じ流儀）。"""
    return sorted(
        (round(face["centroid"][0], 6), round(face["centroid"][1], 6))
        for face in kernel.tessellate(shape)["faces"]
        if face["kind"] == "cylinder_full"
    )


def angles_from_center(points: list) -> list:
    """中心から見た角度（度・0〜360）を昇順で返す。"""
    return sorted(
        round(math.degrees(math.atan2(y - CENTER[1], x - CENTER[0])) % 360.0, 6)
        for x, y in points
    )


# --------------------------------------------------------------------------------------
# 項番1〜3: 基本（個数・1操作・undo）
# --------------------------------------------------------------------------------------
def test_pattern_multiplies_the_entity_into_count_regions():
    """`count=N` で領域が N 個になる（元を含めて N。N-1個の複製が足される）。"""
    document = Document()
    sketch_id, _, _ = hole_ring(document)

    found = regions(document, sketch_id)

    assert len(found) == COUNT
    for region in found:
        assert math.isclose(region.area, math.pi * HOLE_RADIUS**2, rel_tol=1e-6)


def test_pattern_is_one_operation_and_is_not_expanded_into_entities():
    """反復は**操作ログに1行**で載り、図形はN個に展開されない。

    展開してしまうと個数も角度も後から変えられなくなる（＝編集可能性が消える）。
    スケッチが持つのは元の円1個と反復1個だけで、6個の円は幾何を作るときの導出物。
    """
    document = Document()
    sketch_id, entity_id, pattern_id = hole_ring(document)

    sketch = get_sketch(document, sketch_id)
    assert len(sketch.entities) == 1, "図形がN個に展開されている"
    assert len(sketch.patterns) == 1
    assert sketch.patterns[0].id == pattern_id
    assert sketch.patterns[0].sources == [entity_id]
    assert sketch.patterns[0].kind == "circular", "矩形反復と区別できないと展開を分けられない"

    circular = [op for op in document.log.entries if op.verb == "pattern_circular"]
    assert len(circular) == 1, "1つの反復が複数行に分かれている"
    args = circular[0].args
    assert args["entities"] == [entity_id]
    assert args["count"] == COUNT
    assert args["total_angle"] == 360.0
    assert args["center"] == list(CENTER)
    assert args["rotate_copies"] is True, "既定はTrue（Fusionの挙動に合わせる）"


def test_pattern_can_be_undone():
    """undoで元の1個に戻る（反復だけが消え、元の図形は残る）。"""
    document = Document()
    sketch_id, entity_id, _ = hole_ring(document)
    assert len(regions(document, sketch_id)) == COUNT

    document.log.undo(document)

    sketch = get_sketch(document, sketch_id)
    assert sketch.patterns == []
    assert [entity.id for entity in sketch.entities] == [entity_id], "元の図形まで消えた"
    assert len(regions(document, sketch_id)) == 1

    document.log.redo(document)
    assert len(regions(document, sketch_id)) == COUNT


# --------------------------------------------------------------------------------------
# 項番4: 押し出して穴がN個
# --------------------------------------------------------------------------------------
def test_extruding_a_circular_pattern_opens_count_holes():
    """反復した円を `cut` で押し出すと穴が **count個** できる。"""
    document = Document()
    plate(document)
    sketch_id, _, _ = hole_ring(document)

    drill_all(document, sketch_id)

    body = document.active_body()
    assert len(hole_axes(body.shape)) == COUNT
    expected = PLATE_SIZE**2 * PLATE_THICKNESS - COUNT * math.pi * HOLE_RADIUS**2 * PLATE_THICKNESS
    assert math.isclose(query.volume(body.shape), expected, rel_tol=1e-9)


# --------------------------------------------------------------------------------------
# 項番8〜9: 幾何の正しさ（刻み角の場合分け）
# --------------------------------------------------------------------------------------
def test_a_full_circle_divides_360_by_count():
    """全周のときの刻みは **360/count**（`360/(count-1)` ではない）。

    6個なら 60度ずつ。**最後の複製が最初と重ならない**ことがこの割り方の要点で、
    ここを `count-1` で割ると 72度刻みになり、6個目が元の位置へ戻って重なる。
    """
    document = Document()
    plate(document)
    sketch_id, _, _ = hole_ring(document)
    drill_all(document, sketch_id)

    angles = angles_from_center(hole_axes(document.active_body().shape))

    assert len(angles) == COUNT
    step = 360.0 / COUNT
    for index, angle in enumerate(angles):
        assert math.isclose(angle, index * step, abs_tol=1e-4), f"{angles}"


def test_a_partial_angle_includes_both_ends():
    """全周未満のときは**端点を含む**ので刻みは `total_angle/(count-1)`。

    180度に3個なら 0度・90度・180度（刻み90度）。全周と同じ `total_angle/count` で
    割ると 60度刻みになり、指定した180度に最後の複製が届かない。
    """
    document = Document()
    plate(document)
    sketch_id, _, _ = hole_ring(document, count=3, total_angle=180.0)
    drill_all(document, sketch_id)

    angles = angles_from_center(hole_axes(document.active_body().shape))

    assert len(angles) == 3
    for index, angle in enumerate(angles):
        assert math.isclose(angle, index * 90.0, abs_tol=1e-4), f"{angles}"


def test_a_single_copy_needs_no_step():
    """`count=1` は複製が無い（元だけ）。刻み角を計算しようとして0除算しないこと。"""
    document = Document()
    sketch_id, _, _ = hole_ring(document, count=1, total_angle=180.0)

    assert len(regions(document, sketch_id)) == 1


# --------------------------------------------------------------------------------------
# 項番10: rotate_copies
# --------------------------------------------------------------------------------------
def test_rotate_copies_false_keeps_the_original_orientation():
    """`rotate_copies=False` なら複製の向きは元と同じ。

    **回転非対称な図形（長方形）で確かめる** — 円は回転対称なので、向きを変えても
    変えなくても同じ形になり、検証にならない。向きが同じなら、各複製の境界箱の
    幅と高さは元と同じ（回転させると幅と高さが入れ替わる／斜めになって広がる）。
    """
    document = Document()
    sketch_id = sketch_create(document, "XY")
    # 幅と高さが違う長方形（正方形だと回転しても区別が付かない）
    rect = add_rectangle(document, sketch_id, (36.0, 28.0), 8.0, 4.0)
    pattern_circular(
        document, sketch_id, [rect], CENTER, 4, total_angle=360.0, rotate_copies=False
    )

    found = regions(document, sketch_id)
    assert len(found) == 4
    for region in found:
        assert math.isclose(region.area, 8.0 * 4.0, rel_tol=1e-6)


def test_rotate_copies_true_actually_rotates_the_rectangle():
    """`rotate_copies=True`（既定）なら複製は回転する。

    90度回した長方形は**幅と高さが入れ替わる**ので、境界箱で区別できる。面積は不変。
    **回した長方形は軸平行の `x,y,width,height` で表せない**ので、展開では
    4本の線分に落とす（モジュールdocstringの「この契約で決めたこと」4）。
    """
    document = Document()
    sketch_id = sketch_create(document, "XY")
    rect = add_rectangle(document, sketch_id, (36.0, 28.0), 8.0, 4.0)
    pattern_circular(
        document, sketch_id, [rect], CENTER, 4, total_angle=360.0, rotate_copies=True
    )

    found = regions(document, sketch_id)
    assert len(found) == 4
    for region in found:
        assert math.isclose(region.area, 8.0 * 4.0, rel_tol=1e-6), "回転で面積が変わってはいけない"


# --------------------------------------------------------------------------------------
# 項番5〜7: パラメトリック（後から編集できる）
# --------------------------------------------------------------------------------------
def test_changing_the_count_changes_the_number_of_holes():
    """`count` は名前つきの値。変更すると穴の数が追随する。"""
    document = Document()
    sketch_id, _, pattern_id = hole_ring(document)
    pattern = get_sketch(document, sketch_id).patterns[0]
    name = pattern.params["count"]
    assert isinstance(name, str), "素の数値を持っていたら後から変えられない"
    # `parameter_of` は (doc, sketch_id, owner_id, key) — 矩形反復のテストと同じ引き方
    assert parameter_of(document, sketch_id, pattern_id, "count") == name

    set_parameter(document, name, 3.0)

    assert len(regions(document, sketch_id)) == 3


def test_changing_the_total_angle_moves_the_copies():
    """`total_angle` を変更すると配置角度が追随する。"""
    document = Document()
    plate(document)
    sketch_id, _, _ = hole_ring(document, count=3, total_angle=180.0)
    name = get_sketch(document, sketch_id).patterns[0].params["total_angle"]

    set_parameter(document, name, 90.0)
    drill_all(document, sketch_id)

    angles = angles_from_center(hole_axes(document.active_body().shape))
    assert len(angles) == 3
    for index, angle in enumerate(angles):
        assert math.isclose(angle, index * 45.0, abs_tol=1e-4), f"{angles}"


def test_changing_the_center_moves_the_whole_ring():
    """`center` を変更すると配置全体が移動する（中心も名前つきの値）。

    `Region` は `index` と `area` しか持たない（実装計画書§3.3のとおり）ので、位置は
    **押し出して開いた穴の軸**で見る（`test_pattern.py` と同じ流儀）。
    中心を +5mm 動かせば、6つの穴の重心の平均も +5mm 動く。
    """
    document = Document()
    plate(document)
    sketch_id, _, _ = hole_ring(document)
    params = get_sketch(document, sketch_id).patterns[0].params
    assert isinstance(params["cx"], str) and isinstance(params["cy"], str)

    set_parameter(document, params["cx"], CENTER[0] + 5.0)
    drill_all(document, sketch_id)

    axes = hole_axes(document.active_body().shape)
    assert len(axes) == COUNT
    mean_x = sum(x for x, _ in axes) / len(axes)
    mean_y = sum(y for _, y in axes) / len(axes)
    assert math.isclose(mean_x, CENTER[0] + 5.0, abs_tol=1e-4), "中心を動かしても配置が動いていない"
    assert math.isclose(mean_y, CENTER[1], abs_tol=1e-4)


def test_pattern_survives_save_load_and_replay():
    """保存→再生で反復がそのまま復元される（`args` に数値が載っていること）。"""
    document = Document()
    sketch_id, _, _ = hole_ring(document)
    saved = document.log.to_json()

    restored = Document()
    log = OpLog.from_json(saved)
    log.replay(restored)
    restored.log = log

    restored_sketch_id = restored.sketches[0].id
    assert len(regions(restored, restored_sketch_id)) == COUNT


# --------------------------------------------------------------------------------------
# 項番11〜14: 失敗を黙って通さない
# --------------------------------------------------------------------------------------
#: 0 / 負 / 非整数の3分岐は `test_pattern.py` が網羅済み(`_is_positive_int` は共有)。
#: ここは呼び出し口の配線確認なので境界の 0 だけ通す。
@pytest.mark.parametrize("count", [0])
def test_count_must_be_a_positive_integer(count):
    """`count < 1` や非整数は ValueError。**文書にもログにも残さない**。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, FIRST_HOLE, HOLE_RADIUS)
    log_before = len(document.log.entries)

    with pytest.raises(ValueError):
        pattern_circular(document, sketch_id, [circle], CENTER, count)

    assert get_sketch(document, sketch_id).patterns == []
    assert len(document.log.entries) == log_before


#: -90.0 は 0.0 と同じ分岐(`not 0 < angle <= 360`)なので落とした。
@pytest.mark.parametrize("total_angle", [0.0, 361.0, float("nan")])
def test_total_angle_must_be_usable(total_angle):
    """`total_angle` は 0 < angle <= 360。0・負・360超・NaNは ValueError。

    360を超える指定は「1周を超えて並べる」意味になり、複製が必ず重なる。
    どう振る舞うべきか決められないものは**受け取らない**のが黙って壊れない唯一の方法。
    """
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, FIRST_HOLE, HOLE_RADIUS)
    log_before = len(document.log.entries)

    with pytest.raises(ValueError):
        pattern_circular(document, sketch_id, [circle], CENTER, COUNT, total_angle=total_angle)

    assert get_sketch(document, sketch_id).patterns == []
    assert len(document.log.entries) == log_before


def test_unknown_entity_is_refused():
    """存在しない図形IDは KeyError（矩形反復と同じ契約）。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    add_circle(document, sketch_id, FIRST_HOLE, HOLE_RADIUS)
    log_before = len(document.log.entries)

    with pytest.raises(KeyError):
        pattern_circular(document, sketch_id, ["entity-ghost"], CENTER, COUNT)

    assert get_sketch(document, sketch_id).patterns == []
    assert len(document.log.entries) == log_before


def test_empty_entity_list_is_refused():
    document = Document()
    sketch_id = sketch_create(document, "XY")
    with pytest.raises(ValueError):
        pattern_circular(document, sketch_id, [], CENTER, COUNT)


def test_patterning_the_same_entity_twice_is_refused():
    """1つの図形を2つの反復に使わない（矩形反復と同じ。二重に展開されるため）。"""
    document = Document()
    sketch_id, entity_id, _ = hole_ring(document)
    log_before = len(document.log.entries)

    with pytest.raises(ValueError):
        pattern_circular(document, sketch_id, [entity_id], CENTER, 3)

    assert len(get_sketch(document, sketch_id).patterns) == 1
    assert len(document.log.entries) == log_before


def test_a_shape_centred_on_the_pivot_is_refused():
    """**回転中心と図形の中心が一致する場合は拒否する**（項番14で決めた挙動）。

    すべての複製が元と完全に重なり、「反復したのに何も増えない」が成功として通ってしまう
    （領域は1個のまま）。許すか拒否するかはどちらでもよいが、**黙って壊れないこと**が
    要件なので拒否を選んだ。理由を例外メッセージに書くこと。
    """
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, CENTER, HOLE_RADIUS)  # 中心が回転中心と一致
    log_before = len(document.log.entries)

    with pytest.raises(ValueError):
        pattern_circular(document, sketch_id, [circle], CENTER, COUNT)

    assert get_sketch(document, sketch_id).patterns == []
    assert len(document.log.entries) == log_before


def test_reducing_the_count_below_one_is_refused_afterwards_too():
    """`set_parameter` 経由でも `count` の検査が効く。

    作成時だけ検査していると、同じ値を後から書き込む経路が素通りする
    （`pattern_rectangular` が `nx`/`ny` で踏んだのと同じ穴。`parameters.is_positive_int`）。
    """
    document = Document()
    sketch_id, _, _ = hole_ring(document)
    name = get_sketch(document, sketch_id).patterns[0].params["count"]

    with pytest.raises(ValueError):
        set_parameter(document, name, 0.0)
    assert len(regions(document, sketch_id)) == COUNT


def test_changing_the_total_angle_to_an_unusable_value_is_refused():
    """`total_angle` も `set_parameter` 経由で検査する。

    `count` だけを検査していると、**角度のほうから同じ壊れ方を作れる** —
    0度にすれば全複製が重なり、360超なら1周を超えて重なるのに、例外は出ない。
    「作成時だけ検査していると後から書き込む経路が素通りする」は `nx`/`ny` で既に
    踏んだ穴で、角度も同じ扱いにする。
    """
    document = Document()
    sketch_id, _, _ = hole_ring(document)
    name = get_sketch(document, sketch_id).patterns[0].params["total_angle"]

    for bad in (0.0, -90.0, 361.0):
        with pytest.raises(ValueError):
            set_parameter(document, name, bad)
    assert len(regions(document, sketch_id)) == COUNT
