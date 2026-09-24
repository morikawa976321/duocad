"""固定凸包による塊(設計空間)の導出(`duocad/topopt/design_space.py`)の単体検査。

合成の箱だけで走る(私物のSTEPや実行フォルダに依存しない)。SIMP は回さない。
実物(hanger)での再現は `topopt_checks/check_design_space.py --run <実行フォルダ>`。

1. **幾何** —— 1つの箱なら塊は「箱から距離 m 以内」(角の丸い箱) / 離れた2部品では凸包が
   両者を橋渡しし、外枠より小さい / 平面オフセットの多面体より角が丸いぶん小さい /
   平行移動で塊が一緒に動く / 余白は最長辺の比率。
2. **入力** —— `bodies` は必須で、保持ボディが要る / 退化した点群は摂動せずに落とす /
   外接箱は事前の三角形分割に依存しない。
3. **条件の保持と接続** —— 全条件を個別に見る(一部だけつながる配置は落ちる)/ 条件を含まない
   成分だけを落とす / 角1点の接触はつながっていない / 保持と除外の衝突は名前付きで落ちる。
4. **固定性と記録** —— 塊は読み取り専用でハッシュが安定 / 記録には `derive` の段が1回だけ出る。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from duocad.topopt import anchor as an
from duocad.topopt import design_space as ds
from duocad.topopt import voxelize as vx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'topopt_checks'))
import _fixtures  # noqa: E402  合成題材(build123d を直接使うので duocad/ の外にある)

LONG_AXIS = 16  # 合成の箱は 20mm 級なので、余白込みで要素サイズ 2〜3mm になる


def _box(size, at):
    from build123d import Box, Pos

    return Pos(*at) * Box(*size)


def _face(shape, normal, point) -> int:
    soup = vx.triangles_from_shape(vx._unwrap(shape), deviation=0.2)
    hits = an.faces_matching(soup, kind="plane", normal=normal, near_point=point, near_tol=1e-4)
    assert len(hits) == 1, hits
    return hits[0]


def _one_box_conditions(at=(0.0, 0.0, 0.0), size=(20.0, 10.0, 10.0)):
    """1つの箱。拘束面は -x、荷重面は +x(どちらも同じ保持ボディの面)。"""
    keep = _box(size, at)
    fixed = _face(keep, (-1, 0, 0), (at[0] - size[0] / 2, at[1], at[2]))
    load = _face(keep, (1, 0, 0), (at[0] + size[0] / 2, at[1], at[2]))
    return ds.Conditions(
        keep=keep,
        fixed_faces=fixed,
        load_faces=load,
        bodies=(ds.condition_body("pad", keep, roles=("keep", "fixed", "load")),),
    )


def _two_pads_conditions(offset=(40.0, 30.0, 0.0), obstacle=None):
    """離れた2つのパッド。固定側の -x 面が拘束、荷重側の +z 面が荷重。"""
    left = _box((10.0, 10.0, 10.0), (0.0, 0.0, 0.0))
    right = _box((10.0, 10.0, 10.0), offset)
    keep = left + right
    fixed = _face(keep, (-1, 0, 0), (-5.0, 0.0, 0.0))
    load = _face(keep, (0, 0, 1), (offset[0], offset[1], offset[2] + 5.0))
    return ds.Conditions(
        keep=keep,
        fixed_faces=fixed,
        load_faces=load,
        obstacle=obstacle,
        bodies=(
            ds.condition_body("left", left, roles=("keep", "fixed")),
            ds.condition_body("right", right, roles=("keep", "load")),
        ),
    )


# --------------------------------------------------------------------------------------
# 1. 幾何
# --------------------------------------------------------------------------------------
def test_a_single_box_gives_the_box_within_the_margin_distance() -> None:
    """箱の凸包は箱そのもの。塊は「箱から距離 m 以内」の点(角・稜が丸い箱)で、
    箱を各方向に m 広げた直方体(平面オフセット)より角のぶん小さい。"""
    space = ds.derive(_one_box_conditions(), elements_long_axis=LONG_AXIS)
    assert space.margin == pytest.approx(ds.DEFAULT_MARGIN_RATIO * 20.0)
    lo = np.asarray(space.bbox_min)
    hi = np.asarray(space.bbox_max)
    centers = space.grid.centers_of(np.argwhere(np.ones(space.grid.shape, dtype=bool)))
    clamped = np.clip(centers, lo, hi)
    distance = np.linalg.norm(centers - clamped, axis=1)
    expected = (distance <= space.margin + 1e-9).reshape(space.grid.shape)
    assert np.array_equal(space.mask, expected)
    box_offset = ds.box_mask(space.grid, lo - space.margin, hi + space.margin)
    assert space.stats["cells"]["offset_polytope"] == int(box_offset.sum())
    assert space.stats["cells"]["hull"] < space.stats["cells"]["offset_polytope"]
    assert np.all(space.mask <= box_offset)  # 距離 m 以内 ⊂ 平面オフセット
    assert space.stats["derivation"] == ds.DERIVATION
    assert space.stats["inflation"] == "minkowski-ball"
    assert space.stats["hull"]["facet_count"] >= 6


def test_two_separated_bodies_are_bridged_by_the_hull_but_the_frame_corners_are_cut() -> None:
    """斜めに離れた2部品では、凸包は両者を含む斜めの領域になり、外枠の角は塊に入らない。"""
    space = ds.derive(_two_pads_conditions(), elements_long_axis=LONG_AXIS)
    cells = space.stats["cells"]
    assert cells["hull"] < cells["frame"]
    assert cells["connected"] == space.element_count
    # 塊の全セル中心は凸包から距離 m 以内にある(定義の再計算)。平面オフセットは必要条件。
    idx = np.argwhere(space.mask)
    centers = space.grid.centers_of(idx)
    planes = space.hull_planes
    signed = centers @ planes[:, :3].T + planes[:, 3]
    assert np.all(signed <= space.margin + 1e-9)
    assert np.all(ds.distance_to_hull(centers, planes, space.hull_triangles) <= space.margin + 1e-9)
    # 塊の外で平面オフセットには入るセルは、凸包から距離 m より遠い(丸めた角・稜)。
    outside = np.argwhere(~space.mask & ds.offset_hull_mask(space.grid, planes, space.margin))
    assert outside.shape[0] > 0
    far = ds.distance_to_hull(space.grid.centers_of(outside), planes, space.hull_triangles)
    assert np.all(far > space.margin)
    # 外枠の角(x 最大・y 最小)は両部品から遠く、凸包から距離 m 以内に入らない。
    corner = space.grid.centers_of(np.array([[space.grid.shape[0] - 2, 1, space.grid.shape[2] // 2]]))[0]
    assert np.max(corner @ planes[:, :3].T + planes[:, 3]) > space.margin
    # 保持ボディの箱は丸ごと塊に入っている(凸包が箱の頂点から起きている)。
    for body in space.conditions.bodies:
        inside = ds.box_mask(space.grid, body.bbox_min, body.bbox_max)
        assert np.all(space.mask[inside])


def test_translation_moves_the_space_with_the_bodies() -> None:
    """条件を平行移動すると、格子の原点だけが動いて塊のセルは同じになる(要素サイズ固定)。"""
    shift = (7.5, -3.25, 11.0)
    base = ds.derive(_two_pads_conditions(), spacing=2.5)
    moved = ds.derive(
        _two_pads_conditions_shifted(shift), spacing=2.5
    )
    assert moved.grid.shape == base.grid.shape
    assert np.allclose(np.asarray(moved.grid.origin) - np.asarray(base.grid.origin), shift)
    assert np.array_equal(moved.mask, base.mask)


def _two_pads_conditions_shifted(shift):
    left = _box((10.0, 10.0, 10.0), shift)
    right = _box((10.0, 10.0, 10.0), (40.0 + shift[0], 30.0 + shift[1], shift[2]))
    keep = left + right
    fixed = _face(keep, (-1, 0, 0), (shift[0] - 5.0, shift[1], shift[2]))
    load = _face(keep, (0, 0, 1), (40.0 + shift[0], 30.0 + shift[1], shift[2] + 5.0))
    return ds.Conditions(
        keep=keep,
        fixed_faces=fixed,
        load_faces=load,
        bodies=(
            ds.condition_body("left", left, roles=("keep", "fixed")),
            ds.condition_body("right", right, roles=("keep", "load")),
        ),
    )


def test_the_margin_is_a_ratio_of_the_longest_edge_of_the_point_cloud() -> None:
    cond = _two_pads_conditions(offset=(60.0, 0.0, 0.0))
    space = ds.derive(cond, elements_long_axis=LONG_AXIS, margin_ratio=0.1)
    assert space.bbox_max[0] - space.bbox_min[0] == pytest.approx(70.0)
    assert space.margin == pytest.approx(7.0)
    assert space.stats["design_bbox"][0][0] == pytest.approx(-5.0 - 7.0)


# --------------------------------------------------------------------------------------
# 2. 入力
# --------------------------------------------------------------------------------------
def test_conditions_require_the_condition_bodies() -> None:
    keep = _box((10.0, 10.0, 10.0), (0.0, 0.0, 0.0))
    fixed = _face(keep, (-1, 0, 0), (-5.0, 0.0, 0.0))
    load = _face(keep, (1, 0, 0), (5.0, 0.0, 0.0))
    with pytest.raises(TypeError):
        ds.Conditions(keep=keep, fixed_faces=fixed, load_faces=load)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="bodies が空"):
        ds.Conditions(keep=keep, fixed_faces=fixed, load_faces=load, bodies=())
    only_anchor = ds.ConditionBody("plate", ("fixed",), (0, 0, 0), (1, 1, 1))
    with pytest.raises(ValueError, match="保持ボディ"):
        ds.Conditions(keep=keep, fixed_faces=fixed, load_faces=load, bodies=(only_anchor,))
    pad = ds.condition_body("pad", keep, roles=("keep",))
    with pytest.raises(ValueError, match="重複"):
        ds.Conditions(keep=keep, fixed_faces=fixed, load_faces=load, bodies=(pad, pad))
    with pytest.raises(ValueError, match="shape"):
        ds.ConditionBody("pad", ("keep",), (0, 0, 0), (1, 1, 1))
    with pytest.raises(ValueError, match="裏返"):
        ds.ConditionBody("pad", ("fixed",), (0, 0, 0), (-1, 1, 1))
    with pytest.raises(ValueError, match="非有限"):
        ds.ConditionBody("pad", ("fixed",), (0, 0, 0), (float("nan"), 1, 1))


def test_a_flat_point_cloud_is_rejected_instead_of_being_perturbed() -> None:
    """厚み0の点群では凸包が起こせない。ランダム摂動で通さず、階数を名指しして落とす。"""
    flat = ds.ConditionBody("sheet", ("fixed",), (0.0, 0.0, 0.0), (10.0, 10.0, 0.0))
    with pytest.raises(ds.DesignSpaceGeometryError, match="平面"):
        ds.convex_hull(ds.condition_corners([flat]))
    line = ds.ConditionBody("rod", ("fixed",), (0.0, 0.0, 0.0), (10.0, 0.0, 0.0))
    with pytest.raises(ds.DesignSpaceGeometryError, match="直線"):
        ds.convex_hull(ds.condition_corners([line]))
    # 有限の厚みがあれば平面状の部品でも通る(0.5mm の板)。
    plate = ds.ConditionBody("plate", ("fixed",), (0.0, 0.0, 0.0), (10.0, 10.0, 0.5))
    planes, triangles, info = ds.convex_hull(ds.condition_corners([plate]))
    assert planes.shape[1] == 4 and info["facet_count"] >= 6
    assert triangles.shape[1:] == (3, 3) and triangles.shape[0] == info["facet_count"]
    assert np.allclose(np.linalg.norm(planes[:, :3], axis=1), 1.0)


def test_the_closest_point_on_a_triangle_covers_all_seven_regions() -> None:
    """Ericson の領域分け(頂点3・辺3・面1)を1つの三角形で全部踏み、最近点を手計算と突き合わせる。"""
    a = np.array([0.0, 0.0, 0.0])
    b = np.array([4.0, 0.0, 0.0])
    c = np.array([0.0, 4.0, 0.0])
    points = np.array(
        [
            [-1.0, -1.0, 2.0],  # 頂点 A の領域
            [6.0, -1.0, -2.0],  # 頂点 B
            [-1.0, 6.0, 1.0],  # 頂点 C
            [2.0, -3.0, 1.0],  # 辺 AB の正面
            [-3.0, 2.0, -1.0],  # 辺 AC の正面
            [4.0, 4.0, 2.0],  # 辺 BC の正面(斜辺の外側)
            [1.0, 1.0, 5.0],  # 面の内側の真上
        ]
    )
    expected = np.array(
        [
            [0.0, 0.0, 0.0],
            [4.0, 0.0, 0.0],
            [0.0, 4.0, 0.0],
            [2.0, 0.0, 0.0],
            [0.0, 2.0, 0.0],
            [2.0, 2.0, 0.0],
            [1.0, 1.0, 0.0],
        ]
    )
    closest = ds._closest_points_on_triangle(points, a, b, c)
    assert np.allclose(closest, expected)
    # 三角形の上の点はその点自身が最近点(距離0)。
    on = np.array([[1.0, 2.0, 0.0], [0.0, 0.0, 0.0], [4.0, 0.0, 0.0], [2.0, 2.0, 0.0]])
    assert np.allclose(ds._closest_points_on_triangle(on, a, b, c), on)


def test_the_distance_to_the_hull_matches_closed_forms() -> None:
    """凸包(箱)からの距離: 内側 0 / 面の正面は平面距離 / 稜・頂点の外は稜・頂点までの距離。"""
    planes, triangles, _info = ds.convex_hull(
        ds.condition_corners([ds.ConditionBody("box", ("fixed",), (0.0, 0.0, 0.0), (10.0, 10.0, 10.0))])
    )
    points = np.array(
        [[5.0, 5.0, 5.0], [12.0, 5.0, 5.0], [12.0, 12.0, 5.0], [12.0, 12.0, 12.0], [-3.0, 5.0, 5.0], [5.0, 5.0, -4.0]]
    )
    expected = [0.0, 2.0, np.sqrt(8.0), np.sqrt(12.0), 3.0, 4.0]
    assert np.allclose(ds.distance_to_hull(points, planes, triangles), expected)


def test_the_bounding_box_does_not_depend_on_an_earlier_tessellation() -> None:
    """表示用の三角形分割が先に付いていても外接箱は変わらない(B-rep から取る)。"""
    from build123d import Cylinder

    from duocad import kernel

    shape = Cylinder(radius=10.0, height=30.0)
    before = ds.condition_body("cyl", shape, roles=("keep",))
    vx.triangles_from_shape(vx._unwrap(shape), deviation=2.0)  # 粗い三角形分割を付ける
    after = ds.condition_body("cyl", shape, roles=("keep",))
    assert after.bbox_min == before.bbox_min and after.bbox_max == before.bbox_max
    assert np.allclose(before.bbox_min, (-10.0, -10.0, -15.0), atol=1e-6)
    assert np.allclose(before.bbox_max, (10.0, 10.0, 15.0), atol=1e-6)
    # 製品(`optimize_run.build_conditions`)が使う `kernel.bounding_box` も同じ箱を返す。
    product = kernel.bounding_box(vx._unwrap(shape))
    assert np.allclose(product["min"], before.bbox_min, atol=1e-6)
    assert np.allclose(product["max"], before.bbox_max, atol=1e-6)


# --------------------------------------------------------------------------------------
# 3. 条件の保持と接続
# --------------------------------------------------------------------------------------
def _seed(shape, *cells) -> np.ndarray:
    mask = np.zeros(shape, dtype=bool)
    for cell in cells:
        mask[cell] = True
    return mask


def test_components_without_conditions_are_dropped_and_conditioned_ones_are_kept() -> None:
    shape = (12, 4, 4)
    design = np.zeros(shape, dtype=bool)
    design[0:3] = True  # 島A: 全条件
    design[5:7] = True  # 島B: 条件なし → 落ちる
    design[9:12] = True  # 島C: 条件なし → 落ちる
    seeds = {
        "fixed_face_1": _seed(shape, (0, 0, 0)),
        "load_face_2": _seed(shape, (2, 3, 3)),
        "keep_pad": _seed(shape, (1, 1, 1)),
    }
    final, report = ds.connect_seeds(design, seeds)
    assert report["components"] == 3
    assert report["dropped_components"] == 2
    assert report["dropped_elements"] == 2 * 16 + 3 * 16
    assert np.array_equal(final, design & (np.arange(12)[:, None, None] < 3))
    assert report["seeds"]["keep_pad"]["labels"] == [1]


def test_a_condition_left_on_another_island_is_a_failure_not_a_majority_vote() -> None:
    """条件が2成分に分かれたら、大きい方を選んで成功にしない。取り残された条件を名指しする。"""
    shape = (12, 4, 4)
    design = np.zeros(shape, dtype=bool)
    design[0:8] = True  # 大きい島: 拘束 + 荷重
    design[10:12] = True  # 小さい島: 保持だけ
    seeds = {
        "fixed_face_1": _seed(shape, (0, 0, 0)),
        "load_face_2": _seed(shape, (7, 3, 3)),
        "keep_pad": _seed(shape, (11, 1, 1)),
    }
    with pytest.raises(ds.DesignSpaceDisconnectedError) as caught:
        ds.connect_seeds(design, seeds)
    assert caught.value.components == 2
    assert caught.value.unreached == ["keep_pad"]
    assert caught.value.seeds["keep_pad"]["labels"] == [2]
    assert "keep_pad" in str(caught.value)


@pytest.mark.parametrize("split_names", [
    ("keep_pad",), ("fixed_face_1",), ("load_face_2",),
    ("keep_pad", "fixed_face_1", "load_face_2"),
])
def test_conditions_spanning_islands_fail_even_with_a_common_component(split_names) -> None:
    """全条件に共通する島があっても、条件の一部が別の島に残る場合は失敗する。"""
    shape = (12, 4, 4)
    design = np.zeros(shape, dtype=bool)
    design[:8] = True
    design[10:] = True
    seeds = {name: _seed(shape, (1, 1, 1)) for name in
             ("fixed_face_1", "load_face_2", "keep_pad")}
    for name in split_names:
        seeds[name][11, 1, 1] = True
    with pytest.raises(ds.DesignSpaceDisconnectedError) as caught:
        ds.connect_seeds(design, seeds)
    assert caught.value.components == 2
    assert set(caught.value.unreached) == set(split_names)
    for name in split_names:
        assert caught.value.seeds[name]["labels"] == [1, 2]
        assert name in str(caught.value)
    design[8:10] = True
    connected, _ = ds.connect_seeds(design, seeds)
    assert ds.element_components(connected)[1] == 1
    assert np.array_equal(connected, design)


def test_a_compound_keep_body_separated_by_an_obstacle_fails() -> None:
    """1ボディ内の保持片が除外壁の向こうに孤立したら、CADからの導出でも検出する。"""
    keep = _box((10, 10, 10), (-15, 0, 0)) + _box((10, 10, 10), (15, 0, 0))
    conditions = ds.Conditions(
        keep=keep,
        fixed_faces=_face(keep, (-1, 0, 0), (-20, 0, 0)),
        load_faces=_face(keep, (0, 0, 1), (-15, 0, 5)),
        obstacle=_box((4, 100, 100), (0, 0, 0)),
        bodies=(ds.condition_body("compound", keep, roles=("keep", "fixed", "load")),),
    )
    with pytest.raises(ds.DesignSpaceDisconnectedError) as caught:
        ds.derive(conditions, spacing=2)
    assert caught.value.components == 2
    assert caught.value.unreached == ["keep_compound"]
    assert caught.value.seeds["keep_compound"]["labels"] == [1, 2]


def test_a_condition_with_no_element_inside_the_space_is_named() -> None:
    shape = (6, 4, 4)
    design = np.ones(shape, dtype=bool)
    design[5] = False
    seeds = {
        "fixed_face_1": _seed(shape, (0, 0, 0)),
        "load_face_2": _seed(shape, (5, 3, 3)),  # 塊の外にしか無い
    }
    with pytest.raises(ds.DesignSpaceDisconnectedError) as caught:
        ds.connect_seeds(design, seeds)
    assert caught.value.unreached == ["load_face_2"]
    assert caught.value.seeds["load_face_2"]["elements"] == 0
    assert caught.value.seeds["load_face_2"]["elements_outside"] == 1


def test_corner_touching_islands_are_not_connected() -> None:
    """角1点だけで接する2島は 6近傍では別成分(26近傍なら1成分になることを同じ入力で見せる)。"""
    shape = (8, 8, 8)
    design = np.zeros(shape, dtype=bool)
    design[0:4, 0:4, 0:4] = True
    design[4:8, 4:8, 4:8] = True
    assert ds.element_components(design, neighbourhood=6)[1] == 2
    assert ds.element_components(design, neighbourhood=26)[1] == 1
    with pytest.raises(ds.DesignSpaceDisconnectedError):
        ds.connect_seeds(design, {"fixed_face_1": _seed(shape, (0, 0, 0)), "load_face_2": _seed(shape, (7, 7, 7))})


def test_an_obstacle_wall_between_the_conditions_fails_with_names() -> None:
    with pytest.raises(ds.DesignSpaceDisconnectedError) as caught:
        ds.derive(_fixtures.severed_conditions(0.0), elements_long_axis=LONG_AXIS)
    assert caught.value.components == 2
    assert caught.value.unreached  # 荷重側の面と保持が取り残される
    assert all(name.startswith(("load_face_", "keep_", "fixed_face_")) for name in caught.value.unreached)
    assert not isinstance(caught.value, ds.DesignSpaceTooSmallError)
    # 壁を外せば同じ条件が通る(落ちた原因が壁であることの裏取り)。
    ok = ds.derive(_fixtures.demo_conditions(0.0), elements_long_axis=LONG_AXIS)
    assert ok.stats["connectivity"]["components"] >= 1
    assert set(ok.stats["connectivity"]["seeds"]) == {
        f"fixed_face_{ok.fixed_faces[0]}",
        f"load_face_{ok.load_faces[0]}",
        "keep_fixed_pad",
        "keep_load_pad",
    }


def test_an_unrelated_obstacle_far_away_does_not_change_the_space() -> None:
    far = _box((20.0, 20.0, 20.0), (300.0, 0.0, 0.0))
    plain = ds.derive(_two_pads_conditions(), elements_long_axis=LONG_AXIS)
    with_far = ds.derive(_two_pads_conditions(obstacle=far), elements_long_axis=LONG_AXIS)
    assert np.array_equal(plain.mask, with_far.mask)
    assert with_far.stats["obstacle_elements_removed"] == 0


def test_an_obstacle_straddling_the_grid_edge_removes_only_its_inside_part() -> None:
    """格子に一部だけ重なる除外は、格子の中にある占有だけを引く(例外にならない)。"""
    plain = ds.derive(_two_pads_conditions(), elements_long_axis=LONG_AXIS)
    grid_lo, _grid_hi = plain.grid.world_box()
    # 格子の -x 端をまたぐ棒(半分は格子の外)。固定側パッド(x -5..5)の手前の塊を削り、パッドには触れない。
    straddling = _box((20.0, 6.0, 6.0), (float(grid_lo[0]), 0.0, 0.0))
    cut = ds.derive(_two_pads_conditions(obstacle=straddling), elements_long_axis=LONG_AXIS)
    assert np.array_equal(cut.grid.as_dict()["origin"], plain.grid.as_dict()["origin"])
    removed = cut.stats["obstacle_elements_removed"]
    assert removed > 0
    assert cut.element_count == plain.element_count - removed
    assert np.all(cut.mask <= plain.mask)  # 引くだけで、足していない
    assert cut.stats["obstacle_occupancy"].get("skipped") is None


def test_an_obstacle_that_eats_a_keep_body_is_a_named_conflict() -> None:
    eater = _box((14.0, 14.0, 14.0), (0.0, 0.0, 0.0))  # 固定側パッドを丸ごと覆う
    with pytest.raises(ds.DesignSpaceConflictError) as caught:
        ds.derive(_two_pads_conditions(obstacle=eater), elements_long_axis=LONG_AXIS)
    assert set(caught.value.bodies) == {"left"}
    assert caught.value.bodies["left"] > 0
    assert "入力矛盾" in str(caught.value)


def test_every_keep_body_and_every_condition_face_is_checked_individually() -> None:
    """種は保持ボディごと・条件面ごとに1つずつ(和集合ではない)。"""
    space = ds.derive(_two_pads_conditions(), elements_long_axis=LONG_AXIS)
    seeds = space.stats["connectivity"]["seeds"]
    assert set(seeds) == {
        f"fixed_face_{space.fixed_faces[0]}",
        f"load_face_{space.load_faces[0]}",
        "keep_left",
        "keep_right",
    }
    assert all(item["elements"] > 0 for item in seeds.values())
    assert [item["name"] for item in space.stats["bodies"]] == ["left", "right"]
    assert all(item["keep_elements"] > 0 for item in space.stats["bodies"])


# --------------------------------------------------------------------------------------
# 4. 固定性と記録
# --------------------------------------------------------------------------------------
def test_the_mask_is_read_only_and_the_hash_is_reproducible() -> None:
    first = ds.derive(_two_pads_conditions(), elements_long_axis=LONG_AXIS)
    second = ds.derive(_two_pads_conditions(), elements_long_axis=LONG_AXIS)
    assert first.mask_hash == second.mask_hash == first.stats["mask_hash"]
    assert not first.mask.flags.writeable
    assert not first.model.design.flags.writeable and not first.model.keep.flags.writeable
    with pytest.raises(ValueError):
        first.mask[0, 0, 0] = True
    flipped = first.mask.copy()
    flipped[0, 0, 0] = not flipped[0, 0, 0]
    assert ds.mask_digest(flipped) != first.mask_hash


def test_the_recording_has_one_derive_stage(tmp_path, monkeypatch) -> None:
    from duocad.topopt import trace

    monkeypatch.setattr(trace, "DEFAULT_ROOT", tmp_path)
    monkeypatch.setattr(trace.Recorder, "provenance", lambda self: None)
    with trace.recording() as rec:
        space = ds.derive(_two_pads_conditions(), elements_long_axis=LONG_AXIS)
    names = sorted(p.name.split("-", 1)[1] for p in rec.path.glob("*.json") if p.name[:6].isdigit())
    assert names.count("derive-input.json") == 1
    assert names.count("derive-output.json") == 1
    assert names.count("derive-masks.json") == 1
    recorded = trace.load_snapshot(next(rec.path.glob("*-derive-output.json")))
    assert np.array_equal(recorded["mask"], space.mask)
    assert recorded["stats"]["mask_hash"] == space.mask_hash
    assert recorded["stats"]["derivation"] == ds.DERIVATION
    masks = trace.load_snapshot(next(rec.path.glob("*-derive-masks.json")))
    assert np.array_equal(masks["connected"], space.mask)
    assert masks["hull"].sum() >= masks["minus_obstacle"].sum() >= masks["connected"].sum()
