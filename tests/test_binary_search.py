import hashlib

import numpy as np
import pytest
from build123d import Box, Pos

from duocad.topopt import acceptance, anchor, fem, stress, voxelize
from duocad.topopt import binary_search as bs


class Table:
    """`anchor.AnchorTable` と同じ面/節点インタフェースの軽量な代用(test_pre_smoothing.py と同じ形)。"""

    def __init__(self, grid, faces, empty=()):
        self.node_grid = anchor.NodeGrid(grid)
        self.faces = faces
        self._empty = set(empty)

    def node_ids(self, faces):
        face = faces[0]
        if face in self._empty:
            return np.zeros(0, dtype=int)
        return np.array(self.faces[face], dtype=int)

    def fixed_dofs(self, faces):
        ids = self.node_ids(faces)
        return (ids[:, None] * 3 + np.arange(3)).ravel()

    def load_vector(self, faces, force):
        out = np.zeros(self.node_grid.ndof)
        ids = self.node_ids(faces)
        out.reshape(-1, 3)[ids] = np.asarray(force) / len(ids)
        return out

    def empty_faces(self):
        return sorted(self._empty)


def bar_conditions(shape=(4, 2, 2), *, fixed=(1,), load=(2,), empty=(), forces=((0., 0., -1.),),
                    keep_box=None, target_safety_factor=.001, solver='direct'):
    """まっすぐな棒(4x2x2既定)。固定面=x最小平面(節点0..8)、荷重面=x最大平面。

    `keep_box` を省略すると、格子全体を覆う箱にする(保持条件を常に自明に満たし、
    島処理の判定を固定面・荷重面だけに絞れるようにする補助)。
    """
    grid = voxelize.Grid((0., 0., 0.), 1., shape)
    ny1, nz1 = shape[1] + 1, shape[2] + 1
    x_max_node = shape[0] * ny1 * nz1
    faces = {1: list(range(ny1 * nz1)), 2: list(range(x_max_node, x_max_node + ny1 * nz1))}
    table = Table(grid, faces, empty=empty)
    if keep_box is None:
        cx, cy, cz = (shape[0] + .0) / 2, shape[1] / 2, shape[2] / 2
        keep_box = Pos(cx, cy, cz) * Box(shape[0] + 2., shape[1] + 2., shape[2] + 2.)
    return bs.SearchConditions(
        grid=grid, table=table, fixed_faces=fixed, load_faces=load, forces=forces,
        preserves=[('keep', keep_box.wrapped)], excludes=[],
        target_safety_factor=target_safety_factor, solver=solver,
    )


# ---------------------------------------------------------------------------
# §4.3 候補生成: 決定的な系列とブロック演算
# ---------------------------------------------------------------------------
def test_block_sizes_are_the_deterministic_1_2_4_series_capped_at_the_longest_edge():
    assert bs.block_sizes((4, 2, 2)) == (1, 2, 4)
    assert bs.block_sizes((9, 1, 1)) == (1, 2, 4, 8)
    assert bs.block_sizes((1, 1, 1)) == (1,)


def test_repair_candidates_delete_only_removes_existing_material_and_add_only_touches_the_face():
    current = np.zeros((4, 1, 1), bool)
    current[0] = current[1] = True  # 材料は先頭2セルだけ
    design_space = np.ones((4, 1, 1), bool)  # 設計空間は全体
    ops = list(bs.repair_candidates(current, design_space, 1))
    kinds = {kind for kind, _ in ops}
    assert kinds == {'delete', 'add'}  # このブロック大きさでは置換の材料が無い場所もある
    deletes = [mask for kind, mask in ops if kind == 'delete']
    adds = [mask for kind, mask in ops if kind == 'add']
    # 削除は既存材料からしか削れない(2セルぶんの削除候補しかない)
    assert len(deletes) == 2
    for mask in deletes:
        assert not np.any(mask & ~current)  # 削除は材料を増やさない
    # 追加は設計空間内で既存材料と面で接するセルにしか起きない(§4.3)。
    # 現在材料は index0,1 なので、面で接するのは index2 だけ(index3 はまだ接していない)。
    assert len(adds) == 1
    added = adds[0]
    assert np.all(added[:2]) and bool(added[2]) and not bool(added[3])
    assert not np.array_equal(added, current)


def test_repair_candidates_replace_pairs_a_removal_with_an_addition_in_one_candidate():
    current = np.array([True, True, False, False]).reshape(4, 1, 1)
    design_space = np.ones((4, 1, 1), bool)
    # size=4: 唯一のブロックが配列全体を覆うので、削除できる材料と追加できる空きの
    # どちらも同じブロックに入り、置換候補が1つ生成される。
    ops = list(bs.repair_candidates(current, design_space, 4))
    replaces = [mask for kind, mask in ops if kind == 'replace']
    assert len(replaces) == 1
    replaced = replaces[0]
    assert replaced.sum() > 0
    # 置換候補は削除ブロックと追加ブロックの両方の効果を1候補にまとめている
    assert not np.array_equal(replaced, current)


def test_block_starts_truncate_at_the_array_boundary_without_overlap():
    starts = list(bs._block_starts((5, 1, 1), 2))
    assert starts == [(0, 0, 0), (2, 0, 0), (4, 0, 0)]  # 最後のブロックは幅1に切り詰まる


# ---------------------------------------------------------------------------
# §4.4 島処理: 最大成分を選ばず、条件を単独で満たす成分だけを残す
# ---------------------------------------------------------------------------
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


def test_resolve_islands_keeps_the_component_that_alone_satisfies_every_condition():
    shape = (4, 2, 2)
    conditions = bar_conditions(shape=shape)
    candidate = np.ones(shape, bool)
    survivors, diagnostics = bs.resolve_islands(candidate, conditions)
    assert len(survivors) == 1
    assert np.array_equal(survivors[0], candidate)
    assert diagnostics['components'] == 1
    assert diagnostics['removed_islands'] == []


# ---------------------------------------------------------------------------
# 統合: find_initial_feasible
# ---------------------------------------------------------------------------
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
    grid = voxelize.Grid((0., 0., 0.), 1., shape)
    ny1, nz1 = shape[1] + 1, shape[2] + 1
    bar_end_node = 4 * ny1 * nz1
    faces = {1: list(range(ny1 * nz1)), 2: list(range(bar_end_node, bar_end_node + ny1 * nz1))}
    table = Table(grid, faces)
    keep_box = Pos(2., 1., 1.) * Box(8., 4., 4.)  # 全体を覆う箱(保持条件を常に自明に満たす)
    conditions = bs.SearchConditions(
        grid=grid, table=table, fixed_faces=(1,), load_faces=(2,), forces=((0., 0., -1.),),
        preserves=[('keep', keep_box.wrapped)], excludes=[], target_safety_factor=.001, solver='direct',
    )
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
    conditions = bar_conditions(shape=shape)
    # 保持条件を、設計空間から遠く離れた位置(絶対に重ならない)に差し替える。
    unreachable = Pos(100., 100., 100.) * Box(1., 1., 1.)
    conditions = bs.SearchConditions(
        grid=conditions.grid, table=conditions.table, fixed_faces=conditions.fixed_faces,
        load_faces=conditions.load_faces, forces=conditions.forces,
        preserves=[('keep', unreachable.wrapped)], excludes=[],
        target_safety_factor=conditions.target_safety_factor, solver=conditions.solver,
    )
    result = bs.find_initial_feasible(np.ones(shape, bool), conditions, max_evaluations=10)
    assert result.status == 'input_contradiction'
    assert result.evaluations == 0
    assert result.best_feasible is None


def test_full_material_still_fails_safety_is_not_laundered_into_a_pass():
    """全材料でも応力不足の例: どの候補を試しても目標安全率に届かない場合、
    「探索し尽くして見つからない」であって「合格に化ける」ではない。"""
    shape = (4, 2, 2)
    conditions = bar_conditions(shape=shape, target_safety_factor=1e12)
    result = bs.find_initial_feasible(np.ones(shape, bool), conditions, max_evaluations=25)
    assert result.status == 'no_feasible_found'
    assert result.best_feasible is None
    assert result.termination in ('neighbourhood_exhausted', 'evaluation_limit')
    assert len(result.history) >= 2  # 初期候補だけで諦めていない(修復候補を実際に試した)
    assert all(trial.outcome != 'ok' for trial in result.history)


def test_no_feasible_found_records_history_and_does_not_return_a_dangling_best():
    shape = (4, 2, 2)
    conditions = bar_conditions(shape=shape, target_safety_factor=1e12)
    result = bs.find_initial_feasible(np.ones(shape, bool), conditions, max_evaluations=8)
    assert result.status == 'no_feasible_found'
    assert result.best_feasible is None
    assert result.evaluations <= 8
    assert len(result.history) >= 1


def test_add_and_replace_repairs_never_violate_the_before_superset_contract(monkeypatch):
    """`acceptance.inspect` は `material ⊆ before` を要求する(材料は常に島除去前の部分集合)。

    add/replace は親の材料の外へ広がりうるので、`before` に親候補を使うとここが破れる
    (frontier の各候補は自分自身を`before`にすること、という §2 の契約の回帰確認)。
    """
    shape = (4, 2, 2)
    conditions = bar_conditions(shape=shape, target_safety_factor=1e12)
    calls = []
    original = acceptance.inspect

    def spy(material, before, grid, **kwargs):
        calls.append((np.asarray(material, dtype=bool).copy(), np.asarray(before, dtype=bool).copy()))
        return original(material, before, grid, **kwargs)

    monkeypatch.setattr(acceptance, 'inspect', spy)
    result = bs.find_initial_feasible(np.ones(shape, bool), conditions, max_evaluations=30)
    assert result.status == 'no_feasible_found'  # target が非現実的なので、例外を出さず探索し尽くす
    assert calls  # 実際にadd/replaceを含む多数の候補が評価されたことの確認
    for material, before in calls:
        assert not np.any(material & ~before), '材料が島除去前(before)の外へ出ている'


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
    # 解法変更の再試行(§4.5)が記録されている(同じマスクハッシュで別の解法)
    retried = [t for t in result.history if t.operation.startswith('solver_retry:')]
    assert retried and retried[0].mask_hash == result.history[0].mask_hash
