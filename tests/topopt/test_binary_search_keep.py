"""探索は保持セル(`SearchConditions.keep_cells`)を、どの操作でも空にしない(`docs/TOPOPT_PRESERVE.md`)。"""
import dataclasses

import numpy as np
import pytest

from duocad.topopt import acceptance
from duocad.topopt import binary_search as bs
from topopt_support import (Table, appendage_conditions, assert_min_width_invariants, assert_peel_invariants,
                            bar_conditions, slim_bar_conditions)


def _column(m):
    """z 方向の柱(m=1 は 1×1×12、m=3 は 3×3×12)。荷重経路の根元は z=0 の面で、z=6 の層を抜くと z=7..11 が島になる。"""
    side = 1 if m == 1 else 3
    shape = (side, side, 12)
    mask = np.ones(shape, bool)
    node_shape = tuple(n + 1 for n in shape)
    anchors = np.array([np.ravel_multi_index((i, j, 0), node_shape) for i in range(side + 1) for j in range(side + 1)],
                       dtype=np.int64)
    return mask, anchors, np.array([np.ravel_multi_index((0, side // 2, 6), shape)])


@pytest.mark.parametrize('m', [1, 3])
def test_peel_refuses_a_move_that_cuts_a_protected_cell_off_as_an_island(m):
    """落ちたら、保持セルを含む島が消されている(対照: 保護なしなら同じ手が通り、島 z=7..11 が消える)。"""
    mask, anchors, order = _column(m)
    kwargs = dict(min_width_cells=m)
    free, stats = bs.peel(mask, order, 1, anchors, **kwargs)
    assert stats.picked == 1 and stats.islands == 5 * mask.shape[0] * mask.shape[1] and not free[:, :, 7:].any()

    protected = np.zeros_like(mask)
    protected[:, :, 10] = True
    kwargs['exempt'] = protected if m > 1 else None
    kept, stats = bs.peel(mask, order, 1, anchors, protected=protected, **kwargs)
    assert np.array_equal(kept, mask) and (stats.picked, stats.islands, stats.skipped_connectivity) == (0, 0, 1)


def test_peel_skips_a_protected_cell_at_the_head_of_the_order_and_takes_the_next():
    """落ちたら、順位の先頭の保持セルが抜かれている(対照: 保護なしなら先頭のセルが抜ける)。"""
    shape = (5, 5, 5)
    anchors = bs._anchor_node_ids(bar_conditions(shape=shape))
    edge = int(np.ravel_multi_index((2, 4, 4), shape))
    next_cell = int(np.ravel_multi_index((2, 4, 3), shape))
    order = np.array([edge, next_cell])
    full = np.ones(shape, bool)
    assert bs.peel(full, order, 1, anchors)[1].picked_cells.tolist() == [edge]
    protected = np.zeros(shape, bool)
    protected[2, 4, 4] = True
    kept, stats = bs.peel(full, order, 1, anchors, protected=protected)
    assert kept[2, 4, 4] and not kept[2, 4, 3] and stats.picked_cells.tolist() == [next_cell]


@pytest.mark.parametrize('m', [1, 2, 3])
def test_peel_keeps_every_protected_cell_and_every_invariant_over_random_rounds(m):
    """落ちたら、保護セルが順位の並びにかかわらず抜けている、または保護セルの周りで不変条件が壊れている。"""
    rng = np.random.default_rng(20260921 + m)
    moves = 0
    for _ in range(8):
        shape = tuple(int(x) for x in rng.integers(6, 10, size=3))
        protected = rng.random(shape) < 0.15
        conditions = bar_conditions(shape=shape)
        anchors = bs._anchor_node_ids(conditions)
        mask = np.ones(shape, bool)
        for _ in range(5):
            order = np.concatenate([np.flatnonzero(protected), rng.permutation(mask.size)])
            after, stats = bs.peel(mask, order, int(rng.integers(1, 1 + int(mask.sum()) // 3)), anchors,
                                   min_width_cells=m, exempt=protected if m > 1 else None, protected=protected)
            assert after[protected].all()
            assert not set(stats.picked_cells.tolist()) & set(np.flatnonzero(protected).tolist())
            assert_peel_invariants(after, conditions)
            if m > 1:
                assert_min_width_invariants(after, m, protected)
            moves += stats.picked
            mask = after
    assert moves > 50


def test_peel_with_a_protected_cell_that_a_cube_move_could_take_needs_it_exempt():
    """m>1 で保護セルが `exempt` に入っていないと、巻き添えに入りうる。呼び出しの誤りとして断る。"""
    protected = np.zeros((4, 4, 4), bool)
    protected[0, 0, 0] = True
    with pytest.raises(ValueError, match='exempt'):
        bs.peel(np.ones((4, 4, 4), bool), np.arange(64), 1, np.zeros(0, dtype=np.int64), min_width_cells=2,
                protected=protected)


def _bar_and_island(*, keep_cells):
    """棒(10x2x2)と、どの条件にも触れない孤立した1セル (2,3,3)。固定面・荷重面の節点は棒の断面だけ。"""
    shape = (10, 4, 4)
    keep = np.zeros(shape, bool)
    for cell in keep_cells:
        keep[cell] = True
    conditions = dataclasses.replace(slim_bar_conditions(), keep_cells=keep)
    design = np.zeros(shape, bool)
    design[:, :2, :2] = True
    design[2, 3, 3] = True
    return design, conditions


@pytest.mark.parametrize('keep_cells, survivors, removed_keep', [
    ((), 1, [0]),
    (((5, 1, 1),), 1, [0]),
    (((2, 3, 3),), 0, [0, 1]),
    (((5, 1, 1), (2, 3, 3)), 0, [1, 1]),
])
def test_resolve_islands_returns_only_a_component_that_holds_every_keep_cell(keep_cells, survivors, removed_keep):
    """落ちたら、保持セルが島として消されている(条件を満たす棒が保持セルを欠いても生存成分になっている)。"""
    design, conditions = _bar_and_island(keep_cells=keep_cells)
    found, diagnostics = bs.resolve_islands(design, conditions)
    assert len(found) == survivors and diagnostics['components'] == 2
    assert all(component[conditions.keep_cells].all() for component in found)
    assert sorted(island['keep_cells'] for island in diagnostics['removed_islands']) == removed_keep


def test_a_full_material_that_splits_the_keep_cells_between_components_is_infeasible_and_says_so():
    """落ちたら、保持セルを島として消して合格させている、または理由が診断に出ない。"""
    design, conditions = _bar_and_island(keep_cells=[(5, 1, 1), (2, 3, 3)])
    result = bs.find_initial_feasible(design, conditions)
    assert (result.status, result.termination) == ('no_feasible_found', 'full_material_infeasible')
    attempt = result.diagnostics['full_material']['attempts'][0]
    assert attempt['failed'] == ['no_component']
    assert sorted(i['keep_cells'] for i in result.diagnostics['full_material']['islands']['removed_islands']) == [1, 1]
    assert result.history[0].outcome == 'no_component'


def test_keep_cells_outside_the_design_space_are_refused_at_entry():
    design, conditions = _bar_and_island(keep_cells=[(9, 3, 3)])
    with pytest.raises(ValueError, match='設計空間の外'):
        bs.find_initial_feasible(design, conditions)


def test_evaluating_a_candidate_that_lacks_a_keep_cell_raises_instead_of_repainting_it():
    """落ちたら、評価の入口の不変条件が無い(保持セルを欠いた形が黙って評価に回る)。"""
    design, conditions = _bar_and_island(keep_cells=[(5, 1, 1)])
    candidate = design.copy()
    candidate[5, 1, 1] = False
    history: list = []
    with pytest.raises(RuntimeError, match='保持セル'):
        bs._evaluate_survivor(candidate, design, conditions, parent=None, operation='x', history=history)
    assert history == []


def _search_with_keep(monkeypatch, m, keep_cells):
    conditions, design, appendage = appendage_conditions()
    keep = np.zeros(design.shape, bool)
    for cell in keep_cells:
        keep[cell] = True
    conditions = dataclasses.replace(conditions, keep_cells=keep)
    seen = []
    real = acceptance.inspect

    def spy(material, *args, **kwargs):
        seen.append(np.array(material, copy=True))
        return real(material, *args, **kwargs)

    monkeypatch.setattr(acceptance, 'inspect', spy)
    result = bs.find_best_feasible(design, conditions, max_evaluations=300, min_width_cells=m)
    return result, keep, seen, appendage


@pytest.mark.parametrize('m', [1, 2])
def test_the_search_keeps_every_keep_cell_in_every_evaluated_candidate_and_the_adopted_shape(monkeypatch, m):
    """落ちたら、探索のどこかの経路(順位・島の除去)が保持セルを空にして、その形を評価している。
    保持セルは荷重の通らない付属部分の先端 (4,3,0) と、棒の途中 (5,0,1)。"""
    result, keep, seen, appendage = _search_with_keep(monkeypatch, m, [(4, 3, 0), (5, 0, 1)])
    best = result.best_feasible.mask
    assert result.status == 'feasible' and best[keep].all() and len(seen) >= result.evaluations
    assert all(candidate[keep].all() for candidate in seen)
    assert (best & appendage).any() and (best & appendage).sum() < appendage.sum()
    assert acceptance.connectivity_check(best)['components'] == 1
    reduction = result.diagnostics['reduction']
    assert reduction['geometry_rejections'] == [] and reduction['final_region_recheck'] == 'pass'
    assert sum(step['peel']['skipped_connectivity'] for step in reduction['steps']) > 0


def test_the_search_with_min_width_three_keeps_a_keep_cell_in_a_side_block(monkeypatch):
    """落ちたら、m=3 の段階4が保護セルを渡していない(3×3×4 の付属部の保持セルが削られる)。対照: 保持セルが無ければ付属部は全部削られる。"""
    shape = (14, 6, 3)
    conditions = bar_conditions(shape=shape)
    nodes = lambda x: [(x * 7 + y) * 4 + z for y in range(4) for z in range(4)]
    conditions = dataclasses.replace(conditions, table=Table(conditions.grid, {1: nodes(0), 2: nodes(14)}))
    design = np.zeros(shape, bool)
    design[:, :3, :] = True
    block = np.zeros(shape, bool)
    block[5:9, 3:, :] = True
    design |= block
    keep = np.zeros(shape, bool)
    keep[7, 5, 1] = True

    without = bs.find_best_feasible(design, conditions, max_evaluations=300, min_width_cells=3)
    assert not (without.best_feasible.mask & block).any()

    seen = []
    real = acceptance.inspect
    monkeypatch.setattr(acceptance, 'inspect', lambda material, *a, **k: seen.append(np.array(material)) or real(material, *a, **k))
    result = bs.find_best_feasible(design, dataclasses.replace(conditions, keep_cells=keep), max_evaluations=300,
                                   min_width_cells=3)
    best = result.best_feasible.mask
    assert result.status == 'feasible' and best[keep].all() and all(candidate[keep].all() for candidate in seen)
    assert (best & block).any()
    assert_min_width_invariants(best, 3, keep)
    reduction = result.diagnostics['reduction']
    assert reduction['geometry_rejections'] == []
    assert sum(step['peel']['skipped_connectivity'] for step in reduction['steps']) > 0
