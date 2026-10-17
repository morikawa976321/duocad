"""最小幅の規則 S(m)(侵食した芯の連結。`min_width.CoreConnectivity`)。R(m) は m 立方が斜めに共有1セルで重なる首を許すので、
その首を塞ぐ規則を `peel`(段階4)と段階3の全材料の検査が守ることを、実装とは独立な物差し(`core_groups_oracle`)で確かめる。"""
import dataclasses

import numpy as np
import pytest
from scipy import ndimage

from duocad.topopt import binary_search as bs
from duocad.topopt import min_width

from topopt_support import Table, assert_min_width_invariants, bar_conditions, core_groups_oracle, peel_box_until_stuck


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
    if exempt is not None:
        mask = mask & ~exempt  # `verdict` の契約は exempt を除いた材料を受ける(物差しは和集合で数えるので真値は変わらない)
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


@pytest.mark.parametrize('m,step,groups', [
    (4, (1, 1, 0), 1), (4, (1, 1, 1), 1), (5, (1, 1, 0), 1),  # 26近傍の段(球の直径 m を通す)
    (4, (3, 3, 0), 4),  # 角のずれ m−1 の稜線の共有は26近傍でもない
    (3, (1, 1, 0), 4), (3, (1, 1, 1), 4),  # m ≤ 3 は6のまま
])
def test_diagonal_steps_join_the_core_from_m_4_and_the_adjacency_can_be_fixed_to_6(m, step, groups):
    """落ちたら、芯の隣接の閾値(#122: m ≥ 4 で26)が仕様とずれているか、`structure` の上書きが効いていない。"""
    chain = _chain(step, m=m)
    assert bs._mask_satisfies_min_width(chain, m)
    assert min_width.CoreConnectivity(m).groups(chain) == core_groups_oracle(chain, m) == groups
    six = min_width.CoreConnectivity(m, structure=ndimage.generate_binary_structure(3, 1))
    assert six.groups(chain) == core_groups_oracle(chain, m, adjacency=6) == 4


def test_removing_the_middle_of_a_diagonal_chain_splits_the_core_even_though_no_remaining_core_is_a_face_neighbour():
    """m=4 の (1,1,0) 段の鎖で、真ん中の立方だけが持つセルを抜くと、残りの芯は (2,2,0) 離れて割れる。抜かれた芯の前線は斜めの隣だけ。
    落ちたら、`verdict` の前線の膨張が芯の隣接(26)とずれていて、前線が空のまま「つながる」と答えている。"""
    m, n = 4, 6
    chain = _chain((1, 1, 0), n=n, m=m)
    middle = np.zeros_like(chain)
    middle[3:3 + m, 3:3 + m, :m] = True
    others = np.zeros_like(chain)
    for i in range(n):
        if i != 3:
            others[i:i + m, i:i + m, :m] = True
    bite = np.argwhere(middle & ~others)
    after = chain & ~(middle & ~others)
    assert core_groups_oracle(chain, m) == 1 and core_groups_oracle(after, m) == 2
    assert not min_width.CoreConnectivity(m).after_bite(after, bite, 0)


def test_a_diagonal_chain_split_near_a_short_stub_is_proven_split_after_widening_the_window_once():
    """m=4 の (1,1,0) 段の長い鎖で、片側に8段だけ残る位置の立方を抜く。余白 m の窓では両側の芯が窓の面に触れて言えず、余白 2m で
    短い側が閉じて「確実に割れる」と言える。落ちたら、窓を広げる経路の前線か「閉じた塊」の判定が26の芯とずれている。"""
    m, n, j = 4, 40, 31
    mask = np.zeros((n + m + 2, n + m + 2, 24), bool)
    for i in range(n):
        mask[i:i + m, i:i + m, :m] = True
    others = np.zeros_like(mask)
    for i in range(n):
        if i != j:
            others[i:i + m, i:i + m, :m] = True
    exclusive = mask & ~others
    bite = np.argwhere(exclusive)
    after = mask & ~exclusive
    assert core_groups_oracle(mask, m) == 1 and core_groups_oracle(after, m) == 2
    core = min_width.CoreConnectivity(m)
    assert not core.after_bite(after, bite, 0)
    assert core.stats['window_false'] == 1 and core.stats['max_margin'] == 2 and core.stats['global_count'] == 0


@pytest.mark.parametrize('b_corner', [(9, 2, 1), (9, 2, 2)], ids=['edge', 'vertex'])
def test_exempt_material_touching_a_cube_only_along_an_edge_or_at_a_vertex_does_not_join_it_even_when_cores_are_26(b_corner):
    """m=4(芯は26連結)でも、保持成分への結びは「立方が重なるか6近傍で接する」のまま(#122)。保持の棒は立方Aに面で接し、立方Bには
    稜線か頂点だけで接する。落ちたら、保持への結びまで芯の隣接(26)で数えている(保持は規則の対象外で細いこともある)。"""
    m = 4
    mask = np.zeros((14, 7, 7), bool)
    mask[1:5, 1:5, 1:5] = True
    b = np.array(b_corner)
    mask[b[0]:b[0] + m, b[1]:b[1] + m, b[2]:b[2] + m] = True
    bar = np.zeros_like(mask)
    bar[5:9, 1, 1] = True
    assert min_width.CoreConnectivity(m).groups(mask) == 2
    assert min_width.CoreConnectivity(m, bar).groups(mask) == core_groups_oracle(mask, m, bar) == 2
    touching = bar.copy()
    touching[8, 1:3, 1:3] = True  # 棒の先を B の面に届かせると結ぶ
    assert min_width.CoreConnectivity(m, touching).groups(mask) == core_groups_oracle(mask, m, touching) == 1


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
            mask = mask & ~exempt
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


def _random_skeleton_mask(rng, m, shape, *, walks=4, steps=12, diagonal=False):
    """m 立方を面で重ねながら歩かせた細い骨格(幅 m)。芯は歩いた角の集合で、面で重ねるので 1 塊。
    枝の先端が多く、「先端が落ちて芯が割れる手」と「割れない手」の両方が出る。`diagonal` なら半分の段を {−1,0,1}³ の斜めにする
    (芯が26でしかつながらない段ができる)。"""
    mask = np.zeros(shape, bool)
    corners = [np.array([int(rng.integers(0, n - m + 1)) for n in shape])]
    for _ in range(walks):
        c = corners[int(rng.integers(len(corners)))].copy()
        for _ in range(steps):
            if diagonal and rng.random() < 0.5:
                c = np.clip(c + rng.integers(-1, 2, size=3), 0, np.array(shape) - m)
            else:
                axis = int(rng.integers(3))
                c[axis] = int(np.clip(c[axis] + rng.choice([-1, 1]) * rng.integers(1, m), 0, shape[axis] - m))
            mask[c[0]:c[0] + m, c[1]:c[1] + m, c[2]:c[2] + m] = True
            corners.append(c.copy())
    return mask


def test_the_window_verdict_is_exact_on_grids_larger_than_the_window_and_rarely_needs_the_global_count():
    """落ちたら、窓を広げて「確実に割れる」と答える経路が大域の数え直しとずれているか、窓が広がらず大域に落ち続けている。

    格子は窓より大きく(28〜36 セル)、細い骨格の先端が落ちる手と、遠回りでつながり直す手の両方が混ざる。"""
    rng = np.random.default_rng(20260924)
    checked, truths = 0, set()
    stats = dict(window_true=0, window_false=0, global_count=0)
    for _ in range(30):
        m = int(rng.integers(2, 5))
        shape = tuple(int(x) for x in rng.integers(28, 37, size=3))
        mask = _random_skeleton_mask(rng, m, shape)
        exempt = None
        if rng.random() < 0.4:
            exempt = np.zeros(shape, bool)
            c = np.argwhere(mask)[int(rng.integers(int(mask.sum())))]
            exempt[c[0]:c[0] + 2, c[1]:c[1] + 2, c[2]] = True
            mask = mask & ~exempt
        if core_groups_oracle(mask, m, exempt) != 1:
            continue
        core = min_width.CoreConnectivity(m, exempt)
        for cell in np.argwhere(mask)[rng.permutation(int(mask.sum()))[:8]]:
            bite = np.vstack(([list(cell)], min_width.casualties(mask, tuple(int(v) for v in cell), m, exempt)))
            after = mask.copy()
            after[tuple(bite.T)] = False
            truth = core_groups_oracle(after, m, exempt) <= 1
            assert core.after_bite(after, bite, 0) == truth
            truths.add(truth)
            checked += 1
        for key in stats:
            stats[key] += core.stats[key]
    assert checked > 100 and truths == {True, False}
    assert stats['window_false'] > 0  # 窓の中で「確実に割れる」と言えた手がある
    assert stats['global_count'] < stats['window_false'] + stats['window_true']  # 大半は窓で決まる


def test_the_window_verdict_is_exact_with_26_adjacent_cores_on_diagonal_skeletons():
    """m ≥ 4(芯は26連結)で、斜めの段を含む骨格から一口を抜く手を大域の物差しと突き合わせる。落ちたら、窓の判定(前線・閉じた塊)の
    どこかが6のまま。6で数えると答えが変わる形を踏んだことも確かめる(差が無ければ検査が効いていない)。"""
    rng = np.random.default_rng(20261008)
    checked, truths, six_differs = 0, set(), 0
    for _ in range(24):
        m = int(rng.integers(4, 6))
        shape = tuple(int(x) for x in rng.integers(20, 29, size=3))
        mask = _random_skeleton_mask(rng, m, shape, diagonal=True)
        exempt = None
        if rng.random() < 0.3:
            exempt = np.zeros(shape, bool)
            c = np.argwhere(mask)[int(rng.integers(int(mask.sum())))]
            exempt[c[0]:c[0] + 2, c[1]:c[1] + 2, c[2]] = True
        if core_groups_oracle(mask, m, exempt) != 1:
            continue
        six_differs += core_groups_oracle(mask, m, exempt, adjacency=6) != 1
        core = min_width.CoreConnectivity(m, exempt)
        for cell in np.argwhere(mask)[rng.permutation(int(mask.sum()))[:8]]:
            bite = np.vstack(([list(cell)], min_width.casualties(mask, tuple(int(v) for v in cell), m, exempt)))
            after = mask.copy()
            after[tuple(bite.T)] = False
            truth = core_groups_oracle(after, m, exempt) <= 1
            assert core.after_bite(after, bite, 0) == truth
            truths.add(truth)
            checked += 1
    assert checked > 60 and truths == {True, False} and six_differs > 5


def test_the_window_grows_with_the_margin_and_never_leaves_the_grid():
    core = min_width.CoreConnectivity(3)
    lo1, hi1, ext1 = core.window((10, 10, 10), (11, 11, 11), (40, 40, 40))
    lo2, hi2, ext2 = core.window((10, 10, 10), (11, 11, 11), (40, 40, 40), margin=2)
    assert (lo2 < lo1).all() and (hi2 > hi1).all()
    assert (lo1 == 10 - 2 - 3).all() and (hi1 == 11 + 3).all() and ext1[0].stop == int(hi1[0]) + 2
    lo3, hi3, ext3 = core.window((1, 1, 1), (2, 2, 2), (12, 12, 12), margin=8)
    assert (lo3 == 0).all() and (hi3 == 12).all() and ext3[0].stop == 12


def test_a_move_that_also_removed_an_island_is_judged_on_the_whole_core_not_in_the_window():
    """落ちたら、島を一緒に消した手の芯の判定を一口の周りの窓で済ませている(島は一口から離れた材料を消すので、窓では言えない)。"""
    split = _chain((1, 1, 1))  # 芯は4つに割れている
    far = (split.shape[0] - 1,) * 3  # どの立方からも離れたセル
    core = min_width.CoreConnectivity(2)
    assert core.after_bite(split, far, 0)  # 窓の中では芯は変わらない
    assert not core.after_bite(split, far, 1)


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
            assert_min_width_invariants(after, m)
            # 芯を割る手は、材料が窓内でつながったままならくびれの判定(`skipped_neck`)が先に断る。
            refused += stats.skipped_core + stats.skipped_neck
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
    """落ちたら、芯を割る手を断る分だけ削れなくなった(全ステップの不変条件は `peel_box_until_stuck` が見る)。"""
    assert peel_box_until_stuck(m, kind) < 0.3


def test_a_design_space_whose_core_is_split_is_recorded_and_no_candidate_that_keeps_it_split_is_returned():
    """落ちたら、芯が割れた設計空間(斜めの共有1セルで3つの立方をつないだ形)を入口で数えていない、または割れたまま合格を返している。
    両端の条件節点は別々の立方にあるので、芯を1つにするには片方を落とすしかなく、条件が成り立たない。"""
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
