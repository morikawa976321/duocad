"""保持・除外CADと材料の共通体積を前計算から引く経路(`acceptance.RegionOverlaps`)が、
候補ごとにブーリアンを取る直接ブーリアン(`intersection_volume`)と同じ答えを返すこと。"""
import numpy as np
import pytest
from build123d import Box, Cylinder, Pos, Rot

from duocad import kernel
from duocad.topopt import acceptance, voxelize
from duocad.topopt.acceptance import RegionOverlaps, intersection_volume, region_check


def grid_and_masks():
    grid = voxelize.Grid((0.3, -0.7, 1.1), 1.0, (12, 10, 10))
    rng = np.random.default_rng(3)
    slab = np.zeros(grid.shape, bool)
    slab[4:7] = True
    return grid, {'full': np.ones(grid.shape, bool), 'half': rng.random(grid.shape) < 0.5, 'slab': slab}


def solids():
    return {
        'l-shape': ((Pos(4.4, 4.2, 5.6) * Box(7.1, 2.3, 2.9)) + (Pos(2.4, 5.9, 5.6) * Box(2.6, 5.4, 2.9))).wrapped,
        'box': (Pos(6.37, 4.1, 5.8) * Box(5.3, 4.7, 5.2)).wrapped,
        'tilted-drilled-cylinder': (Pos(6.4, 4.2, 5.5) * Rot(30, 60, 0)
                                    * (Cylinder(3.3, 7.1) - Cylinder(1.0, 7.1))).wrapped,
    }


@pytest.mark.parametrize('name', ['box', 'l-shape', 'tilted-drilled-cylinder'])
def test_precomputed_overlap_matches_direct_boolean_for_every_mask(name):
    """落ちたら、前計算の重なり体積が直接ブーリアンとずれている(境界セルの部分体積の取りこぼし・二重計上)。
    許容差は `region_check` の既定(相対1e-9)。曲面の実CAD(hanger)では、ブーリアンの体積積分の
    ばらつきで相対 4e-8 までずれる(`docs/TOPOPT_SPEED.md`)ので、ここは平面と単純な曲面だけを使う。"""
    grid, masks = grid_and_masks()
    shape = solids()[name]
    overlaps = RegionOverlaps()
    for label, mask in masks.items():
        total, direct = intersection_volume(mask, grid, shape)
        total_pre, precomputed = overlaps.overlap_volume(mask, grid, shape)
        assert total_pre == total
        assert precomputed == pytest.approx(direct, rel=1e-9, abs=1e-9), label
    assert not overlaps.fallbacks


def test_subcell_region_and_contact_agree_with_direct_boolean():
    """セルより小さい保持と、材料に面で接するだけの保持(共通体積0)は、直接ブーリアンと同じ合否になる。
    `len(overlaps)` が落ちたら、`region_check` が渡された前計算を使っていない。"""
    grid = voxelize.Grid((10., 20., 30.), 2., (1, 1, 1))
    mask = np.ones(grid.shape, bool)
    bodies = [('tiny', (Pos(10.1, 20.1, 30.1) * Box(.1, .1, .1)).wrapped),
              ('contact', (Pos(12.5, 21, 31) * Box(1, 1, 1)).wrapped),
              ('partial', (Pos(12, 21, 31) * Box(1, 1, 1)).wrapped)]
    overlaps = RegionOverlaps()
    for preserve in (True, False):
        direct = region_check(mask, grid, bodies, preserve=preserve)
        pre = region_check(mask, grid, bodies, preserve=preserve, overlaps=overlaps)
        assert [b['status'] for b in pre['bodies']] == [b['status'] for b in direct['bodies']]
    assert [b['status'] for b in region_check(mask, grid, bodies, preserve=True, overlaps=overlaps)['bodies']] == [
        'pass', 'fail', 'pass']
    assert len(overlaps) == len(bodies)


def test_only_cells_touched_by_the_mask_are_computed_and_each_is_computed_once(monkeypatch):
    """材料の無い塊は計算しない(設計空間の外の巨大な除外ソリッドを丸ごと前計算しない)。同じ塊を2度計算しない。"""
    grid = voxelize.Grid((0., 0., 0.), 1., (20, 20, 20))
    shape = (Pos(10.3, 10.3, 10.3) * Box(18.4, 18.4, 18.4)).wrapped
    calls = []
    original = kernel.grid_cell_common_volumes

    def spy(*args):
        calls.append(args[1])
        return original(*args)

    monkeypatch.setattr(kernel, 'grid_cell_common_volumes', spy)
    overlaps = RegionOverlaps()
    corner = np.zeros(grid.shape, bool)
    corner[:3, :3, :3] = True
    overlaps.overlap_volume(corner, grid, shape)
    assert len(calls) == 1  # 材料のある塊(TILE_CELLS 角)だけ
    overlaps.overlap_volume(corner, grid, shape)
    assert len(calls) == 1
    entry = next(iter(overlaps._entries.values()))
    computed = ~np.isnan(entry.values)
    assert computed.sum() == acceptance.TILE_CELLS ** 3 and computed[:8, :8, :8].all()


def test_entries_are_keyed_by_solid_and_grid():
    """ソリッド・格子の原点・間隔・形のどれか1つが違えば別の項目で、古い値を読まない。"""
    grid = voxelize.Grid((0., 0., 0.), 1., (10, 10, 10))
    shape = (Pos(5.3, 5.1, 5.2) * Box(5.5, 5.5, 5.5)).wrapped
    moved = (Pos(5.9, 5.1, 5.2) * Box(5.5, 5.5, 5.5)).wrapped
    overlaps = RegionOverlaps()
    for g, s in ((grid, shape), (grid, shape), (grid, moved),
                 (voxelize.Grid((.5, 0., 0.), 1., (10, 10, 10)), shape),
                 (voxelize.Grid((0., 0., 0.), 2., (10, 10, 10)), shape),
                 (voxelize.Grid((0., 0., 0.), 1., (7, 10, 10)), shape)):  # x=7 でソリッドを切る
        mask = np.zeros(g.shape, bool)
        mask[:5] = True  # 全部の材料だと原点をずらしても共通体積が変わらない
        assert overlaps.overlap_volume(mask, g, s)[1] == pytest.approx(intersection_volume(mask, g, s)[1], rel=1e-9)
    assert len(overlaps) == 5 and not overlaps.fallbacks


def test_shape_key_distinguishes_location_and_orientation():
    shape = Box(2, 3, 4).wrapped
    assert kernel.shape_key(shape) == kernel.shape_key(shape)
    assert hash(kernel.shape_key(shape)) == hash(kernel.shape_key(shape))
    assert kernel.shape_key(shape) != kernel.shape_key((Pos(1, 0, 0) * Box(2, 3, 4)).wrapped)
    assert kernel.shape_key(shape) != kernel.shape_key(shape.Reversed())


def test_solids_the_precompute_cannot_handle_go_back_to_the_direct_boolean(monkeypatch):
    """前計算が失敗したソリッドは、そのソリッドだけ直接ブーリアンへ戻り、理由が `fallbacks` に残る。値は直接ブーリアンと同じ。"""
    grid, masks = grid_and_masks()
    shape = solids()['box']
    calls = []

    def broken(*args):
        calls.append(1)
        raise RuntimeError('boom')

    monkeypatch.setattr(kernel, 'grid_cell_common_volumes', broken)
    overlaps = RegionOverlaps()
    mask = masks['half']
    assert overlaps.overlap_volume(mask, grid, shape) == intersection_volume(mask, grid, shape)
    assert overlaps.overlap_volume(mask, grid, shape) == intersection_volume(mask, grid, shape)
    assert len(calls) == 1  # 2回目は前計算を試さない
    assert list(overlaps.fallbacks.values()) == ['RuntimeError: boom']


def test_solid_without_positive_volume_is_reported_the_same_way():
    """条件CADが不正なときの `unknown` 行(誤りの型と文言)も直接ブーリアンと同じ。"""
    grid, masks = grid_and_masks()
    face = Box(2, 2, 2).faces()[0].wrapped
    direct = region_check(masks['full'], grid, [('face', face)], preserve=True)
    pre = region_check(masks['full'], grid, [('face', face)], preserve=True, overlaps=RegionOverlaps())
    assert direct['status'] == pre['status'] == 'unknown'
    assert direct['bodies'] == pre['bodies']


def test_grid_cell_common_volumes_kinds_and_cell_volumes():
    shape = (Pos(5, 5, 5) * Box(10, 10, 10)).wrapped  # [0, 10]^3
    assert kernel.grid_cell_common_volumes(shape, [20., 20., 20.], 1.0, [4, 4, 4]) == ('empty', {})
    assert kernel.grid_cell_common_volumes(shape, [2., 2., 2.], 1.0, [4, 4, 4]) == ('full', {})
    kind, volumes = kernel.grid_cell_common_volumes(shape, [8.5, 1., 1.], 1.0, [4, 2, 2])
    assert kind == 'partial'
    assert set(volumes) == {(i, j, k) for i in range(2) for j in range(2) for k in range(2)}
    assert volumes[(0, 0, 0)] == pytest.approx(1.0) and volumes[(1, 0, 0)] == pytest.approx(0.5)
    assert sum(volumes.values()) == pytest.approx(2 * 2 * 1.5)


def test_a_piece_the_cutting_planes_did_not_split_is_rejected(monkeypatch):
    """破片は重心でセルへ振り分けるので、切れていない破片(複数セルにまたがる)は黙って別のセルへ入る。それを通さない。"""
    shape = (Pos(5, 5, 5) * Box(10, 10, 10)).wrapped
    real, calls = kernel._split_by_planes, []

    def split(argument, planes):
        calls.append(1)
        return real(argument, planes) if len(calls) == 1 else argument  # 2回目(セルの面で切る)を素通しにする

    monkeypatch.setattr(kernel, '_split_by_planes', split)
    with pytest.raises(RuntimeError, match='did not split'):
        kernel.grid_cell_common_volumes(shape, [8.5, 1., 1.], 1.0, [4, 2, 2])
