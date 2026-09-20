import dataclasses
import hashlib
import itertools
import math
from collections import deque

import numpy as np
import pytest
from build123d import Box, Pos
from scipy import ndimage

from duocad.topopt import acceptance, anchor, fem, min_width, stress, voxelize
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


def test_delete_candidates_are_only_blocks_touching_the_material_surface():
    """内部のブロック(全部材料で周りも全部材料)を空にすると閉じた空洞ができるので、削除の候補にしない。"""
    shape = (8, 8, 8)
    full = np.ones(shape, bool)
    seen = set()
    for size in bs.block_sizes(shape):
        deletes = [region for op, region, _ in bs.candidate_descriptors(full, full, (size,)) if op == 'delete']
        for region in deletes:
            child = bs._materialize(full, full, 'delete', region, None)
            assert acceptance.void_components(child) == 1, (size, region)
            seen.add(size)
        interior = [lo for lo in bs._block_starts(shape, size)
                    if all(0 < l and l + size < n for l, n in zip(lo, shape))]
        assert len(deletes) == len(list(bs._block_starts(shape, size))) - len(interior), size
    assert seen == set(bs.block_sizes(shape))


@pytest.mark.parametrize('axis, side', [(a, s) for a in range(3) for s in (0, 1)])
def test_an_interior_block_becomes_a_delete_candidate_once_a_face_or_its_inside_is_open(axis, side):
    """内部のブロック(12³ の中央 4³)は、格子の外へ抜ける細い穴が6面のどこかに接するか、ブロックを貫けば候補になる。"""
    shape = (12, 12, 12)
    centre = (slice(4, 8),) * 3

    def has_centre_block(mask):
        return any(op == 'delete' and region == centre for op, region, _ in bs.candidate_descriptors(mask, mask, (4,)))

    mask = np.ones(shape, bool)
    assert not has_centre_block(mask)
    index = [5, 5, 5]
    index[axis] = slice(8, 12) if side else slice(0, 4)  # 中央ブロックの外側から格子の端へ
    mask[tuple(index)] = False
    assert has_centre_block(mask)
    index[axis] = slice(4, 8)  # 穴がブロックの中だけ(外へ通じない)でも、ブロックは空セルを含むので候補
    inside = np.ones(shape, bool)
    inside[tuple(index)] = False
    assert has_centre_block(inside)


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


def _assert_peel_invariants(mask, conditions):
    """peel の出力が満たすべき4つ(1成分・不正頂点0・閉じた空洞なし・全条件節点に支えあり)を、全体の判定で確かめる。"""
    assert acceptance.connectivity_check(mask)['components'] == 1
    assert acceptance.boundary_check(mask)['irregular_vertices'] == 0
    assert acceptance.void_components(mask) == 1
    assert acceptance.active_nodes(mask).ravel()[bs._anchor_node_ids(conditions)].all()


def _random_connected_mask(rng, shape, density=.7):
    labels, count = ndimage.label(rng.random(shape) < density, structure=bs.NEIGHBOURHOOD_STRUCTURE)
    return labels == 1 + int(np.argmax(np.bincount(labels.ravel())[1:])) if count else np.zeros(shape, bool)


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
            _assert_peel_invariants(after, conditions)
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
        mask = _random_connected_mask(rng, (6, 6, 6))
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
    _assert_peel_invariants(after, conditions)


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
    _assert_peel_invariants(after, conditions)


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
    _assert_peel_invariants(after, conditions)


def test_peel_refuses_a_cell_whose_removal_leaves_an_irregular_vertex():
    """L字の折れ目のセルを抜くと残る2セルが辺だけで接する(頂点の多様体条件を破る)ので、連結性より先に落ちる。"""
    mask = np.zeros((2, 2, 1), bool)
    mask[0, 0] = mask[1, 0] = mask[1, 1] = True
    after, stats = bs.peel(mask, np.array([int(np.ravel_multi_index((1, 0, 0), mask.shape))]), 1,
                           np.zeros(0, dtype=np.int64))
    assert np.array_equal(after, mask) and (stats.picked, stats.skipped_manifold) == (0, 1)


# ---------------------------------------------------------------------------
# 最小幅の保証(`min_width_cells`)
# ---------------------------------------------------------------------------
def _opening_satisfied(mask: np.ndarray, m: int) -> bool:
    """`mask` の全材料セルが m 立方の材料だけのブロックのどれかに属するか(peel の局所判定とは
    独立な、全体を数え直す物差し)。"""
    if m <= 1:
        return True
    opened = ndimage.binary_opening(mask, structure=np.ones((m, m, m), dtype=bool))
    return not (mask & ~opened).any()


def _rule_violations(mask: np.ndarray, m: int, exempt=None) -> np.ndarray:
    """規則を破るセル(実装とは独立に scipy で数える)。`exempt` は被覆を問わず、`mask` に無くても材料に数える。"""
    material = mask if exempt is None else mask | exempt
    bad = mask & ~ndimage.binary_opening(material, structure=np.ones((m, m, m), dtype=bool))
    return bad if exempt is None else bad & ~exempt


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
    assert (stats.picked, stats.casualties, stats.skipped_thickness) == (1, 3, 0)
    assert stats.picked + stats.casualties + stats.islands == mask.sum() - after.sum()
    assert _opening_satisfied(after, 2)


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
        assert _opening_satisfied(mask, m)
        for cell in rng.permutation(mask.size)[:30]:
            i, j, k = np.unravel_index(int(cell), mask.shape)
            if not mask[i, j, k]:
                continue
            checked += 1
            after, stats = bs.peel(mask, np.array([cell]), 1, anchors, min_width_cells=m)
            assert stats.skipped_thickness == 0 and _opening_satisfied(after, m)
            if stats.picked == 1 and not stats.islands:
                without = mask.copy()
                without[i, j, k] = False
                assert np.array_equal(after, bs.min_width.cube_opening(without, m))
                bites += stats.casualties > 0
    assert checked > 100 and bites > 20  # 何も試せていない・巻き添えが出ないケースだけで合格していない


def _peel_box_until_stuck(m, kind, *, exempt_fixed_layer=False):
    """12x20x16 の直方体(固定面 y=0・荷重面 y=最大の中央)を、固定のエネルギー場で `peel`(2割要求)が止まるまで
    繰り返し、各ステップの不変条件を全体で数え直す。戻り値は止まったときの材料割合。"""
    shape = (12, 20, 16)
    nodes = np.zeros(tuple(n + 1 for n in shape), bool)
    nodes[:, 0, :] = True
    nodes[4:8, shape[1], 5:10] = True
    anchors = np.flatnonzero(nodes.ravel())
    x, y, z = np.meshgrid(*(np.arange(n, dtype=float) for n in shape), indexing='ij')
    noise = np.random.default_rng(1).random(shape)
    energy = (((shape[1] - y) / shape[1]) ** 2 * ((z - 7.5) / 8) ** 2 + 1e-6 * noise) if kind == 'bending' else noise
    exempt = None
    if exempt_fixed_layer:
        exempt = np.zeros(shape, bool)
        exempt[:, 0, :] = True
    mask = np.ones(shape, bool)
    for _ in range(60):
        after, stats = bs.peel(mask, bs.ranked_cells(mask, energy), max(1, int(.2 * mask.sum())), anchors,
                               min_width_cells=m, exempt=exempt)
        assert stats.picked + stats.casualties + stats.islands == mask.sum() - after.sum()
        if after.sum() == mask.sum():
            break
        _assert_min_width_invariants(after, m, exempt)
        assert acceptance.active_nodes(after).ravel()[anchors].all()
        mask = after
    return mask.sum() / mask.size


@pytest.mark.parametrize('m', [2, 3])
@pytest.mark.parametrize('kind', ['bending', 'random'])
def test_peel_with_min_width_keeps_every_invariant_and_carves_far_below_where_it_used_to_stall(m, kind):
    """落ちたら、射影つきの `peel` が途中で不変条件を破る、または厚みで手が止まる(1セルずつ却下していた版は
    同じ題材・同じ手順で m=2 が67〜76%・m=3 が78〜84%で止まった)。"""
    assert _peel_box_until_stuck(m, kind) < 0.3


def test_peel_with_min_width_keeps_every_invariant_when_the_fixed_layer_is_exempt():
    """`exempt`(固定面の層)があっても、全ステップの不変条件(`exempt` を除く)が保たれ、同じく深く削れる。"""
    assert _peel_box_until_stuck(2, 'bending', exempt_fixed_layer=True) < 0.3


GOLDEN_PEEL_PICKED = 1815
GOLDEN_PEEL_DIGEST = 'e567d9b0d72be90b5903b2300e86c61fd5189d64e012e8d3e061985a04e2f051'


def test_peel_without_min_width_gives_the_same_masks_and_counts_as_before_the_projection():
    """落ちたら、m=1(既定)の `peel` の戻りマスク・実績・選んだセルが、射影つきを入れる前と変わっている。
    期待値は変更前のコードで同じ手順を回して得たハッシュ(乱数入力40個・各3回の `peel`・条件節点あり)。"""
    rng = np.random.default_rng(20260921)
    digest = hashlib.sha256()
    picked = 0
    for _ in range(40):
        shape = tuple(int(x) for x in rng.integers(4, 9, size=3))
        mask = _random_connected_mask(rng, shape, .75)
        if not mask.any():
            continue
        nodes = np.zeros(tuple(n + 1 for n in shape), bool)
        nodes[0] = True
        nodes[-1, :shape[1] // 2 + 1] = True
        anchors = np.flatnonzero(nodes.ravel())
        for _ in range(3):
            order = rng.permutation(mask.size)
            count = int(rng.integers(1, max(2, int(mask.sum()))))
            after, stats = bs.peel(mask, order, count, anchors)
            assert stats.casualties == 0 and stats.skipped_thickness == 0
            digest.update(np.ascontiguousarray(after).tobytes())
            digest.update(repr([int(after.sum()), stats.picked, stats.islands, stats.skipped_support, stats.skipped_surface,
                                stats.skipped_manifold, stats.skipped_connectivity, stats.already_removed,
                                stats.global_checks, stats.picked_cells.tolist()]).encode())
            picked += stats.picked
            mask = after
    assert picked == GOLDEN_PEEL_PICKED and digest.hexdigest() == GOLDEN_PEEL_DIGEST


def _stage3_child(mask, design_space, op, region, addable, m=2, exempt=None):
    return bs._materialize(mask, design_space, op, region, addable, min_width_cells=m, exempt=exempt)


def test_stage3_delete_of_a_single_cell_takes_the_layer_that_loses_its_cube_instead_of_being_refused():
    """厚さ2セルの塊(2x2x4)で、1セルの削除は m=2 でも候補に残り、子はその層ごと(z=0 の4セル)を空にした形になる。
    m=1 の子は1セルだけ。"""
    mask = np.ones((2, 2, 4), bool)
    corner = (slice(0, 1), slice(0, 1), slice(0, 1))
    deletes = [region for op, region, _ in bs.candidate_descriptors(mask, mask, (1,)) if op == 'delete']
    assert len(deletes) == 16 and corner in deletes
    assert (~bs._materialize(mask, mask, 'delete', corner, None)).sum() == 1
    child = _stage3_child(mask, mask, 'delete', corner, None)
    assert not child[:, :, 0].any() and child[:, :, 1:].all()
    assert _opening_satisfied(child, 2)


def test_stage3_block_delete_and_replace_children_equal_the_global_opening_of_the_raw_child():
    """落ちたら、段階3の射影(削除・置換で被覆を失うセルを一緒に空にする)が、全体を数え直した opening とずれている。
    足した `addable` が m 立方に入らなければそのセルも空になる(置換)。追加は膨らませる(下の専用の検査)。"""
    rng = np.random.default_rng(20260921)
    checked = casualties = replace_casualties = addable_cleared = truncated = 0
    for _ in range(40):
        shape = tuple(int(x) for x in rng.integers(4, 10, size=3))
        m = int(rng.integers(2, 5))
        raw = rng.random(shape) < rng.uniform(0.6, 0.95)
        mask = ndimage.binary_opening(raw, structure=np.ones((m, m, m), dtype=bool))  # 冪等: 前提を満たす入力
        design_space = np.ones(shape, bool)
        for size in bs.block_sizes(shape):
            for op, region, addable in bs.candidate_descriptors(mask, design_space, (size,)):
                if op == 'add':
                    continue
                child = _stage3_child(mask, design_space, op, region, addable, m)
                unprojected = bs._materialize(mask, design_space, op, region, addable)
                assert np.array_equal(child, min_width.cube_opening(unprojected, m))
                assert _opening_satisfied(child, m)
                checked += 1
                truncated += any(r.stop - r.start < size for r in region)
                lost = int(unprojected.sum() - child.sum())
                casualties += lost > 0
                replace_casualties += op == 'replace' and lost > 0
                addable_cleared += op == 'replace' and bool((addable & ~child[region]).any())
    assert checked > 100 and casualties > 30 and truncated > 20  # 巻き添え・端で切り詰まったブロックが実際に出ている
    assert replace_casualties > 10 and addable_cleared > 3


def _stage3_add(mask, design_space, cell, m=2, exempt=None):
    """`cell`(1セルのブロック)への追加候補の子。候補が1つあることも確かめる。"""
    region = tuple(slice(c, c + 1) for c in cell)
    adds = [addable for op, r, addable in bs.candidate_descriptors(mask, design_space, (1,))
            if op == 'add' and r == region]
    assert len(adds) == 1
    return _stage3_child(mask, design_space, 'add', region, adds[0], m, exempt)


def _plate_with_a_through_hole():
    """2セル厚の板(6x6x2)の中央に貫通穴(3,3,*)。穴の1セルだけを足すと、そのセルは m=2 の立方に入らない。"""
    plate = np.ones((6, 6, 2), bool)
    plate[3, 3, :] = False
    return plate, np.ones_like(plate)


def test_stage3_add_fills_a_cube_around_the_added_cell_instead_of_leaving_it_thin():
    """落ちたら、追加が閉じていない。2セル厚の板の貫通穴に1セルを足すと、そのセルだけが m=2 の立方に入らず残る(または足さずに消える)。
    膨らませた子は穴の2セルを両方足す。"""
    plate, design_space = _plate_with_a_through_hole()
    child = _stage3_add(plate, design_space, (3, 3, 0))
    assert child[3, 3, 0] and child[3, 3, 1] and int(child.sum() - plate.sum()) == 2
    assert _opening_satisfied(child, 2)


def test_stage3_add_next_to_a_bar_takes_the_cube_that_adds_the_fewest_cells_and_the_smallest_corner_on_a_tie():
    """落ちたら、膨らませる立方の選び方が変わっている(足すセルが最少・同数なら最小の角が先)。2x2x4 の棒の脇の1セル (0,2,1) を覆う
    立方は y=1..2 の側が3セルで済み、z=0..1 と z=1..2 が同数なので z=0..1 を選ぶ。"""
    design_space = np.ones((2, 4, 4), bool)
    bar = np.zeros_like(design_space)
    bar[:, :2, :] = True
    child = _stage3_add(bar, design_space, (0, 2, 1))
    assert {tuple(int(x) for x in c) for c in np.argwhere(child & ~bar)} == {
        (x, 2, z) for x in range(2) for z in range(2)}
    assert _opening_satisfied(child, 2)


def test_stage3_add_that_has_no_cube_inside_the_design_space_is_not_added():
    """落ちたら、m 立方が設計空間に収まらないセルが足されて、規則を破る子ができる(設計空間を opening で絞ってあれば起きない入力)。"""
    design_space = np.zeros((2, 4, 4), bool)
    design_space[:, :2, :] = True
    design_space[0, 2, 1] = True
    bar = design_space.copy()
    bar[0, 2, 1] = False
    child = _stage3_add(bar, design_space, (0, 2, 1))
    assert np.array_equal(child, bar)


def test_stage3_add_counts_an_exempt_cell_missing_from_the_mask_as_material_and_never_asks_it_for_a_cube():
    """落ちたら、追加の膨らませ方が保持セルの扱いとずれている。保持セルが mask に無くても材料に数えるので、穴の相方が保持セルなら足すのは1セルだけ。
    足すセル自身が保持セルなら、立方に入らなくてもそのまま足す。"""
    plate, design_space = _plate_with_a_through_hole()
    partner = np.zeros_like(plate)
    partner[3, 3, 1] = True
    child = _stage3_add(plate, design_space, (3, 3, 0), exempt=partner)
    assert child[3, 3, 0] and not child[3, 3, 1] and int(child.sum() - plate.sum()) == 1
    assert bs._mask_satisfies_min_width(child, 2, partner)
    added = np.zeros_like(plate)
    added[3, 3, 0] = True
    child = _stage3_add(plate, design_space, (3, 3, 0), exempt=added)
    assert child[3, 3, 0] and not child[3, 3, 1] and bs._mask_satisfies_min_width(child, 2, added)


def test_stage3_add_children_are_closed_under_the_rule():
    """落ちたら、追加の子が規則を破る。規則を満たす親の全ブロックの追加候補で、子は規則(`exempt` を材料に数える)を満たし、
    親のセルも足したセルも消えていない(設計空間を絞ってあるので、足せないセルは無い)。"""
    rng = np.random.default_rng(20260922)
    checked = inflated = 0
    for _ in range(30):
        m = int(rng.integers(2, 5))
        shape = tuple(int(x) for x in rng.integers(m + 3, m + 8, size=3))
        raw = rng.random(shape) < rng.uniform(0.55, 0.9)
        exempt = (rng.random(shape) < rng.uniform(0.0, 0.25)) & raw if rng.random() < 0.6 else None
        design = min_width.cube_opening(raw, m)
        if exempt is not None:
            design = design | (raw & exempt)
        picked = design & (rng.random(shape) < rng.uniform(0.3, 0.8))
        mask = min_width.cube_opening(picked, m)
        if exempt is not None:
            mask = mask | (picked & exempt)
        assert not _rule_violations(mask, m, exempt).any()
        for op, region, addable in bs.candidate_descriptors(mask, design, (1, 2, 4)):
            if op != 'add':
                continue
            child = _stage3_child(mask, design, 'add', region, addable, m, exempt)
            raw_child = bs._materialize(mask, design, 'add', region, addable)
            assert not _rule_violations(child, m, exempt).any()
            assert (raw_child <= child).all() and (child <= design).all()
            checked += 1
            inflated += int(child.sum() > raw_child.sum())
    assert checked > 300 and inflated > 100  # 膨らませた追加が実際に出ている


def test_stage3_exempt_cells_are_never_taken_along_and_still_count_as_material():
    """2x2x4 の棒から (0,0,1) を1セル削ると z=0・z=1 の8セルが被覆を失う。(1,1,1) が対象外なら巻き添えにならず材料のまま残る。"""
    bar = np.ones((2, 2, 4), bool)
    exempt = np.zeros_like(bar)
    exempt[1, 1, 1] = True
    region = (slice(0, 1), slice(0, 1), slice(1, 2))
    plain = _stage3_child(bar, bar, 'delete', region, None)
    assert not plain[:, :, :2].any() and plain[:, :, 2:].all()
    child = _stage3_child(bar, bar, 'delete', region, None, exempt=exempt)
    assert child[1, 1, 1] and int((bar & ~child).sum()) == 7
    assert bs._mask_satisfies_min_width(child, 2, exempt)


def test_stage3_delete_of_an_exempt_cell_takes_nothing_along():
    """落ちたら、段階3の削除が保持セルを材料から外した扱いで巻き添えを出している。保持セルの削除は、そのセルだけが空になる。"""
    bar = np.ones((2, 2, 4), bool)
    region = (slice(0, 1), slice(0, 1), slice(1, 2))
    exempt = np.zeros_like(bar)
    exempt[0, 0, 1] = True
    child = _stage3_child(bar, bar, 'delete', region, None, exempt=exempt)
    assert not child[0, 0, 1] and int((bar & ~child).sum()) == 1
    assert bs._mask_satisfies_min_width(child, 2, exempt)


def test_lost_coverage_counts_the_cells_a_replacement_adds_as_coverage():
    """落ちたら、`refill`(置換で足すセル)が被覆に数えられていない: 層を同じ形で入れ直せば何も失わず、空にすれば隣の層を失う。"""
    mask = np.zeros((4, 2, 2), bool)
    mask[1:3] = True
    region = (slice(2, 3), slice(0, 2), slice(0, 2))
    assert min_width.lost_coverage(mask, region, 2)[1].any()
    assert not min_width.lost_coverage(mask, region, 2, refill=np.ones((1, 2, 2), bool))[1].any()
    origin, lost = min_width.lost_coverage(mask, region, 2, refill=np.zeros((1, 2, 2), bool))
    assert {tuple(int(x) for x in c) for c in np.argwhere(lost) + np.asarray(origin)} == {
        (1, y, z) for y in range(2) for z in range(2)}


def test_stage3_children_that_differ_before_the_projection_are_the_same_candidate_after_it():
    """落ちたら、訪問済みの判定が射影の前のマスクで行われ、別のブロックから同じ子ができたときに二度解く。
    2セル厚の板では、1セルの上面と下面のブロックは同じ貫通柱になる。"""
    plate = np.ones((6, 6, 2), bool)
    design_space = plate.copy()
    top, bottom = ((slice(3, 4), slice(3, 4), slice(z, z + 1)) for z in (1, 0))
    raw = [bs._materialize(plate, design_space, 'delete', r, None) for r in (top, bottom)]
    projected = [_stage3_child(plate, design_space, 'delete', r, None) for r in (top, bottom)]
    assert not np.array_equal(*raw) and np.array_equal(*projected)

    frontier = bs._Frontier(4)
    frontier.push(bs._node(plate, 0, 0, design_space, (1,), 2))
    budget = bs._Budget(max_evaluations=10 ** 6, max_candidates=10 ** 6)
    visited, children = set(), []
    while True:
        pulled = bs._pull(frontier, design_space, budget, visited, 'id', None, min_width_cells=2)
        if isinstance(pulled, str):
            break
        children.append(acceptance.material_sha256(pulled[0]))
    assert len(children) == len(set(children)) == budget.generated - budget.duplicates and budget.duplicates > 0
    distinct_raw = {acceptance.material_sha256(bs._materialize(plate, design_space, op, region, addable))
                    for op, region, addable in bs.candidate_descriptors(plate, design_space, (1,))}
    assert len(children) < len(distinct_raw)


def _plate_with_an_excluded_column():
    """8x7x2 の板(固定面=x最小・荷重面=x最大)の列 (2,4) が除外領域に掛かる。全材料は除外で不合格。
    2セル大の整列ブロック [2,4)x[4,6) だけを削ると y=6 に1セル幅の薄皮が残り、m=2 の規則を破る。"""
    shape = (8, 7, 2)
    hole = Pos(2.5, 4.5, 1.0) * Box(.8, .8, 1.8)
    conditions = dataclasses.replace(bar_conditions(shape=shape), excludes=[('hole', hole.wrapped)])
    return conditions, np.ones(shape, bool)


def test_stage3_reaches_an_initial_feasible_shape_through_a_projected_delete_that_a_thickness_filter_cannot_reach():
    """落ちたら、段階3の削除が被覆を失うセルを一緒に空にしていない。この題材では、除外された列を含む2セル大の整列ブロックの
    素朴な子は薄皮が残って規則を破る。射影した子は規則を満たし、評価60回以内で初期合格に届く。"""
    conditions, design = _plate_with_an_excluded_column()
    block = (slice(2, 4), slice(4, 6), slice(0, 2))
    naive = bs._materialize(design, design, 'delete', block, None)
    child = _stage3_child(design, design, 'delete', block, None)
    assert not _opening_satisfied(naive, 2) and _opening_satisfied(child, 2)
    assert not child[2:4, 4:7].any() and child.sum() == design.sum() - 12

    result = bs.find_initial_feasible(design, conditions, max_evaluations=60, min_width_cells=2)
    assert result.status == 'feasible' and result.evaluations <= 60
    mask = result.best_feasible.mask
    assert _opening_satisfied(mask, 2) and not mask[2, 4].any()


GOLDEN_STAGE3_M1_DIGEST = 'ee6914b03c9f50242ba242ff0755e1663a7108c446f69723d199c704a910369d'


def test_stage3_with_min_width_one_gives_the_same_candidates_masks_and_search_as_before_the_projection():
    """落ちたら、m=1(既定)の段階3の候補の並び・子マスク・評価回数・履歴が、射影を入れる前と変わっている。
    期待値は変更前のコードで同じ手順を回して得たハッシュ(乱数の親25個の全候補と、除外つきの板の探索3本)。"""
    rng = np.random.default_rng(20260926)
    digest = hashlib.sha256()
    for _ in range(25):
        shape = tuple(int(x) for x in rng.integers(2, 8, size=3))
        design = rng.random(shape) < .9
        mask = design & (rng.random(shape) < .7)
        for size in bs.block_sizes(shape):
            for allow_add in (True, False):
                for op, region, addable in bs.candidate_descriptors(mask, design, (size,), allow_add=allow_add):
                    child = bs._materialize(mask, design, op, region, addable)
                    digest.update(repr((op, [(s.start, s.stop) for s in region])).encode())
                    digest.update(b'' if addable is None else np.ascontiguousarray(addable).tobytes())
                    digest.update(np.ascontiguousarray(child).tobytes())

    def hole_conditions(shape, cell):
        hole = Pos(cell[0] + .5, cell[1] + .5, shape[2] / 2) * Box(.8, .8, shape[2] - .2)
        return dataclasses.replace(bar_conditions(shape=shape), excludes=[('hole', hole.wrapped)])

    for conditions, design, budget in ((*detour_conditions(), 120),
                                       (hole_conditions((8, 7, 2), (3, 3)), np.ones((8, 7, 2), bool), 1400),
                                       (hole_conditions((6, 7, 2), (3, 4)), np.ones((6, 7, 2), bool), 120)):
        result = bs.find_initial_feasible(design, conditions, max_evaluations=budget)
        digest.update(repr([(t.operation, t.mask_hash, t.outcome, t.parent) for t in result.history]).encode())
        digest.update(repr((result.status, result.termination, result.evaluations,
                            result.diagnostics['candidates'])).encode())
    assert digest.hexdigest() == GOLDEN_STAGE3_M1_DIGEST


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
    assert _opening_satisfied(best.mask, 2)
    casualties = sum(step['peel']['casualties'] for step in result.diagnostics['reduction']['steps'])
    assert casualties > 0  # 制約が実際に効いている(0なら題材が制約を試せていない)
    assert best.material_mm3 > unconstrained.best_feasible.material_mm3  # 薄くしすぎない分だけ材料が残る


def test_min_width_search_starts_from_the_design_space_thickness_opening():
    """2x2の棒の脇に離れた1セル突起が2つ。突起は m=2 の立方に入らず局所判定では1セルずつしか抜けないが、
    設計空間を opening で絞ってから探索するので、合格形状に突起は入らない。"""
    shape = (10, 4, 4)
    design = np.zeros(shape, bool)
    design[:, :2, :2] = True
    design[2, 2, 0] = design[7, 2, 0] = True
    conditions = bar_conditions(shape=shape)
    nodes = lambda i: [(i * 5 + j) * 5 + k for j in range(3) for k in range(3)]
    conditions = dataclasses.replace(conditions, table=Table(conditions.grid, {1: nodes(0), 2: nodes(10)}))
    result = bs.find_best_feasible(design, conditions, max_evaluations=200, min_width_cells=2)
    assert result.status == 'feasible'
    assert _opening_satisfied(result.best_feasible.mask, 2)
    assert not result.best_feasible.mask[2, 2, 0] and not result.best_feasible.mask[7, 2, 0]


def test_a_design_space_that_already_violates_min_width_still_reaches_a_conforming_shape():
    """落ちたら、違反した設計空間を入口の opening で絞らずに出発している(絞らない探索は評価23回、絞ると1回)。"""
    design = np.ones((6, 3, 3), bool)
    for cell in [(2, 0, 0), (3, 0, 0), (2, 0, 2), (3, 0, 2)]:
        design[cell] = False
    conditions = bar_conditions(shape=design.shape)
    assert not _opening_satisfied(design, 2)
    result = bs.find_initial_feasible(design, conditions, max_evaluations=40, min_width_cells=2)
    assert result.status == 'feasible' and result.evaluations <= 5
    assert _opening_satisfied(result.best_feasible.mask, 2)
    assert result.diagnostics['min_width'] == dict(cells=2, design_cells=int(design.sum()),
                                                   trimmed_cells=int(design.sum() - ndimage.binary_opening(
                                                       design, structure=np.ones((2, 2, 2), bool)).sum()))
    assert result.diagnostics['min_width']['trimmed_cells'] > 0


def test_the_min_width_check_runs_before_any_fem_evaluation(monkeypatch):
    """落ちたら、規則を破る候補にFEM(hanger 2.4mm で1回100秒超)を払っている。"""
    calls = []
    real_inspect = acceptance.inspect
    monkeypatch.setattr(acceptance, 'inspect', lambda *a, **k: calls.append(1) or real_inspect(*a, **k))
    monkeypatch.setattr(bs, '_mask_satisfies_min_width', lambda mask, m, exempt=None: False)
    conditions = bar_conditions(shape=(4, 2, 2))
    result = bs.find_initial_feasible(np.ones((4, 2, 2), bool), conditions, max_evaluations=6, min_width_cells=2)
    assert result.status == 'no_feasible_found' and result.best_feasible is None
    assert calls == []
    assert result.history[0].checks == dict(min_width='fail')
    assert all(t.checks == dict(min_width='fail') for t in result.history if t.outcome != 'no_component')


def test_a_candidate_that_fails_the_min_width_check_is_not_returned_and_the_search_goes_on(monkeypatch):
    """落ちたら、大域判定に落ちた候補が合格として返る、または探索が止まる(`add` は局所判定しない)。"""
    real = bs._mask_satisfies_min_width
    state = {'first': True}

    def first_fails(mask, m, exempt=None):
        verdict = real(mask, m, exempt) and not state['first']
        state['first'] = False
        return verdict

    monkeypatch.setattr(bs, '_mask_satisfies_min_width', first_fails)
    conditions = bar_conditions(shape=(6, 3, 3))
    result = bs.find_initial_feasible(np.ones((6, 3, 3), bool), conditions, max_evaluations=40, min_width_cells=2)
    assert result.history[0].outcome == 'infeasible' and result.history[0].checks == dict(min_width='fail')
    assert result.status == 'feasible' and result.best_feasible.trial_index > 0
    assert _opening_satisfied(result.best_feasible.mask, 2)


def test_trimming_the_design_space_can_cut_a_preserved_region_and_is_then_refused_at_entry_with_the_count():
    """保持が突起にしか重ならないと、絞った空間では保持が成り立たず入口で止まる(規則適合の形が存在しない)。m=1 は合格する。"""
    shape = (10, 4, 4)
    design = np.zeros(shape, bool)
    design[:, :2, :2] = True
    design[5, 2, 0] = True  # 1セルの突起
    conditions = bar_conditions(shape=shape, keep_box=Pos(5.5, 2.5, 0.5) * Box(.9, .9, .9))
    nodes = lambda i: [(i * 5 + j) * 5 + k for j in range(3) for k in range(3)]
    conditions = dataclasses.replace(conditions, table=Table(conditions.grid, {1: nodes(0), 2: nodes(10)}))
    assert bs.find_best_feasible(design, conditions, max_evaluations=60, min_width_cells=1).status == 'feasible'
    result = bs.find_best_feasible(design, conditions, max_evaluations=60, min_width_cells=2)
    assert result.status == 'input_contradiction' and result.evaluations == 0
    assert result.diagnostics['min_width'] == dict(cells=2, design_cells=41, trimmed_cells=1)


def test_min_width_with_a_design_space_that_holds_no_m_cube_is_refused_at_entry():
    """`(4, 1, 1)` は y・z が1セルなので m=2 の立方が1つも入らない。opening で設計空間が空になり、
    条件節点が材料に接しないので探索を始めずに `boundary_mapping_failed` で止まる。"""
    conditions = bar_conditions(shape=(4, 1, 1))
    result = bs.find_best_feasible(np.ones((4, 1, 1), bool), conditions, max_evaluations=50, min_width_cells=2)
    assert result.status == 'boundary_mapping_failed' and result.best_feasible is None
    assert result.evaluations == 0
    assert result.diagnostics['min_width'] == dict(cells=2, design_cells=4, trimmed_cells=4)


# ---------------------------------------------------------------------------
# 最小幅の規則から保持ソリッド由来のセルを外す(`SearchConditions.keep_cells`)
# ---------------------------------------------------------------------------
def _bar_with_protrusion(*, keep_protrusion: bool, second_protrusion: bool = False):
    """棒の脇の1セルの突起(5,2,0)は m=2 の立方に入らない。`keep_protrusion` でそこだけを保持セルにする。"""
    shape = (10, 4, 4)
    design = np.zeros(shape, bool)
    design[:, :2, :2] = True
    design[5, 2, 0] = True
    keep = np.zeros(shape, bool)
    keep[5, 2, 0] = keep_protrusion
    if second_protrusion:
        design[2, 2, 0] = True
    conditions = bar_conditions(shape=shape, keep_box=Pos(5.5, 2.5, 0.5) * Box(.9, .9, .9))
    nodes = lambda i: [(i * 5 + j) * 5 + k for j in range(3) for k in range(3)]
    conditions = dataclasses.replace(conditions, table=Table(conditions.grid, {1: nodes(0), 2: nodes(10)}),
                                     keep_cells=keep)
    return design, conditions


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


def _assert_min_width_invariants(mask, m, exempt=None):
    """`peel` の不変条件(規則(`exempt` を除く)・非正則頂点0・1成分・閉じた空洞0)を、全体を数え直して確かめる。"""
    assert bs._mask_satisfies_min_width(mask, m, exempt)
    assert acceptance.boundary_check(mask)['irregular_vertices'] == 0
    assert acceptance.connectivity_check(mask)['components'] == 1
    assert acceptance.void_components(mask) == 1


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
            assert stats.skipped_thickness == 0 and stats.islands == 0
            removed = {int(c) for c in np.flatnonzero(mask & ~after)}
            assert len(removed) == stats.picked + stats.casualties
            assert {int(c) for c in np.flatnonzero(exempt.ravel())} & removed <= set(stats.picked_cells.tolist())
            _assert_min_width_invariants(after, m, exempt)
            assert not _rule_violations(after, m, exempt).any()
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
    _assert_min_width_invariants(after, 2, exempt)


def test_an_exempt_cell_missing_from_the_mask_still_counts_as_material_for_the_others():
    """落ちたら、探索で削られて `mask` に無い保持セルが材料に数えられていない(最終形状では保持の実形状が戻るのに、その幅が判定に入らない)。"""
    block = np.ones((2, 2, 2), bool)
    block[0, 0, 0] = False
    keep = np.zeros_like(block)
    keep[0, 0, 0] = True
    assert bs._mask_satisfies_min_width(block, 2, keep)
    assert not bs._mask_satisfies_min_width(block, 2)


def test_peel_refuses_an_island_removal_that_takes_the_cube_a_missing_kept_cell_bridged():
    """落ちたら、島の除去が規則を壊す手を通している。m=3 で、主成分の層(x=2)と島になる層(x=4)は、mask に無い保持セルの層(x=3)を
    挟んで1つの 3x3x3 の立方に入る。保持セルの通路(mask にある)の途中を抜くと層(x=4)が島になるが、消すと x=2 の層が被覆を失う。"""
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
    """落ちたら、抜いた保持セルが材料でなくなって巻き添えを出している。2x2x4 の棒の (0,0,1) は普通は z=0・z=1 の層の被覆を奪うが、
    保持セルなら1セルだけが抜け、残りは(抜けた保持セルを材料に数えて)被覆されたまま。"""
    bar = np.ones((2, 2, 4), bool)
    region = (slice(0, 1), slice(0, 1), slice(1, 2))
    keep = np.zeros_like(bar)
    keep[0, 0, 1] = True
    assert min_width.lost_coverage(bar, region, 2)[1].any()
    assert not min_width.lost_coverage(bar, region, 2, keep)[1].any()
    gone, stats, after = _bite_of(bar, (0, 0, 1), exempt=keep)
    assert gone == {(0, 0, 1)} and (stats.picked, stats.casualties) == (1, 0)
    assert bs._mask_satisfies_min_width(after, 2, keep) and not bs._mask_satisfies_min_width(after, 2)


def test_the_design_space_opening_keeps_the_keep_cells_and_still_trims_the_others():
    """落ちたら、保持ソリッド由来のセルが設計空間から消える(入口で保持の重なりが足りず止まる)、または
    保持でない薄いセルまで残る。"""
    design, conditions = _bar_with_protrusion(keep_protrusion=True, second_protrusion=True)
    result = bs.find_initial_feasible(design, conditions, max_evaluations=60, min_width_cells=2)
    assert result.status == 'feasible'
    assert result.diagnostics['min_width'] == dict(cells=2, design_cells=int(design.sum()), trimmed_cells=1)
    mask = result.best_feasible.mask
    assert mask[5, 2, 0] and not mask[2, 2, 0]
    assert bs._mask_satisfies_min_width(mask, 2, conditions.keep_cells) and not _opening_satisfied(mask, 2)


def test_without_keep_cells_the_same_design_space_is_trimmed_and_refused_at_entry():
    """対照: `keep_cells` が無ければ従来どおり保持の突起が落ち、入口で止まる。"""
    design, conditions = _bar_with_protrusion(keep_protrusion=False)
    result = bs.find_initial_feasible(design, conditions, max_evaluations=60, min_width_cells=2)
    assert result.status == 'input_contradiction' and result.evaluations == 0


def test_the_full_search_keeps_the_keep_cell_and_every_non_keep_cell_stays_in_a_cube():
    """落ちたら、段階4まで通した形で保持でないセルが m 立方を外れている(対象外なのは保持セルだけ)。"""
    design, conditions = _bar_with_protrusion(keep_protrusion=True)
    result = bs.find_best_feasible(design, conditions, max_evaluations=200, min_width_cells=2)
    mask = result.best_feasible.mask
    assert result.status == 'feasible' and mask[5, 2, 0]
    cube = np.ones((2, 2, 2), bool)
    assert (mask & ~ndimage.binary_opening(mask, structure=cube)).any()
    keep = conditions.keep_cells
    assert not (mask & ~keep & ~ndimage.binary_opening(mask | keep, structure=cube)).any()


def test_with_min_width_one_keep_cells_change_neither_the_result_nor_the_cost(monkeypatch):
    """落ちたら、既定(m=1)で最小幅の判定関数が呼ばれている、または `keep_cells` が探索の結果を変えている。"""
    conditions = bar_conditions(shape=(8, 3, 3), target_safety_factor=.001)
    design = np.ones((8, 3, 3), bool)
    plain = bs.find_best_feasible(design, conditions, max_evaluations=200, min_width_cells=1)

    def forbidden(*args, **kwargs):
        raise AssertionError('m=1 で最小幅の判定を呼んだ')

    for owner, name in ((min_width, 'cube_opening'), (min_width, 'lost_coverage'), (min_width, 'casualties'),
                        (min_width, 'bite_verdict'), (min_width, 'cover_additions'),
                        (bs, '_mask_satisfies_min_width')):
        monkeypatch.setattr(owner, name, forbidden)
    marked = dataclasses.replace(conditions, keep_cells=np.ones((8, 3, 3), bool))
    with_keep = bs.find_best_feasible(design, marked, max_evaluations=200, min_width_cells=1)
    assert with_keep.evaluations == plain.evaluations and len(with_keep.history) == len(plain.history)
    assert np.array_equal(with_keep.best_feasible.mask, plain.best_feasible.mask)
    assert bs._min_width_exempt(marked, (8, 3, 3), 1) is None


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


def test_a_bite_that_two_different_picked_cells_produce_is_not_solved_again_after_it_failed(monkeypatch):
    """落ちたら、不合格になった一口が、別のセルを選んだ手でもう一度できたときに FEM を解き直している。
    2セル厚の板では、上面のセルも下面のセルも同じ貫通柱を一口にする(順位の先頭に両方を置く)。"""
    shape = (6, 6, 2)
    plate = np.ones(shape, bool)
    conditions = bar_conditions(shape=shape)
    top, bottom = (int(np.ravel_multi_index(c, shape)) for c in ((3, 3, 1), (3, 3, 0)))
    monkeypatch.setattr(bs, 'ranked_cells', lambda mask, energy: np.array(
        [top, bottom] + [c for c in range(plate.size) if c not in (top, bottom)]))
    _stub_evaluation(monkeypatch)  # どの候補も安全率で不合格
    seed = bs.FeasibleModel(mask=plate, before_mask=plate, report=_fake_report(plate, 'pass'), trial_index=0,
                            energy=np.ones(shape))
    initial = bs.SearchResult(status='feasible', termination='seed', best_feasible=seed,
                              history=[bs.Trial(index=0, parent=None, operation='seed', mask_hash=seed.mask_hash,
                                                before_mm3=72., material_mm3=72., removed_islands_mm3=0.,
                                                outcome='ok', safety_factor=.5)],
                              evaluations=1, conditions_identifier=bs.evaluation_identifier(conditions))
    result = bs._reduce_volume(initial, plate, conditions, max_evaluations=400, is_cancelled=None,
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
    for step, following in zip(steps, steps[1:]):
        if step['outcome'] == 'accepted':
            assert following['multiplier'] == min(1.0, 2 * step['multiplier'])
        elif step['peel']['picked'] > 1:
            assert following['multiplier'] == step['multiplier'] / 2
        else:
            assert following['multiplier'] == step['multiplier']
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
    assert long_reduction['safety_excluded_cells'] > long_reduction['single_cell_patience'] - 1  # 通算では patience を超えて外している


def test_a_single_cell_failure_that_is_not_a_safety_failure_is_not_counted_toward_patience(monkeypatch):
    """落ちたら、計算失敗(`unknown`)が安全率の不合格として数えられ、patience=1 で止まってしまう。"""
    conditions, design, _ = appendage_conditions()
    original = acceptance.inspect

    def unsolvable(material, before, grid, **kwargs):
        if kwargs.get('fields') is not None and material.sum() < design.sum() - 6:
            report = original(material, before, grid, **{**kwargs, 'fields': None})
            report['checks']['safety'] = dict(status='unknown', reason='evaluation_error', error='stub')
            report['status'] = 'unknown'
            return report
        return original(material, before, grid, **kwargs)

    monkeypatch.setattr(acceptance, 'inspect', unsolvable)
    result = bs.find_best_feasible(design, conditions, max_evaluations=400, single_cell_patience=1)
    reduction = result.diagnostics['reduction']
    assert result.termination == 'converged' and reduction['stop_reason'] == 'no_removable_cell'
    assert reduction['safety_excluded_cells'] == 0
    assert any(step['peel']['picked'] == 1 and step['outcome'] == 'rejected' for step in reduction['steps'])


@pytest.mark.parametrize('patience', [0, -1, 1.5, '2', True, False])
def test_single_cell_patience_must_be_a_positive_integer_or_none(patience):
    conditions, design, _ = appendage_conditions()
    with pytest.raises(ValueError):
        bs.find_best_feasible(design, conditions, max_evaluations=10, single_cell_patience=patience)


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
    assert possible > 500  # 全部先に作れば数百枚になる題材(表面の削除464+追加144)で、5枚しか作っていない

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
    assert eager_masks > 10_000
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
        # 探索は容量+作業配列の数枚しか足さない。全候補を先に作る方式の 1/200 以下。
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


def partial_region_conditions():
    """`appendage_conditions` と同じ棒だが、保持・除外CADが格子の一部にだけ掛かる(境界のセルが部分的に重なる)。"""
    conditions, design, appendage = appendage_conditions()
    keep = Pos(5.3, 1.1, 0.9) * Box(7.4, 1.8, 1.6)
    hole = Pos(5.2, 3.4, 1.0) * Box(2.3, 0.9, 1.7)
    return dataclasses.replace(conditions, preserves=[('keep', keep.wrapped)], excludes=[('hole', hole.wrapped)]), design


def test_the_search_is_identical_with_and_without_the_region_overlap_precompute():
    """落ちたら、前計算が旧経路と違う合否を返して探索の道筋を変えている。"""
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
    """候補の評価は前計算から引き、ブーリアンで取り直すのは採用形状(段階3の最終再検査と段階4の最終再検査)だけ。"""
    conditions, design = partial_region_conditions()
    direct_calls, inspects = [], []
    original_direct, original_inspect = acceptance.intersection_volume, acceptance.inspect
    monkeypatch.setattr(acceptance, 'intersection_volume',
                        lambda *args, **kwargs: direct_calls.append(1) or original_direct(*args, **kwargs))
    monkeypatch.setattr(acceptance, 'inspect',
                        lambda *args, **kwargs: inspects.append(1) or original_inspect(*args, **kwargs))
    result = bs.find_best_feasible(design, conditions, max_evaluations=60)
    assert result.status == 'feasible' and len(inspects) > 6
    # 検査1回の保持1本+除外1本 = 2回。最終再検査は段階3で1回、段階4で保持・除外だけを1回。
    assert len(direct_calls) == 4, (len(direct_calls), len(inspects))
    assert result.diagnostics['reduction']['final_region_recheck'] in ('pass', 'not_needed')
    assert conditions.region_overlaps is not None and len(conditions.region_overlaps) == 2


def test_inspect_kwargs_drop_the_precompute_together_with_the_bbox_cache():
    """`bbox_cache` を外した再検査(§3)は、前計算も外して旧経路のブーリアンで取り直す。"""
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


def test_a_failed_final_region_recheck_ends_the_reduction_on_the_verified_stage3_shape(monkeypatch):
    conditions, design = partial_region_conditions()
    initial = bs.find_initial_feasible(design, conditions, max_evaluations=60)
    monkeypatch.setattr(bs, '_recheck_regions_without_precompute', lambda best, seed, conditions: (seed, 'failed:exclude:fail'))
    result = bs._reduce_volume(initial, design, conditions, max_evaluations=60, is_cancelled=None)
    assert result.best_feasible is initial.best_feasible and result.termination == 'evaluation_error'
    assert result.diagnostics['reduction']['final_region_recheck'] == 'failed:exclude:fail'


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


def _cg_iterations(run):
    with fem.collect_solve_stats() as stats:
        result = run()
    return result, stats['cg_iterations']


def _warm_start_bar():
    conditions = bar_conditions((12, 3, 3), solver='cg')
    full = np.ones((12, 3, 3), bool)
    return conditions, full, conditions.inspect_kwargs(bbox_cache=None)


def test_the_solved_displacement_is_returned_in_fields_and_used_as_the_cg_start_without_changing_the_answer():
    """落ちたら、`u0` が CG に届いていない(反復が減らない)か、初期値で安全率が変わっている。"""
    conditions, full, kwargs = _warm_start_bar()
    fields: dict = {}
    _, cold_iterations = _cg_iterations(lambda: acceptance.inspect(full, full, conditions.grid, **kwargs, fields=fields))
    displacement = fields['displacement']
    assert displacement.shape == (conditions.table.node_grid.ndof,) and fields['warm_start'] == 'cold'
    cold = acceptance.inspect(full, full, conditions.grid, **kwargs)
    warm_fields: dict = {}
    warm, warm_iterations = _cg_iterations(
        lambda: acceptance.inspect(full, full, conditions.grid, **kwargs, fields=warm_fields, u0=displacement))
    assert warm_fields['warm_start'] == 'used' and warm_iterations < cold_iterations
    assert warm['checks']['safety']['safety_factor'] == pytest.approx(cold['checks']['safety']['safety_factor'], rel=1e-8)


@pytest.mark.parametrize('label, make', [
    ('shape', lambda u: u[:10]),
    ('shape', lambda u: u[np.flatnonzero(u)]),
    ('shape', lambda u: u.astype(int)),
    ('shape', lambda u: np.stack([u, u])),
    ('shape', lambda u: [[0.0], [1.0, 2.0]]),
    ('non_finite', lambda u: np.where(np.arange(u.size) == 5, np.nan, u)),
])
def test_a_start_vector_that_does_not_fit_falls_back_to_cold_without_raising(label, make):
    """落ちたら、合わない `u0` が例外になるか、cold と違う答え・違う反復数になっている(黙って別の初期値で走っている)。"""
    conditions, full, kwargs = _warm_start_bar()
    fields: dict = {}
    acceptance.inspect(full, full, conditions.grid, **kwargs, fields=fields)
    bad = make(fields['displacement'])
    candidate = full.copy()
    candidate[6, 2, 2] = False
    cold, cold_iterations = _cg_iterations(lambda: acceptance.inspect(candidate, full, conditions.grid, **kwargs))
    got: dict = {}
    fallen, iterations = _cg_iterations(
        lambda: acceptance.inspect(candidate, full, conditions.grid, **kwargs, fields=got, u0=bad))
    assert got['warm_start'] == f'fell_back:{label}' and iterations == cold_iterations
    assert fallen['checks']['safety']['safety_factor'] == cold['checks']['safety']['safety_factor']


def test_the_reduction_passes_the_adopted_displacement_on_and_warm_and_cold_agree_on_this_small_fixture(monkeypatch):
    """落ちたら、前の形の変位が次の候補へ渡っていない、またはこの小さい題材で初期値が探索の合否・採用形状を変えている(実機の hanger は一致しない)。"""
    conditions, design, _ = appendage_conditions()
    conditions = dataclasses.replace(conditions, solver='cg')
    original = acceptance.inspect
    passed = []

    def spy(material, before, grid, **kwargs):
        passed.append(kwargs.get('u0'))
        return original(material, before, grid, **kwargs)

    monkeypatch.setattr(acceptance, 'inspect', spy)
    warm = bs.find_best_feasible(design, conditions, max_evaluations=200, warm_start=True)
    warm_calls, passed[:] = list(passed), []
    cold = bs.find_best_feasible(design, conditions, max_evaluations=200)
    ndof = conditions.table.node_grid.ndof
    assert any(u0 is not None and u0.shape == (ndof,) for u0 in warm_calls) and all(u0 is None for u0 in passed)
    assert warm.best_feasible.displacement.shape == (ndof,) and cold.best_feasible.displacement is None
    assert (warm.termination, warm.evaluations, warm.best_feasible.mask_hash) == (
        cold.termination, cold.evaluations, cold.best_feasible.mask_hash)
    assert [(t.mask_hash, t.outcome) for t in warm.history] == [(t.mask_hash, t.outcome) for t in cold.history]
    for got, expected in zip(warm.history, cold.history):
        assert got.safety_factor == pytest.approx(expected.safety_factor, rel=1e-8)
    reduction = warm.diagnostics['reduction']
    assert reduction['warm_start'] is True and reduction['warm_start_used'] > 0 and reduction['warm_start_fallbacks'] == 0
    assert cold.diagnostics['reduction']['warm_start_used'] == 0


def test_a_start_vector_the_evaluation_discards_is_counted_as_a_fallback(monkeypatch):
    """落ちたら、cold に落ちた回数が診断に残らず、効果が出ない理由を追えない。"""
    conditions, design, _ = appendage_conditions()
    conditions = dataclasses.replace(conditions, solver='cg')
    original = acceptance.inspect
    monkeypatch.setattr(acceptance, 'inspect', lambda material, before, grid, **kwargs: original(
        material, before, grid, **{**kwargs, 'u0': None if kwargs.get('u0') is None else kwargs['u0'][:-1]}))
    result = bs.find_best_feasible(design, conditions, max_evaluations=200, warm_start=True)
    reduction = result.diagnostics['reduction']
    assert reduction['warm_start_used'] == 0 and reduction['warm_start_fallbacks'] > 0
    assert result.termination == 'converged'


def test_the_direct_solver_ignores_the_start_vector_and_it_is_not_counted_as_used():
    """落ちたら、初期値を使わない直接解法で `warm_start_used` が増え、効果の診断が実態と食い違う。"""
    conditions = bar_conditions((12, 3, 3), solver='direct')
    full = np.ones((12, 3, 3), bool)
    kwargs = conditions.inspect_kwargs(bbox_cache=None)
    fields: dict = {}
    acceptance.inspect(full, full, conditions.grid, **kwargs, fields=fields)
    got: dict = {}
    acceptance.inspect(full, full, conditions.grid, **kwargs, fields=got, u0=fields['displacement'])
    assert got['warm_start'] == 'unused:direct'


def test_a_start_vector_used_by_a_failed_first_attempt_is_still_counted_when_the_solver_retry_runs(monkeypatch):
    """落ちたら、解法再試行の前の `fields.clear()` で、1回目に初期値を使った事実が診断から消える。"""
    conditions, design, _ = appendage_conditions()
    conditions = dataclasses.replace(conditions, solver='cg')
    original = stress.solve_displacement

    def fail_when_started(model, rho, penal=3.0, **kwargs):
        if kwargs.get('u0') is not None:
            raise RuntimeError('stub')
        return original(model, rho, penal, **kwargs)

    monkeypatch.setattr(stress, 'solve_displacement', fail_when_started)
    result = bs.find_best_feasible(design, conditions, max_evaluations=200, warm_start=True)
    retried = [t for t in result.history if t.operation.startswith('solver_retry')]
    reduction = result.diagnostics['reduction']
    assert retried and reduction['warm_start_used'] == len(retried) and reduction['warm_start_fallbacks'] == 0

