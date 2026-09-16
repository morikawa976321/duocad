import hashlib
import itertools
from collections import deque

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


def detour_conditions():
    """5x2x1の細長い設計空間。両端の列(x=0, x=4)が固定面・荷重面で、全断面(y=0,1)が
    活性化していないと条件2(単一成分)・条件5(荷重欠落なし)を満たせない(§2)。
    中間(x=1..3)は幅1セルでもつながっていれば足りる(`target_safety_factor`を小さくし、
    安全率で形状が決まらないようにしてある——このフィクスチャの主眼は幾何と体積)。
    """
    shape = (5, 2, 1)
    return bar_conditions(shape=shape, target_safety_factor=.001, solver='direct'), np.ones(shape, bool)


def detour_initial_mask():
    """`detour_conditions` の設計空間の中で、両端は最小(条件を満たす最小断面)だが、
    中間の経路が遠回り(4セル)になっている合格形状(計8セル)。

    **この形状はどのブロック削除を試しても不合格になる**——
    `test_pure_deletion_cannot_improve_on_the_detour_shape` が両端セル・中間の関節点の
    どれを単独削除しても不合格になることと、削除だけを辿るBFS(部分集合256通り)全体でも
    改善しないことを検証する。このテストが崩れたら、以降のテストが前提にしているこの
    フィクスチャの性質(=削除だけでは詰まる)が崩れているので、他のテストの結果も無意味になる。
    両端の各セルは固定面・荷重面の全節点活性化に必須、中間の4セルは経路上の関節点
    (どれか1つを消すと固定面側と荷重面側が分離し、どちらの成分も両方の面に触れない)。
    真の最小(7セル、直線経路)は `(3, 0)` を新たに要求する——このセルは初期形状に無く
    (初期形状は同じx=3列で`(3, 1)`を使っている)、削除だけでは絶対に到達できない(§4.3「置換」)。
    """
    shape = (5, 2, 1)
    mask = np.zeros(shape, bool)
    mask[0, 0] = mask[0, 1] = True  # 固定面側の端(x=0列、全断面)
    mask[4, 0] = mask[4, 1] = True  # 荷重面側の端(x=4列、全断面)
    mask[1, 0] = True
    mask[2, 0] = True
    mask[2, 1] = True
    mask[3, 1] = True  # 中間の遠回り経路(4セル、直線なら2セルで足りる)
    return mask


def _seed_search_result(mask, conditions):
    """`mask` を唯一の合格記録として持つ `SearchResult`(段階3を経ずに段階4を単体で検証するため)。"""
    report = acceptance.inspect(mask, mask, conditions.grid, **conditions.inspect_kwargs(bbox_cache=None))
    assert report['status'] == 'pass'
    seed = bs.Trial(index=0, parent=None, operation='seed', mask_hash=report['material_sha256'],
                    before_mm3=report['volumes']['before_mm3'], material_mm3=report['volumes']['material_mm3'],
                    removed_islands_mm3=0.0, outcome='ok',
                    safety_factor=report['checks']['safety']['safety_factor'])
    best = bs.FeasibleModel(mask=mask, before_mask=mask, report=report, trial_index=0)
    return bs.SearchResult(status='feasible', termination='seed', best_feasible=best, history=[seed],
                           evaluations=1, conditions_identifier=bs.evaluation_identifier(conditions))


# ---------------------------------------------------------------------------
# §8 段階4: 削除・追加・置換による体積削減
# ---------------------------------------------------------------------------
def test_pure_deletion_cannot_improve_on_the_detour_shape():
    """削除だけでは詰まる題材であることの確認(§9)。detour形状はどの削除でも不合格になる
    ——単独ブロック削除も、削除だけを辿るBFS(何手先まで許しても)も改善しない。
    このテストが崩れたらフィクスチャの前提が壊れているので、以降のテストの結果は無意味。
    """
    conditions, design_space = detour_conditions()
    initial_mask = detour_initial_mask()
    sizes = tuple(reversed(bs.block_sizes(design_space.shape)))

    # 単独削除: どのサイズ・位置でも不合格
    for size in sizes:
        for kind, mask in bs.repair_candidates(initial_mask, design_space, size):
            if kind != 'delete':
                continue
            survivors, _ = bs.resolve_islands(mask, conditions)
            for survivor in survivors:
                report = acceptance.inspect(survivor, mask, conditions.grid, **conditions.inspect_kwargs())
                assert report['status'] != 'pass'

    # 削除だけを辿るBFS: 初期形状の部分集合(2**8=256通り)を尽くしても改善しない
    visited, frontier = set(), deque([initial_mask])
    best_mm3 = float(initial_mask.sum())
    steps = 0
    while frontier:
        current = frontier.popleft()
        key = hashlib.sha256(np.ascontiguousarray(current).tobytes()).hexdigest()
        if key in visited:
            continue
        visited.add(key)
        steps += 1
        survivors, _ = bs.resolve_islands(current, conditions)
        for survivor in survivors:
            report = acceptance.inspect(survivor, current, conditions.grid, **conditions.inspect_kwargs())
            if report['status'] == 'pass':
                best_mm3 = min(best_mm3, report['volumes']['material_mm3'])
            if report['status'] != 'unknown':
                for size in sizes:
                    for kind, mask in bs.repair_candidates(survivor, design_space, size):
                        if kind == 'delete':
                            frontier.append(mask)
        if not survivors:
            for size in sizes:
                for kind, mask in bs.repair_candidates(current, design_space, size):
                    if kind == 'delete':
                        frontier.append(mask)
    assert steps == 256  # 初期形状(8セル)の部分集合を全て尽くした(2**8)
    assert best_mm3 == 8.0  # 一切改善しない


def test_add_and_replace_escape_the_deletion_deadlock_the_pure_deletion_test_hits():
    """削除だけでは詰まる detour 形状から、追加・置換を使うと実際に体積が減る(§9、段階4の核心)。

    真の最小(7セル、直線経路)は全列挙(brute force、2**10=1024通り)でも同じ値になる
    ことを別テストで確認する。ここでは既定の frontier 設定(§4.3 追記参照)で
    実際に7.0まで縮むことだけを確認する。
    """
    conditions, design_space = detour_conditions()
    initial = _seed_search_result(detour_initial_mask(), conditions)
    result = bs._reduce_volume(initial, design_space, conditions, max_evaluations=1500,
                               frontier_capacity=bs.DEFAULT_FRONTIER_CAPACITY,
                               max_consecutive_adds=bs.DEFAULT_MAX_CONSECUTIVE_ADDS, is_cancelled=None)
    assert result.status == 'feasible'
    assert result.best_feasible.material_mm3 < 8.0
    assert result.best_feasible.material_mm3 == 7.0
    # best_feasible はキャッシュ無しの最終再検査を経ている(§3)ので、それ自体が独立に合格する
    recheck = acceptance.inspect(result.best_feasible.mask, result.best_feasible.before_mask,
                                 conditions.grid, **conditions.inspect_kwargs(bbox_cache=None))
    assert recheck['status'] == 'pass'
    # 少なくとも1回は'add'を経由している(そうでなければ削除だけで着いた矛盾になる)
    assert any(trial.operation == 'add' for trial in result.history)


def test_best_feasible_promotion_is_reproducible_from_history():
    """`best_feasible`の採否は履歴から再現できる(§9「記録から候補と採否を再現できる」)。

    `_is_smaller_feasible`と同じ規則(体積→安全率→マスクハッシュ、`_reduce_volume`が
    昇格判定に使っているもの)を、履歴上の合格(`outcome=='ok'`)エントリへ記録順
    (=trial.index順、最初は必ず初期シード)に自前で適用し、最後に残る候補が実際の
    `best_feasible`と体積・マスクハッシュの両方で一致することを確かめる
    ——単なる「全'ok'エントリの最小値と一致する」だけでは、'add'で発生する
    体積が大きい合格候補が紛れても常に真になってしまい検証にならないため、
    採用規則そのものを記録順に再適用する形にしてある。
    """
    conditions, design_space = detour_conditions()
    initial = _seed_search_result(detour_initial_mask(), conditions)
    result = bs._reduce_volume(initial, design_space, conditions, max_evaluations=1500,
                               frontier_capacity=bs.DEFAULT_FRONTIER_CAPACITY,
                               max_consecutive_adds=bs.DEFAULT_MAX_CONSECUTIVE_ADDS, is_cancelled=None)
    ok_trials = sorted((t for t in result.history if t.outcome == 'ok'), key=lambda t: t.index)
    assert ok_trials  # 初期シードの1件は必ずある
    reconstructed = ok_trials[0]
    for trial in ok_trials[1:]:
        if trial.material_mm3 < reconstructed.material_mm3 - conditions.volume_atol:
            better = True
        elif trial.material_mm3 <= reconstructed.material_mm3 + conditions.volume_atol:
            if trial.safety_factor > reconstructed.safety_factor + 1e-9:
                better = True
            elif (abs(trial.safety_factor - reconstructed.safety_factor) <= 1e-9
                  and trial.mask_hash < reconstructed.mask_hash):
                better = True
            else:
                better = False
        else:
            better = False
        if better:
            reconstructed = trial
    assert reconstructed.material_mm3 == result.best_feasible.material_mm3
    assert reconstructed.mask_hash == result.best_feasible.mask_hash


def test_reduction_is_deterministic_for_the_same_input_and_budget():
    """同じ入力・同じ予算なら同じ結果になる(§9)。"""
    conditions, design_space = detour_conditions()
    kwargs = dict(max_evaluations=300, frontier_capacity=bs.DEFAULT_FRONTIER_CAPACITY,
                 max_consecutive_adds=bs.DEFAULT_MAX_CONSECUTIVE_ADDS, is_cancelled=None)
    first = bs._reduce_volume(_seed_search_result(detour_initial_mask(), conditions), design_space, conditions,
                              **kwargs)
    second = bs._reduce_volume(_seed_search_result(detour_initial_mask(), conditions), design_space, conditions,
                               **kwargs)
    assert first.evaluations == second.evaluations
    assert first.termination == second.termination
    assert first.best_feasible.mask_hash == second.best_feasible.mask_hash
    assert first.best_feasible.material_mm3 == second.best_feasible.material_mm3
    assert len(first.history) == len(second.history)


def test_evaluation_budget_exhaustion_keeps_the_best_feasible_found_so_far():
    """予算終了なら、その時点の `best_feasible` をそのまま返す(§4.6)。"""
    conditions, design_space = detour_conditions()
    initial = _seed_search_result(detour_initial_mask(), conditions)
    result = bs._reduce_volume(initial, design_space, conditions, max_evaluations=5,
                               frontier_capacity=bs.DEFAULT_FRONTIER_CAPACITY,
                               max_consecutive_adds=bs.DEFAULT_MAX_CONSECUTIVE_ADDS, is_cancelled=None)
    assert result.status == 'feasible'
    assert result.termination == 'evaluation_limit'
    assert result.evaluations == 5
    assert result.best_feasible is not None
    assert result.best_feasible.material_mm3 <= 8.0


def test_cancellation_returns_the_best_feasible_found_before_the_cancel_signal():
    """中断すると、その時点までに見つけた合格チェックポイント(§4.6)を保って終了する。

    単に `best_feasible is not None` だけでなく、返ってきた `best_feasible` が
    実際にキャッシュ無しで再検査しても合格する(=無効化されたマスクを返していない)ことと、
    体積が初期形状(8.0)以下であること(悪化していない)まで確認する。
    """
    conditions, design_space = detour_conditions()
    initial = _seed_search_result(detour_initial_mask(), conditions)
    calls = {'n': 0}

    def is_cancelled():
        calls['n'] += 1
        return calls['n'] > 3

    result = bs._reduce_volume(initial, design_space, conditions, max_evaluations=1500,
                               frontier_capacity=bs.DEFAULT_FRONTIER_CAPACITY,
                               max_consecutive_adds=bs.DEFAULT_MAX_CONSECUTIVE_ADDS, is_cancelled=is_cancelled)
    assert result.status == 'feasible'
    assert result.termination == 'cancelled'
    assert result.best_feasible is not None
    assert result.best_feasible.material_mm3 <= 8.0
    recheck = acceptance.inspect(result.best_feasible.mask, result.best_feasible.before_mask,
                                 conditions.grid, **conditions.inspect_kwargs(bbox_cache=None))
    assert recheck['status'] == 'pass'
    assert calls['n'] >= 4  # 実際に呼ばれて真を返した回で止まった


def test_cancellation_after_an_improvement_preserves_the_improved_checkpoint():
    """中断が体積改善(7.0)の後に来た場合、初期形状(8.0)へ後退せず改善後の状態を保つ(§4.6)。

    `test_cancellation_returns_the_best_feasible_found_before_the_cancel_signal` は
    改善が起きる**前**に中断する場合を確認しているので、ここでは改善が既に起きた
    **後**に中断する場合を別途確認する(呼び出し回数999は実測で7.0発見後に中断される値)。
    """
    conditions, design_space = detour_conditions()
    initial = _seed_search_result(detour_initial_mask(), conditions)
    calls = {'n': 0}

    def is_cancelled():
        calls['n'] += 1
        return calls['n'] > 999

    result = bs._reduce_volume(initial, design_space, conditions, max_evaluations=1500,
                               frontier_capacity=bs.DEFAULT_FRONTIER_CAPACITY,
                               max_consecutive_adds=bs.DEFAULT_MAX_CONSECUTIVE_ADDS, is_cancelled=is_cancelled)
    assert result.status == 'feasible'
    assert result.termination == 'cancelled'
    assert result.best_feasible.material_mm3 == 7.0  # 改善後の状態のまま(8.0へ後退していない)
    recheck = acceptance.inspect(result.best_feasible.mask, result.best_feasible.before_mask,
                                 conditions.grid, **conditions.inspect_kwargs(bbox_cache=None))
    assert recheck['status'] == 'pass'


def test_failed_final_recheck_keeps_the_previous_best_and_does_not_crash(monkeypatch):
    """最良候補をキャッシュなしで最終再検査し、失敗したら成功を返さない(§3・§4.6)。

    昇格判定でだけ使う最終再検査(`history=`付き呼び出し)を常に不合格にすり替え、
    体積が小さい候補が見つかっても `best_feasible` が初期形状のまま(8.0)据え置かれる
    ——探索そのものは継続し、クラッシュもしない——ことを確認する。
    """
    conditions, design_space = detour_conditions()
    initial = _seed_search_result(detour_initial_mask(), conditions)
    original = acceptance.inspect

    def spy(*args, **kwargs):
        if 'history' in kwargs:  # 昇格判定の最終再検査だけを狙い撃つ(§3の呼び出し形)
            report = dict(original(*args, **kwargs))
            report['status'] = 'fail'
            return report
        return original(*args, **kwargs)

    monkeypatch.setattr(acceptance, 'inspect', spy)
    result = bs._reduce_volume(initial, design_space, conditions, max_evaluations=1500,
                               frontier_capacity=bs.DEFAULT_FRONTIER_CAPACITY,
                               max_consecutive_adds=bs.DEFAULT_MAX_CONSECUTIVE_ADDS, is_cancelled=None)
    assert result.status == 'feasible'
    assert result.best_feasible.material_mm3 == 8.0  # 昇格が一度も成立しなかった
    assert result.best_feasible.mask_hash == initial.best_feasible.mask_hash


def test_tiny_grid_exhaustive_enumeration_matches_the_search_optimum():
    """微小格子では、許可マスクの全列挙を検査専用の正解として、評価と最良候補の選択を照合する(§9)。

    通常探索が常に大域最小を発見するというassertにはしない —— ここでの一致は
    このフィクスチャの分岐が小さく(設計空間10セル、2**10=1024通り)、
    十分な予算・frontier容量を与えたときの実測。
    """
    conditions, design_space = detour_conditions()
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

    result = bs.find_best_feasible(design_space, conditions, max_evaluations=3000, frontier_capacity=64,
                                   max_consecutive_adds=bs.DEFAULT_MAX_CONSECUTIVE_ADDS)
    assert result.status == 'feasible'
    assert result.best_feasible.material_mm3 == brute_force_optimum


def test_default_budget_and_frontier_settings_reach_the_optimum_on_this_tiny_fixture():
    """docs/PLAN_BINARY_STRESS_SEARCH.md §4.3 追記の実測(既定設定・全設計空間からの通し探索で
    932試行、既定予算1000に収まる)を固定する。`find_best_feasible` を**一切の引数指定なし**
    (=`find_best_feasible`自身の既定値)で呼び、真の最小(7.0、上のテストでbrute forceにより
    別途確認済み)へ実際に届くことを確認する。既定値を変えてこの実測が崩れたら、
    この追記を書き直すこと。
    """
    conditions, design_space = detour_conditions()
    result = bs.find_best_feasible(design_space, conditions)  # 既定: max_evaluations/frontier_capacity/adds
    assert result.status == 'feasible'
    assert result.best_feasible.material_mm3 == 7.0
    assert result.evaluations <= 1000


def test_find_best_feasible_propagates_a_non_feasible_stage3_result_unchanged():
    """初期合格そのものが見つからなければ、段階4は何もせず段階3の`SearchResult`をそのまま返す。"""
    shape = (4, 2, 2)
    conditions = bar_conditions(shape=shape, target_safety_factor=1e12)  # 全材料でも安全率に届かない
    result = bs.find_best_feasible(np.ones(shape, bool), conditions, max_evaluations=8)
    assert result.status == 'no_feasible_found'
    assert result.best_feasible is None


def test_find_best_feasible_honours_cancellation_before_any_search_starts():
    """探索を始める前から中断済みなら、何も評価せずに`no_feasible_found`/`cancelled`で返す。"""
    conditions, design_space = detour_conditions()
    result = bs.find_best_feasible(design_space, conditions, is_cancelled=lambda: True)
    assert result.status == 'no_feasible_found'
    assert result.termination == 'cancelled'
    assert result.best_feasible is None
    assert result.evaluations == 0


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
