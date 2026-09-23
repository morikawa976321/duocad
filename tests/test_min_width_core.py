"""最小幅の規則 S(m)(侵食した芯の連結。`min_width.CoreConnectivity`)。R(m) は m 立方が斜めに共有1セルで重なる首を許すので、
その首を塞ぐ規則を `peel`(段階4)と段階3の全材料の検査が守ることを、実装とは独立な物差し(`core_groups_oracle`)で確かめる。"""
import dataclasses

import numpy as np
import pytest
from scipy import ndimage

from duocad.topopt import binary_search as bs
from duocad.topopt import min_width

from test_binary_search import _assert_min_width_invariants, _peel_box_until_stuck, core_groups_oracle


def _chain(step, n=4, m=2):
    """m 立方を `step` ずつずらして n 個つないだ形。"""
    mask = np.zeros((m * n + 2,) * 3, bool)
    for i in range(n):
        o = np.array(step) * i
        mask[o[0]:o[0] + m, o[1]:o[1] + m, o[2]:o[2] + m] = True
    return mask


def _random_rule_mask(rng, m, *, exempt=None, density=(0.7, 0.95)):
    """規則 R(m)(`exempt` を除く)と S(m) を満たす乱数のマスク。満たす形が取れなければ None。"""
    shape = exempt.shape if exempt is not None else tuple(int(x) for x in rng.integers(8, 13, size=3))
    mask = ndimage.binary_opening(rng.random(shape) < rng.uniform(*density), structure=np.ones((m,) * 3, bool))
    if not mask.any() or core_groups_oracle(mask, m, exempt) != 1:
        return None
    return mask


def _bite_verdicts(core, mask, cell, m, exempt=None):
    """`cell` と巻き添えを抜いた形について、窓の証明 `proven` と大域の真値を返す。どちらの経路の答えも真値と食い違わないことも確かめる。"""
    bite = np.vstack(([list(cell)], min_width.casualties(mask, cell, m, exempt)))
    after = mask.copy()
    after[tuple(bite.T)] = False
    truth = core_groups_oracle(after, m, exempt) <= 1
    lo, hi, ext = core.window(bite.min(0), bite.max(0) + 1, mask.shape)
    proven = core.proven(mask[ext], after[ext], lo, hi)
    assert not (proven and not truth)
    assert core.after_bite(after, bite, 0) == truth
    return proven, truth


@pytest.mark.parametrize('m', [2, 3])
def test_a_chain_of_cubes_sharing_one_cell_breaks_the_core_but_a_staircase_overlapping_by_faces_does_not(m):
    """落ちたら、S(m) が斜めの共有1セルの首を通している、または面で重なる階段を落としている。R(m) は前者も満たす。"""
    diagonal, staircase = _chain((1, 1, 1), m=m), _chain((1, 0, 0), m=m)
    core = min_width.CoreConnectivity(m)
    assert bs._mask_satisfies_min_width(diagonal, m) and bs._mask_satisfies_min_width(staircase, m)
    assert core.groups(diagonal) == core_groups_oracle(diagonal, m) == 4
    assert not core.connected(diagonal)
    assert core.groups(staircase) == core_groups_oracle(staircase, m) == 1 and core.connected(staircase)


def test_exempt_material_joins_the_cores_it_touches_and_a_mask_without_a_core_holds_vacuously():
    """落ちたら、`exempt`(保持ソリッド由来)を介したつながりが数えられていない。立方が重なるか6近傍で接する E が2つの芯を結ぶ。"""
    mask = np.zeros((10, 6, 6), bool)
    mask[1:3, 1:3, 1:3] = True
    mask[6:8, 1:3, 1:3] = True
    bridge = np.zeros_like(mask)
    bridge[3:6, 2, 2] = True
    far = np.zeros_like(mask)
    far[4:6, 5, 5] = True  # どちらの立方とも接しない
    assert min_width.CoreConnectivity(2).groups(mask) == 2
    assert min_width.CoreConnectivity(2, bridge).groups(mask) == 1
    assert min_width.CoreConnectivity(2, far).groups(mask) == 2
    assert min_width.CoreConnectivity(2).groups(np.zeros_like(mask)) == 0


def test_the_core_count_matches_the_independent_count_on_random_masks_with_exempt_cells():
    """落ちたら、芯の塊の数(大域)が独立な物差しとずれている。"""
    rng = np.random.default_rng(20260925)
    split = 0
    for _ in range(150):
        m = int(rng.integers(2, 4))
        shape = tuple(int(x) for x in rng.integers(7, 13, size=3))
        mask = rng.random(shape) < rng.uniform(0.6, 0.95)
        exempt = None
        if rng.random() < 0.6:
            exempt = np.zeros(shape, bool)
            for _ in range(int(rng.integers(1, 4))):
                c = [int(rng.integers(0, n)) for n in shape]
                exempt[c[0]:c[0] + int(rng.integers(1, 3)), c[1]:c[1] + int(rng.integers(1, 3)), c[2]] = True
        want = core_groups_oracle(mask, m, exempt)
        assert min_width.CoreConnectivity(m, exempt).groups(mask) == want
        split += want > 1
    assert split > 20  # 割れた形を試せている


def test_the_window_proof_is_never_wrong_and_the_bite_verdict_equals_the_global_count():
    """落ちたら、一口の周りの窓が「芯は切れない」と誤って言っている(`proven` が真なら大域でも真)、または `after_bite` が大域の数え直しとずれている。"""
    rng = np.random.default_rng(20260926)
    proven_yes = 0
    for _ in range(300):
        m = int(rng.integers(2, 4))
        exempt = None
        if rng.random() < 0.5:
            exempt = np.zeros(tuple(int(x) for x in rng.integers(8, 13, size=3)), bool)
            c = [int(rng.integers(0, n - 2)) for n in exempt.shape]
            exempt[c[0]:c[0] + 2, c[1]:c[1] + 2, c[2]] = True
        mask = _random_rule_mask(rng, m, exempt=exempt)
        if mask is None:
            continue
        core = min_width.CoreConnectivity(m, exempt)
        for cell in np.argwhere(mask)[rng.permutation(int(mask.sum()))[:4]]:
            proven_yes += _bite_verdicts(core, mask, tuple(int(v) for v in cell), m, exempt)[0]
    assert proven_yes > 20  # 窓で通す経路を試している


def test_peel_refuses_the_moves_that_would_split_the_core_and_every_result_keeps_it_connected():
    """落ちたら、`peel` が芯を割る手を通している(R(m) だけを守る一口は、同じ入力で芯を割る)。"""
    rng = np.random.default_rng(20260927)
    anchors = np.zeros(0, dtype=np.int64)
    refused = would_split = proven_yes = split_seen = 0
    for _ in range(12):
        m = int(rng.integers(2, 4))
        mask = np.ones(tuple(int(x) for x in rng.integers(7, 11, size=3)), bool)
        for _ in range(12):
            after, stats = bs.peel(mask, rng.permutation(mask.size), max(1, int(mask.sum()) // 4), anchors, min_width_cells=m)
            _assert_min_width_invariants(after, m)
            refused += stats.skipped_core
            core = min_width.CoreConnectivity(m)
            for cell in np.argwhere(mask)[rng.permutation(int(mask.sum()))[:20]]:
                without = mask.copy()
                without[tuple(cell)] = False
                would_split += core_groups_oracle(min_width.cube_opening(without, m), m) > 1
                proven, truth = _bite_verdicts(core, mask, tuple(int(v) for v in cell), m)
                proven_yes += proven
                split_seen += not truth
            if after.sum() == mask.sum():
                break
            mask = after
    assert refused > 50 and would_split > 5 and proven_yes > 50 and split_seen > 30  # 芯を割る手が実際に出る入力で、両方の経路を試している


@pytest.mark.parametrize('m,kind', [(2, 'bending'), (3, 'random')])
def test_peel_with_min_width_carves_a_box_deeply_with_the_core_connected_at_every_step(m, kind):
    """落ちたら、芯を割る手を断る分だけ削れなくなった(全ステップの不変条件は `_peel_box_until_stuck` が見る)。"""
    assert _peel_box_until_stuck(m, kind) < 0.3


def test_a_design_space_whose_core_is_split_is_recorded_and_no_candidate_that_keeps_it_split_is_returned():
    """落ちたら、芯が割れた設計空間(斜めの共有1セルで3つの立方をつないだ形)を入口で数えていない、または割れたまま合格を返している。
    両端の条件節点は別々の立方にあるので、芯を1つにするには片方を落とすしかなく、条件が成り立たない。"""
    from test_binary_search import Table, bar_conditions

    design = _chain((1, 1, 1), n=3)[:4, :4, :4]
    assert bs._mask_satisfies_min_width(design, 2) and core_groups_oracle(design, 2) == 3
    conditions = bar_conditions(shape=design.shape)
    node = lambda x, y, z: (x * 5 + y) * 5 + z
    conditions = dataclasses.replace(conditions, table=Table(conditions.grid, {1: [node(0, 0, 0)], 2: [node(4, 4, 4)]}))
    result = bs.find_initial_feasible(design, conditions, max_evaluations=30, min_width_cells=2)
    assert result.status == 'no_feasible_found' and result.best_feasible is None
    assert result.diagnostics['min_width']['design_core_groups'] == 3
    assert result.history[0].checks == dict(min_width='fail')
    assert all(t.outcome != 'ok' for t in result.history)


@pytest.mark.parametrize('m', [2, 3, 4])
def test_a_grid_thinner_than_the_cube_has_no_core_instead_of_failing(m):
    """落ちたら、m 立方が格子の軸に収まらない入力で芯の数え上げが例外になる(`optimize_run` は入口で断るが、`binary_search` の直接呼び出しは通る)。"""
    thin = np.ones((m - 1, m + 2, m + 2), bool)
    assert min_width.core_cells(thin, m).shape == thin.shape and not min_width.core_cells(thin, m).any()
    assert min_width.CoreConnectivity(m).groups(thin) == 0
    exempt = np.zeros_like(thin)
    exempt[0, 0, 0] = True
    assert min_width.CoreConnectivity(m, exempt).groups(thin) == 0
