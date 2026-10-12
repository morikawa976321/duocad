import dataclasses
import hashlib
import itertools
import json
import math

import numpy as np
import pytest
from build123d import Box, Pos
from scipy import ndimage

from duocad.topopt import acceptance, fem, min_width, stress
from duocad.topopt import binary_search as bs
from topopt_support import (Table, appendage_conditions, assert_min_width_invariants, assert_peel_invariants,
                            bar_conditions, fake_report, opening_satisfied, random_connected_mask, rule_violations,
                            slim_bar_conditions)


def test_resolve_islands_rejects_an_isolated_component_that_holds_only_one_condition():
    """孤立成分が条件を持つ例: 一方は固定面だけ、他方は荷重面だけを持ち、どちらも
    単独では6条件の前提を満たさない。**大きい方を選んで一方の条件を無視してはいけない。**"""
    shape = (5, 2, 2)
    conditions = bar_conditions(shape=shape, fixed=(1,), load=(2,))
    candidate = np.zeros(shape, bool)
    candidate[0] = True  # 固定面(x=0)だけに触れる成分
    candidate[4] = True  # 荷重面(x=5平面)だけに触れる成分。x=1..3は空(切れている)
    survivors, diagnostics = bs.resolve_islands(candidate, conditions)
    assert survivors == []
    assert diagnostics['components'] == 2
    assert diagnostics['surviving_components'] == 0
    assert all(island['touches_condition'] for island in diagnostics['removed_islands'])
    assert all(set(island) == {'cells', 'volume_mm3', 'touches_condition'}  # マスクを持たず数値だけ
               for island in diagnostics['removed_islands'])


def test_resolve_islands_keeps_the_component_that_alone_satisfies_every_condition():
    shape = (4, 2, 2)
    conditions = bar_conditions(shape=shape)
    candidate = np.ones(shape, bool)
    survivors, diagnostics = bs.resolve_islands(candidate, conditions)
    assert len(survivors) == 1
    assert np.array_equal(survivors[0], candidate)
    assert diagnostics['components'] == 1
    assert diagnostics['removed_islands'] == []


def test_full_design_space_already_feasible_is_found_and_saved_on_the_first_try():
    shape = (4, 2, 2)
    conditions = bar_conditions(shape=shape)
    design_space_mask = np.ones(shape, bool)
    result = bs.find_initial_feasible(design_space_mask, conditions, max_evaluations=5)
    assert result.status == 'feasible'
    assert result.termination == 'initial_feasible_found'
    assert result.best_feasible is not None
    assert result.best_feasible.material_mm3 == 16.0
    assert result.evaluations == 1
    assert len(result.history) == 1
    assert result.history[0].outcome == 'ok'


def test_island_is_stripped_before_the_safety_recheck_mask_hash_and_solver_input_agree():
    """安全率の再評価が島除去後のマスクを使っていることを、マスクハッシュと解への入力で確かめる。"""
    shape = (6, 2, 2)  # 4本分の棒(x=0..3)+浮島(x=5)。棒の終端(x=4平面)を荷重面にする。
    conditions = bar_conditions(shape=shape)
    conditions = dataclasses.replace(conditions, table=Table(conditions.grid, {1: list(range(9)), 2: list(range(36, 45))}))
    design_space_mask = np.zeros(shape, bool)
    design_space_mask[:4] = True  # 4本分の棒(条件を満たす)
    design_space_mask[5, 0, 0] = True  # 浮島(棒とはx=4の空隙で切れている、どの条件にも触れない)

    result = bs.find_initial_feasible(design_space_mask, conditions, max_evaluations=5)
    assert result.status == 'feasible'
    best = result.best_feasible

    expected_bar = np.zeros(shape, bool)
    expected_bar[:4] = True
    expected_hash = hashlib.sha256(np.ascontiguousarray(expected_bar).tobytes()).hexdigest()

    assert best.mask_hash == expected_hash  # 島を含まない
    assert int(best.mask.sum()) == 16
    assert best.report['volumes']['removed_islands_mm3'] == pytest.approx(1.0)
    # FEMへの入力(evaluated_material_cells)が島除去後の16セルであること(候補全体の17ではない)
    assert best.report['checks']['safety']['evaluated_material_cells'] == 16
    assert best.report['material_sha256'] == expected_hash


def test_resolution_insufficient_is_reported_without_running_the_search():
    shape = (4, 2, 2)
    conditions = bar_conditions(shape=shape, empty=(1,))
    result = bs.find_initial_feasible(np.ones(shape, bool), conditions, max_evaluations=10)
    assert result.status == 'resolution_insufficient'
    assert result.evaluations == 0
    assert result.history == []
    assert result.diagnostics['empty_faces'] == [1]


def test_input_contradiction_is_confirmed_when_even_the_full_design_space_cannot_preserve():
    shape = (4, 2, 2)
    unreachable = Pos(100., 100., 100.) * Box(1., 1., 1.)  # 設計空間から遠く離れた保持(絶対に重ならない)
    conditions = dataclasses.replace(bar_conditions(shape=shape), preserves=[('keep', unreachable.wrapped)])
    result = bs.find_initial_feasible(np.ones(shape, bool), conditions, max_evaluations=10)
    assert result.status == 'input_contradiction'
    assert result.evaluations == 0
    assert result.best_feasible is None


def test_a_closed_void_in_the_design_space_stops_the_search_before_any_candidate():
    """除外領域が塊に完全に包まれた入力(設計空間の内部に空セル)は、探索前に入力矛盾として止まる。"""
    shape = (5, 5, 5)
    conditions = bar_conditions(shape=shape)
    design_space_mask = np.ones(shape, bool)
    design_space_mask[2, 2, 2] = False
    design_space_mask[3, 1:3, 3] = False
    result = bs.find_initial_feasible(design_space_mask, conditions, max_evaluations=10)
    assert (result.status, result.termination, result.evaluations) == ('input_contradiction', 'input_contradiction', 0)
    assert result.best_feasible is None and result.history == []
    voids = result.diagnostics['closed_voids']
    assert voids['count'] == 2
    assert voids['largest'] == [dict(cells=2, representative_cell=[3, 1, 3]), dict(cells=1, representative_cell=[2, 2, 2])]


def test_a_design_space_open_to_the_outside_is_not_a_closed_void():
    shape = (5, 5, 5)
    design_space_mask = np.ones(shape, bool)
    design_space_mask[:, 2, 2] = False  # 貫通穴
    result = bs.find_initial_feasible(design_space_mask, bar_conditions(shape=shape), max_evaluations=3)
    assert result.status != 'input_contradiction' and result.evaluations >= 1


def test_full_material_that_fails_safety_stops_after_one_evaluation_and_names_the_check(monkeypatch):
    """落ちたら、全材料が不合格のとき別の形を探しに行っている(FEMを2回以上払う)か、落ちた検査が診断に出ていない。"""
    shape = (4, 2, 2)
    conditions = bar_conditions(shape=shape, target_safety_factor=1e12)
    calls = []
    original = acceptance.inspect
    monkeypatch.setattr(acceptance, 'inspect', lambda *a, **k: calls.append(1) or original(*a, **k))
    result = bs.find_initial_feasible(np.ones(shape, bool), conditions, max_evaluations=25)
    assert (result.status, result.termination, result.best_feasible) == ('no_feasible_found', 'full_material_infeasible', None)
    assert result.evaluations == 1 and len(calls) == 1
    assert [(trial.operation, trial.outcome) for trial in result.history] == [('initial', 'infeasible')]
    attempt, = result.diagnostics['full_material']['attempts']
    assert attempt['failed'] == ['safety'] and attempt['trial'] == 0
    assert attempt['checks']['safety']['safety_factor'] < attempt['checks']['safety']['target_safety_factor'] == 1e12


def test_full_material_is_evaluated_once_without_the_bbox_cache_or_the_region_precompute(monkeypatch):
    """落ちたら、段階4が戻り先にする合格形状(§3)がキャッシュを通した検査だけで確定しているか、全材料を2回解いている。"""
    from duocad import kernel

    seen = []
    original = acceptance.inspect
    monkeypatch.setattr(acceptance, 'inspect', lambda *a, **k: seen.append(k) or original(*a, **k))
    conditions, design = partial_region_conditions()
    conditions = dataclasses.replace(conditions, bbox_cache=kernel.BoundingBoxCache())
    assert conditions.region_overlaps is not None
    result = bs.find_initial_feasible(design, conditions)
    assert result.termination == 'initial_feasible_found' and result.best_feasible.energy is not None
    assert len(seen) == 1 and seen[0]['bbox_cache'] is None and seen[0]['region_overlaps'] is None


def test_step_ratio_is_zero_at_the_target_grows_with_the_margin_and_is_capped():
    """削る割合は安全率の余裕で決まる。目標ちょうどで0、余裕が大きいほど大きく、上限で止まる。"""
    assert bs.step_ratio(2.0, 2.0) == 0.0
    ratios = [bs.step_ratio(s, 2.0, exponent=0.5, max_ratio=1.0) for s in (2.5, 4.0, 8.0, 32.0)]
    assert ratios == sorted(ratios) and ratios[1] == pytest.approx(1 - 0.5 ** 0.5)
    assert bs.step_ratio(164.7, 2.0, exponent=0.5, max_ratio=0.5) == 0.5
    assert bs.step_ratio(2.1, 2.0, exponent=math.inf, max_ratio=0.1) == 0.1  # 固定割合
    with pytest.raises(ValueError):
        bs.step_ratio(0.0, 2.0)


def test_the_energy_field_follows_the_cantilever_moment_and_is_zero_outside_material():
    """落ちたら、削る順位に使う要素エネルギーが検査の変位と食い違っている(層をまたぐ契約)。"""
    shape = (12, 2, 2)
    conditions = bar_conditions(shape=shape)
    full = np.ones(shape, bool)
    fields = {}
    report = acceptance.inspect(full, full, conditions.grid, fields=fields, **conditions.inspect_kwargs())
    energy = fields['element_energy']
    assert report['status'] == 'pass' and 'element_energy' not in report['checks']['safety']
    assert np.all(np.diff(energy.sum(axis=(1, 2))) < 0)  # 曲げモーメントの大きい根元ほど大きい
    order = bs.ranked_cells(full, energy)
    assert np.unravel_index(order[:8], shape)[0].min() > np.unravel_index(order[-8:], shape)[0].max()

    notched = full.copy()
    notched[6, 1, 1] = False
    fields = {}
    acceptance.inspect(notched, notched, conditions.grid, fields=fields, **conditions.inspect_kwargs())
    assert np.all(fields['element_energy'][~notched] == 0) and np.all(fields['element_energy'][notched] > 0)

    broken = np.zeros(shape, bool)
    broken[0], broken[1, 0, 0], broken[2, 1, 1] = True, True, True  # 辺だけで接する(水密で落ち、FEMへ進まない)
    fields = {}
    acceptance.inspect(broken, broken, conditions.grid, fields=fields, **conditions.inspect_kwargs())
    assert fields == {}


def test_peel_output_is_one_component_with_regular_vertices_no_void_and_supported_anchors():
    """落ちたら、peel の局所判定が全体の判定(6条件の幾何)とずれている。幾何の検算(島処理・
    `geometry_rejection`)が0件であることも同じケースで固定する。"""
    shape = (6, 5, 4)
    conditions = bar_conditions(shape=shape)
    anchors = bs._anchor_node_ids(conditions)
    rng = np.random.default_rng(20260919)
    peeled_any = 0
    for _ in range(30):
        mask = np.ones(shape, bool)
        mask.setflags(write=False)
        for _ in range(4):
            order = rng.permutation(mask.size)  # 材料でないセルも混ぜる(飛ばされること)
            count = int(rng.integers(1, int(mask.sum())))
            after, stats = bs.peel(mask, order, count, anchors)
            assert_peel_invariants(after, conditions)
            assert not (after & ~mask).any()
            assert stats.picked + stats.islands == mask.sum() - after.sum()
            survivors, diagnostics = bs.resolve_islands(after, conditions)
            assert bs._geometry_rejection(after, survivors, diagnostics, conditions) is None
            peeled_any += stats.picked
            after.setflags(write=False)
            mask = after
    assert peeled_any > 100  # 何も削れないケースだけで合格していない


def test_a_local_connectivity_pass_implies_the_global_one():
    """落ちたら、局所判定(3×3×3 の窓)が通したセルを抜くとマスク全体が割れる。"""
    rng = np.random.default_rng(7)
    verdicts = set()
    for _ in range(12):
        mask = random_connected_mask(rng, (6, 6, 6))
        padded = np.pad(mask, 1)
        for i, j, k in np.argwhere(mask):
            window = padded[i:i + 3, j:j + 3, k:k + 3].copy()
            window[1, 1, 1] = False
            local = bs._neighbours_stay_connected(window)
            rest = mask.copy()
            rest[i, j, k] = False
            globally = ndimage.label(rest, structure=bs.NEIGHBOURHOOD_STRUCTURE)[1] == 1 if rest.any() else True
            if local:
                assert globally
            verdicts.add((local, globally))
    assert {(True, True), (False, True), (False, False)} <= verdicts  # 局所で通らないが全体では通るセルもある


def ring_mask():
    """x-y 平面の一周(7×7 の外周、厚さ1セル)。固定面(x最小)と荷重面(x最大)の節点は全て外周の材料に接する。"""
    mask = np.zeros((7, 7, 1), bool)
    mask[[0, 6], :, :] = True
    mask[:, [0, 6], :] = True
    return mask


def test_peel_cuts_a_ring_once_through_the_global_check_and_refuses_the_second_cut():
    """1本目の切断はマスク全体を数え直して通り、2本目は条件節点の支えが2成分に分かれるので通らない。"""
    conditions = bar_conditions(shape=(7, 7, 1))
    ring = ring_mask()
    assert acceptance.boundary_check(ring)['status'] == 'pass' and acceptance.connectivity_check(ring)['status'] == 'pass'
    order = np.array([int(np.ravel_multi_index(cell, ring.shape)) for cell in ((3, 6, 0), (3, 0, 0))])
    after, stats = bs.peel(ring, order, 2, bs._anchor_node_ids(conditions))
    assert (stats.picked, stats.islands, stats.skipped_connectivity, stats.global_checks) == (1, 0, 1, 2)
    assert not after[3, 6, 0] and after[3, 0, 0] and after.sum() == ring.sum() - 1
    assert_peel_invariants(after, conditions)


def test_peel_removes_an_unloaded_dead_end_branch_as_an_island_in_one_call():
    """荷重の通らない袋小路の枝は、根元のセルを抜いた時点で島になり、1回の peel で丸ごと消える(数に入る)。"""
    conditions, design, appendage = appendage_conditions()
    shape = design.shape
    stub = np.zeros(shape, bool)
    stub[3, 2:, :] = True  # 幅1セル・長さ2セルの枝(付属部分の一部)
    mask = (design & ~appendage) | stub
    root_cells = [int(np.ravel_multi_index((3, 2, z), shape)) for z in (0, 1)]
    after, stats = bs.peel(mask, np.array(root_cells), 8, bs._anchor_node_ids(conditions))
    assert not (after & stub).any()
    assert (stats.picked, stats.islands) == (2, 2) and stats.global_checks >= 1
    assert after.sum() == mask.sum() - stub.sum()
    assert_peel_invariants(after, conditions)


def test_peel_counts_islands_and_casualties_toward_the_requested_count():
    """落ちたら、`count` が順位から選んだ手の数だけで数えられ、島・巻き添えとして消えた分を超えて削っている。"""
    conditions, design, appendage = appendage_conditions()
    stub = np.zeros(design.shape, bool)
    stub[3, 2:, :] = True
    mask = (design & ~appendage) | stub
    order = np.array([np.ravel_multi_index(c, mask.shape) for c in ((3, 2, 0), (3, 2, 1), (6, 0, 0))])
    for count, third_stays in ((3, True), (5, False)):  # 2手で根元2セル + 島2セル = 4セル
        after, _ = bs.peel(mask, order, count, bs._anchor_node_ids(conditions))
        assert after[6, 0, 0] == third_stays
    bar = np.ones((2, 2, 4), bool)
    order = np.array([np.ravel_multi_index(c, bar.shape) for c in ((0, 0, 0), (0, 0, 3))])
    for count, far_layer_stays in ((2, True), (5, False)):  # 1手で角1セル + 巻き添え3セル = 4セル
        after, _ = bs.peel(bar, order, count, np.zeros(0, dtype=np.int64), min_width_cells=2)
        assert after[:, :, 3].all() == far_layer_stays


def test_peel_never_takes_an_interior_cell():
    """落ちたら、内部のセル(空の6近傍を持たない)が抜けて閉じた空洞ができる(中空構造を許容しない制約の破れ)。"""
    shape = (5, 5, 5)
    conditions = bar_conditions(shape=shape)
    anchors = bs._anchor_node_ids(conditions)
    centre = int(np.ravel_multi_index((2, 2, 2), shape))
    full = np.ones(shape, bool)
    kept, stats = bs.peel(full, np.array([centre]), 1, anchors)
    assert kept.all() and (stats.picked, stats.skipped_surface) == (0, 1)


def test_peel_never_takes_the_last_material_cell_of_a_condition_node_and_leaves_its_input_alone():
    shape = (4, 2, 2)
    conditions = bar_conditions(shape=shape)
    full = np.ones(shape, bool)
    full.setflags(write=False)
    after, stats = bs.peel(full, np.arange(full.size), full.size, bs._anchor_node_ids(conditions))
    assert full.all() and 0 < stats.picked < full.size - 1
    assert stats.skipped_support > 0
    assert_peel_invariants(after, conditions)


def test_peel_refuses_a_cell_whose_removal_leaves_an_irregular_vertex():
    """L字の折れ目のセルを抜くと残る2セルが辺だけで接する(頂点の多様体条件を破る)ので、連結性より先に落ちる。"""
    mask = np.zeros((2, 2, 1), bool)
    mask[0, 0] = mask[1, 0] = mask[1, 1] = True
    after, stats = bs.peel(mask, np.array([int(np.ravel_multi_index((1, 0, 0), mask.shape))]), 1,
                           np.zeros(0, dtype=np.int64))
    assert np.array_equal(after, mask) and (stats.picked, stats.skipped_manifold) == (0, 1)


def test_cube_opening_equals_scipy_binary_opening():
    """落ちたら、軸ごとに分けた opening が `binary_opening` とずれている(最小幅の全判定がこれを使う)。"""
    rng = np.random.default_rng(20260922)
    for _ in range(120):
        shape = tuple(int(x) for x in rng.integers(1, 14, size=3))
        m = int(rng.integers(2, 7))
        mask = rng.random(shape) < rng.uniform(0.5, 0.98)
        assert np.array_equal(min_width.cube_opening(mask, m),
                              ndimage.binary_opening(mask, structure=np.ones((m, m, m), dtype=bool)))


def _bite_of(mask, cell, m=2, anchors=None, exempt=None):
    """`cell` だけを順位に置いて1手だけ `peel` し、抜けたセルの集合と実績を返す。"""
    order = np.array([int(np.ravel_multi_index(cell, mask.shape))])
    after, stats = bs.peel(mask, order, 1, np.zeros(0, dtype=np.int64) if anchors is None else anchors,
                           min_width_cells=m, exempt=exempt)
    return {tuple(int(x) for x in c) for c in np.argwhere(mask & ~after)}, stats, after


def test_peel_takes_the_cells_that_lose_their_cube_in_the_same_move():
    """厚さ2セルの塊(2x2x4)の角セルを抜くと、同じ層の3セルが m=2 立方の被覆を失う。m=1 は1セル、
    m=2 は厚みで却下せず、その4セルを1手(picked 1・casualties 3)で抜く。"""
    mask = np.ones((2, 2, 4), bool)
    gone, stats, _ = _bite_of(mask, (0, 0, 0), m=1)
    assert gone == {(0, 0, 0)} and (stats.picked, stats.casualties) == (1, 0)
    gone, stats, after = _bite_of(mask, (0, 0, 0), m=2)
    assert gone == {(x, y, 0) for x in range(2) for y in range(2)}
    assert (stats.picked, stats.casualties) == (1, 3)
    assert stats.picked + stats.casualties + stats.islands == mask.sum() - after.sum()
    assert opening_satisfied(after, 2)


def test_peel_bite_shapes_on_a_plate_a_free_edge_a_bar_and_a_thick_block():
    """落ちたら、一口(x + 巻き添え)の形が変わっている: 2セル厚の板の上面は貫通柱・自由縁のすぐ内側はリブごと・
    厚い塊の表面は巻き添え0(m=1 と同じ1セルの手)。"""
    plate = np.ones((6, 6, 2), bool)
    gone, stats, after = _bite_of(plate, (3, 3, 1))
    assert gone == {(3, 3, 0), (3, 3, 1)} and (stats.picked, stats.casualties) == (1, 1)
    assert not after[3, 3].any()
    gone, stats, _ = _bite_of(plate, (1, 3, 1))
    assert gone == {(x, 3, z) for x in (0, 1) for z in (0, 1)} and (stats.picked, stats.casualties) == (1, 3)
    block = np.ones((6, 6, 6), bool)
    gone, stats, after = _bite_of(block, (3, 3, 5))
    assert gone == {(3, 3, 5)} and (stats.picked, stats.casualties) == (1, 0)
    assert np.array_equal(after, bs.peel(block, np.array([int(np.ravel_multi_index((3, 3, 5), block.shape))]), 1,
                                         np.zeros(0, dtype=np.int64))[0])


def test_peel_takes_a_whole_cross_section_of_a_2x2_bar_and_leaves_the_split_to_the_connectivity_check():
    """2x2 の棒の途中の1セルは断面ごと(4セル)が一口になる。棒は割れるので、固定側が無ければ却下(全体の数え直し)、
    片側に条件節点があれば反対側は島として一緒に消える。"""
    bar = np.ones((2, 2, 8), bool)
    gone, stats, after = _bite_of(bar, (0, 0, 4))
    assert not gone and after.all()
    assert (stats.global_checks, stats.skipped_connectivity, stats.picked) == (1, 1, 0)
    fixed_end = np.flatnonzero(np.pad(np.ones((3, 3, 1), bool), ((0, 0), (0, 0), (0, 8))).ravel())
    gone, stats, after = _bite_of(bar, (0, 0, 4), anchors=fixed_end)
    assert (stats.picked, stats.casualties, stats.islands) == (1, 3, 12)
    assert not after[:, :, 4:].any() and after[:, :, :4].all()


def test_peel_refuses_a_bite_that_would_take_the_last_support_of_a_condition_node():
    """巻き添えが条件節点の最後の支えを奪う手は却下する(`skipped_support`)。x 自身は条件節点に接しない位置で固定する。"""
    bar = np.ones((2, 2, 4), bool)
    end = np.flatnonzero(np.pad(np.ones((3, 3, 1), bool), ((0, 0), (0, 0), (0, 4))).ravel())  # z=0 の節点
    gone, stats, after = _bite_of(bar, (0, 0, 1), anchors=end)  # z=0 と z=1 の層が巻き添え。z=0 の節点は支えを失う
    assert not gone and (stats.picked, stats.skipped_support) == (0, 1)
    gone, stats, _ = _bite_of(bar, (0, 0, 3), anchors=end)  # 反対の端の層だけが巻き添え
    assert gone == {(x, y, 3) for x in range(2) for y in range(2)} and stats.skipped_support == 0


def test_peel_local_bite_matches_the_opening_of_the_mask_without_the_picked_cell():
    """落ちたら、`peel` の一口が全体を数え直した `opening(mask − {x})` とずれている。1手で島が出なかったとき、
    返すマスクは `opening(mask − {x})` に等しい(厚みの不変条件は構成上保たれる)。"""
    rng = np.random.default_rng(20260920)
    anchors = np.zeros(0, dtype=np.int64)
    checked = bites = 0
    for _ in range(25):
        shape = tuple(int(x) for x in rng.integers(6, 12, size=3))
        m = int(rng.integers(2, 4))
        raw = rng.random(shape) < rng.uniform(0.6, 0.95)
        mask = ndimage.binary_opening(raw, structure=np.ones((m, m, m), dtype=bool))  # 冪等: 規則を満たす入力
        labels, count = ndimage.label(mask, structure=bs.NEIGHBOURHOOD_STRUCTURE)
        if not count or mask.sum() < 2 * m ** 3:
            continue
        mask = labels == 1 + int(np.argmax(np.bincount(labels.ravel())[1:]))
        assert opening_satisfied(mask, m)
        for cell in rng.permutation(mask.size)[:30]:
            i, j, k = np.unravel_index(int(cell), mask.shape)
            if not mask[i, j, k]:
                continue
            checked += 1
            after, stats = bs.peel(mask, np.array([cell]), 1, anchors, min_width_cells=m)
            assert opening_satisfied(after, m)
            if stats.picked == 1 and not stats.islands:
                without = mask.copy()
                without[i, j, k] = False
                assert np.array_equal(after, bs.min_width.cube_opening(without, m))
                bites += stats.casualties > 0
    assert checked > 100 and bites > 20  # 何も試せていない・巻き添えが出ないケースだけで合格していない


def test_search_with_min_width_cells_produces_a_shape_that_an_independent_opening_check_accepts():
    """最終形状を `peel` とは別の測り方(`binary_opening`)で確かめる(局所判定の穴の検出器)。
    同じ題材の m=1 は厚み1セルまで削るので、規則が実際に効いていることの対照になる。"""
    conditions = bar_conditions(shape=(8, 3, 3), target_safety_factor=.001)
    design = np.ones((8, 3, 3), bool)

    unconstrained = bs.find_best_feasible(design, conditions, max_evaluations=400, min_width_cells=1)
    assert unconstrained.status == 'feasible'
    assert (unconstrained.best_feasible.mask & ~ndimage.binary_opening(
        unconstrained.best_feasible.mask, structure=np.ones((2, 2, 2), bool))).any()

    result = bs.find_best_feasible(design, conditions, max_evaluations=400, min_width_cells=2)
    assert result.status == 'feasible'
    best = result.best_feasible
    assert opening_satisfied(best.mask, 2)
    casualties = sum(step['peel']['casualties'] for step in result.diagnostics['reduction']['steps'])
    assert casualties > 0  # 制約が実際に効いている(0なら題材が制約を試せていない)
    assert best.material_mm3 > unconstrained.best_feasible.material_mm3  # 薄くしすぎない分だけ材料が残る


def test_min_width_search_starts_from_the_design_space_thickness_opening():
    """2x2の棒の脇に離れた1セル突起が2つ。突起は m=2 の立方に入らず局所判定では1セルずつしか抜けないが、
    設計空間を opening で絞ってから探索するので、合格形状に突起は入らない。"""
    design = np.zeros((10, 4, 4), bool)
    design[:, :2, :2] = True
    design[2, 2, 0] = design[7, 2, 0] = True
    result = bs.find_best_feasible(design, slim_bar_conditions(), max_evaluations=200, min_width_cells=2)
    assert result.status == 'feasible'
    assert opening_satisfied(result.best_feasible.mask, 2)
    assert not result.best_feasible.mask[2, 2, 0] and not result.best_feasible.mask[7, 2, 0]


def test_a_design_space_that_already_violates_min_width_still_reaches_a_conforming_shape():
    """落ちたら、違反した設計空間を入口の opening で絞らずに評価している(絞らない全材料は規則を破り、合格に届かない)。"""
    design = np.ones((6, 3, 3), bool)
    for cell in [(2, 0, 0), (3, 0, 0), (2, 0, 2), (3, 0, 2)]:
        design[cell] = False
    conditions = bar_conditions(shape=design.shape)
    assert not opening_satisfied(design, 2)
    result = bs.find_initial_feasible(design, conditions, max_evaluations=40, min_width_cells=2)
    assert result.status == 'feasible' and result.evaluations == 1
    assert opening_satisfied(result.best_feasible.mask, 2)
    opened = ndimage.binary_opening(design, structure=np.ones((2, 2, 2), bool))
    assert result.diagnostics['min_width']['trimmed_cells'] == int(design.sum() - opened.sum()) > 0


def test_the_min_width_check_runs_before_any_fem_evaluation(monkeypatch):
    """落ちたら、規則を破る全材料にFEM(hanger 2.4mm で1回100秒超)を払っているか、規則を破る形を合格として返している。"""
    calls = []
    real_inspect = acceptance.inspect
    monkeypatch.setattr(acceptance, 'inspect', lambda *a, **k: calls.append(1) or real_inspect(*a, **k))
    monkeypatch.setattr(bs, '_mask_satisfies_min_width', lambda mask, m, exempt=None: False)
    conditions = bar_conditions(shape=(4, 2, 2))
    result = bs.find_initial_feasible(np.ones((4, 2, 2), bool), conditions, max_evaluations=6, min_width_cells=2)
    assert (result.status, result.termination, result.best_feasible) == ('no_feasible_found', 'full_material_infeasible', None)
    assert calls == []
    assert [t.checks for t in result.history] == [dict(min_width='fail')]
    assert result.diagnostics['full_material']['attempts'][0]['failed'] == ['min_width']


def test_min_width_with_a_design_space_that_holds_no_m_cube_is_refused_at_entry():
    """`(4, 1, 1)` は y・z が1セルなので m=2 の立方が1つも入らない。opening で設計空間が空になり、
    条件節点が材料に接しないので探索を始めずに `boundary_mapping_failed` で止まる。"""
    conditions = bar_conditions(shape=(4, 1, 1))
    result = bs.find_best_feasible(np.ones((4, 1, 1), bool), conditions, max_evaluations=50, min_width_cells=2)
    assert result.status == 'boundary_mapping_failed' and result.best_feasible is None
    assert result.evaluations == 0
    assert result.diagnostics['min_width']['trimmed_cells'] == 4


def _bar_with_protrusion(*, second_protrusion: bool = False):
    """棒の脇の1セルの突起(5,2,0)は m=2 の立方に入らない。保持ソリッドはそこだけに重なり、そのセルを保持セルにする。"""
    design = np.zeros((10, 4, 4), bool)
    design[:, :2, :2] = True
    design[5, 2, 0] = True
    if second_protrusion:
        design[2, 2, 0] = True
    keep = np.zeros(design.shape, bool)
    keep[5, 2, 0] = True
    conditions = slim_bar_conditions(keep_box=Pos(5.5, 2.5, 0.5) * Box(.9, .9, .9))
    return design, dataclasses.replace(conditions, keep_cells=keep)


def test_an_exempt_cell_needs_no_cube_but_still_counts_as_material_for_the_others():
    """落ちたら、対象外のセルが材料として数えられていない(他のセルの立方を壊す)か、対象外でないセルが見逃されている。"""
    block = np.ones((2, 2, 2), bool)
    corner = np.zeros_like(block)
    corner[0, 0, 0] = True
    assert bs._mask_satisfies_min_width(block, 2, corner)

    mask = np.zeros((6, 3, 3), bool)
    mask[:2, :2, :2] = True
    mask[2, 0, 0] = True
    protrusion = np.zeros_like(mask)
    protrusion[2, 0, 0] = True
    other = np.zeros_like(mask)
    other[0, 0, 0] = True
    assert not bs._mask_satisfies_min_width(mask, 2)
    assert not bs._mask_satisfies_min_width(mask, 2, other)
    assert bs._mask_satisfies_min_width(mask, 2, protrusion)
    region = (slice(0, 1), slice(0, 1), slice(0, 1))
    assert min_width.lost_coverage(mask, region, 2, protrusion)[1].any()
    assert min_width.lost_coverage(mask, (slice(1, 2), slice(1, 2), slice(1, 2)), 2, protrusion)[1].any()


def test_an_exempt_cell_is_never_taken_as_a_casualty_and_the_invariants_hold_next_to_it():
    """落ちたら、`exempt` のセルが巻き添えとして抜かれている、または `exempt` の近くの手が不変条件を壊している。
    全材料から `peel` を繰り返し(条件節点なし=島は出ない)、抜けた `exempt` のセルは順位から選んだセル自身だけ。"""
    rng = np.random.default_rng(20260924)
    anchors = np.zeros(0, dtype=np.int64)
    moves = bites = uncovered_exempt = via_removed_keep = 0
    for _ in range(12):
        shape = tuple(int(x) for x in rng.integers(6, 10, size=3))
        m = int(rng.integers(2, 4))
        exempt = rng.random(shape) < 0.15
        mask = np.ones(shape, bool)
        for _ in range(6):
            order = rng.permutation(mask.size)
            after, stats = bs.peel(mask, order, int(rng.integers(1, 1 + int(mask.sum()) // 3)), anchors,
                                   min_width_cells=m, exempt=exempt)
            assert stats.islands == 0
            removed = {int(c) for c in np.flatnonzero(mask & ~after)}
            assert len(removed) == stats.picked + stats.casualties
            assert {int(c) for c in np.flatnonzero(exempt.ravel())} & removed <= set(stats.picked_cells.tolist())
            assert_min_width_invariants(after, m, exempt)
            assert not rule_violations(after, m, exempt).any()
            moves += stats.picked
            bites += stats.casualties > 0
            uncovered_exempt += int((exempt & after & ~min_width.cube_opening(after, m)).sum())
            via_removed_keep += int((after & ~exempt & ~min_width.cube_opening(after, m)).sum())
            mask = after
    # 巻き添えも、被覆を失った対象外のセルも、抜かれた対象外のセルの幅に頼るセルも実際に出ている
    assert moves > 100 and bites > 10 and uncovered_exempt > 0 and via_removed_keep > 0


def test_an_exempt_cell_that_loses_its_cube_stays_while_the_others_of_its_layers_go():
    """2x2x4 の棒から (0,0,1) を抜くと z=0・z=1 の8セルが被覆を失う。(1,1,1) が対象外なら巻き添えにならず、材料のまま残る。"""
    bar = np.ones((2, 2, 4), bool)
    exempt = np.zeros_like(bar)
    exempt[1, 1, 1] = True
    plain, _, _ = _bite_of(bar, (0, 0, 1))
    assert plain == {(x, y, z) for x in range(2) for y in range(2) for z in (0, 1)}
    gone, stats, after = _bite_of(bar, (0, 0, 1), exempt=exempt)
    assert gone == plain - {(1, 1, 1)} and after[1, 1, 1] and (stats.picked, stats.casualties) == (1, 6)
    assert_min_width_invariants(after, 2, exempt)


def test_an_exempt_cell_missing_from_the_mask_still_counts_as_material_for_the_others():
    """落ちたら、`mask` に無い `exempt` セルが材料に数えられていない(判定関数の単独の契約。`keep_cells` を与えた探索では保持セルは常に `mask` にある)。"""
    block = np.ones((2, 2, 2), bool)
    block[0, 0, 0] = False
    keep = np.zeros_like(block)
    keep[0, 0, 0] = True
    assert bs._mask_satisfies_min_width(block, 2, keep)
    assert not bs._mask_satisfies_min_width(block, 2)


def test_peel_refuses_an_island_removal_that_takes_the_cube_a_missing_kept_cell_bridged():
    """落ちたら、島の除去が規則を壊す手を通している。m=3 で、主成分の層(x=2)と島になる層(x=4)は、mask に無い `exempt` の層(x=3)を
    挟んで1つの 3x3x3 の立方に入る。`exempt` の通路(mask にある)の途中を抜くと層(x=4)が島になるが、消すと x=2 の層が被覆を失う。"""
    shape = (7, 4, 3)
    mask = np.zeros(shape, bool)
    mask[2, :3, :3] = mask[4, :3, :3] = True
    exempt = np.zeros(shape, bool)
    exempt[3, :3, :3] = True
    for x in (2, 3, 4):
        mask[x, 3, 0] = exempt[x, 3, 0] = True
    assert bs._mask_satisfies_min_width(mask, 3, exempt)
    node_shape = tuple(n + 1 for n in shape)
    anchors = np.array([np.ravel_multi_index((2, 0, 0), node_shape)], dtype=np.int64)
    order = np.array([np.ravel_multi_index((3, 3, 0), shape)])
    after, stats = bs.peel(mask, order, 1, anchors, min_width_cells=3, exempt=exempt)
    assert np.array_equal(after, mask) and (stats.picked, stats.islands, stats.skipped_connectivity) == (0, 0, 1)


def test_peeling_an_exempt_cell_takes_no_coverage_from_the_cells_around_it():
    """落ちたら、抜いた `exempt` セルが材料でなくなって巻き添えを出している(`protected` を渡さない単独の呼び方)。2x2x4 の棒の (0,0,1) は普通は
    z=0・z=1 の層の被覆を奪うが、`exempt` なら1セルだけが抜け、残りは(抜けた `exempt` を材料に数えて)被覆されたまま。"""
    bar = np.ones((2, 2, 4), bool)
    region = (slice(0, 1), slice(0, 1), slice(1, 2))
    keep = np.zeros_like(bar)
    keep[0, 0, 1] = True
    assert min_width.lost_coverage(bar, region, 2)[1].any()
    assert not min_width.lost_coverage(bar, region, 2, keep)[1].any()
    gone, stats, after = _bite_of(bar, (0, 0, 1), exempt=keep)
    assert gone == {(0, 0, 1)} and (stats.picked, stats.casualties) == (1, 0)
    assert bs._mask_satisfies_min_width(after, 2, keep) and not bs._mask_satisfies_min_width(after, 2)


def test_trimming_the_design_space_can_cut_a_preserved_region_and_is_then_refused_at_entry_with_the_count():
    """保持が突起にしか重ならないと、絞った空間では保持が成り立たず入口で止まる(規則適合の形が存在しない)。m=1 は合格する。"""
    design, conditions = _bar_with_protrusion()
    conditions = dataclasses.replace(conditions, keep_cells=None)
    assert bs.find_best_feasible(design, conditions, max_evaluations=60, min_width_cells=1).status == 'feasible'
    result = bs.find_best_feasible(design, conditions, max_evaluations=60, min_width_cells=2)
    assert result.status == 'input_contradiction' and result.evaluations == 0
    assert result.diagnostics['min_width']['trimmed_cells'] == 1


def test_the_design_space_opening_keeps_the_keep_cells_and_still_trims_the_others():
    """落ちたら、保持ソリッド由来のセルが設計空間から消える(入口で保持の重なりが足りず止まる)、または
    保持でない薄いセルまで残る。"""
    design, conditions = _bar_with_protrusion(second_protrusion=True)
    result = bs.find_initial_feasible(design, conditions, max_evaluations=60, min_width_cells=2)
    assert result.status == 'feasible'
    assert result.diagnostics['min_width']['trimmed_cells'] == 1
    mask = result.best_feasible.mask
    assert mask[5, 2, 0] and not mask[2, 2, 0]
    assert bs._mask_satisfies_min_width(mask, 2, conditions.keep_cells) and not opening_satisfied(mask, 2)


def test_the_full_search_keeps_the_keep_cell_and_every_non_keep_cell_stays_in_a_cube():
    """落ちたら、段階4まで通した形で保持でないセルが m 立方を外れている(対象外なのは保持セルだけ)。"""
    design, conditions = _bar_with_protrusion()
    result = bs.find_best_feasible(design, conditions, max_evaluations=200, min_width_cells=2)
    mask = result.best_feasible.mask
    assert result.status == 'feasible' and mask[5, 2, 0]
    cube = np.ones((2, 2, 2), bool)
    assert (mask & ~ndimage.binary_opening(mask, structure=cube)).any()
    keep = conditions.keep_cells
    assert not (mask & ~keep & ~ndimage.binary_opening(mask | keep, structure=cube)).any()


def test_with_min_width_one_keep_cells_only_keep_their_cells_and_call_no_min_width_function(monkeypatch):
    """落ちたら、既定(m=1)で最小幅の判定関数が呼ばれている、または `keep_cells` が保持セルを残す以外に探索を変えている。"""
    conditions = bar_conditions(shape=(8, 3, 3), target_safety_factor=.001)
    design = np.ones((8, 3, 3), bool)
    plain = bs.find_best_feasible(design, conditions, max_evaluations=200, min_width_cells=1)
    keep = np.zeros((8, 3, 3), bool)
    keep[4, 2, 2] = keep[4, 0, 0] = True
    assert not plain.best_feasible.mask[keep].all()

    class NoMinWidth:
        def __getattr__(self, name):
            raise AssertionError(f'm=1 で min_width.{name} を使った')

    monkeypatch.setattr(bs, 'min_width', NoMinWidth())
    marked = dataclasses.replace(conditions, keep_cells=keep)
    with_keep = bs.find_best_feasible(design, marked, max_evaluations=200, min_width_cells=1)
    assert with_keep.status == 'feasible' and with_keep.best_feasible.mask[keep].all()
    assert with_keep.best_feasible.mask.sum() >= plain.best_feasible.mask.sum()


def test_a_keep_cells_array_of_the_wrong_shape_is_refused():
    conditions = dataclasses.replace(bar_conditions(shape=(4, 2, 2)), keep_cells=np.ones((3, 2, 2), bool))
    with pytest.raises(ValueError, match='keep_cells'):
        bs.find_initial_feasible(np.ones((4, 2, 2), bool), conditions, min_width_cells=2)


def _plate_between_exclusions():
    """8x8x7 の格子で、z=3 の 4x4 の板だけが設計空間(全て保持)。板のまわり(x,y=1..6・z=2..4)は除外。"""
    design = np.zeros((8, 8, 7), bool)
    design[2:6, 2:6, 3] = True
    obstacle = np.zeros_like(design)
    obstacle[1:7, 1:7, 2:5] = True
    obstacle &= ~design
    return design, design.copy(), obstacle


def test_keep_cells_without_room_counts_the_stranded_cells_and_what_surrounds_them():
    """設計空間の opening(m=2)に入らない保持セルを数え、周りの空きセル(26近傍)が除外か設計空間の外かを言い分ける。"""
    design, keep, obstacle = _plate_between_exclusions()
    # 期待値: 板(16セル)の26近傍は 3×6×6 − 16 = 92
    assert bs.keep_cells_without_room(design, keep, 2, obstacle) == dict(cells=16, around_exclude=92, around_outside=0)
    assert bs.keep_cells_without_room(design, keep, 2) == dict(cells=16, around_exclude=0, around_outside=92)
    half = obstacle.copy()
    half[:, :, 3:] = False  # 除外は z=2 の層(36 セル)だけ
    assert bs.keep_cells_without_room(design, keep, 2, half) == dict(cells=16, around_exclude=36, around_outside=56)
    assert bs.keep_cells_without_room(design, keep, 1, obstacle) == dict(cells=0, around_exclude=0, around_outside=0)


def test_keep_cells_without_room_ignores_keep_cells_that_a_cube_covers_and_counts_the_grid_edge_as_outside():
    design = np.zeros((6, 6, 6), bool)
    design[:3, :3, :3] = True  # 3 立方(m=2 が入る)。格子の隅にある
    keep = design.copy()
    assert bs.keep_cells_without_room(design, keep, 2)['cells'] == 0
    design[4, 4, 4] = keep[4, 4, 4] = True  # 孤立した1セルの保持
    assert bs.keep_cells_without_room(design, keep, 2) == dict(cells=1, around_exclude=0, around_outside=26)


def test_reduction_strips_the_unloaded_appendage_and_keeps_every_condition_supported():
    conditions, design, appendage = appendage_conditions()
    result = bs.find_best_feasible(design, conditions, max_evaluations=200)
    best = result.best_feasible
    assert result.status == 'feasible' and result.termination == 'converged'
    assert not (best.mask & appendage).any()
    assert best.material_mm3 < design.sum() - appendage.sum()  # 付属部分に加えて棒も削った
    assert result.diagnostics['reduction']['single_cell_patience'] == bs.DEFAULT_SINGLE_CELL_PATIENCE
    assert result.diagnostics['reduction']['target_tolerance'] == bs.DEFAULT_TARGET_TOLERANCE
    assert acceptance.active_nodes(best.mask).ravel()[bs._anchor_node_ids(conditions)].all()
    recheck = acceptance.inspect(best.mask, best.before_mask, conditions.grid, **conditions.inspect_kwargs(bbox_cache=None))
    assert recheck['status'] == 'pass' and recheck['material_sha256'] == best.mask_hash
    assert result.diagnostics['reduction']['geometry_rejections'] == []
    assert not any(trial.outcome == 'no_component' for trial in result.history[1:])


def test_a_preserve_lost_by_the_peel_is_a_geometry_rejection_without_fem_and_the_search_still_converges():
    """`peel` は保持の重なりを見ない。落ちる候補は FEM の前に幾何の不合格として数えられ、
    k=1 でも落ちるセルはその形状の間だけ飛ばされて、同じ候補を解き直し続けない。"""
    conditions, design, _ = appendage_conditions()
    keep_cell = Pos(4.5, 3.5, 0.5) * Box(1., 1., 1.)  # 付属部分の1セル (4, 3, 0)
    conditions = dataclasses.replace(conditions, preserves=[('keep', keep_cell.wrapped)])
    result = bs.find_best_feasible(design, conditions, max_evaluations=300)
    reduction = result.diagnostics['reduction']
    assert result.termination == 'converged' and reduction['final_region_recheck'] == 'pass'
    assert result.best_feasible.mask[4, 3, 0]
    assert reduction['geometry_rejections'] and all(
        row['missing'] == ['preserve'] and row['components'] == 1 for row in reduction['geometry_rejections'])
    geometry_steps = [step for step in reduction['steps'] if step['failed'] == ['geometry']]
    assert len(geometry_steps) == len(reduction['geometry_rejections'])
    assert reduction['fem_solves'] == len(reduction['steps']) - len(geometry_steps)
    assert reduction['repeated_candidates'] > 0  # 順位が尽きて同じ候補に戻る場面を踏んでいる
    assert reduction['stop_reason'] == 'no_removable_cell' and reduction['safety_excluded_cells'] == 0
    strict = bs.find_best_feasible(design, conditions, max_evaluations=300, single_cell_patience=1)
    assert strict.diagnostics['reduction']['stop_reason'] == 'no_removable_cell'
    assert strict.best_feasible.mask_hash == result.best_feasible.mask_hash
    stage4 = result.history[1:]
    assert len({(trial.parent, trial.mask_hash) for trial in stage4}) == len(stage4)  # 同じ形から同じ候補を2度評価しない


def _stub_islands(monkeypatch):
    """島処理を「候補をそのまま1成分として返す」に置き換える(FEM・CAD を呼ばずに候補管理だけを見るため)。"""
    monkeypatch.setattr(bs, 'resolve_islands', lambda candidate, conditions: (
        [np.array(candidate, dtype=bool)], dict(components=1, surviving_components=1, removed_islands=[])))


def _seeded(mask, safety_factor, conditions):
    """`mask` を出発形状(合格・安全率 `safety_factor`)とする段階3の結果(段階3を経ずに `_reduce_volume` を呼ぶため)。"""
    report = fake_report(mask, 'pass')
    report['checks']['safety']['safety_factor'] = safety_factor
    best = bs.FeasibleModel(mask=mask, before_mask=mask, report=report, trial_index=0, energy=np.ones(mask.shape))
    volume = float(mask.sum())
    trial = bs.Trial(index=0, parent=None, operation='seed', mask_hash=best.mask_hash, before_mm3=volume,
                     material_mm3=volume, removed_islands_mm3=0., outcome='ok', safety_factor=safety_factor)
    return bs.SearchResult(status='feasible', termination='seed', best_feasible=best, history=[trial], evaluations=1,
                           conditions_identifier=bs.evaluation_identifier(conditions))


def test_a_bite_that_two_different_picked_cells_produce_is_not_solved_again_after_it_failed(monkeypatch):
    """落ちたら、不合格になった一口が、別のセルを選んだ手でもう一度できたときに FEM を解き直している。
    2セル厚の板では、上面のセルも下面のセルも同じ貫通柱を一口にする(順位の先頭に両方を置く)。"""
    shape = (6, 6, 2)
    plate = np.ones(shape, bool)
    conditions = bar_conditions(shape=shape)
    top, bottom = (int(np.ravel_multi_index(c, shape)) for c in ((3, 3, 1), (3, 3, 0)))
    monkeypatch.setattr(bs, 'ranked_cells', lambda mask, energy: np.array(
        [top, bottom] + [c for c in range(plate.size) if c not in (top, bottom)]))
    _stub_islands(monkeypatch)
    monkeypatch.setattr(acceptance, 'inspect', lambda material, before, grid, **kwargs: fake_report(material))  # どの候補も不合格
    result = bs._reduce_volume(_seeded(plate, .5, conditions), conditions, max_evaluations=400, is_cancelled=None,
                               max_step_ratio=.01, single_cell_patience=None, min_width_cells=2)
    reduction = result.diagnostics['reduction']
    stage4 = result.history[1:]
    assert reduction['repeated_candidates'] >= 1
    assert len({trial.mask_hash for trial in stage4}) == len(stage4)  # 同じ形を2度評価していない
    assert all(step['peel']['picked'] == 1 for step in reduction['steps'])
    assert any(step['peel']['casualties'] > 0 for step in reduction['steps'])  # 一口が複数セルの手で起きている


def test_a_candidate_split_into_components_is_reported_as_a_geometry_rejection_with_its_reason():
    """`peel` の不変条件が破れて候補が割れていたときの検算(段階4のループはこれを不合格として数える)。"""
    conditions = bar_conditions(shape=(5, 2, 2))
    candidate = np.zeros((5, 2, 2), bool)
    candidate[0] = candidate[4] = True  # 固定面側と荷重面側に分かれた2成分
    survivors, diagnostics = bs.resolve_islands(candidate, conditions)
    rejection = bs._geometry_rejection(candidate, survivors, diagnostics, conditions)
    assert rejection['components'] == 2 and rejection['surviving_components'] == 0
    whole = np.ones((5, 2, 2), bool)
    assert bs._geometry_rejection(whole, *bs.resolve_islands(whole, conditions), conditions) is None


def _fail_safety_when(monkeypatch, predicate):
    """`predicate(material)` が真の合格形状を、安全率の不足で不合格に見立てる(FEMの答えは変えない)。"""
    original = acceptance.inspect

    def limited(material, before, grid, **kwargs):
        report = original(material, before, grid, **kwargs)
        if report['status'] == 'pass' and predicate(material):
            report['checks']['safety']['status'] = report['status'] = 'fail'
            if kwargs.get('fields') is not None:
                kwargs['fields'].clear()
        return report

    monkeypatch.setattr(acceptance, 'inspect', limited)


def _safety_unknown_when(monkeypatch, predicate, reason='evaluation_error'):
    """FEM まで進む評価(`fields` を渡された検査)のうち `predicate(material)` が真のものを、安全率の判定不能に見立てる。"""
    original = acceptance.inspect

    def stubbed(material, before, grid, **kwargs):
        if kwargs.get('fields') is not None and predicate(material):
            report = original(material, before, grid, **{**kwargs, 'fields': None})
            report['checks']['safety'] = dict(status='unknown', reason=reason)
            if reason == 'evaluation_error':
                report['checks']['safety']['error'] = 'stub'
            report['status'] = 'unknown'
            return report
        return original(material, before, grid, **kwargs)

    monkeypatch.setattr(acceptance, 'inspect', stubbed)


def _single_cell_safety_failures(steps):
    return [step for step in steps if step['peel']['picked'] == 1 and 'safety' in step['failed']]


def test_rejected_steps_halve_the_step_accepted_ones_double_it_and_a_single_cell_safety_failure_converges(monkeypatch):
    """落ちたら、削減割合の後退規則か、`single_cell_patience=1` で1セルの安全率不合格が即収束になる規則が壊れている。"""
    conditions, design, _ = appendage_conditions()
    floor = 0.75 * design.sum()
    _fail_safety_when(monkeypatch, lambda material: material.sum() < floor)  # 体積の下限を安全率の不足に見立てる
    result = bs.find_best_feasible(design, conditions, max_evaluations=400, single_cell_patience=1)
    reduction = result.diagnostics['reduction']
    steps = reduction['steps']
    assert result.termination == 'converged' and reduction['stop_reason'] == 'safety_at_single_cell'
    assert floor <= result.best_feasible.mask.sum() < design.sum()

    def halvings(before, after):
        ratio = before / after
        assert ratio >= 1 and math.log2(ratio) == int(math.log2(ratio)), (before, after)
        return int(math.log2(ratio))

    for step, following in zip(steps, steps[1:]):
        if step['outcome'] == 'accepted':
            assert halvings(min(1.0, 2 * step['multiplier']), following['multiplier']) >= 0
        elif step['peel']['picked'] > 1:
            assert halvings(step['multiplier'], following['multiplier']) >= 1
        else:
            assert halvings(step['multiplier'], following['multiplier']) >= 0
    assert steps[-1]['peel']['picked'] == 1 and 'safety' in steps[-1]['failed']
    assert (reduction['single_cell_patience'], reduction['safety_excluded_cells']) == (1, 0)


def test_a_single_cell_safety_failure_excludes_that_cell_until_patience_consecutive_failures_converge(monkeypatch):
    """落ちたら、`single_cell_patience` の数え方(安全率で落ちた1セルを外して次へ進み、指定の数に達したら止まる)が壊れている。"""
    conditions, design, _ = appendage_conditions()
    floor = 0.75 * design.sum()
    _fail_safety_when(monkeypatch, lambda material: material.sum() < floor)  # 下限の手前では、どのセルを削っても不合格
    finals = {}
    for patience in (1, 3):
        result = bs.find_best_feasible(design, conditions, max_evaluations=400, single_cell_patience=patience)
        reduction = result.diagnostics['reduction']
        assert result.termination == 'converged' and reduction['stop_reason'] == 'safety_at_single_cell'
        assert reduction['single_cell_patience'] == patience and reduction['safety_excluded_cells'] == patience - 1
        assert reduction['steps'][-patience:] == _single_cell_safety_failures(reduction['steps'])[-patience:]
        assert any(step['peel']['picked'] > 1 and step['outcome'] == 'rejected' for step in reduction['steps'])
        stage4 = result.history[1:]
        assert len({(trial.parent, trial.mask_hash) for trial in stage4}) == len(stage4)
        finals[patience] = result.best_feasible.mask.sum()
    assert finals[3] == finals[1]  # 下限の手前では採用が起きないので、形は同じ


def test_unlimited_patience_walks_the_whole_ranking_and_stops_because_no_cell_can_be_removed(monkeypatch):
    """落ちたら、`single_cell_patience=None` が順位の尽きるまで(有限回で)続けて `no_removable_cell` で止まる規則が壊れている。"""
    conditions, design, _ = appendage_conditions()
    floor = 0.75 * design.sum()
    _fail_safety_when(monkeypatch, lambda material: material.sum() < floor)
    unlimited = bs.find_best_feasible(design, conditions, max_evaluations=10_000, single_cell_patience=None)
    reduction = unlimited.diagnostics['reduction']
    assert unlimited.termination == 'converged' and reduction['stop_reason'] == 'no_removable_cell'
    assert reduction['safety_excluded_cells'] == len(_single_cell_safety_failures(reduction['steps'])) > 0
    assert unlimited.evaluations < 10_000  # 予算切れではなく、規則で止まった
    stage4 = unlimited.history[1:]
    assert len({(trial.parent, trial.mask_hash) for trial in stage4}) == len(stage4)
    assert unlimited.best_feasible.mask.sum() >= floor
    patience_one = bs.find_best_feasible(design, conditions, max_evaluations=400, single_cell_patience=1)
    assert unlimited.best_feasible.mask.sum() <= patience_one.best_feasible.mask.sum()


def test_the_streak_is_reset_by_an_adoption_and_a_longer_patience_reaches_a_smaller_shape(monkeypatch):
    """落ちたら、採用でカウントが戻らない、または外したセルの先へ進めない(patience=1 で止まる形の先に、より軽い合格形状がある題材)。"""
    conditions, design, _ = appendage_conditions()
    protected = [(4, 0, 0), (5, 1, 1), (6, 0, 1)]  # 1つでも欠けたら安全率が足りないことにする棒の中ほどの3セル
    _fail_safety_when(monkeypatch, lambda material: not all(material[cell] for cell in protected))
    short = bs.find_best_feasible(design, conditions, max_evaluations=400, single_cell_patience=1)
    long = bs.find_best_feasible(design, conditions, max_evaluations=400, single_cell_patience=2)
    short_reduction, long_reduction = short.diagnostics['reduction'], long.diagnostics['reduction']
    assert short_reduction['stop_reason'] == 'safety_at_single_cell'
    assert long_reduction['stop_reason'] == 'no_removable_cell'  # 2回続けて落ちる前に必ず採用が挟まる
    assert long.best_feasible.mask.sum() < short.best_feasible.mask.sum()
    assert all(long.best_feasible.mask[cell] for cell in protected)
    assert long_reduction['adopted_after_exclusion'] >= 1
    assert long_reduction['safety_excluded_cells'] == long_reduction['poison_cells'] >= 1  # 外したセルは採用をまたいで保ち、二度数えない


def test_a_cell_that_failed_safety_stays_excluded_across_adoptions_and_is_counted_once(monkeypatch):
    """落ちたら、安全率で落ちた1セルが採用のたびに順位へ戻り、同じセルを何度も解き直している(issue #22 の終盤の無駄)。"""
    conditions, design, _ = appendage_conditions()
    protected = [(4, 0, 0), (5, 1, 1), (6, 0, 1)]  # 1つでも欠けたら安全率が足りないことにする棒の中ほどの3セル
    _fail_safety_when(monkeypatch, lambda material: not all(material[cell] for cell in protected))
    result = bs.find_best_feasible(design, conditions, max_evaluations=400, single_cell_patience=None)
    reduction = result.diagnostics['reduction']
    assert reduction['stop_reason'] == 'no_removable_cell'
    assert reduction['adopted_after_exclusion'] >= 1  # 外した後に採用が挟まっている
    assert 1 <= reduction['safety_excluded_cells'] == reduction['poison_cells'] <= len(protected)  # 各セルを一度だけ外す
    assert reduction['poison_released'] == 0  # 見立ての不合格は候補の安全率が目標以上のままなので不足分が取れず、戻さない
    assert all(result.best_feasible.mask[cell] for cell in protected)
    stage4 = result.history[1:]
    assert len({trial.mask_hash for trial in stage4}) == len(stage4)  # 採用をまたいでも同じ形を2度評価しない


def _plate_search(monkeypatch, order, verdict, *, max_evaluations, max_step_ratio=.042):
    """6x6x2 の板を、固定した順位 `order`(材料に残るセルだけ)と、候補への見立て `verdict(material)`(不合格なら
    その安全率、合格なら None)で削る。合格の安全率は 10 から合格のたびに 0.2 上がる。FEM・CAD は呼ばない。"""
    shape = (6, 6, 2)
    plate = np.ones(shape, bool)
    conditions = bar_conditions(shape=shape)
    passes = []

    def inspect(material, before, grid, **kwargs):
        material = np.asarray(material, dtype=bool)
        failed_sf = verdict(material)
        status = 'pass' if failed_sf is None else 'fail'
        rep = fake_report(material, status)
        if status == 'pass':
            passes.append(1)
            rep['checks']['safety']['safety_factor'] = 10.0 + 0.2 * len(passes)
            if kwargs.get('fields') is not None:
                kwargs['fields']['element_energy'] = np.ones(shape)
        else:
            rep['checks']['safety']['safety_factor'] = failed_sf
        return rep

    monkeypatch.setattr(bs, 'ranked_cells',
                        lambda mask, energy: np.array([c for c in order if mask.ravel()[c]], dtype=np.int64))
    _stub_islands(monkeypatch)
    monkeypatch.setattr(acceptance, 'inspect', inspect)
    return bs._reduce_volume(_seeded(plate, 10.0, conditions), conditions, max_evaluations=max_evaluations, is_cancelled=None,
                             max_step_ratio=max_step_ratio, single_cell_patience=None, target_tolerance=None)


def test_a_rejected_bite_that_returns_after_an_adoption_is_not_solved_again(monkeypatch):
    """落ちたら、採用で `rejected` を空にしていて、採用の前に落ちた形と同じ形をもう一度 FEM に掛けている。

    順位 [P, Q, a, b, …]。P と Q の両方を欠く候補だけ落ちる。要求 3 の {P,Q,a} が落ち、要求 1 の {P} が採用された後、
    倍率が戻って要求 2 の {Q,a} を抜くと、落ちた形 板−{P,Q,a} がそのまま戻る(max_step_ratio=.042 は 72 セルで 3、71 セルで 2)。"""
    shape = (6, 6, 2)
    cells = dict(P=(3, 3, 1), Q=(1, 1, 1), a=(3, 1, 1), b=(1, 3, 1))
    flat = {name: int(np.ravel_multi_index(c, shape)) for name, c in cells.items()}
    order = list(flat.values()) + [c for c in range(72) if c not in flat.values()]
    target = float(bar_conditions(shape=shape).target_safety_factor)
    result = _plate_search(monkeypatch, order,
                           lambda m: None if (m[cells['P']] or m[cells['Q']]) else target - 0.1, max_evaluations=6)
    reduction = result.diagnostics['reduction']
    stage4 = result.history[1:]
    assert reduction['repeated_candidates'] >= 1
    assert len({trial.mask_hash for trial in stage4}) == len(stage4)  # 採用をまたいで同じ形を2度評価しない
    assert [trial.outcome for trial in stage4[:2]] != ['ok', 'ok'] and any(trial.outcome == 'ok' for trial in stage4)


def test_a_poison_cell_returns_to_the_ranking_once_the_safety_factor_rose_by_its_shortfall(monkeypatch):
    """落ちたら、外したセルを戻す条件(合格形状の安全率が不足分以上に上がった)が効いていないか、戻したのに数えていない。

    P を欠く候補は 目標 − 0.1 で落ちる。合格するたびに安全率が 0.2 上がる見立てなので、P は次の採用で順位へ戻り、
    もう一度単独で解かれて再び外される。"""
    shape = (6, 6, 2)
    cells = dict(P=(3, 3, 1), a=(3, 1, 1), b=(1, 3, 1))
    flat = {name: int(np.ravel_multi_index(c, shape)) for name, c in cells.items()}
    order = list(flat.values()) + [c for c in range(72) if c not in flat.values()]
    target = float(bar_conditions(shape=shape).target_safety_factor)
    result = _plate_search(monkeypatch, order, lambda m: None if m[cells['P']] else target - 0.1, max_evaluations=8)
    reduction = result.diagnostics['reduction']
    assert reduction['poison_released'] >= 1
    assert reduction['safety_excluded_cells'] >= 2 and reduction['poison_cells'] <= 1  # P を外し、戻し、また外した(最後が戻した直後なら 0)
    singles = _single_cell_safety_failures(reduction['steps'])
    assert len(singles) >= 2 and all(step['removed_cells'] == 1 for step in singles)
    assert result.best_feasible.mask[cells['P']]


def test_a_single_cell_failure_that_is_not_a_safety_failure_is_not_counted_toward_patience(monkeypatch):
    """落ちたら、計算失敗(`unknown`)が安全率の不合格として数えられ、patience=1 で止まってしまう。"""
    conditions, design, _ = appendage_conditions()
    _safety_unknown_when(monkeypatch, lambda material: material.sum() < design.sum() - 6)
    result = bs.find_best_feasible(design, conditions, max_evaluations=400, single_cell_patience=1,
                                   evaluation_error_patience=None)
    reduction = result.diagnostics['reduction']
    assert result.termination == 'converged' and reduction['stop_reason'] == 'no_removable_cell'
    assert reduction['safety_excluded_cells'] == 0
    assert any(step['peel']['picked'] == 1 and step['outcome'] == 'rejected' for step in reduction['steps'])


def test_repeated_unsolvable_candidates_stop_the_reduction_and_keep_the_best_shape(monkeypatch):
    """落ちたら、安全率が取れない候補が続いても打ち切らず、予算を使い切るまで走っている。"""
    conditions, design, _ = appendage_conditions()
    _safety_unknown_when(monkeypatch, lambda material: material.sum() < design.sum() - 6)
    result = bs.find_best_feasible(design, conditions, max_evaluations=400, evaluation_error_patience=2)
    reduction = result.diagnostics['reduction']
    assert result.termination == 'evaluation_error'
    assert reduction['stop_reason'] == 'repeated_evaluation_error'
    assert reduction['evaluation_errors'] >= 2  # 通算。止める判断は連続回数で見る
    assert reduction['safety_excluded_cells'] == 0  # 安全率の不合格としては数えない
    assert result.best_feasible is not None and result.status == 'feasible'


def test_a_prerequisite_failure_is_not_counted_as_an_evaluation_error(monkeypatch):
    """落ちたら、幾何で落ちて安全率まで進まなかった候補(`prerequisites_failed`)まで評価不能として数えている。"""
    conditions, design, _ = appendage_conditions()
    _safety_unknown_when(monkeypatch, lambda material: material.sum() < design.sum() - 6, reason='prerequisites_failed')
    result = bs.find_best_feasible(design, conditions, max_evaluations=400, evaluation_error_patience=2)
    reduction = result.diagnostics['reduction']
    assert sum(1 for step in reduction['steps'] if step['outcome'] == 'rejected') >= 2  # スタブが効いている
    assert reduction['evaluation_errors'] == 0
    assert reduction['stop_reason'] != 'repeated_evaluation_error'


@pytest.mark.parametrize('name, value', [
    ('single_cell_patience', True), ('single_cell_patience', 0),
    ('evaluation_error_patience', True), ('evaluation_error_patience', 0),
    ('target_tolerance', True), ('target_tolerance', float('inf')),
])
def test_stage4_options_refuse_a_bool_and_an_out_of_range_value(name, value):
    """落ちたら、段階4の引数が bool(int の部分型)や範囲外の値を黙って受け付けている ——
    または、検査が段階3(全材料の FEM)の後にしか無く、全材料が不合格の題材では不正な値が黙って通る。"""
    with pytest.raises(ValueError, match=name):
        bs.find_best_feasible(np.ones((4, 2, 2), bool), bar_conditions(), max_evaluations=10, **{name: value})
    with pytest.raises(ValueError, match=name):
        bs.find_best_feasible(np.zeros((4, 2, 2), bool), bar_conditions(), max_evaluations=10, **{name: value})


def _full_material_safety_factor():
    conditions, design, _ = appendage_conditions()
    return bs.find_initial_feasible(design, conditions).best_feasible.safety_factor


def test_within_the_target_tolerance_the_reduction_stops_at_the_first_single_cell_safety_failure():
    """落ちたら、許容に入っただけで止めている(V が小さいと削れる材料を残して止まる)か、許容内で1セルが安全率で落ちても続けている。"""
    conditions, design, _ = appendage_conditions(target_safety_factor=_full_material_safety_factor() / 2)
    limit = conditions.target_safety_factor * 1.25
    stopped = bs.find_best_feasible(design, conditions, max_evaluations=400, target_tolerance=.25)
    reduction = stopped.diagnostics['reduction']
    assert stopped.termination == 'converged' and reduction['stop_reason'] == 'within_target_tolerance'
    assert reduction['target_tolerance'] == .25
    assert conditions.target_safety_factor <= stopped.best_feasible.safety_factor <= limit
    last = reduction['steps'][-1]
    assert last['outcome'] == 'rejected' and last['failed'] == ['safety'] and last['peel']['picked'] == 1
    assert last['safety_factor_before'] <= limit
    unlimited = bs.find_best_feasible(design, conditions, max_evaluations=400, target_tolerance=None)
    assert unlimited.best_feasible.mask.sum() <= stopped.best_feasible.mask.sum()


def test_a_starting_shape_within_the_target_tolerance_is_still_reduced():
    """落ちたら、出発形状(全材料)が許容内というだけで、削れる材料を残したまま全材料で止まっている。"""
    conditions, design, _ = appendage_conditions(target_safety_factor=_full_material_safety_factor() / 1.1)
    result = bs.find_best_feasible(design, conditions, max_evaluations=400, target_tolerance=.25)
    reduction = result.diagnostics['reduction']
    assert result.termination == 'converged' and reduction['steps']
    assert result.best_feasible.mask.sum() < design.sum()


def test_on_evaluation_reports_the_start_and_every_counted_candidate_including_the_one_that_stops_the_search(monkeypatch):
    """落ちたら、途中経過(アプリの進捗の材料)が評価と1対1でない、または採用後の合格形状を映していない。"""
    conditions, design, _ = appendage_conditions()
    floor = 0.75 * design.sum()
    _fail_safety_when(monkeypatch, lambda material: material.sum() < floor)
    events: list = []
    result = bs.find_best_feasible(design, conditions, max_evaluations=400, single_cell_patience=1,
                                   on_evaluation=events.append)
    assert result.diagnostics['reduction']['stop_reason'] == 'safety_at_single_cell'
    assert [event['evaluation'] for event in events] == list(range(1, result.evaluations + 1))
    assert events[0]['outcome'] == 'initial' and events[0]['cells'] == design.sum()
    assert [event['outcome'] for event in events[1:]] == [
        step['outcome'] for step in result.diagnostics['reduction']['steps']]
    assert events[-1]['outcome'] == 'rejected' and events[-1]['failed'] == ['safety']
    for before, event in zip(events, events[1:]):
        if event['outcome'] == 'accepted':
            assert (event['cells'], event['safety_factor']) == (event['candidate_cells'], event['candidate_safety_factor'])
            assert event['cells'] < before['cells']
        else:
            assert (event['cells'], event['safety_factor']) == (before['cells'], before['safety_factor'])
    assert events[-1]['cells'] == result.best_feasible.mask.sum()
    assert all(event['target_safety_factor'] == conditions.target_safety_factor for event in events)
    json.dumps(events)  # 素の値だけ(そのまま WebSocket に載る)


def test_a_computation_failure_is_not_accepted_and_does_not_stop_the_reduction(monkeypatch):
    conditions, design, _ = appendage_conditions()
    failed = []

    def first_solved_candidate(material):  # FEMまで進んだ最初の候補
        if material.sum() < design.sum() and len(failed) < 1:
            failed.append(1)
            return True
        return False

    _safety_unknown_when(monkeypatch, first_solved_candidate)
    result = bs.find_best_feasible(design, conditions, max_evaluations=200)
    steps = result.diagnostics['reduction']['steps']
    failed_step = next(step for step in steps if 'safety' in step['failed'])
    assert len(failed) == 1 and failed_step['outcome'] == 'rejected'
    assert result.best_feasible.material_mm3 < design.sum() and result.termination == 'converged'


def test_reduction_is_deterministic_for_the_same_input_and_budget():
    """同じ入力・同じ予算なら同じ結果になる(§9)。"""
    conditions, design, _ = appendage_conditions()
    first = bs.find_best_feasible(design, conditions, max_evaluations=30)
    second = bs.find_best_feasible(design, conditions, max_evaluations=30)
    assert first.evaluations == second.evaluations and first.termination == second.termination
    assert first.best_feasible.mask_hash == second.best_feasible.mask_hash

    def untimed(result):
        return [dict(step, peel={k: v for k, v in step['peel'].items() if k != 'seconds'})
                for step in result.diagnostics['reduction']['steps']]

    assert untimed(first) == untimed(second)


def test_evaluation_budget_exhaustion_keeps_the_best_feasible_found_so_far():
    """予算終了なら、その時点の `best_feasible` をそのまま返す(§4.6)。"""
    conditions, design, _ = appendage_conditions()
    result = bs.find_best_feasible(design, conditions, max_evaluations=4)
    assert result.status == 'feasible' and result.termination == 'evaluation_limit' and result.evaluations == 4
    assert result.best_feasible.material_mm3 <= design.sum()
    recheck = acceptance.inspect(result.best_feasible.mask, result.best_feasible.before_mask,
                                 conditions.grid, **conditions.inspect_kwargs(bbox_cache=None))
    assert recheck['status'] == 'pass'


def test_cancellation_after_an_improvement_preserves_the_improved_checkpoint(monkeypatch):
    """中断が体積改善の後に来たら、初期形状へ後退せず改善後の合格形状を返す(§4.6)。"""
    conditions, design, _ = appendage_conditions()
    original = bs.ranked_cells
    ranked = []  # 出発形状と、合格して進んだ形状ごとに1回ずつ呼ばれる

    def spy(mask, energy):
        ranked.append(int(mask.sum()))
        return original(mask, energy)

    monkeypatch.setattr(bs, 'ranked_cells', spy)
    result = bs.find_best_feasible(design, conditions, max_evaluations=400, is_cancelled=lambda: len(ranked) >= 2)
    assert result.termination == 'cancelled'
    assert len(ranked) == 2 and result.best_feasible.mask.sum() == ranked[1] < design.sum()
    recheck = acceptance.inspect(result.best_feasible.mask, result.best_feasible.before_mask,
                                 conditions.grid, **conditions.inspect_kwargs(bbox_cache=None))
    assert recheck['status'] == 'pass'


def test_tiny_grid_exhaustive_enumeration_matches_the_search_optimum():
    """落ちたら、微小格子(10セル・1024通り)の全列挙が示す最小体積に探索が届いていない(§9)。
    一致はこの題材が小さいことによる実測で、探索が常に大域最小を見つけるという主張ではない。"""
    conditions, design_space = bar_conditions(shape=(5, 2, 1)), np.ones((5, 2, 1), bool)  # 両端は全断面、中間は幅1で足りる
    free_cells = np.argwhere(design_space)

    brute_force_optimum = None
    for bits in itertools.product((False, True), repeat=len(free_cells)):
        mask = np.zeros(design_space.shape, bool)
        for on, (x, y, z) in zip(bits, free_cells):
            mask[x, y, z] = on
        if not mask.any():
            continue
        survivors, _ = bs.resolve_islands(mask, conditions)
        for survivor in survivors:
            report = acceptance.inspect(survivor, mask, conditions.grid, **conditions.inspect_kwargs())
            if report['status'] == 'pass':
                volume = report['volumes']['material_mm3']
                if brute_force_optimum is None or volume < brute_force_optimum:
                    brute_force_optimum = volume
    assert brute_force_optimum == 7.0

    result = bs.find_best_feasible(design_space, conditions)
    assert result.status == 'feasible' and result.termination == 'converged'
    assert result.best_feasible.material_mm3 == brute_force_optimum
    assert result.evaluations <= 20


def test_computation_failure_is_recorded_as_unknown_and_never_becomes_the_best_feasible(monkeypatch):
    shape = (4, 2, 2)
    conditions = bar_conditions(shape=shape)

    def broken(*args, **kwargs):
        raise fem.SolveResidualError('injected failure for the test')

    monkeypatch.setattr(stress, 'solve_displacement', broken)
    result = bs.find_initial_feasible(np.ones(shape, bool), conditions, max_evaluations=5)
    assert result.status == 'no_feasible_found'
    assert result.best_feasible is None
    outcomes = {trial.outcome for trial in result.history}
    assert outcomes == {'unknown'}
    assert len(result.history) == 1


def test_uncovered_anchor_nodes_stop_at_the_entry_without_evaluating(monkeypatch):
    """確定表の節点が全設計空間の材料に接しないなら、FEM0回で `boundary_mapping_failed`。
    APIを直接呼ぶ経路でも同じ判定を通る(製品入口だけの防御にしない)。"""
    shape = (4, 2, 2)
    conditions = bar_conditions(shape=shape)
    design = np.ones(shape, bool)
    design[3] = False  # 荷重面(x=4平面)の9節点はどの材料セルにも接しない
    monkeypatch.setattr(acceptance, 'inspect', lambda *a, **k: pytest.fail('入口で止まるべき経路が評価に進んだ'))
    result = bs.find_initial_feasible(design, conditions, max_evaluations=10)
    assert result.status == 'boundary_mapping_failed'
    assert result.termination == 'boundary_mapping_failed'
    assert result.evaluations == 0 and result.history == [] and result.best_feasible is None
    rows = result.diagnostics['uncovered_condition_nodes']
    assert [(r['condition'], r['nodes'], r['uncovered_nodes']) for r in rows] == [('load_face_2', 9, 9)]
    assert result.diagnostics['mapped'] is False  # 元入力(未対応付けの表)か対応付け済みかを区別する
    assert bs.find_best_feasible(design, conditions, max_evaluations=10).status == 'boundary_mapping_failed'


def test_a_failed_mapping_report_stops_the_direct_api_even_when_every_node_is_on_material(monkeypatch):
    """全節点が材料に接していても、渡された対応付けの検証が不成立ならFEM0回で止まる。"""
    conditions = bar_conditions(shape=(4, 2, 2))
    failed = dict(status='fail', faces=[dict(condition='load_face_2', status='fail', reasons=['unmapped_samples'])])
    conditions = dataclasses.replace(conditions, mapping=failed)
    monkeypatch.setattr(acceptance, 'inspect', lambda *a, **k: pytest.fail('入口で止まるべき経路が評価に進んだ'))
    result = bs.find_best_feasible(np.ones((4, 2, 2), bool), conditions, max_evaluations=10)
    assert result.status == 'boundary_mapping_failed' and result.evaluations == 0
    assert result.diagnostics['uncovered_condition_nodes'] == []
    assert result.diagnostics['mapping'] is failed


def test_the_full_material_evaluation_checks_cancellation_before_the_fem(monkeypatch):
    """落ちたら、中断済みなのに全材料のFEM(hanger 2.4mm で1回100秒超)を始めている。"""
    shape = (4, 2, 2)
    conditions = bar_conditions(shape=shape)
    monkeypatch.setattr(acceptance, 'inspect', lambda *a, **k: pytest.fail('中断済みなのに評価に進んだ'))
    result = bs.find_initial_feasible(np.ones(shape, bool), conditions, is_cancelled=lambda: True)
    assert (result.status, result.termination) == ('no_feasible_found', 'cancelled')
    assert result.evaluations == 0 and result.best_feasible is None
    assert bs.find_best_feasible(np.ones(shape, bool), conditions, is_cancelled=lambda: True).termination == 'cancelled'


def test_the_identifier_depends_on_the_fixed_nodes_not_only_on_the_face_indices():
    """同じ面添字でも節点集合が違えば別の評価条件(確定した節点・荷重・拘束をハッシュに含める)。"""
    a = bar_conditions()
    b = dataclasses.replace(a, table=Table(a.grid, {1: list(range(9)), 2: list(range(36, 44))}))  # 荷重面の節点を1つ減らす
    assert bs.evaluation_identifier(a) != bs.evaluation_identifier(b)
    assert bs.evaluation_identifier(a) == bs.evaluation_identifier(bar_conditions())


def test_removing_the_scaffold_under_a_fixed_anchor_is_infeasible_and_never_renormalised():
    """初期合格後に確定アンカーの足場を削ると不合格になり、合力の再正規化で合格へ戻らない。"""
    shape = (4, 2, 2)
    conditions = bar_conditions(shape=shape)
    full = np.ones(shape, bool)
    stripped = full.copy()
    stripped[3, 0, 0] = False  # 荷重面の角の節点(x=4,y=0,z=0)が接する唯一の材料セル
    check, _, _ = acceptance.condition_check(stripped, conditions.table, conditions.fixed_faces,
                                             conditions.load_faces, conditions.forces)
    load_row = next(row for row in check['faces'] if row['condition'] == 'load_face_2')
    assert check['status'] == 'fail' and load_row['lost_nodes'] == 1
    assert load_row['resultant_n'] == pytest.approx(list(conditions.forces[0]))  # 合力は保存されているのに不合格
    assert bs.resolve_islands(stripped, conditions)[0] == []  # 島処理でも成立しない
    report = acceptance.inspect(stripped, full, conditions.grid, **conditions.inspect_kwargs())
    assert report['status'] == 'fail' and report['checks']['conditions']['status'] == 'fail'

    result = bs.find_best_feasible(full, conditions, max_evaluations=40)
    assert result.status == 'feasible'
    live = acceptance.active_nodes(result.best_feasible.mask).ravel()
    for face in conditions.fixed_faces + conditions.load_faces:
        assert live[conditions.table.node_ids([face])].all()  # 採用マスクは確定節点を全て支えている


def partial_region_conditions():
    """`appendage_conditions` と同じ棒だが、保持CADが格子の一部にだけ掛かり(境界のセルが部分的に重なる)、除外CADは
    設計空間の切り欠きの中にある。除外に掛かるセルは設計空間から抜いておく(製品の `design_space.derive` と同じ。
    全材料が除外で落ちると段階3は形を探さない)。"""
    conditions, design, appendage = appendage_conditions()
    keep = Pos(5.3, 1.1, 0.9) * Box(7.4, 1.8, 1.6)
    hole = Pos(5.2, 3.4, 1.0) * Box(2.3, 0.9, 1.7)
    design = design.copy()
    design[4:7, 2:4, :] = False  # hole の x∈[4.05, 6.35]・y∈[2.95, 3.85] に掛かるセル
    return dataclasses.replace(conditions, preserves=[('keep', keep.wrapped)], excludes=[('hole', hole.wrapped)]), design


def test_the_search_is_identical_with_and_without_the_region_overlap_precompute():
    """落ちたら、前計算が直接のブーリアンと違う合否を返して探索の道筋を変えている。"""
    conditions, design = partial_region_conditions()
    with_precompute = bs.find_best_feasible(design, conditions, max_evaluations=60)
    without = bs.find_best_feasible(design, dataclasses.replace(conditions, region_overlaps=None), max_evaluations=60)
    assert with_precompute.best_feasible.mask_hash == without.best_feasible.mask_hash
    assert (with_precompute.termination, with_precompute.evaluations) == (without.termination, without.evaluations)
    assert [(t.operation, t.mask_hash, t.outcome, t.checks) for t in with_precompute.history] == [
        (t.operation, t.mask_hash, t.outcome, t.checks) for t in without.history]
    for name in ('preserve', 'exclude'):
        assert with_precompute.best_feasible.report['checks'][name]['status'] == without.best_feasible.report['checks'][name]['status']


def test_candidates_use_the_precompute_and_only_the_adopted_shape_is_rechecked_with_the_direct_boolean(monkeypatch):
    """候補の評価は前計算から引き、ブーリアンで測るのは段階3の全材料の評価と段階4の採用形状の再検査だけ。"""
    conditions, design = partial_region_conditions()
    direct_calls, inspects = [], []
    original_direct, original_inspect = acceptance.intersection_volume, acceptance.inspect
    monkeypatch.setattr(acceptance, 'intersection_volume',
                        lambda *args, **kwargs: direct_calls.append(1) or original_direct(*args, **kwargs))
    monkeypatch.setattr(acceptance, 'inspect',
                        lambda *args, **kwargs: inspects.append(1) or original_inspect(*args, **kwargs))
    result = bs.find_best_feasible(design, conditions, max_evaluations=60)
    assert result.status == 'feasible' and len(inspects) > 2
    # 検査1回の保持1本+除外1本 = 2回。段階3の全材料で1回、段階4の最終再検査で保持・除外だけを1回。
    assert len(direct_calls) == 4, (len(direct_calls), len(inspects))
    assert result.diagnostics['reduction']['final_region_recheck'] in ('pass', 'not_needed')
    assert conditions.region_overlaps is not None and len(conditions.region_overlaps) == 2


def test_inspect_kwargs_drop_the_precompute_together_with_the_bbox_cache():
    """`bbox_cache` を外した再検査(§3)は、前計算も外して直接のブーリアンで取り直す。"""
    conditions, _ = partial_region_conditions()
    assert conditions.inspect_kwargs()['region_overlaps'] is conditions.region_overlaps
    assert conditions.inspect_kwargs(bbox_cache=None)['region_overlaps'] is None


def test_the_final_region_recheck_replaces_the_report_rows_and_falls_back_to_the_seed_on_failure(monkeypatch):
    conditions, design = partial_region_conditions()
    result = bs.find_best_feasible(design, conditions, max_evaluations=60)
    best = result.best_feasible
    seed = dataclasses.replace(best)  # 別のオブジェクト = 段階4が更新した形状
    assert bs._recheck_regions_without_precompute(best, best, conditions) == (best, 'not_needed')
    rechecked, verdict = bs._recheck_regions_without_precompute(best, seed, conditions)
    assert verdict == 'pass' and rechecked.mask is best.mask
    assert rechecked.report['checks']['preserve']['status'] == best.report['checks']['preserve']['status']
    assert rechecked.report is not best.report  # 元の報告は書き換えない

    monkeypatch.setattr(acceptance, 'region_check', lambda *a, **k: dict(status='fail', bodies=[], rule='x'))
    fallback, verdict = bs._recheck_regions_without_precompute(best, seed, conditions)
    assert fallback is seed and verdict == 'failed:preserve:fail'


def test_every_adopted_shape_is_kept_in_order_from_the_stage3_shape_to_the_best():
    """落ちたら、完成形検査に落ちたときに戻る先(探索の合格形の並び)が欠けているか、順・中身が合格形とずれている。"""
    conditions, design, _ = appendage_conditions()
    result = bs.find_best_feasible(design, conditions, max_evaluations=200)
    accepted = result.accepted
    steps = result.diagnostics['reduction']['steps']
    adopted = [step['trials'][0] for step in steps if step.get('outcome') == 'accepted']
    assert len(accepted) >= 2 and [shape.trial_index for shape in accepted][1:] == adopted
    assert np.array_equal(accepted[-1].mask(), result.best_feasible.mask)
    assert accepted[-1].safety_factor == result.best_feasible.safety_factor
    assert all(a.material_mm3 >= b.material_mm3 for a, b in zip(accepted, accepted[1:]))


def test_a_failed_final_region_recheck_ends_the_reduction_on_the_verified_stage3_shape(monkeypatch):
    conditions, design = partial_region_conditions()
    initial = bs.find_initial_feasible(design, conditions, max_evaluations=60)
    monkeypatch.setattr(bs, '_recheck_regions_without_precompute', lambda best, seed, conditions: (seed, 'failed:exclude:fail'))
    result = bs._reduce_volume(initial, conditions, max_evaluations=60, is_cancelled=None)
    assert result.best_feasible is initial.best_feasible and result.termination == 'evaluation_error'
    assert result.diagnostics['reduction']['final_region_recheck'] == 'failed:exclude:fail'
    assert [shape.trial_index for shape in result.accepted] == [initial.best_feasible.trial_index]  # 戻した先より後は持たない


def test_the_best_feasible_mask_is_read_only_and_survives_later_reduction():
    """最良合格のマスクは読み取り専用で、後続の削減・失敗がその内容とハッシュを変えない。"""
    conditions, design, _ = appendage_conditions()
    result = bs.find_best_feasible(design, conditions, max_evaluations=6)
    best = result.best_feasible
    assert not best.mask.flags.writeable and not best.before_mask.flags.writeable
    assert acceptance.material_sha256(best.mask) == best.mask_hash
    snapshot = best.mask.copy()
    later = bs._reduce_volume(result, conditions, max_evaluations=200, is_cancelled=None)
    assert later.best_feasible.material_mm3 <= best.material_mm3
    assert np.array_equal(best.mask, snapshot) and acceptance.material_sha256(best.mask) == best.mask_hash
