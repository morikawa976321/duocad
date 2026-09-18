import dataclasses
import hashlib
import itertools
import math
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


def repair_candidates(current, design_space, size):
    """1つのブロック大きさの候補 `(操作, マスク)`。段階3の生成規則(記述 → マスク)をテストから読む。"""
    current = np.asarray(current, dtype=bool)
    design_space = np.asarray(design_space, dtype=bool)
    for op, region, addable in bs.candidate_descriptors(current, design_space, (int(size),)):
        yield op, bs._materialize(current, design_space, op, region, addable)


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
    ops = list(repair_candidates(current, design_space, 1))
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
    ops = list(repair_candidates(current, design_space, 4))
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
# detour 題材の前提(段階3の追加・置換と、段階4の既知の限界が依存する)
# ---------------------------------------------------------------------------
def test_pure_deletion_cannot_improve_on_the_detour_shape():
    """削除だけでは詰まる題材であることの確認(§9)。detour形状はどの削除でも不合格になる
    ——単独ブロック削除も、削除だけを辿るBFS(何手先まで許しても)も改善しない。
    このテストが崩れたらフィクスチャの前提が壊れているので、detour を使う他のテストの結果は無意味。
    """
    conditions, design_space = detour_conditions()
    initial_mask = detour_initial_mask()
    sizes = tuple(reversed(bs.block_sizes(design_space.shape)))

    # 単独削除: どのサイズ・位置でも不合格
    for size in sizes:
        for kind, mask in repair_candidates(initial_mask, design_space, size):
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
                    for kind, mask in repair_candidates(survivor, design_space, size):
                        if kind == 'delete':
                            frontier.append(mask)
        if not survivors:
            for size in sizes:
                for kind, mask in repair_candidates(current, design_space, size):
                    if kind == 'delete':
                        frontier.append(mask)
    assert steps == 256  # 初期形状(8セル)の部分集合を全て尽くした(2**8)
    assert best_mm3 == 8.0  # 一切改善しない


# ---------------------------------------------------------------------------
# §8 段階4: ひずみエネルギーの順位で削る体積削減(計画書§11)
# ---------------------------------------------------------------------------
def appendage_conditions(target_safety_factor=.001):
    """固定面(x=0)から荷重面(x=10)へ通る 10x2x2 の棒と、荷重経路に乗らない 4x2x2 の付属部分(y=2..3)。"""
    shape = (10, 4, 2)
    grid = voxelize.Grid((0., 0., 0.), 1., shape)
    ny1, nz1 = shape[1] + 1, shape[2] + 1

    def bar_end(x):
        return [x * ny1 * nz1 + y * nz1 + z for y in range(3) for z in range(nz1)]

    keep = Pos(5., 2., 1.) * Box(12., 6., 4.)
    conditions = bs.SearchConditions(
        grid=grid, table=Table(grid, {1: bar_end(0), 2: bar_end(shape[0])}), fixed_faces=(1,), load_faces=(2,),
        forces=((0., 0., -1.),), preserves=[('keep', keep.wrapped)], excludes=[],
        target_safety_factor=target_safety_factor, solver='direct')
    design = np.zeros(shape, bool)
    design[:, :2, :] = True
    appendage = np.zeros(shape, bool)
    appendage[3:7, 2:, :] = True
    return conditions, design | appendage, appendage


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

    conditions, design_space = detour_conditions()
    detour = detour_initial_mask()
    fields = {}
    acceptance.inspect(detour, detour, conditions.grid, fields=fields, **conditions.inspect_kwargs())
    assert np.all(fields['element_energy'][~detour] == 0) and np.all(fields['element_energy'][detour] > 0)

    bar = bar_conditions(shape=shape)
    broken = np.zeros(shape, bool)
    broken[0], broken[1, 0, 0], broken[2, 1, 1] = True, True, True  # 辺だけで接する(水密で落ち、FEMへ進まない)
    fields = {}
    acceptance.inspect(broken, broken, bar.grid, fields=fields, **bar.inspect_kwargs())
    assert fields == {}


def test_select_removal_never_takes_the_last_material_cell_of_a_condition_node():
    shape = (4, 2, 2)
    conditions = bar_conditions(shape=shape)
    full = np.ones(shape, bool)
    anchors = bs._anchor_node_ids(conditions)
    order = np.arange(full.size)
    picked = bs.select_removal(full, order, full.size, anchors, excluded={5})
    assert 5 not in picked.tolist() and 0 < picked.size < full.size - 1
    left = full.copy()
    left.ravel()[picked] = False
    assert acceptance.active_nodes(left).ravel()[anchors].all()


def test_restore_irregular_brings_back_only_removed_cells_until_the_boundary_is_watertight():
    parent = np.ones((6, 6, 6), bool)
    candidate = parent.copy()
    candidate[1:5, 1:5, 1:5] = False
    candidate[1, 1, 1] = candidate[2, 2, 2] = True  # 空洞の隅に付いたセルと、それに頂点だけで接する浮いたセル
    assert acceptance.boundary_check(candidate)['status'] == 'fail'
    repaired = bs.restore_irregular(candidate, parent)
    assert acceptance.boundary_check(repaired)['status'] == 'pass'
    assert np.all(repaired <= parent) and np.all(candidate <= repaired) and repaired.sum() < parent.sum()


def test_reduction_strips_the_unloaded_appendage_and_keeps_every_condition_supported():
    conditions, design, appendage = appendage_conditions()
    result = bs.find_best_feasible(design, conditions, max_evaluations=200)
    best = result.best_feasible
    assert result.status == 'feasible' and result.termination == 'converged'
    assert not (best.mask & appendage).any()
    assert best.material_mm3 < design.sum() - appendage.sum()  # 付属部分に加えて棒も削った
    assert acceptance.active_nodes(best.mask).ravel()[bs._anchor_node_ids(conditions)].all()
    recheck = acceptance.inspect(best.mask, best.before_mask, conditions.grid, **conditions.inspect_kwargs(bbox_cache=None))
    assert recheck['status'] == 'pass' and recheck['material_sha256'] == best.mask_hash
    assert result.diagnostics['reduction']['fem_solves'] < result.evaluations


def test_rejected_steps_halve_the_step_accepted_ones_double_it_and_a_single_cell_safety_failure_converges(monkeypatch):
    """落ちたら、削減割合の後退規則か収束の判定が §11 の規則と違う。"""
    conditions, design, _ = appendage_conditions()
    original = acceptance.inspect
    floor = 0.75 * design.sum()

    def volume_limited(material, before, grid, **kwargs):
        report = original(material, before, grid, **kwargs)
        if report['status'] == 'pass' and material.sum() < floor:  # 体積の下限を安全率の不足に見立てる
            report['checks']['safety']['status'] = report['status'] = 'fail'
            if kwargs.get('fields') is not None:
                kwargs['fields'].clear()
        return report

    monkeypatch.setattr(acceptance, 'inspect', volume_limited)
    result = bs.find_best_feasible(design, conditions, max_evaluations=400)
    reduction = result.diagnostics['reduction']
    steps = reduction['steps']
    assert result.termination == 'converged' and reduction['stop_reason'] == 'safety_at_single_cell'
    assert floor <= result.best_feasible.mask.sum() < design.sum()
    for step, following in zip(steps, steps[1:]):
        if step['outcome'] == 'accepted':
            assert following['multiplier'] == min(1.0, 2 * step['multiplier'])
        elif step['selected_cells'] > 1:
            assert following['multiplier'] == step['multiplier'] / 2
        else:
            assert following['multiplier'] == step['multiplier']
    assert steps[-1]['selected_cells'] == 1 and 'safety' in steps[-1]['failed']


def test_a_computation_failure_is_not_accepted_and_does_not_stop_the_reduction(monkeypatch):
    conditions, design, _ = appendage_conditions()
    original = acceptance.inspect
    calls = {'failed': 0}

    def first_solved_candidate_fails(material, before, grid, **kwargs):
        if kwargs.get('fields') is not None and material.sum() < design.sum() and calls['failed'] < 2:
            calls['failed'] += 1  # FEMまで進んだ最初の候補と、その解法再試行
            report = original(material, before, grid, **{**kwargs, 'fields': None})
            report['checks']['safety'] = dict(status='unknown', reason='evaluation_error', error='stub')
            report['status'] = 'unknown'
            return report
        return original(material, before, grid, **kwargs)

    monkeypatch.setattr(acceptance, 'inspect', first_solved_candidate_fails)
    result = bs.find_best_feasible(design, conditions, max_evaluations=200)
    steps = result.diagnostics['reduction']['steps']
    failed_step = next(step for step in steps if 'safety' in step['failed'])
    assert calls['failed'] == 2 and failed_step['outcome'] == 'rejected'
    assert any(trial.operation.startswith('solver_retry') for trial in result.history)
    assert result.best_feasible.material_mm3 < design.sum() and result.termination == 'converged'


def test_deletion_only_reduction_cannot_leave_the_detour_shape_a_known_limit():
    """段階4は削るだけなので、どの削除でも不合格になる detour 形状からは動けない(既知の限界。
    追加・置換で抜けるのは段階3の探索だけ)。落ちたら段階4が材料を足している。"""
    conditions, _ = detour_conditions()
    initial = _seed_search_result(detour_initial_mask(), conditions)
    result = bs._reduce_volume(initial, detour_initial_mask(), conditions, max_evaluations=200, is_cancelled=None)
    assert result.termination == 'converged' and result.best_feasible.material_mm3 == 8.0
    assert all(np.all(trial_mask <= detour_initial_mask()) for trial_mask in [result.best_feasible.mask])


def test_reduction_is_deterministic_for_the_same_input_and_budget():
    """同じ入力・同じ予算なら同じ結果になる(§9)。"""
    conditions, design, _ = appendage_conditions()
    first = bs.find_best_feasible(design, conditions, max_evaluations=30)
    second = bs.find_best_feasible(design, conditions, max_evaluations=30)
    assert first.evaluations == second.evaluations and first.termination == second.termination
    assert first.best_feasible.mask_hash == second.best_feasible.mask_hash
    assert first.diagnostics['reduction']['steps'] == second.diagnostics['reduction']['steps']


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
    """微小格子では、許可マスクの全列挙を検査専用の正解として、評価と最良候補の選択を照合する(§9)。

    通常探索が常に大域最小を発見するというassertにはしない —— ここでの一致は
    このフィクスチャの分岐が小さく(設計空間10セル、2**10=1024通り)、既定設定で11評価で届く実測。
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

    result = bs.find_best_feasible(design_space, conditions)
    assert result.status == 'feasible' and result.termination == 'converged'
    assert result.best_feasible.material_mm3 == brute_force_optimum
    assert result.evaluations <= 20



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


# ---------------------------------------------------------------------------
# 修正B: 入口の必要条件(docs/FIX_BINARY_SEARCH_INITIALIZATION.md §4)
# ---------------------------------------------------------------------------
def test_uncovered_anchor_nodes_stop_at_the_entry_without_generating_or_evaluating(monkeypatch):
    """確定表の節点が全設計空間の材料に接しないなら、候補生成0回・FEM0回で `boundary_mapping_failed`。
    APIを直接呼ぶ経路でも同じ判定を通る(製品入口だけの防御にしない)。"""
    shape = (4, 2, 2)
    conditions = bar_conditions(shape=shape)
    design = np.ones(shape, bool)
    design[3] = False  # 荷重面(x=4平面)の9節点はどの材料セルにも接しない
    calls = {'inspect': 0, 'materialize': 0}

    def counting_inspect(*args, **kwargs):
        calls['inspect'] += 1
        raise AssertionError('入口で止まるべき経路が評価に進んだ')

    original = bs._materialize

    def counting_materialize(*args, **kwargs):
        calls['materialize'] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(acceptance, 'inspect', counting_inspect)
    monkeypatch.setattr(bs, '_materialize', counting_materialize)
    result = bs.find_initial_feasible(design, conditions, max_evaluations=10)
    assert result.status == 'boundary_mapping_failed'
    assert result.termination == 'boundary_mapping_failed'
    assert result.evaluations == 0 and result.history == [] and result.best_feasible is None
    rows = result.diagnostics['uncovered_condition_nodes']
    assert [(r['condition'], r['nodes'], r['uncovered_nodes']) for r in rows] == [('load_face_2', 9, 9)]
    assert result.diagnostics['mapped'] is False  # 元入力(未対応付けの表)か対応付け済みかを区別する
    assert calls == {'inspect': 0, 'materialize': 0}
    assert bs.find_best_feasible(design, conditions, max_evaluations=10).status == 'boundary_mapping_failed'


def test_a_failed_mapping_report_stops_the_direct_api_even_when_every_node_is_on_material(monkeypatch):
    """全節点が材料に接していても、渡された対応付けの検証が不成立なら候補生成0回・FEM0回で止まる。"""
    conditions = bar_conditions(shape=(4, 2, 2))
    failed = dict(status='fail', faces=[dict(condition='load_face_2', status='fail', reasons=['unmapped_samples'])])
    conditions = dataclasses.replace(conditions, mapping=failed)
    monkeypatch.setattr(acceptance, 'inspect', lambda *a, **k: pytest.fail('入口で止まるべき経路が評価に進んだ'))
    monkeypatch.setattr(bs, '_materialize', lambda *a, **k: pytest.fail('入口で止まるべき経路が候補を作った'))
    result = bs.find_best_feasible(np.ones((4, 2, 2), bool), conditions, max_evaluations=10)
    assert result.status == 'boundary_mapping_failed' and result.evaluations == 0
    assert result.diagnostics['uncovered_condition_nodes'] == []
    assert result.diagnostics['mapping'] is failed


def test_the_identifier_depends_on_the_fixed_nodes_not_only_on_the_face_indices():
    """同じ面添字でも節点集合が違えば別の評価条件(確定した節点・荷重・拘束をハッシュに含める)。"""
    shape = (4, 2, 2)
    a = bar_conditions(shape=shape)
    grid = a.grid
    ny1, nz1 = shape[1] + 1, shape[2] + 1
    x_max_node = shape[0] * ny1 * nz1
    faces = {1: list(range(ny1 * nz1)), 2: list(range(x_max_node, x_max_node + ny1 * nz1 - 1))}  # 荷重面の節点を1つ減らす
    b = bs.SearchConditions(grid=grid, table=Table(grid, faces), fixed_faces=a.fixed_faces, load_faces=a.load_faces,
                            forces=a.forces, preserves=a.preserves, excludes=a.excludes,
                            target_safety_factor=a.target_safety_factor, solver=a.solver)
    assert bs.evaluation_identifier(a) != bs.evaluation_identifier(b)
    assert bs.evaluation_identifier(a) == bs.evaluation_identifier(bar_conditions(shape=shape))


# ---------------------------------------------------------------------------
# 条件固定: 確定後は再配分しない(§3.2)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 修正C: 遅延生成・保持上限・生成予算・中断・公平性(§5)
# ---------------------------------------------------------------------------
def _fake_report(material, status='fail'):
    """FEM・CADを置き換える小さな報告(候補管理だけを検査するため)。"""
    material = np.asarray(material, dtype=bool)
    volume = float(material.sum())
    return dict(status=status, material_sha256=acceptance.material_sha256(material),
                volumes=dict(material_mm3=volume, before_mm3=volume, removed_islands_mm3=0.0),
                checks=dict(watertight=dict(status='pass'), single_solid=dict(status='pass'),
                            preserve=dict(status='pass'), exclude=dict(status='pass'),
                            conditions=dict(status='pass'), safety=dict(status=status, safety_factor=0.5)))


def _stub_evaluation(monkeypatch):
    monkeypatch.setattr(bs, 'resolve_islands',
                        lambda candidate, conditions: ([np.array(candidate, dtype=bool)],
                                                       dict(components=1, surviving_components=1, removed_islands=[])))
    monkeypatch.setattr(acceptance, 'inspect', lambda material, before, grid, **kwargs: _fake_report(material))


def test_candidates_are_materialised_one_at_a_time_not_all_up_front(monkeypatch):
    """1候補を要求した時点で1マスクしか作られていない(generator 型の確認ではなく、作った数を数える)。"""
    calls = {'n': 0}
    original = bs._materialize

    def counting(*args, **kwargs):
        calls['n'] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(bs, '_materialize', counting)
    current = np.ones((12, 12, 12), bool)
    current[6:] = False
    design = np.ones((12, 12, 12), bool)
    stream = repair_candidates(current, design, 1)
    kind, mask = next(stream)
    assert calls['n'] == 1 and kind == 'delete' and mask.sum() == current.sum() - 1
    for _ in range(4):
        next(stream)
    assert calls['n'] == 5
    possible = sum(1 for _ in bs.candidate_descriptors(current, design, (1,)))
    assert possible > 800  # 全部先に作れば数百枚になる題材で、5枚しか作っていない

    _stub_evaluation(monkeypatch)
    calls['n'] = 0
    conditions = bar_conditions(shape=(12, 12, 12))
    result = bs.find_initial_feasible(design, conditions, max_evaluations=4)
    assert result.termination == 'evaluation_limit'
    generated = result.diagnostics['candidates']['generated']
    assert calls['n'] == generated
    assert generated < 40  # 親1つの子候補(数千)を先に作っていない


def test_retained_masks_do_not_grow_with_the_evaluation_budget(monkeypatch):
    """実格子相当の大きさ(48³)で、評価回数を4倍にしてもキューと親マスクの数は上限以内で、
    実測メモリのピークも上限×マスク大きさの定数倍に収まる(初期・削減の両経路)。"""
    import tracemalloc

    _stub_evaluation(monkeypatch)
    shape = (48, 48, 48)
    conditions = bar_conditions(shape=shape)
    design = np.ones(shape, bool)
    mask_bytes = design.nbytes
    capacity = 6
    # 親1つが提案できる記述の数(全部先に配列にすると、この枚数のマスクが要る)。
    eager_masks = sum(1 for _ in bs.candidate_descriptors(design, design, tuple(reversed(bs.block_sizes(shape)))))
    assert eager_masks > 100_000
    peaks = {}
    for budget in (20, 160):
        tracemalloc.start()
        result = bs.find_initial_feasible(design, conditions, max_evaluations=budget, frontier_capacity=capacity)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        peaks[budget] = peak
        assert result.evaluations == budget
        frontier = result.diagnostics['frontier']
        assert frontier['peak_retained'] <= capacity and frontier['retained'] <= capacity
        # 実測のピークは入口検査(保持CADとの重なり: 材料の直方体列)の定数分が約28枚ぶんで、
        # 探索は容量+作業配列の数枚しか足さない。全候補を先に作る方式の 1/1000 以下。
        assert peak <= 64 * mask_bytes, (budget, peak / mask_bytes)
    assert peaks[160] <= peaks[20] + 2 * mask_bytes  # 評価回数を8倍にしても増えない



def test_generation_budget_and_cancellation_act_on_duplicates_that_never_reach_evaluation(monkeypatch):
    """無効・重複候補だけで評価数が増えないときも、走査予算と中断が効く(無限に生成し続けない)。"""
    shape = (6, 2, 2)
    conditions = bar_conditions(shape=shape, target_safety_factor=1e12)
    monkeypatch.setattr(acceptance, 'material_sha256', lambda mask: 'same')  # 全候補が同一に見える → 重複しか生成されない
    result = bs.find_initial_feasible(np.ones(shape, bool), conditions, max_evaluations=50, max_candidates=25)
    assert result.status == 'no_feasible_found' and result.termination == 'generation_limit'
    assert result.evaluations == 1
    candidates = result.diagnostics['candidates']
    assert candidates['scanned'] == 25 and candidates['limit'] == 25
    assert 0 < candidates['generated'] < 25  # 全材料の親では追加・置換のブロックは無効で、予算だけを使う
    assert candidates['duplicates'] == candidates['generated']
    assert (candidates['evaluations'], candidates['evaluation_limit']) == (1, 50)

    calls = {'n': 0}

    def is_cancelled():
        calls['n'] += 1
        return calls['n'] > 10

    result = bs.find_initial_feasible(np.ones(shape, bool), conditions, max_evaluations=50, max_candidates=1000,
                                      is_cancelled=is_cancelled)
    assert result.termination == 'cancelled' and result.evaluations == 1
    assert result.diagnostics['candidates']['generated'] < 1000  # 生成の途中で止まった


def test_a_long_run_of_invalid_blocks_is_bounded_by_the_scan_budget_and_cancellation(monkeypatch):
    """次の有効な候補が3万ブロック先にあっても、無効なブロックの走査が予算・中断の外で続かない。
    落ちたら、1回の候補要求の中で格子を黙って1周しうる。"""
    shape = (32, 32, 32)
    design = np.ones(shape, bool)
    parent = np.zeros(shape, bool)
    parent[-1, -1, -1] = True  # 削除できるのは走査順で最後のブロックだけ
    visited_blocks = {'n': 0}
    original = bs._block_region

    def counting_region(*args, **kwargs):
        visited_blocks['n'] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(bs, '_block_region', counting_region)

    def pull(max_candidates, is_cancelled=None):
        frontier = bs._Frontier(4)
        frontier.push(bs._node(parent.copy(), 0, 0, design, (1,), max_consecutive_adds=0))
        budget = bs._Budget(max_evaluations=1, max_candidates=max_candidates)
        return bs._pull(frontier, design, budget, set(), 'id', is_cancelled), budget

    visited_blocks['n'] = 0
    outcome, budget = pull(100)
    assert outcome == 'generation_limit'
    assert budget.scanned == 100 and budget.generated == 0
    assert visited_blocks['n'] <= 101

    calls = {'n': 0}

    def cancel_after_fifty():
        calls['n'] += 1
        return calls['n'] > 50

    visited_blocks['n'] = 0
    outcome, budget = pull(10 ** 9, cancel_after_fifty)
    assert outcome == 'cancelled' and budget.scanned == 50 and visited_blocks['n'] <= 51

    outcome, budget = pull(10 ** 9)  # 予算が足りれば、最後のブロックの削除候補に届く
    assert outcome[1] == 'delete' and not outcome[0].any()
    assert budget.scanned == parent.size and budget.generated == 1


def test_the_default_scan_budget_allows_one_sweep_of_the_grid_per_evaluation():
    assert bs._candidate_budget(10, None, (40, 50, 60)) == 10 * 40 * 50 * 60
    assert bs._candidate_budget(10, None, (2, 2, 2)) == 10 * bs.DEFAULT_CANDIDATES_PER_EVALUATION
    assert bs._candidate_budget(10, 7, (40, 50, 60)) == 7


def test_the_initial_search_checks_cancellation_before_each_generation_and_evaluation():
    shape = (4, 2, 2)
    conditions = bar_conditions(shape=shape, target_safety_factor=1e12)
    calls = {'n': 0}

    def is_cancelled():
        calls['n'] += 1
        return calls['n'] > 2

    result = bs.find_initial_feasible(np.ones(shape, bool), conditions, max_evaluations=50, is_cancelled=is_cancelled)
    assert result.status == 'no_feasible_found' and result.termination == 'cancelled'
    assert result.evaluations <= 3 and result.best_feasible is None
    assert bs.find_best_feasible(np.ones(shape, bool), conditions, max_evaluations=50,
                                 is_cancelled=lambda: calls['n'] > 2).termination == 'cancelled'


def test_operation_generators_are_interleaved_so_adds_and_replaces_are_not_starved():
    """削除候補が並んでいても、各大きさで削除→追加→置換の順に1つずつ巡回する(削除優先は保つ)。"""
    current = np.zeros((16, 1, 1), bool)
    current[:7] = True
    design = np.ones((16, 1, 1), bool)
    ops = [op for op, _, _ in itertools.islice(bs.candidate_descriptors(current, design, (2,)), 6)]
    assert ops[:3] == ['delete', 'add', 'replace']
    assert ops[3:] == ['delete', 'delete', 'delete']  # 追加・置換が尽きたら削除だけが続く
    # 連続追加の上限に達した親は追加を生成しないが、置換(体積を増やさない)は残す
    limited = {op for op, _, _ in bs.candidate_descriptors(current, design, (2,), allow_add=False)}
    assert limited == {'delete', 'replace'}


def test_frontier_eviction_is_recorded_and_never_reported_as_neighbourhood_exhausted(monkeypatch):
    """容量超過で親を捨てた探索は、キューが空になっても「近傍を全て探索した」と言わない(段階3)。"""
    _stub_evaluation(monkeypatch)  # 全候補が不合格 → 到達できる部分集合(2**6)を尽くすまで親が積まれる
    shape = (3, 2, 1)
    conditions = bar_conditions(shape=shape)
    tight = bs.find_initial_feasible(np.ones(shape, bool), conditions, max_evaluations=3000, frontier_capacity=2)
    assert tight.termination == 'frontier_truncated'
    assert tight.diagnostics['frontier']['evictions'] > 0
    assert tight.diagnostics['frontier']['peak_retained'] <= 2
    wide = bs.find_initial_feasible(np.ones(shape, bool), conditions, max_evaluations=3000, frontier_capacity=4096)
    assert wide.termination == 'neighbourhood_exhausted'
    assert wide.diagnostics['frontier']['evictions'] == 0


def test_the_best_feasible_mask_is_read_only_and_survives_later_reduction():
    """最良合格のマスクは読み取り専用で、後続の削減・失敗がその内容とハッシュを変えない。"""
    conditions, design, _ = appendage_conditions()
    result = bs.find_best_feasible(design, conditions, max_evaluations=6)
    best = result.best_feasible
    assert not best.mask.flags.writeable and not best.before_mask.flags.writeable
    assert acceptance.material_sha256(best.mask) == best.mask_hash
    snapshot = best.mask.copy()
    later = bs._reduce_volume(result, design, conditions, max_evaluations=200, is_cancelled=None)
    assert later.best_feasible.material_mm3 <= best.material_mm3
    assert np.array_equal(best.mask, snapshot) and acceptance.material_sha256(best.mask) == best.mask_hash


def test_resolve_islands_keeps_only_the_numbers_of_removed_islands():
    shape = (5, 2, 2)
    conditions = bar_conditions(shape=shape)
    candidate = np.zeros(shape, bool)
    candidate[0] = candidate[4] = True
    survivors, diagnostics = bs.resolve_islands(candidate, conditions)
    assert survivors == []
    assert all(set(island) == {'cells', 'volume_mm3', 'touches_condition'} for island in diagnostics['removed_islands'])
