"""形状最適化の**配線**(`duocad/optimize_run.py`)の契約。

ここで固定するのは、**外れても例外が出ないまま静かに間違う**ところと、**入口の断り方**である。
端から端まで実際に二値探索を回す受入検査は `topopt_checks/check_optimize_wiring.py`
(約20秒。pytest には入れない —— `topopt_checks/_bootstrap.py` と同じ理由)。

1. **面の対応** —— 条件面が複数のボディに散っていると `kernel.make_compound` で1つにまとめる。
   面添字は `face_count` の累積だけずれるが、**ずれても例外は出ない**(荷重が別の面に入り、
   最適化はもっともらしく収束する)。だから対応を測り直して確かめる層があり、
   ここではその**検出力**(わざとずらすと落ちる)まで見る。
2. **入口の断り** —— 条件が足りない/この版で扱えない/メッシュボディを指した、を名指しする。
3. **メッシュボディ** —— 保持・除外に指定できない。ただし
   **過去のログの再生は落とさない**(門は verb の入口だけで、`_apply_bodies` には無い)。
4. **解像度** —— 上限は「条件面からの要求」だけで決まる(密度フィルタ半径は二値探索に
   無いので上限に効かない。計画書§6.2)。下限は総要素数の上限から。
5. **座標系** —— 結果メッシュは要素添字空間で来るので、ワールド[mm]へ直す。
6. **中断** —— 旗が立っていれば文書を1ビットも変えずに降りる。
7. **import の費用** —— `import duocad.session` で skimage と `duocad.topopt` が来ない。
8. **面アンカーの門** —— 比べる相手は面の**面積重心**であって、条件に焼き込んだ
   `Face.center()` ではない。取り違えると曲面の条件面で解像度を上げるほど厳しくなる。
"""
from __future__ import annotations

import json
import math
import subprocess
import sys

import pytest

from duocad import optimize_run
from duocad.server import Session
from session_support import face_with_normal, four_conditions, pad, select as _select, two_pads

from duocad.paths import REPO_ROOT


#: 固定値の上限そのものと、最小幅の上限の割合(テストの多くは速さのため 8mm で回すので、下の fixture が緩める)。
REAL_MAX_ELEMENT_SIZE_MM = optimize_run.MAX_ELEMENT_SIZE_MM
REAL_MIN_WIDTH_CEILING_RATIO = optimize_run.MIN_WIDTH_CEILING_RATIO


@pytest.fixture(autouse=True)
def _coarse_grids_allowed(monkeypatch) -> None:
    """この題材を 8mm 前後の粗い格子(最短の辺 6〜9 セル)で回すため、要素サイズの上限の固定値(#130)を外し、最小幅の
    上限を格子の最短の辺いっぱいまで緩める(1/4 だと段が無くなる)。実際の値を見るテストは自分で戻す。"""
    monkeypatch.setattr(optimize_run, "MAX_ELEMENT_SIZE_MM", math.inf)
    monkeypatch.setattr(optimize_run, "MIN_WIDTH_CEILING_RATIO", 1.0)


@pytest.fixture
def real_min_width_bounds(monkeypatch) -> None:
    monkeypatch.setattr(optimize_run, "MIN_WIDTH_CEILING_RATIO", REAL_MIN_WIDTH_CEILING_RATIO)


# --------------------------------------------------------------------------------------
# 1. 入口の断り
# --------------------------------------------------------------------------------------
def test_conditions_report_names_every_missing_piece(session: Session) -> None:
    report = optimize_run.conditions_report(session.document)
    assert report["ready"] is False
    joined = "。".join(report["problems"])
    assert "保持ソリッド" in joined
    assert "拘束面" in joined
    assert "荷重面" in joined


def test_conditions_report_is_ready_once_all_four_are_set(session: Session) -> None:
    four_conditions(session)
    report = optimize_run.conditions_report(session.document)
    assert report["ready"] is True
    assert report["problems"] == []
    assert report["load_total_n"] == 500.0
    assert report["loads"][0]["vector"] == [0.0, 0.0, -1.0]


def test_two_fixture_faces_are_accepted(session: Session) -> None:
    """拘束面は**何枚でもよい**(拘束する自由度の和集合を取るだけで、力学的な曖昧さが無い)。

    落ちるなら `Conditions.fixed_faces` から下のどこかが1枚しか受けていない。
    """
    left, right, fixed, load = four_conditions(session)
    other = face_with_normal(left, 1.0, 0.0, 0.0)
    _select(
        session,
        [
            {"body": left.id, "kind": "face", "index": fixed},
            {"body": left.id, "kind": "face", "index": other},
        ],
    )
    session.set_fixtures()
    session.clear_selection()

    report = optimize_run.conditions_report(session.document)
    assert report["ready"] is True, report["problems"]
    bundle = optimize_run.build_conditions(session.document)
    assert len(bundle.fixed_faces) == 2
    assert bundle.conditions.fixed_faces == bundle.fixed_faces


def test_stale_condition_face_blocks_the_run(session: Session) -> None:
    """**指紋が合わない条件では走らせない。** 別の面へ荷重が入るのを黙って通さない。"""
    four_conditions(session)
    # 荷重面の指紋を人為的に壊す(形を変えるのと同じ効果。OCCTを回さない)。
    session.document.optimize.loads[0]["area"] *= 2.0
    report = optimize_run.conditions_report(session.document)
    assert report["ready"] is False
    assert any("今の形と合わない" in problem for problem in report["problems"])
    with pytest.raises(optimize_run.OptimizeInputError, match="今の形と合わない"):
        optimize_run.build_conditions(session.document)


# --------------------------------------------------------------------------------------
# 2. メッシュボディ
# --------------------------------------------------------------------------------------
def _mesh_body(session: Session) -> str:
    positions = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    indices = [0, 2, 1, 0, 1, 3, 1, 2, 3, 2, 0, 3]
    return session.add_mesh_body(positions, indices)["body_id"]


def test_mesh_body_cannot_be_a_preserve_or_exclude_solid(session: Session) -> None:
    body_id = _mesh_body(session)
    for verb in ("set_preserve_bodies", "set_exclude_bodies"):
        _select(session, [{"body": body_id, "kind": "body"}])
        with pytest.raises(ValueError, match="mesh body"):
            getattr(session, verb)()
        session.clear_selection()
    # **文書にもログにも何も残らない**(失敗したverbは集合を書き換えない)。
    assert session.document.optimize.preserve_bodies == []
    assert session.document.optimize.exclude_bodies == []


def test_mesh_body_left_over_from_a_replayed_log_is_refused_at_run_time(
    session: Session,
) -> None:
    """門は verb の入口にしか無い(再生は落とさない)ので、**走らせる側でも見る**。"""
    body_id = _mesh_body(session)
    four_conditions(session)
    session.document.optimize.preserve_bodies.append(body_id)  # 再生で入った状態を模す
    report = optimize_run.conditions_report(session.document)
    assert report["ready"] is False
    assert any("mesh body" in problem for problem in report["problems"])


# --------------------------------------------------------------------------------------
# 3. 面の対応(compound の面添字)
# --------------------------------------------------------------------------------------
def test_face_indices_shift_by_the_face_count_of_earlier_bodies(session: Session) -> None:
    from duocad import kernel

    left, right, fixed, load = four_conditions(session)
    bundle = optimize_run.build_conditions(session.document)

    assert bundle.anchor_body_ids == [left.id, right.id]
    assert bundle.face_offsets[left.id] == 0
    assert bundle.face_offsets[right.id] == kernel.face_count(left.shape)
    assert bundle.fixed_faces == (fixed,)
    assert bundle.load_faces == (kernel.face_count(left.shape) + load,)
    # **面ごとの力ベクトルは実単位[N]**(向き × 大きさ)。単位ベクトルではない。
    assert bundle.forces[0] == pytest.approx((0.0, 0.0, -500.0))


def test_two_loads_with_different_directions_become_two_force_vectors(
    session: Session,
) -> None:
    """面ごとに違う向き・大きさが、**面ごとの力ベクトル[N]**として翻訳される。

    落ちるなら、どこかが1本の向きを全ての荷重面へ使い回している。
    """
    left, right, _fixed, load = four_conditions(session)
    other = face_with_normal(right, 1.0, 0.0, 0.0)
    _select(session, [{"body": right.id, "kind": "face", "index": other}])
    session.set_loads(20.0, "normal")
    session.clear_selection()

    bundle = optimize_run.build_conditions(session.document)
    assert len(bundle.load_faces) == 2 and len(bundle.forces) == 2
    assert bundle.forces[0] == pytest.approx((0.0, 0.0, -500.0))
    assert bundle.forces[1] == pytest.approx((20.0, 0.0, 0.0))


def test_a_wrong_offset_is_caught_instead_of_silently_loading_another_face(
    session: Session,
) -> None:
    """**検出力の変異検査。** ずれても例外は出ない経路なので、ここが最後の砦である。"""
    left, right, _fixed, _load = four_conditions(session)
    anchor_shape, offsets = optimize_run._compound_with_offsets(
        session.document, [left.id, right.id]
    )
    conditions = [
        dict(session.document.optimize.fixtures[0]),
        dict(session.document.optimize.loads[0]),
    ]
    # 正しい対応は通る。
    optimize_run._verify_face_mapping(session.document, anchor_shape, offsets, conditions)
    # 1つずらすと落ちる(**この検査を外すと荷重が別の面に入ったまま最適化が成功する**)。
    broken = dict(offsets)
    broken[right.id] -= 1  # +1 は最後の面で範囲外になる(それも落ちるが、見たいのは取り違え)
    with pytest.raises(optimize_run.FaceMappingError, match="別の面である"):
        optimize_run._verify_face_mapping(
            session.document, anchor_shape, broken, conditions
        )


def test_identical_bodies_are_told_apart_by_the_centroid(session: Session) -> None:
    """**重心を見ないと通ってしまう**配置(同じ形のボディが2つ)を名指しで押さえる。"""
    first = pad(session, 20.0, 20.0, 20.0)
    second = pad(session, 20.0, 20.0, 20.0, corner=(50.0, 0.0))
    anchor_shape, offsets = optimize_run._compound_with_offsets(
        session.document, [first.id, second.id]
    )
    from duocad import query

    index = face_with_normal(second, 0.0, 0.0, 1.0)
    condition = dict(query.face_fingerprint(second.shape, index), body=second.id, index=index)
    optimize_run._verify_face_mapping(
        session.document, anchor_shape, offsets, [condition]
    )
    # 面積も法線も同じで**重心だけが違う**面へずらす。
    broken = dict(offsets)
    broken[second.id] = 0
    with pytest.raises(optimize_run.FaceMappingError, match="別の面である"):
        optimize_run._verify_face_mapping(
            session.document, anchor_shape, broken, [condition]
        )


# --------------------------------------------------------------------------------------
# 4. 解像度
# --------------------------------------------------------------------------------------
def test_the_upper_bound_is_the_finer_of_the_fixed_cap_and_the_condition_faces(session: Session, monkeypatch) -> None:
    """落ちたら、要素サイズの上限が min(固定値 4mm, 条件面の要求 min(sqrt(CAD面積))) を実格子へ切り下げた値から外れている(#130)。"""
    monkeypatch.setattr(optimize_run, "MAX_ELEMENT_SIZE_MM", REAL_MAX_ELEMENT_SIZE_MM)
    four_conditions(session)
    limits = optimize_run.resolution_limits(session.document)
    # 最小の条件面は荷重面 10x40mm で sqrt(400) = 20mm。固定値 4mm の方が細かい。
    assert limits["from_condition_faces_mm"] == pytest.approx(20.0, rel=1e-6)
    assert limits["max_element_size_cap_mm"] == REAL_MAX_ELEMENT_SIZE_MM == 4.0
    lo, hi = optimize_run._condition_bbox(session.document)
    assert limits["max_element_size_mm"] <= 4.0 * (1.0 + 1e-9)
    assert limits["max_element_size_mm"] == pytest.approx(optimize_run._snap_to_grid(lo, hi, 4.0))
    assert optimize_run._snap_to_grid(lo, hi, limits["max_element_size_mm"]) == pytest.approx(
        limits["max_element_size_mm"]
    )
    assert limits["default_element_size_mm"] == limits["max_element_size_mm"]
    assert limits["min_element_size_mm"] < limits["max_element_size_mm"]
    assert limits["feasible"] is True


def test_condition_faces_finer_than_the_cap_set_the_upper_bound(session: Session, monkeypatch) -> None:
    """落ちたら、条件面の要求が固定値より細かいときに上限が条件面の要求に従っていない(小さい面に格子節点が付かなくなる)。"""
    monkeypatch.setattr(optimize_run, "MAX_ELEMENT_SIZE_MM", 100.0)
    four_conditions(session)
    limits = optimize_run.resolution_limits(session.document)
    lo, hi = optimize_run._condition_bbox(session.document)
    assert limits["max_element_size_mm"] == pytest.approx(optimize_run._snap_to_grid(lo, hi, 20.0))


def test_a_free_element_size_returns_the_same_step_the_ladder_would(session: Session) -> None:
    """落ちたら、自由入力した要素サイズの段(`selected`)が梯子の段と別の数え方をしているか、範囲外を段として返している。"""
    four_conditions(session)
    limits = optimize_run.resolution_limits(session.document)
    ladder = limits["steps"][2]
    picked = optimize_run.resolution_limits(session.document, ladder["element_size_mm"])
    assert picked["requested_element_size_mm"] == ladder["element_size_mm"]
    assert picked["selected"] == dict(ladder, index=-1)
    between = 0.5 * (limits["steps"][1]["element_size_mm"] + limits["steps"][2]["element_size_mm"])
    lo, hi = optimize_run._condition_bbox(session.document)
    assert optimize_run.resolution_limits(session.document, between)["selected"]["element_size_mm"] == pytest.approx(
        optimize_run._snap_to_grid(lo, hi, between))
    for wrong, reason in ((limits["max_element_size_mm"] * 2.0, "上限"), (limits["min_element_size_mm"] * 0.5, "下限"), (-1.0, "正の値")):
        refused = optimize_run.resolution_limits(session.document, wrong)
        assert "selected" not in refused and reason in refused["selected_error"]


def test_the_round_cap_itself_is_accepted_although_the_snapped_upper_bound_is_finer() -> None:
    """落ちたら、上限の固定値 4mm をそのまま指定すると、実格子へ切り下げた上限(例 3.98mm)より粗いとして断っている。"""
    limits = {"max_element_size_mm": 3.98, "min_element_size_mm": 1.0, "feasible": True,
              "from_condition_faces_mm": 20.0, "max_element_size_cap_mm": 4.0}
    assert optimize_run._resolve_element_size(limits, 4.0) == 4.0
    with pytest.raises(optimize_run.OptimizeInputError, match="上限"):
        optimize_run._resolve_element_size(limits, 4.01)


def test_element_size_outside_the_bounds_is_refused() -> None:
    limits = {"max_element_size_mm": 4.0, "min_element_size_mm": 1.0, "feasible": True}
    assert optimize_run._resolve_element_size(limits, None) == 4.0
    assert optimize_run._resolve_element_size(limits, 2.0) == 2.0
    with pytest.raises(optimize_run.OptimizeInputError, match="上限"):
        optimize_run._resolve_element_size(limits, 8.0)
    with pytest.raises(optimize_run.OptimizeInputError, match="下限"):
        optimize_run._resolve_element_size(limits, 0.5)
    infeasible = {"max_element_size_mm": 1.0, "min_element_size_mm": 4.0, "feasible": False}
    with pytest.raises(optimize_run.OptimizeInputError, match="下回っている"):
        optimize_run._resolve_element_size(infeasible, None)


def test_min_width_outside_the_bounds_is_refused() -> None:
    """落ちたら、下限の段(m ≥ 2 かつ m × spacing ≥ 2mm)より細い値か、不正な値が通っている。"""
    assert optimize_run._resolve_min_width(2.0, None)["source"] == "default"
    requested = optimize_run._resolve_min_width(2.0, 4.0)
    assert (requested["cells"], requested["source"]) == (2, "requested")
    with pytest.raises(optimize_run.OptimizeInputError, match="下限"):
        optimize_run._resolve_min_width(2.0, 2.0)  # m=1
    with pytest.raises(optimize_run.OptimizeInputError, match="下限"):
        optimize_run._resolve_min_width(0.5, 1.5)  # m=3 だが 2mm の段(m=4)より細い
    with pytest.raises(optimize_run.OptimizeInputError, match="有限の正"):
        optimize_run._resolve_min_width(2.0, 0.0)
    with pytest.raises(optimize_run.OptimizeInputError, match="有限の正"):
        optimize_run._resolve_min_width(2.0, float("nan"))


def test_a_thin_exclude_body_between_the_columns_is_reported_as_unrepresented(session: Session) -> None:
    """落ちたら、要素サイズより薄い除外ソリッドが列の中心を1本も通らないとき、塊の導出が「ずらしが全部縮退した」
    の例外で止まり、`unrepresented_regions` の報告(要素サイズを下げる案内)まで届かない(issue #29)。"""
    import numpy as np

    from duocad.topopt import design_space as ds

    four_conditions(session)
    limits = optimize_run.resolution_limits(session.document)
    spacing = limits["steps"][1]["element_size_mm"]
    snapped = optimize_run._snap_to_grid(limits["bbox_min"], limits["bbox_max"], spacing)
    grid = ds.derive(optimize_run.build_conditions(session.document).conditions, spacing=snapped).grid
    columns = grid.centers_1d(0)
    middle = int(np.searchsorted(columns, 35.0))
    wall_x = (columns[middle - 1] + columns[middle]) / 2 - 0.15
    wall = pad(session, 0.3, 40.0, 20.0, corner=(wall_x, 0.0))
    _select(session, [{"body": wall.id, "kind": "body"}])
    session.set_exclude_bodies()
    session.clear_selection()

    result = optimize_run.run(session.document, optimize_run.OptimizeOptions(element_size_mm=spacing))
    assert result["state"] == "unrepresented_regions"
    [diagnosis] = [item for item in result["unrepresented_regions"] if item["body"] == wall.id]
    assert diagnosis["role"] == "exclude_bodies" and diagnosis["elements"] == 0 and diagnosis["reason"]


def test_min_width_cell_range_is_2mm_up_to_a_quarter_of_the_shortest_grid_edge(real_min_width_bounds) -> None:
    """落ちたら、選べる m の範囲が「2mm 以上で最小の m(m ≥ 2)〜 格子の最短の辺のセル数 × 1/4」から外れている
    (2026-10-09 本人決定の暫定値)。"""
    assert optimize_run.MIN_WIDTH_FLOOR_MM == 2.0 and optimize_run.MIN_WIDTH_CEILING_RATIO == 0.25
    assert optimize_run.min_width_cell_range(4.0, 13) == (2, 3)
    assert optimize_run.min_width_cell_range(0.5, 89) == (4, 22)  # 2.0 / 0.5 ちょうどは 4 のまま
    assert optimize_run.min_width_cell_range(0.509, 89) == (4, 22)
    assert optimize_run.min_width_cell_range(1.0, 7) == (2, 1)  # 選べる段が無い
    assert optimize_run.min_width_cell_range(1.0, None) == (2, None)
    assert optimize_run.min_width_steps_mm(4.0, 13) == [8.0, 12.0]
    assert optimize_run.min_width_steps_mm(1.0, 7) == []


def test_min_width_none_resolves_to_the_thinnest_selectable_step(real_min_width_bounds) -> None:
    """既定は選べるいちばん細い段。落ちたら、指定なしで 2mm より細い立方や m=1 で走る。"""
    coarse = optimize_run._resolve_min_width(2.4, None, max_cells=40)
    assert coarse["source"] == "default" and coarse["cells"] == 2 and coarse["guaranteed_mm"] == pytest.approx(4.8)
    fine = optimize_run._resolve_min_width(0.4, None, max_cells=100)
    assert fine["cells"] == 5 and fine["guaranteed_mm"] == pytest.approx(2.0)
    assert fine["requested_mm"] == fine["guaranteed_mm"]


def test_a_grid_with_no_selectable_step_blames_the_element_size(real_min_width_bounds) -> None:
    """格子が小さく上限の m が下限を下回るときは、指定の有無に依らず「要素サイズを下げよ」と言う。"""
    with pytest.raises(optimize_run.OptimizeInputError, match="要素サイズを下げる"):
        optimize_run._resolve_min_width(2.4, None, max_cells=7)
    with pytest.raises(optimize_run.OptimizeInputError, match="要素サイズを下げる"):
        optimize_run._resolve_min_width(2.4, 4.8, max_cells=7)


def test_min_width_above_the_ceiling_is_refused_and_the_ceiling_itself_is_accepted(real_min_width_bounds) -> None:
    """上限の段より太い値は入口で断る。上限ちょうどの値は `値 / spacing` の丸めに依らず通る。"""
    with pytest.raises(optimize_run.OptimizeInputError, match="上限"):
        optimize_run._resolve_min_width(2.4, 5000.0, max_cells=40)
    with pytest.raises(optimize_run.OptimizeInputError, match="上限"):
        optimize_run._resolve_min_width(2.4, 10 * 2.4 + 0.01, max_cells=40)
    checked = rounded_up = 0
    for spacing in (0.1, 0.7, 1 / 3, 2.4, 2.387506, 4.9):
        for cells in range(21, 61):
            resolved = optimize_run._resolve_min_width(spacing, cells * spacing, max_cells=cells * 4)
            assert resolved["cells"] == cells and resolved["guaranteed_mm"] == cells * spacing
            checked += 1
            rounded_up += math.ceil(cells * spacing / spacing) > cells
    assert checked == 6 * 40 and rounded_up > 0  # 丸めで1つ上へ跳ねる組が実際にあり、それでも通る


def test_run_refuses_a_min_width_beyond_the_grid_before_deriving_anything(session: Session, monkeypatch) -> None:
    """落ちたら、上限が塊の導出・境界条件の確定の後に効いている(巨大な m は導出後にメモリを使い切る)。"""
    from duocad.topopt import design_space as ds

    four_conditions(session)
    monkeypatch.setattr(ds, "derive", lambda *a, **k: pytest.fail("上限の検査より前に導出へ進んだ"))
    with pytest.raises(optimize_run.OptimizeInputError, match="上限"):
        optimize_run.run(session.document, optimize_run.OptimizeOptions(element_size_mm=9.8, min_width_mm=5000.0))


def test_every_ladder_step_carries_its_own_min_width_range(session: Session, real_min_width_bounds) -> None:
    """段ごとの `min_width_floor_mm`・`min_width_ceiling_mm` は、その段の格子での `min_width_cell_range` の両端。"""
    four_conditions(session)
    limits = optimize_run.resolution_limits(session.document)
    for step in limits["steps"]:
        size = step["element_size_mm"]
        lower, upper = optimize_run.min_width_cell_range(
            size, min(optimize_run._grid_shape(limits["bbox_min"], limits["bbox_max"], size)))
        assert step["min_width_floor_mm"] == pytest.approx(lower * size)
        assert step["min_width_ceiling_mm"] == pytest.approx(upper * size)


def test_every_min_width_step_resolves_to_its_own_cell_count(real_min_width_bounds) -> None:
    """各段の値を渡すと、ちょうどその m になる。落ちたら、画面が示す段を選んでも m が1つ飛ぶ。"""
    checked = 0
    for spacing in (0.1, 0.7, 1 / 3, 2.4, 2.387506, 2.4063, 4.9):
        max_cells = 244
        lower, upper = optimize_run.min_width_cell_range(spacing, max_cells)
        steps = optimize_run.min_width_steps_mm(spacing, max_cells)
        assert len(steps) == upper - lower + 1
        for cells, value in zip(range(lower, upper + 1), steps):
            resolved = optimize_run._resolve_min_width(spacing, value, max_cells=max_cells)
            assert resolved["cells"] == cells, (spacing, cells)
            assert value == pytest.approx(cells * spacing, rel=1e-12)
            checked += 1
    assert checked > 0


def test_every_ladder_step_lists_the_selectable_min_widths(session: Session, real_min_width_bounds) -> None:
    """段ごとに、下限から上限まで `m × 要素サイズ` の段が並ぶ(格子が小さく段が無い段は上限 < 下限)。"""
    four_conditions(session)
    for step in optimize_run.resolution_limits(session.document)["steps"]:
        values = step["min_width_steps_mm"]
        if not values:
            assert step["min_width_ceiling_mm"] < step["min_width_floor_mm"]
            continue
        assert values[0] == step["min_width_floor_mm"]
        assert values[-1] == pytest.approx(step["min_width_ceiling_mm"], rel=1e-12)
        assert values[-1] <= step["min_width_ceiling_mm"]
        assert len(values) == round((step["min_width_ceiling_mm"] - step["min_width_floor_mm"]) / step["element_size_mm"]) + 1


def test_every_ladder_step_reports_the_real_grid(session: Session) -> None:
    """落ちたら、梯子の要素サイズ・最小幅の上限が `derive` の格子とずれている(設計空間の要素数は見積もりで実数と数%ずれる)。"""
    from duocad.topopt import design_space as ds

    four_conditions(session)
    limits = optimize_run.resolution_limits(session.document)
    assert len(limits["steps"]) >= 2
    assert all(step["design_elements_estimate"] <= optimize_run.MAX_DESIGN_ELEMENTS for step in limits["steps"])
    assert limits["steps"][-1]["element_size_mm"] == pytest.approx(limits["min_element_size_mm"])
    # 段0(19.6mm)はこの題材で `voxelize.occupancy` が ValueError になり(実測)、末尾は数百万要素で重いので段1で導出する。
    bundle = optimize_run.build_conditions(session.document)
    mid = limits["steps"][1]
    space = ds.derive(bundle.conditions, spacing=mid["element_size_mm"])
    assert space.grid.spacing == pytest.approx(mid["element_size_mm"])
    assert mid["design_elements_estimate"] == pytest.approx(space.element_count, rel=0.3)
    assert mid["min_width_ceiling_mm"] == pytest.approx(
        optimize_run.min_width_cell_range(mid["element_size_mm"], min(space.grid.shape))[1] * mid["element_size_mm"])


def test_a_requested_element_size_off_the_ladder_is_snapped_to_the_real_grid(
    session: Session, monkeypatch
) -> None:
    """梯子の外の要素サイズを渡しても、`derive` が作る格子の要素サイズは要求値以下に揃う。"""
    from duocad.topopt import binary_search

    four_conditions(session)
    limits = optimize_run.resolution_limits(session.document)
    lo, hi = optimize_run._condition_bbox(session.document)
    requested = 0.5 * (limits["max_element_size_mm"] + limits["min_element_size_mm"])
    seen: dict = {}

    def fake_find(design_space_mask, conditions, **kwargs):
        seen["mask"] = design_space_mask
        raise RuntimeError("stop before search")

    monkeypatch.setattr(binary_search, "find_best_feasible", fake_find)
    with pytest.raises(RuntimeError, match="stop before search"):
        optimize_run.run(
            session.document,
            optimize_run.OptimizeOptions(element_size_mm=requested),
        )
    bundle = optimize_run.build_conditions(session.document)
    from duocad.topopt import design_space as ds

    spacing = optimize_run._snap_to_grid(lo, hi, requested)
    space = ds.derive(bundle.conditions, spacing=spacing)
    assert space.grid.spacing <= requested * (1.0 + 1e-9)
    assert seen["mask"].shape == space.mask.shape


def test_a_derived_space_over_the_limit_stops_before_the_search(session: Session, monkeypatch) -> None:
    """落ちたら、実行前の見積もりを通った要求でも、`derive` 後の実数が上限を超えたのに探索へ進んでいる。

    見積もりは実数とずれうるので、拒否の最終判断は `derive` の後の `space.element_count` に置く。
    見積もり側の門(`_min_element_size`)を素通しにして、実数の門だけを効かせる。
    """
    from duocad.topopt import binary_search

    four_conditions(session)
    limits = optimize_run.resolution_limits(session.document)
    mid = limits["steps"][1]
    monkeypatch.setattr(optimize_run, "MAX_DESIGN_ELEMENTS", 10)
    monkeypatch.setattr(optimize_run, "_min_element_size", lambda lo, hi, volume: mid["element_size_mm"] * 0.5)

    def never(*args, **kwargs):
        raise AssertionError("上限超えなのに探索が始まった")

    monkeypatch.setattr(binary_search, "find_best_feasible", never)
    with pytest.raises(optimize_run.OptimizeInputError, match=r"設計空間の要素数 [\d,]+ が上限 10 を超えている.*mm 以上にすること"):
        optimize_run.run(session.document, optimize_run.OptimizeOptions(element_size_mm=mid["element_size_mm"]))


def test_the_element_size_lower_bound_comes_from_design_elements_not_the_box(session: Session) -> None:
    """下限の要素サイズでの設計空間の見積もりが上限に収まり、1段だけ細かい格子だと超える。"""
    four_conditions(session)
    doc = session.document
    lo, hi = optimize_run._condition_bbox(doc)
    volume = optimize_run._design_volume(doc)
    lower = optimize_run.resolution_limits(doc)["min_element_size_mm"]
    box_long = optimize_run._box_longest_edge(lo, hi)
    assert optimize_run._design_element_estimate(volume, lower) <= optimize_run.MAX_DESIGN_ELEMENTS
    finer = box_long / (round(box_long / lower) + 1)
    assert optimize_run._design_element_estimate(volume, finer) > optimize_run.MAX_DESIGN_ELEMENTS


def test_build_conditions_lists_the_condition_bodies_and_ignores_unrelated_ones(session: Session) -> None:
    """凸包の点群になるのは保持ボディと条件面のボディだけ。無関係なボディを足しても変わらない。"""
    left, right, _fixed, _load = four_conditions(session)
    before = optimize_run.build_conditions(session.document).conditions.bodies
    assert [(b.name, b.roles) for b in before] == [
        (left.id, ("keep", "fixed")),
        (right.id, ("keep", "load")),
    ]
    pad(session, 5.0, 5.0, 5.0, corner=(200.0, 200.0))  # 条件に関係ないボディ
    after = optimize_run.build_conditions(session.document).conditions.bodies
    assert [(b.name, b.bbox_min, b.bbox_max) for b in after] == [
        (b.name, b.bbox_min, b.bbox_max) for b in before
    ]


def test_run_derives_once_and_hands_the_space_to_the_search(
    session: Session, monkeypatch
) -> None:
    """**塊は探索の前に1回だけ導出し、そのまま `find_best_feasible` へ渡す。**

    デバッグ配信の塊(`space`)は導出直後の1回だけで、探索の後に別の塊を配り直さない
    (計画書§3「固定設計空間を1回導出」)。
    """
    from duocad.topopt import binary_search
    from duocad.topopt import design_space as ds

    four_conditions(session)
    seen: dict = {}
    derived: list = []
    original_derive = ds.derive

    def spy_derive(conditions, **kwargs):
        space = original_derive(conditions, **kwargs)
        derived.append(space)
        return space

    def fake_find(design_space_mask, conditions, **kwargs):
        seen["mask"] = design_space_mask
        seen["conditions"] = conditions
        seen.update(kwargs)
        raise RuntimeError("stop before search")

    monkeypatch.setattr(ds, "derive", spy_derive)
    monkeypatch.setattr(binary_search, "find_best_feasible", fake_find)
    payloads: list = []
    # **既定(いちばん粗い)解像度は使わない。** `voxelize.occupancy` がこの題材の粗い格子を
    # 通せない実測があり(段階5とは無関係な既知の別問題)、ここで見たいのは配線(1回だけ
    # 導出してそのまま探索へ渡る)であって解像度そのものではないので、動く値を明示する。
    with pytest.raises(RuntimeError, match="stop before search"):
        optimize_run.run(
            session.document,
            optimize_run.OptimizeOptions(element_size_mm=8.0),
            debug=payloads.append,
            debug_kinds=("space", "anchors", "rho"),
        )
    assert len(derived) == 1
    assert seen["mask"] is derived[0].mask
    # アンカーは導出の幾何の表ではなく、境界条件の確定後の表(探索が使うもの)から配る。
    assert [(p["kind"], p["stage"]) for p in payloads] == [("space", "derive"), ("anchors", "mapping")]
    conditions = seen["conditions"]
    assert conditions.table is not derived[0].table
    assert conditions.table.stats["mapping"] == "material"
    assert conditions.mapping["status"] == "pass"


# --------------------------------------------------------------------------------------
# 5. 座標系(要素添字空間 → ワールド[mm])
# --------------------------------------------------------------------------------------
def test_mesh_vertices_are_converted_to_world_millimetres() -> None:
    from duocad.topopt import voxelize as vx

    grid = vx.Grid(origin=(10.0, -5.0, 2.0), spacing=0.5, shape=(4, 4, 4))
    raw = [[0.0, 0.0, 0.0], [3.0, 3.0, 3.0]]
    flat = optimize_run._mesh_to_world(raw, grid)
    assert flat == pytest.approx([10.0, -5.0, 2.0, 11.5, -3.5, 3.5])
    optimize_run._check_mesh_placement(flat, grid)
    # **変換を落とすと格子の箱の外に出る**(=黙って原点付近へ縮んだメッシュを検出できる)。
    with pytest.raises(optimize_run.FaceMappingError, match="格子の箱の外"):
        optimize_run._check_mesh_placement(
            [value for point in raw for value in point], grid
        )


# --------------------------------------------------------------------------------------
# 6. 中断
# --------------------------------------------------------------------------------------
def test_cancelling_leaves_the_document_untouched(session: Session) -> None:
    """**旗が立っていれば1つも作らずに降りる。** 塊の導出より手前で見るので費用も掛からない。"""
    four_conditions(session)
    before = [body.id for body in session.document.bodies]
    applied = session.applied_count
    events: list = []
    with pytest.raises(optimize_run.OptimizeCancelled):
        optimize_run.run(
            session.document,
            optimize_run.OptimizeOptions(),
            progress=events.append,
            should_cancel=lambda: True,
        )
    assert events == []  # 最初の進捗を出す前に降りる
    assert [body.id for body in session.document.bodies] == before
    assert session.applied_count == applied


def test_run_optimization_clears_a_stale_cancel_flag(session: Session) -> None:
    """**旗を畳むのは実行の側**である。畳まないと、前回の中断が次の実行を即死させる。

    実際に走らせずに確かめる —— `run_optimization` は条件が無ければ
    `OptimizeInputError` で落ちるが、そこへ着くのは旗を畳んだ**後**である。
    """
    session.cancel_optimization()
    assert session._optimize_cancel.is_set()
    with pytest.raises(optimize_run.OptimizeInputError):
        session.run_optimization()
    assert not session._optimize_cancel.is_set()


@pytest.mark.parametrize(
    "kwarg", ["rmin_mm", "max_iter", "material_min_mm3", "material_max_mm3"]
)
def test_run_optimization_rejects_each_old_argument_by_name(
    session: Session, kwarg: str
) -> None:
    """落ちたら、`session.run_optimization`(WS 経由の検査 `test_optimize_run_ui.py` と別層の門)が
    旧SIMP方式の引数を黙って無視するか、名指しせずに断っている。"""
    with pytest.raises(optimize_run.OptimizeInputError, match=kwarg):
        session.run_optimization(**{kwarg: 1.0})


def test_run_optimization_refuses_a_min_width_below_the_floor(session: Session) -> None:
    four_conditions(session)
    limits = optimize_run.resolution_limits(session.document)
    spacing = limits["default_element_size_mm"]
    with pytest.raises(optimize_run.OptimizeInputError, match="下限"):
        session.run_optimization(min_width_mm=spacing)  # 下限は 2×spacing なので、spacing ちょうどは断られる


def test_the_resolved_min_width_cells_reach_the_search(session: Session, monkeypatch) -> None:
    """`min_width_mm` → `m = ceil(mm / spacing)` → `find_best_feasible(min_width_cells=m)` の配線。
    落ちたら、指定した幅が探索に届かず制約なしで走る。"""
    from duocad.topopt import binary_search

    four_conditions(session)
    seen: dict = {}

    def fake_find(design_space_mask, conditions, **kwargs):
        seen.update(kwargs)
        return binary_search.SearchResult(
            status="no_feasible_found", termination="cancelled", best_feasible=None,
            history=[], evaluations=0, conditions_identifier="test",
        )

    monkeypatch.setattr(binary_search, "find_best_feasible", fake_find)
    spacing = optimize_run.resolution_limits(session.document)["steps"][1]["element_size_mm"]
    with pytest.raises(optimize_run.OptimizeCancelled):
        optimize_run.run(
            session.document,
            optimize_run.OptimizeOptions(element_size_mm=spacing, min_width_mm=2.5 * spacing),
        )
    assert seen["min_width_cells"] == 3


def test_search_cancellation_leaves_the_document_untouched_even_with_a_found_best_feasible(
    session: Session, monkeypatch
) -> None:
    """落ちたら、探索が `termination='cancelled'` で返したのに、見つかっていた合格形状をメッシュ化して文書へ足している。"""
    import numpy as np

    from duocad.topopt import binary_search
    from duocad.topopt import mesh as topo_mesh

    four_conditions(session)
    before = [body.id for body in session.document.bodies]
    applied = session.applied_count

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("中断時にメッシュ化まで進んでいる")

    monkeypatch.setattr(topo_mesh, "build_surface_from_mask", fail_if_called)

    def fake_find(design_space_mask, conditions, **kwargs):
        mask = np.asarray(design_space_mask, dtype=bool)
        report = dict(
            material_sha256="c" * 64,
            volumes=dict(material_mm3=float(mask.sum()) * conditions.grid.element_volume),
            checks=dict(safety=dict(safety_factor=9.9)),
        )
        best = binary_search.FeasibleModel(mask=mask, before_mask=mask, report=report, trial_index=0)
        return binary_search.SearchResult(
            status="feasible", termination="cancelled", best_feasible=best,
            history=[], evaluations=5, conditions_identifier="test",
        )

    monkeypatch.setattr(binary_search, "find_best_feasible", fake_find)
    with pytest.raises(optimize_run.OptimizeCancelled):
        optimize_run.run(session.document, optimize_run.OptimizeOptions(element_size_mm=8.0))
    assert [body.id for body in session.document.bodies] == before
    assert session.applied_count == applied


def test_every_evaluation_of_the_search_reaches_the_progress_sink_and_never_raises_the_cancel_there(
    session: Session, monkeypatch
) -> None:
    """落ちたら、探索中の途中経過がパネルへ届いていない、または進捗の口で中断の例外を上げて
    探索の戻り値(合格チェックポイントの記録)を失っている。"""
    from duocad.topopt import binary_search

    four_conditions(session)
    cancel = {"now": False}

    def fake_find(design_space_mask, conditions, *, on_evaluation, **kwargs):
        target = conditions.target_safety_factor
        on_evaluation(dict(evaluation=1, outcome="initial", failed=[], candidate_safety_factor=target * 3,
                           candidate_cells=1000, safety_factor=target * 3, cells=1000, target_safety_factor=target))
        cancel["now"] = True
        on_evaluation(dict(evaluation=2, outcome="accepted", failed=[], candidate_safety_factor=target * 1.5,
                           candidate_cells=500, safety_factor=target * 1.5, cells=500, target_safety_factor=target))
        on_evaluation(dict(evaluation=3, outcome="rejected", failed=["safety"], candidate_safety_factor=target * 0.99,
                           candidate_cells=400, safety_factor=target * 1.5, cells=500, target_safety_factor=target))
        on_evaluation(dict(evaluation=4, outcome="rejected", failed=["geometry"], candidate_safety_factor=None,
                           candidate_cells=450, safety_factor=target * 1.5, cells=500, target_safety_factor=target))
        return binary_search.SearchResult(
            status="no_feasible_found", termination="cancelled", best_feasible=None,
            history=[], evaluations=4, conditions_identifier="test")

    monkeypatch.setattr(binary_search, "find_best_feasible", fake_find)
    events: list = []
    with pytest.raises(optimize_run.OptimizeCancelled):
        optimize_run.run(session.document, optimize_run.OptimizeOptions(element_size_mm=8.0),
                         progress=events.append, should_cancel=lambda: cancel["now"])
    evaluations = [event for event in events if "evaluation" in event]
    assert [event["evaluation"] for event in evaluations] == [1, 2, 3, 4]
    assert all(event["phase"] == "search" for event in evaluations)
    assert [event["message"].split(":")[0] for event in evaluations] == ["評価 1", "評価 2", "評価 3", "評価 4"]


def test_search_statuses_other_than_feasible_pass_through_without_touching_the_document(
    session: Session, monkeypatch
) -> None:
    """`SearchResult.status` が `'feasible'` 以外の4値は、そのまま `state` として返る

    (段階4の契約「製品配線側はこの値をそのままUI/ログへ渡せばよい」)。文書は変わらない。
    `boundary_mapping_failed` の一言は評価予算の増加を案内しない(候補では直らない)。
    """
    from duocad.topopt import binary_search

    four_conditions(session)
    before = [body.id for body in session.document.bodies]

    for status in ("no_feasible_found", "input_contradiction", "resolution_insufficient",
                   "boundary_mapping_failed"):
        def fake_find(design_space_mask, conditions, status=status, **kwargs):
            return binary_search.SearchResult(
                status=status, termination=status, best_feasible=None,
                history=[], evaluations=7, conditions_identifier="test",
                diagnostics=dict(
                    empty_faces=["fixed_face_0"], mapped=True,
                    uncovered_condition_nodes=[dict(condition="load_face_1", nodes=10, uncovered_nodes=3)],
                ),
            )

        monkeypatch.setattr(binary_search, "find_best_feasible", fake_find)
        result = optimize_run.run(session.document, optimize_run.OptimizeOptions(element_size_mm=8.0))
        assert result["state"] == status, status
        assert result["body_id"] is None
        assert result["warning"]
        assert result["evaluations"] == 7
        assert [body.id for body in session.document.bodies] == before
        if status == "boundary_mapping_failed":
            assert "予算" not in result["warning"]
            assert "load_face_1" in result["warning"]


def test_a_closed_void_in_the_design_space_is_explained_with_where_and_what_to_do() -> None:
    """設計空間の閉じた空洞による入力矛盾は、保持の矛盾ではなく空洞の位置・大きさと直し方を言う。"""
    from duocad.topopt import binary_search

    result = binary_search.SearchResult(
        status="input_contradiction", termination="input_contradiction", best_feasible=None, history=[],
        evaluations=0, conditions_identifier="test",
        diagnostics=dict(closed_voids=dict(count=2, largest=[dict(cells=12, representative_cell=[3, 4, 5])])))
    warning = optimize_run._search_failure_warning(result)
    assert "閉じた空洞が 2 個" in warning and "[3, 4, 5](12セル)" in warning and "除外" in warning
    assert "保持ソリッド" not in warning


def _failed_result(status: str, termination: str, **diagnostics):
    from duocad.topopt import binary_search

    return binary_search.SearchResult(
        status=status, termination=termination, best_feasible=None, history=[], evaluations=3,
        conditions_identifier="test", diagnostics=diagnostics)


def _full_material_failure(termination: str, status: str, checks: dict, **diagnostics):
    attempt = dict(trial=0, status=status, failed=list(checks), checks=checks)
    return _failed_result("no_feasible_found", termination, full_material=dict(
        islands=dict(components=1, surviving_components=1, removed_islands=[]), attempts=[attempt]), **diagnostics)


def test_a_full_material_failure_names_the_failed_check_with_its_numbers_and_never_the_budget() -> None:
    """落ちたら、全材料が不合格のときに「どの検査が・どんな値で」落ちたかが画面に出ない(予算では直らないのに予算を案内する)。"""
    safety = _full_material_failure("full_material_infeasible", "fail", dict(
        safety=dict(status="fail", safety_factor=1.25, target_safety_factor=2.0)))
    warning = optimize_run._search_failure_warning(safety)
    assert "全材料でも6条件に合格しない" in warning and "安全率 1.25 が目標 2 に届かない" in warning
    assert "予算" not in warning

    geometry = _full_material_failure("full_material_infeasible", "fail", dict(
        watertight=dict(status="fail", irregular_vertices=3, sample_vertex_indices=[[1, 2, 3]], void_components=1),
        exclude=dict(status="fail", bodies=[dict(body="hole", status="fail"), dict(body="slot", status="pass")]),
        safety=dict(status="unknown", reason="prerequisites_failed")))
    warning = optimize_run._search_failure_warning(geometry)
    assert "非多様体の頂点 3 個" in warning and "[[1, 2, 3]]" in warning
    assert "除外ソリッドに材料が掛かっている(hole)" in warning and "slot" not in warning
    assert "安全率" not in warning.split("。")[0]  # 前提の検査が落ちて飛ばしたFEMを、安全率の不足として語らない

    solver = _full_material_failure("evaluation_error", "unknown", dict(
        safety=dict(status="unknown", reason="evaluation_error", error="RuntimeError: singular")))
    warning = optimize_run._search_failure_warning(solver)
    assert "全材料の応力計算が失敗した" in warning and "RuntimeError: singular" in warning


def test_a_full_material_failure_under_min_width_says_the_design_space_was_trimmed() -> None:
    """落ちたら、最小幅で絞った設計空間の全材料が落ちたのに、絞ったことが画面に出ない。最小幅が無い実行の文面は変えない。"""
    min_width = {"requested_mm": 5.0, "spacing_mm": 2.4, "cells": 3, "guaranteed_mm": 7.2}
    checks = dict(safety=dict(status="fail", safety_factor=1.25, target_safety_factor=2.0))
    failed = _full_material_failure("full_material_infeasible", "fail", checks,
                                    min_width=dict(cells=3, design_cells=1000, trimmed_cells=58))
    plain = optimize_run._search_failure_warning(failed)
    assert "最小幅" not in plain
    warning = optimize_run._search_failure_warning(failed, min_width)
    assert warning.startswith(plain) and "最小幅 7.2 mm(3セル)の制約が有効" in warning and "58/1000" in warning


def test_an_entry_failure_says_the_check_ran_on_the_design_space_trimmed_by_min_width() -> None:
    """設計空間を m 立方の opening で絞ると、入口検査は絞った空間に対する結果になる。絞ったセル数を利用者に見せる。
    何も絞っていない(=最小幅が原因でない)ときと、最小幅と無関係な `resolution_insufficient` には添えない。"""
    min_width = {"requested_mm": 5.0, "spacing_mm": 2.4, "cells": 3, "guaranteed_mm": 7.2}
    trimmed = dict(min_width=dict(cells=3, design_cells=1000, trimmed_cells=58))
    failed = _failed_result("boundary_mapping_failed", "boundary_mapping_failed",
                            uncovered_condition_nodes=[dict(condition="load_face_1", uncovered_nodes=6, nodes=25)],
                            **trimmed)
    assert "58/1000 セルを除いた後の空間" in optimize_run._search_failure_warning(failed, min_width)
    contradiction = _failed_result("input_contradiction", "input_contradiction", **trimmed)
    assert "58/1000" in optimize_run._search_failure_warning(contradiction, min_width)
    untouched = _failed_result("input_contradiction", "input_contradiction",
                               min_width=dict(cells=3, design_cells=1000, trimmed_cells=0))
    assert "最小幅" not in optimize_run._search_failure_warning(untouched, min_width)
    starved = _failed_result("resolution_insufficient", "resolution_insufficient", empty_faces=[1], **trimmed)
    assert "最小幅" not in optimize_run._search_failure_warning(starved, min_width)


def test_no_candidate_meshing_and_the_confirmed_mask_reaches_finalize(
    session: Session, monkeypatch
) -> None:
    """**探索候補の評価中はメッシュ化・STL出力を呼ばない。後段へ渡るのは最終確定マスクだけ**

    (計画書§3)。`mesh.write_stl` が呼ばれたら落とし、`mesh.build_surface_from_mask` が
    ちょうど1回だけ、探索が返した確定マスクで呼ばれることを確かめる。
    """
    import numpy as np

    from duocad.topopt import binary_search
    from duocad.topopt import mesh as topo_mesh

    four_conditions(session)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("mesh.write_stl が呼ばれた")

    monkeypatch.setattr(topo_mesh, "write_stl", fail_if_called)

    calls: list = []
    kwargs_seen: list = []
    original_build_from_mask = topo_mesh.build_surface_from_mask

    def spy_build_from_mask(mask, **kwargs):
        calls.append(np.array(mask, copy=True))
        kwargs_seen.append(kwargs)
        return original_build_from_mask(mask, **kwargs)

    monkeypatch.setattr(topo_mesh, "build_surface_from_mask", spy_build_from_mask)

    def fake_find(design_space_mask, conditions, **kwargs):
        mask = np.asarray(design_space_mask, dtype=bool)
        report = dict(
            material_sha256="f" * 64,
            volumes=dict(material_mm3=float(mask.sum()) * conditions.grid.element_volume),
            checks=dict(safety=dict(safety_factor=9.9)),
        )
        best = binary_search.FeasibleModel(mask=mask, before_mask=mask, report=report, trial_index=0)
        return binary_search.SearchResult(
            status="feasible", termination="converged", best_feasible=best,
            history=[], evaluations=3, conditions_identifier="test",
        )

    monkeypatch.setattr(binary_search, "find_best_feasible", fake_find)

    result = optimize_run.run(session.document, optimize_run.OptimizeOptions(element_size_mm=8.0))

    assert result["state"] == "done"
    assert result["body_id"] is not None
    assert result["termination"] == "converged"
    assert result["evaluations"] == 3
    # 結果は編集可能ソリッドではなくメッシュボディで入る(2026-09-23 本人判断。docs/TOPOPT.md)。
    # 三角形は `finalize` が合格させたものそのもので、画面・STL はこれをそのまま使う。
    body = session.document.get_body(result["body_id"])
    assert body.kind == "mesh" and body.mesh is not None and body.shape is None
    assert body.mesh.triangle_count == result["triangle_count"]
    assert len(body.mesh.positions) == 3 * result["vertex_count"]
    assert len(calls) == 1
    # **`spacing` を渡さない(要素添字空間のまま)。** 渡すと `_mesh_to_world` の
    # `verts * grid.spacing` と二重に掛かり、メッシュが格子の外へ大きく外れる
    # (実装時に実際に踏んだ回帰 —— このassertが無いと `_check_mesh_placement` の
    # 例外に化けるまで気づけない)。
    assert "spacing" not in kwargs_seen[0]


# --------------------------------------------------------------------------------------
# 8. import の費用
# --------------------------------------------------------------------------------------
def test_importing_the_session_does_not_pull_in_the_topopt_layer() -> None:
    """`optimize_run` を `session.py` が読むようになっても、**最適化の層は来ない**。

    `duocad/topopt/*` の import は関数の中に置いてある(`docs/contracts/topopt-pipeline-integration.md` の `20260904-1600` 契約②)。
    ここが崩れると pytest 全体が毎回 skimage と `duocad.topopt` の起動費用を払う。

    **`scipy` は見ない** —— `duocad/view.py` が(このタスクの前から)既に読んでいるので、
    scipy の有無ではこの配線の退行を検出できない(2026-09-05実測)。
    """
    code = (
        "import sys; import duocad; import duocad.session; "
        "print(sorted(m for m in sys.modules "
        "if m.startswith('skimage') or m.startswith('duocad.topopt')))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", out.stdout


# --------------------------------------------------------------------------------------
# 9. 開発用のデバッグ表示(`?debug=space` / `?debug=anchors` / `?voxel=1`。20260905-1700)
#
# 端から端まで(本物のSIMPを1回通して塊・節点・密度場が実ブラウザに出る)は
# `web/scripts/smoke-optimize-debug.mjs`。ここで固定するのは、**外れても例外が出ないまま
# 静かに間違う**2点である: (a) セル中心の規約(半要素ずれても絵は出る)、
# (b) 頼んでいない配信が起きない(＝既定の実行に費用が乗らない)。
# --------------------------------------------------------------------------------------
def test_debug_voxels_carry_the_cell_centre_convention() -> None:
    """通し添字はC順(`i*(ny*nz) + j*nz + k`)。**画面はこれを中心へ戻す。**

    ここがずれると、画面のボクセルが半要素ぶん(あるいは丸ごと)動く —— 絵は出るので
    目視では気づけない(`duocad/topopt/mesh.py:169` のセル中心の規約)。
    """
    import numpy as np

    from duocad.topopt import voxelize as vx

    grid = vx.Grid(origin=(1.0, 2.0, 3.0), spacing=0.5, shape=(2, 3, 4))
    mask = np.zeros(grid.shape, dtype=bool)
    mask[0, 0, 0] = True
    mask[1, 2, 3] = True
    payload = optimize_run._debug_voxel_payload("space", "derive", grid, mask, limit=10_000)

    assert payload["kind"] == "space"
    assert payload["stage"] == "derive"
    assert payload["grid"] == {"origin": [1.0, 2.0, 3.0], "spacing": 0.5, "shape": [2, 3, 4]}
    assert payload["elements"] == [0, 1 * (3 * 4) + 2 * 4 + 3]
    assert payload["total"] == 2 and payload["shown"] == 2 and payload["stride"] == 1

    # 画面がやる復元(`web/src/optimizeDebug.ts` の `buildDebugVoxels`)と同じ算術で中心へ戻す。
    _, ny, nz = grid.shape
    for flat, want in zip(payload["elements"], [(0, 0, 0), (1, 2, 3)]):
        ex, rest = divmod(flat, ny * nz)
        ey, ez = divmod(rest, nz)
        assert (ex, ey, ez) == want
        assert grid.center_of(ex, ey, ez).tolist() == pytest.approx(
            [
                1.0 + 0.5 * (ex + 0.5),
                2.0 + 0.5 * (ey + 0.5),
                3.0 + 0.5 * (ez + 0.5),
            ]
        )


def test_debug_voxels_thin_out_instead_of_shipping_a_million_cubes() -> None:
    """上限を超えたら**軸方向に間引く**。間引いたことが payload から読めること。

    立方体1個 = 12三角形なので、実物の部品の塊(5〜12万要素)をそのまま出すと
    60〜144万三角形になる。**黙って全部送らない**(画面が固まる)、
    **黙って切り捨てない**(「全部見えている」と誤解する)、の両方を守る。
    """
    import numpy as np

    from duocad.topopt import voxelize as vx

    grid = vx.Grid(origin=(0.0, 0.0, 0.0), spacing=1.0, shape=(10, 10, 10))
    mask = np.ones(grid.shape, dtype=bool)
    payload = optimize_run._debug_voxel_payload("space", "final", grid, mask, limit=200)

    assert payload["total"] == 1000
    assert payload["stride"] == 2  # 5*5*5 = 125 <= 200(stride=1 の 1000 は超える)
    assert payload["shown"] == 125
    assert len(payload["elements"]) == 125
    # 間引いても**格子の添字のまま**である(座標を作り直していない)。
    _, ny, nz = grid.shape
    for flat in payload["elements"]:
        ex, rest = divmod(flat, ny * nz)
        ey, ez = divmod(rest, nz)
        assert ex % 2 == 0 and ey % 2 == 0 and ez % 2 == 0


def test_debug_sink_builds_nothing_unless_asked() -> None:
    """**頼まれた種類だけ**を作る。頼まれなければ `numpy` の配列を1本も作らない。

    これが崩れると、フラグを付けていない利用者の実行にデバッグ配信の費用が乗る
    (数十万要素の `argwhere` + JSON 化)。`wants()` が門で、`space()` /
    `anchors()` / `rho()` はその門を必ず先に通る。
    """
    import numpy as np

    from duocad.topopt import voxelize as vx

    grid = vx.Grid(origin=(0.0, 0.0, 0.0), spacing=1.0, shape=(2, 2, 2))

    class _Space:
        def __init__(self) -> None:
            self.grid = grid
            self.mask = np.ones(grid.shape, dtype=bool)
            self.table = None

    events: list = []
    # (a) sink が無い → 何も呼ばれない・例外も出ない
    quiet = optimize_run._DebugSink(None, optimize_run.DEBUG_KINDS)
    assert not quiet.wants("space")
    quiet.space("derive", _Space())
    quiet.rho("final", grid, np.ones(grid.shape))

    # (b) 頼んだ種類だけ通る
    sink = optimize_run._DebugSink(events.append, ["space"])
    assert sink.wants("space") and not sink.wants("rho") and not sink.wants("anchors")
    sink.space("derive", _Space())
    sink.rho("final", grid, np.ones(grid.shape))
    sink.anchors("derive", _Space())
    assert [event["kind"] for event in events] == ["space"]

    # (c) 表が無い塊ではアンカーを配らない(検査が直接組んだ塊。**例外にしない**)
    events.clear()
    both = optimize_run._DebugSink(events.append, ["anchors", "rho"])
    both.anchors("derive", _Space())
    assert events == []
    both.rho("final", grid, np.ones(grid.shape))
    assert [event["kind"] for event in events] == ["rho"]
    assert events[0]["threshold"] == pytest.approx(optimize_run.DEBUG_RHO_THRESHOLD)


# --------------------------------------------------------------------------------------
# 9. 面アンカーの門が「面の上のどこか」と比べている
# --------------------------------------------------------------------------------------
def test_face_centroid_is_the_area_centroid_not_the_geometry_center() -> None:
    """**曲面では両者が大きく離れる。** 離れないなら片方をもう片方で代用してしまっている。"""
    from duocad import query
    from duocad.document import Document
    from duocad.region import region_ref
    from duocad.sketch import add_circle, add_rectangle, base_plane, sketch_create
    from duocad.verbs import extrude, fillet

    doc = Document()
    sketch = sketch_create(doc, base_plane("XY"))
    add_rectangle(doc, sketch, (0.0, 0.0), 20.0, 20.0)
    body_id = extrude(doc, [region_ref(sketch, 1)], 20.0, mode="join").args["body_id"]
    hole = sketch_create(doc, base_plane("XY"))
    add_circle(doc, hole, (10.0, 10.0), 4.0)
    extrude(doc, [region_ref(hole, 1)], 20.0, mode="cut", body_id=body_id)
    circles = [
        edge["index"]
        for edge in query.describe_edges(
            next(b for b in doc.bodies if b.id == body_id).shape
        )
        if edge["kind"] == "circle"
    ]
    fillet(doc, circles[:1], 1.5, body_id=body_id)
    shape = next(b for b in doc.bodies if b.id == body_id).shape

    faces = query.describe_faces(shape)
    curved = next(f["index"] for f in faces if f["kind"] == "torus")
    flat = next(f["index"] for f in faces if f["kind"] == "plane")

    def gap(index: int) -> float:
        centroid = query.face_centroid(shape, index)
        center = query.face_fingerprint(shape, index)["center"]
        return math.dist(centroid, center)

    assert gap(flat) == pytest.approx(0.0, abs=1e-6)
    assert gap(curved) > 1.0


def test_the_anchor_gate_does_not_read_the_baked_center(session: Session) -> None:
    """**門は焼き込んだ `center` を見ない。** 見ていると、曲面の条件面で解像度を上げるほど
    厳しくなり(焼き込みは `CenterOf.GEOMETRY` で面パッチから離れる)、いつか必ず落ちる。
    """
    from duocad.topopt import design_space as ds

    four_conditions(session)
    bundle = optimize_run.build_conditions(session.document)
    space = ds.derive(bundle.conditions, elements_long_axis=12)

    healthy = optimize_run._check_anchor_centroids(session.document, space)
    assert healthy["load_mm"] <= healthy["limit_mm"]

    # 焼き込みを 1000mm ずらしても判定は動かない(= 読んでいない)。
    for condition in session.document.optimize.loads:
        condition["center"] = [v + 1000.0 for v in condition["center"]]
    after = optimize_run._check_anchor_centroids(session.document, space)
    assert after["load_mm_each"] == pytest.approx(healthy["load_mm_each"])


def test_the_anchor_gate_still_catches_a_face_that_is_far_away(
    session: Session, monkeypatch
) -> None:
    """検出力の確認 —— 面の重心が本当に離れていれば落ちる(門を無力化していない)。"""
    from duocad import query
    from duocad.topopt import design_space as ds

    four_conditions(session)
    bundle = optimize_run.build_conditions(session.document)
    space = ds.derive(bundle.conditions, elements_long_axis=12)

    real = query.face_centroid

    def far(shape, face_index):
        return [v + 500.0 for v in real(shape, face_index)]

    monkeypatch.setattr(query, "face_centroid", far)
    with pytest.raises(optimize_run.FaceMappingError, match="離れている"):
        optimize_run._check_anchor_centroids(session.document, space)


# --------------------------------------------------------------------------------------
# 9. 境界条件の確定(docs/FIX_BINARY_SEARCH_INITIALIZATION.md 修正A・B)
# --------------------------------------------------------------------------------------
def test_a_load_face_on_a_body_that_is_not_preserved_is_refused(session: Session) -> None:
    """落ちたら、保持しないボディの面に荷重を置けてしまう(探索が削れる材料に荷重が載り、確定表が
    そのボディ以外の材料の節点へ荷重を配る)か、条件の載ったボディを保持から外せてしまう。"""
    left, right = two_pads(session)
    plate = pad(session, 28.0, 40.0, 4.0, corner=(30.0, 0.0))  # 保持しない板(x 30..58)
    fixed = face_with_normal(left, -1.0, 0.0, 0.0)
    load = face_with_normal(plate, 0.0, 0.0, 1.0)
    _select(session, [{"body": left.id, "kind": "body"}, {"body": right.id, "kind": "body"}])
    session.set_preserve_bodies()
    _select(session, [{"body": left.id, "kind": "face", "index": fixed}])
    session.set_fixtures()

    _select(session, [{"body": plate.id, "kind": "face", "index": load}])
    with pytest.raises(ValueError, match="保持ソリッドの面"):
        session.set_loads(500.0, [0.0, 0.0, -1.0])
    assert session.document.optimize.loads == [], "断ったのに文書へ残っている"
    # 保持から外す側も同じ不変条件で守る(条件が載っているボディは保持から外せない)。
    _select(session, [{"body": right.id, "kind": "body"}])
    with pytest.raises(ValueError, match="保持ソリッドから外せない"):
        session.set_preserve_bodies()


def test_the_search_conditions_carry_the_material_mapped_table_and_its_report(session: Session) -> None:
    """`_build_search_conditions` を直接呼ぶ検査・CLIでも、製品と同じ確定表と検証結果が付く。"""
    from duocad.topopt import design_space as ds

    four_conditions(session)
    bundle = optimize_run.build_conditions(session.document)
    space = ds.derive(bundle.conditions, spacing=8.0)
    conditions = optimize_run._build_search_conditions(
        session.document, space, bundle, optimize_run.OptimizeOptions()
    )
    assert conditions.table is not space.table
    assert conditions.table.stats["mapping"] == "material"
    assert space.table.stats["mapping"] == "geometry"
    assert conditions.mapping["status"] == "pass"
    assert set(conditions.mapping["geometry_table_difference"]) == {
        row["condition"] for row in conditions.mapping["faces"]
    }
    check = optimize_run._check_anchor_centroids(session.document, space, table=conditions.table)
    assert check["table"] == "material" and check["load_mm"] <= check["limit_mm"]


def test_keep_cells_without_room_warning_says_what_squeezes_them() -> None:
    """保証が効かない保持セルは黙らず警告になり、周りが除外ソリッドか設計空間の端かで原因が言い分けられる。0個なら何も出ない。"""
    min_width = {"requested_mm": 5.0, "spacing_mm": 2.4, "cells": 3, "guaranteed_mm": 7.2}
    none = {"cells": 0, "around_exclude": 0, "around_outside": 0}
    assert optimize_run._keep_without_room_warning(min_width, none) == ""
    squeezed = optimize_run._keep_without_room_warning(
        min_width, {"cells": 48, "around_exclude": 146, "around_outside": 0})
    assert "48 個" in squeezed and "除外ソリッドに挟まれて" in squeezed and "設計空間の端" not in squeezed
    edge = optimize_run._keep_without_room_warning(
        min_width, {"cells": 5, "around_exclude": 0, "around_outside": 20})
    assert "設計空間の端にあって" in edge and "除外ソリッドに挟まれて" not in edge
    both = optimize_run._keep_without_room_warning(
        min_width, {"cells": 5, "around_exclude": 3, "around_outside": 20})
    assert "除外ソリッドと設計空間の端に挟まれて" in both
    assert optimize_run._join_warnings("a。", "", "b") == "a。b"


def test_the_search_conditions_carry_the_keep_cells_and_the_space_keeps_the_exclusion_occupancy(
    session: Session,
) -> None:
    """探索へ渡る `keep_cells` は塊の保持の占有そのもの(CADから作り直さない)。`obstacle` は格子と同じ形。"""
    from duocad.topopt import design_space as ds

    four_conditions(session)
    bundle = optimize_run.build_conditions(session.document)
    space = ds.derive(bundle.conditions, spacing=8.0)
    conditions = optimize_run._build_search_conditions(
        session.document, space, bundle, optimize_run.OptimizeOptions()
    )
    assert conditions.keep_cells is space.keep and space.keep.any()
    assert space.obstacle.shape == tuple(space.grid.shape) and not space.obstacle.any()
    assert not space.obstacle.flags.writeable


def test_the_result_reports_the_keep_cells_without_room_and_only_when_the_count_is_nonzero(
    session: Session, monkeypatch
) -> None:
    """`min_width_keep_without_room` は戻り値に常にあり、`min-width-keep-without-room` の
    トレース記録も件数に関わらず毎回残る。警告は対象セルが1個以上あるときだけ数が出る。"""
    from duocad.topopt import binary_search, trace

    four_conditions(session)

    def fake_find(design_space_mask, conditions, **kwargs):
        return binary_search.SearchResult(
            status="no_feasible_found", termination="evaluation_limit", best_feasible=None,
            history=[], evaluations=1, conditions_identifier="test")

    monkeypatch.setattr(binary_search, "find_best_feasible", fake_find)
    snapshots: list = []
    real_snapshot = trace.snapshot
    monkeypatch.setattr(trace, "snapshot", lambda name, **values: (snapshots.append((name, values)),
                                                                   real_snapshot(name, **values))[1])
    monkeypatch.setattr(binary_search, "keep_cells_without_room",
                        lambda *args, **kwargs: dict(cells=0, around_exclude=0, around_outside=0))
    spacing = optimize_run.resolution_limits(session.document)["steps"][1]["element_size_mm"]
    plain = optimize_run.run(session.document, optimize_run.OptimizeOptions(element_size_mm=spacing))
    assert plain["min_width"]["source"] == "default"
    assert plain["min_width_keep_without_room"] == dict(cells=0, around_exclude=0, around_outside=0)
    assert "保持ソリッドのセル" not in plain["warning"]
    assert ("min-width-keep-without-room", dict(cells=0, around_exclude=0, around_outside=0)) in snapshots

    seen: dict = {}

    def spy(design_space, keep, m, obstacle=None):
        seen["args"] = (m, keep.shape == design_space.shape, obstacle is not None)
        return dict(cells=4, around_exclude=9, around_outside=0)

    monkeypatch.setattr(binary_search, "keep_cells_without_room", spy)
    limited = optimize_run.run(
        session.document, optimize_run.OptimizeOptions(element_size_mm=spacing, min_width_mm=2.5 * spacing))
    assert seen["args"] == (3, True, True)
    assert limited["min_width_keep_without_room"] == dict(cells=4, around_exclude=9, around_outside=0)
    assert "保持ソリッドのセルが 4 個" in limited["warning"] and "除外ソリッドに挟まれて" in limited["warning"]
    assert ("min-width-keep-without-room", dict(cells=4, around_exclude=9, around_outside=0)) in snapshots


def test_the_keep_cells_without_room_warning_rides_on_every_early_return_too(
    session: Session, monkeypatch
) -> None:
    """探索より前に返る3経路(条件面が空・保持/除外が表現できない・確定表が不成立)でも、警告が落ちない。"""
    from duocad.topopt import anchor, binary_search

    four_conditions(session)
    monkeypatch.setattr(binary_search, "keep_cells_without_room",
                        lambda *args, **kwargs: dict(cells=4, around_exclude=9, around_outside=0))
    spacing = optimize_run.resolution_limits(session.document)["steps"][1]["element_size_mm"]
    options = optimize_run.OptimizeOptions(element_size_mm=spacing, min_width_mm=2.5 * spacing)

    # `monkeypatch.undo()` は conftest の実行記録の置き場の差し替えまで戻すので、段ごとの差し替えは context で閉じる。
    with monkeypatch.context() as patch:
        patch.setattr(optimize_run, "_empty_condition_faces", lambda space: [dict(face=0)])
        patch.setattr(anchor, "format_condition_face_warning", lambda faces: "FACES-EMPTY")
        result = optimize_run.run(session.document, options)
        assert result["state"] == "empty_condition_faces" and result["warning"].startswith("FACES-EMPTY。")
        assert "保持ソリッドのセルが 4 個" in result["warning"]

        patch.setattr(optimize_run, "_empty_condition_faces", lambda space: [])
        patch.setattr(optimize_run, "_region_representation", lambda doc, bundle, space: [
            dict(elements=0, role_label="保持", name="x", element_size_mm=spacing)])
        result = optimize_run.run(session.document, options)
        assert result["state"] == "unrepresented_regions" and "保持ソリッドのセルが 4 個" in result["warning"]

    monkeypatch.setattr(optimize_run, "_map_boundary_conditions", lambda space, bundle: (None, {"status": "fail"}))
    monkeypatch.setattr(anchor, "format_mapping_warning", lambda report: "MAPPING-FAILED")
    result = optimize_run.run(session.document, options)
    assert result["state"] == "boundary_mapping_failed" and result["warning"].startswith("MAPPING-FAILED。")
    assert "保持ソリッドのセルが 4 個" in result["warning"]


# --------------------------------------------------------------------------------------
# 10. 探索に渡す目標 = 実際の目標 × 係数(docs/TOPOPT_TARGET_FACTOR.md)
# --------------------------------------------------------------------------------------
def test_the_search_target_is_the_real_target_times_2_4_unless_the_factor_is_pinned_to_1(session: Session) -> None:
    """落ちたら、探索と6条件の `safety` が、利用者の目標をそのまま(または別の係数で)使っている。"""
    from duocad.topopt import design_space as ds

    four_conditions(session)
    bundle = optimize_run.build_conditions(session.document)
    space = ds.derive(bundle.conditions, spacing=8.0)

    def built(**options):
        return optimize_run._build_search_conditions(
            session.document, space, bundle, optimize_run.OptimizeOptions(**options)).target_safety_factor

    assert built() == pytest.approx(4.8)
    assert built(target_safety_factor=3.0) == pytest.approx(7.2)
    assert built(search_target_factor=1.0) == 2.0
    assert built(target_safety_factor=3.0, search_target_factor=1.0) == 3.0


@pytest.mark.parametrize("factor", [0.999, 0.0, -2.4, math.nan, math.inf, -math.inf])
def test_a_search_target_factor_below_1_or_not_finite_is_refused_at_the_entrance(session: Session, factor) -> None:
    """落ちたら、1.0 未満・非有限の係数が探索に入るまで通る(探索を数十分回した後で気づく)。"""
    from duocad.topopt import design_space as ds

    four_conditions(session)
    options = optimize_run.OptimizeOptions(search_target_factor=factor)
    with pytest.raises(optimize_run.OptimizeInputError, match="search_target_factor"):
        optimize_run.run(session.document, options)
    bundle = optimize_run.build_conditions(session.document)
    space = ds.derive(bundle.conditions, spacing=8.0)
    with pytest.raises(optimize_run.OptimizeInputError, match="search_target_factor"):
        optimize_run._build_search_conditions(session.document, space, bundle, options)


def test_a_search_target_that_overflows_is_refused_at_the_entrance(session: Session) -> None:
    """落ちたら、有限の係数でも 目標 × 係数 が inf になる入力が探索まで通る。"""
    four_conditions(session)
    with pytest.raises(optimize_run.OptimizeInputError, match="有限の数にならない"):
        optimize_run.run(session.document, optimize_run.OptimizeOptions(search_target_factor=1e308))


def test_the_result_reports_the_real_target_the_search_target_and_the_factor(session: Session, monkeypatch) -> None:
    """落ちたら、結果の `target_safety_factor` が探索時の目標に化ける(利用者の目標が画面と報告から消える)、
    または探索時の目標と係数が報告に出ない。探索が受け取る目標は 実際の目標 × 係数。"""
    from duocad.topopt import binary_search, trace

    four_conditions(session)
    seen: list = []
    resolved: list = []
    real_snapshot = trace.snapshot
    monkeypatch.setattr(trace, "snapshot", lambda name, **values: (
        resolved.append(values) if name == "resolved-conditions" else None, real_snapshot(name, **values))[1])

    def fake_find(design_space_mask, conditions, **kwargs):
        seen.append(conditions.target_safety_factor)
        return binary_search.SearchResult(
            status="no_feasible_found", termination="evaluation_limit", best_feasible=None,
            history=[], evaluations=1, conditions_identifier="test")

    monkeypatch.setattr(binary_search, "find_best_feasible", fake_find)
    events: list = []
    result = optimize_run.run(
        session.document, optimize_run.OptimizeOptions(element_size_mm=8.0), progress=events.append)
    assert result["target_safety_factor"] == 2.0
    assert result["search_target_safety_factor"] == pytest.approx(4.8) and result["search_target_factor"] == 2.4
    assert seen == [result["search_target_safety_factor"]]
    assert (resolved[0]["target_safety_factor"], resolved[0]["search_target_factor"]) == (2.0, 2.4)
    assert any("目標安全率 2(探索の格子では 4.8 = 2 × 2.4 を満たす形を探す)" in event.get("message", "")
               for event in events)

    pinned = optimize_run.run(
        session.document, optimize_run.OptimizeOptions(element_size_mm=8.0, search_target_factor=1.0))
    assert (pinned["target_safety_factor"], pinned["search_target_safety_factor"], pinned["search_target_factor"]
            ) == (2.0, 2.0, 1.0)
    assert seen[-1] == 2.0


def test_a_full_material_shortfall_names_the_search_target_and_the_factor_not_a_bare_target() -> None:
    """落ちたら、全材料が探索時の目標に届かない失敗が「目標 4.8」と出て、利用者が入れた 2 と食い違って見える。
    係数 1 の文面は変えない。"""
    failed = _full_material_failure("full_material_infeasible", "fail", dict(
        safety=dict(status="fail", safety_factor=3.1, target_safety_factor=4.8)))
    warning = optimize_run._search_failure_warning(failed, None, 2.4)
    assert "安全率 3.1 が探索の格子で必要な安全率 4.8(= 目標 2 × 係数 2.4)に届かない" in warning
    assert "が目標 4.8 に" not in warning
    assert "安全率 3.1 が目標 4.8 に届かない" in optimize_run._search_failure_warning(failed)


# --------------------------------------------------------------------------------------
# 9. 探索後の 1×1 の空隙埋め(thin_voids)と6条件の取り直し
# --------------------------------------------------------------------------------------
def _replayed_search(monkeypatch, safety_factor: float = 9.9) -> None:
    # 探索を差し替え、最小幅を満たして保持セルを残した確定マスクを返す(評価3回・収束)。
    import numpy as np
    from duocad.topopt import binary_search, min_width

    def fake_find(design_space_mask, conditions, **kwargs):
        mask = np.asarray(design_space_mask, dtype=bool)
        mask = min_width.cube_opening(mask, kwargs.get("min_width_cells", 1))
        if conditions.keep_cells is not None:
            mask |= conditions.keep_cells
        report = dict(material_sha256="f" * 64,
                      volumes=dict(material_mm3=float(mask.sum()) * conditions.grid.element_volume),
                      checks=dict(safety=dict(safety_factor=safety_factor)))
        best = binary_search.FeasibleModel(mask=mask, before_mask=mask, report=report, trial_index=0)
        return binary_search.SearchResult(status="feasible", termination="converged", best_feasible=best,
                                          history=[], evaluations=3, conditions_identifier="test")

    monkeypatch.setattr(binary_search, "find_best_feasible", fake_find)


def test_tube_fill_skips_reinspection_when_nothing_to_fill(session: Session, monkeypatch) -> None:
    # 設計空間いっぱいの確定マスクには管が無い。検査は増えず、探索時の安全率がそのまま結果になる。
    import numpy as np
    from duocad.topopt import acceptance, thin_voids

    four_conditions(session)
    _replayed_search(monkeypatch)
    seen: list = []
    original = thin_voids.fill_tubes

    def spy(mask, allowed):
        seen.append((np.array(mask, copy=True), np.array(allowed, copy=True)))
        return original(mask, allowed)

    def never(*args, **kwargs):
        raise AssertionError("埋めるセルが無いのに acceptance.inspect が呼ばれた")

    monkeypatch.setattr(thin_voids, "fill_tubes", spy)
    monkeypatch.setattr(acceptance, "inspect", never)
    result = optimize_run.run(session.document, optimize_run.OptimizeOptions(element_size_mm=8.0))
    assert result["state"] == "done", result["warning"]
    assert result["filled_cells"] == 0
    assert result["search_safety_factor"] == 9.9 and result["safety_factor"] == 9.9
    assert len(seen) == 1 and seen[0][0].shape == seen[0][1].shape and seen[0][0].dtype == bool


def test_tube_fill_reinspects_and_reports_both_safety_factors(session: Session, monkeypatch) -> None:
    # 埋めたセルがあれば6条件を取り直す。結果の安全率は取り直した値で、探索時の値は別欄に残る。
    import numpy as np
    from duocad.topopt import acceptance, thin_voids

    four_conditions(session)
    _replayed_search(monkeypatch)
    monkeypatch.setattr(thin_voids, "fill_tubes",
                        lambda mask, allowed: (np.array(mask, copy=True), dict(cells=3, rounds=[2, 1])))
    calls: list = []
    original = acceptance.inspect

    def spy(material, before, grid, **kwargs):
        calls.append(np.array(material, copy=True))
        return original(material, before, grid, **kwargs)

    monkeypatch.setattr(acceptance, "inspect", spy)
    result = optimize_run.run(session.document, optimize_run.OptimizeOptions(element_size_mm=8.0))
    assert result["state"] == "done", result["warning"]
    assert len(calls) == 1 and calls[0].dtype == bool
    assert result["filled_cells"] == 3 and result["search_safety_factor"] == 9.9
    assert result["safety_factor"] != 9.9 and np.isfinite(result["safety_factor"])
    assert result["safety_factor"] >= result["search_target_safety_factor"]
    assert result["material_mm3"] == float(calls[0].sum()) * result["grid"]["spacing"] ** 3


def test_tube_fill_failure_is_reported_without_touching_the_document(session: Session, monkeypatch) -> None:
    # 埋めた形が6条件に落ちたら `fill_infeasible`。文書は1ビットも変わらない。
    import numpy as np
    from duocad.topopt import acceptance, thin_voids

    four_conditions(session)
    before = [body.id for body in session.document.bodies]
    _replayed_search(monkeypatch)
    monkeypatch.setattr(thin_voids, "fill_tubes",
                        lambda mask, allowed: (np.array(mask, copy=True), dict(cells=1, rounds=[1])))

    def failing(material, before_mask, grid, **kwargs):
        passing = dict(status="pass")
        return dict(status="fail", volumes=dict(material_mm3=1.0),
                    checks=dict(watertight=passing, single_solid=passing, preserve=passing, exclude=passing,
                                conditions=passing, safety=dict(status="fail", safety_factor=1.5)))

    monkeypatch.setattr(acceptance, "inspect", failing)
    result = optimize_run.run(session.document, optimize_run.OptimizeOptions(element_size_mm=8.0))
    assert result["state"] == "fill_infeasible" and result["body_id"] is None
    assert result["safety_factor"] is None and result["material_mm3"] is None
    assert "1 セル" in result["warning"] and "safety" in result["warning"] and "1.5" in result["warning"]
    assert [body.id for body in session.document.bodies] == before


def test_tube_fill_reports_uncovered_filled_cells_as_a_warning_not_a_failure(session: Session, monkeypatch) -> None:
    # 埋めたセルが最小幅の m 立方に覆われなくても止めない(保持セルと同じ対象外)。数を warning に出し、結果は done。
    # 幾何(埋めたセルが覆われない配置)は tests/test_thin_voids.py が持つので、ここは配線だけを見る: 被覆の判定を差し替え、
    # 埋めたセルが1つも覆われない状況を作る。
    import numpy as np
    from duocad.topopt import acceptance, min_width, thin_voids

    four_conditions(session)
    _replayed_search(monkeypatch)
    added: dict = {}
    real_opening = min_width.cube_opening

    def opening_that_never_covers_the_added_cell(material, m):
        # 差し替えた探索(`_replayed_search`)も opening を呼ぶので、本物を返したうえで、埋めたセルだけを「覆われない」にする
        covered = real_opening(material, m)
        if "index" in added:
            covered[added["index"]] = False
        return covered

    monkeypatch.setattr(min_width, "cube_opening", opening_that_never_covers_the_added_cell)
    monkeypatch.setattr(acceptance, "inspect", lambda *args, **kwargs: dict(
        status="pass", volumes=dict(material_mm3=1.0), checks=dict(safety=dict(status="pass", safety_factor=9.9))))

    monkeypatch.setattr(thin_voids, "fill_tubes", lambda mask, allowed: (np.array(mask, copy=True), dict(cells=1, rounds=[1])))
    result = optimize_run.run(session.document, optimize_run.OptimizeOptions(element_size_mm=8.0))
    assert result["state"] == "done" and result["body_id"] is not None and result["filled_cells"] == 1
    # 形が変わっていなければ「埋めた後 & 探索前に無い」セルは 0 個で、warning は出ない
    assert "覆われない" not in (result["warning"] or "")

    def fake_fill_protruding(mask, allowed):
        # 材料に6近傍で接する空セルを1つ足す(つながったまま1セル張り出す)
        from scipy import ndimage
        filled = np.array(mask, copy=True)
        added["index"] = tuple(np.argwhere(ndimage.binary_dilation(mask) & ~mask)[0])
        filled[added["index"]] = True
        return filled, dict(cells=1, rounds=[1])

    monkeypatch.setattr(thin_voids, "fill_tubes", fake_fill_protruding)
    result = optimize_run.run(session.document, optimize_run.OptimizeOptions(element_size_mm=8.0))
    assert result["state"] == "done" and result["filled_cells"] == 1, result["warning"]
    assert "1 セルのうち 1 セルは最小幅" in result["warning"] and "覆われない" in result["warning"]


def test_the_finished_shape_is_solved_again_and_its_safety_factor_and_neck_diagnosis_are_returned(session: Session, monkeypatch) -> None:
    # 落ちたら、完成形(平滑化・集合演算の後)を解き直さずに探索時の安全率だけを返している(#117)。
    four_conditions(session)
    _replayed_search(monkeypatch)
    result = optimize_run.run(session.document, optimize_run.OptimizeOptions(element_size_mm=8.0))
    assert result["state"] == "done", result["warning"]
    checked = result["final_check"]
    assert result["final_safety_factor"] == checked["safety_factor"] and checked["safety_factor"] > 0
    safety = checked["safety"]
    assert safety["rule"] == "member_surface_peak" and safety["refine"] == 3 and safety["spacing_mm"] == pytest.approx(result["grid"]["spacing"] / 3)
    assert checked["safety_factor"] == pytest.approx(safety["allowable_mpa"] / safety["peak_mpa"])
    assert checked["neck"]["status"] in ("suspect", "undetermined", "not_observed")
    json.dumps(result)  # 結果メッセージにそのまま載る


def test_a_finished_shape_whose_rebuilt_conditions_do_not_cover_the_faces_is_not_measured(session: Session, monkeypatch) -> None:
    # 落ちたら、細かい格子で作り直した確定表が条件面を覆っていないのに、残った節点へ配り直した荷重で強度を測っている。
    from duocad.topopt import anchor

    four_conditions(session)
    _replayed_search(monkeypatch)
    real = anchor.condition_mapping
    calls = []

    def failing_on_the_fine_grid(table, *args, **kwargs):
        report = real(table, *args, **kwargs)
        calls.append(table.grid.spacing)
        if table.grid.spacing < calls[0] - 1e-9:  # 探索の格子より細かい表 = 完成形検査が作り直したもの
            report = dict(report, status="fail")
        return report

    monkeypatch.setattr(anchor, "condition_mapping", failing_on_the_fine_grid)
    result = optimize_run.run(session.document, optimize_run.OptimizeOptions(element_size_mm=8.0))
    assert result["state"] == "done" and result["final_safety_factor"] is None
    assert result["final_check"]["safety"]["reason"] == "boundary_mapping_failed"
    assert "完成形の安全率を測れなかった" in result["warning"]


def test_a_finished_shape_on_a_grid_too_large_to_refine_is_not_measured(session: Session, monkeypatch) -> None:
    # 落ちたら、分割しなくても要素数の上限を超える題材で、巨大な配列を確保しにいく(測らずに警告を返すべき)。
    from duocad.topopt import final_check

    four_conditions(session)
    _replayed_search(monkeypatch)
    monkeypatch.setattr(final_check, "MAX_FINE_ELEMENTS", 1)
    result = optimize_run.run(session.document, optimize_run.OptimizeOptions(element_size_mm=8.0))
    assert result["state"] == "done" and result["final_safety_factor"] is None
    assert result["final_check"]["safety"]["reason"] == "grid_too_large"
    assert result["final_check"]["conditions"]["status"] == "unknown"  # 呼び出し側が conditions を読めること
    assert "完成形の安全率を測れなかった" in result["warning"]


def test_a_finished_shape_below_the_real_target_is_added_with_a_warning_until_reinforcement_exists(session: Session, monkeypatch) -> None:
    # 落ちたら、肉盛りが入るまでの間(2026-10-09 本人指示)に、完成形が目標を割った結果を足さないか、警告なしで足している。
    from duocad.topopt import final_check

    four_conditions(session)
    _replayed_search(monkeypatch)
    monkeypatch.setattr(final_check, "inspect", lambda *a, **k: dict(
        safety_factor=1.2, safety=dict(status="fail", safety_factor=1.2, peak_mpa=33.3, peak_position_mm=[1, 2, 3],
                                       spacing_mm=1.0), conditions=dict(status="pass"),
        material_cells=1, neck=dict(status="not_observed", events=[], required_mm=16.0)))
    result = optimize_run.run(session.document, optimize_run.OptimizeOptions(element_size_mm=8.0))
    assert result["state"] == "done" and result["body_id"] is not None
    assert result["final_safety_factor"] == 1.2 and "1.2" in result["warning"] and "目標" in result["warning"]


def test_a_finished_shape_below_the_real_target_is_a_failure_without_touching_the_document(session: Session, monkeypatch) -> None:
    # 落ちたら、`FINAL_UNSAFE_BLOCKS` のとき完成形が本来の目標安全率を割っても結果を足している(2026-10-07 本人決定)。
    from duocad.topopt import final_check

    monkeypatch.setattr(optimize_run, "FINAL_UNSAFE_BLOCKS", True)
    four_conditions(session)
    before = [body.id for body in session.document.bodies]
    _replayed_search(monkeypatch)
    monkeypatch.setattr(final_check, "inspect", lambda *a, **k: dict(
        safety_factor=1.2, safety=dict(status="fail", safety_factor=1.2), conditions=dict(status="pass"),
        material_cells=1, neck=dict(status="not_observed", events=[], required_mm=16.0)))
    result = optimize_run.run(session.document, optimize_run.OptimizeOptions(element_size_mm=8.0))
    assert result["state"] == "final_unsafe" and result["body_id"] is None
    assert result["final_safety_factor"] == 1.2 and "1.2" in result["warning"] and "目標" in result["warning"]
    assert [body.id for body in session.document.bodies] == before


def test_a_suspected_thin_joint_is_a_warning_not_a_silent_success(session: Session, monkeypatch) -> None:
    # 落ちたら、つなぎ目の幅不足の疑いを成功として黙って返している(#117 の完了条件)。
    from duocad.topopt import final_check

    four_conditions(session)
    _replayed_search(monkeypatch)
    event = dict(kind="loop_neck", diameter_mm=[0.0, 6.9], lifetime_mm=None, verdict="suspect", reason=None,
                 position_mm=[1.0, 2.0, 3.0])
    monkeypatch.setattr(final_check, "inspect", lambda *a, **k: dict(
        safety_factor=5.0, safety=dict(status="pass", safety_factor=5.0), conditions=dict(status="pass"),
        material_cells=1, neck=dict(status="suspect", events=[event], required_mm=16.0)))
    result = optimize_run.run(session.document, optimize_run.OptimizeOptions(element_size_mm=8.0))
    assert result["state"] == "done" and "つなぎ目: 幅不足の疑い 1 か所" in result["warning"]
