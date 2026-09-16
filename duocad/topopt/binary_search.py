"""初期合格探索と削除・追加・置換による体積削減(計画書§8 段階3・段階4
`docs/PLAN_BINARY_STRESS_SEARCH.md`)。

`find_initial_feasible` は設計空間の全材料マスクから出発し、最初に6条件
(`acceptance.inspect`)へ合格する候補を見つけて `best_feasible` に確定するところまで
(段階3、「見つける」だけ)。`find_best_feasible` はそこから削除・追加・置換の巡回で
体積を減らし続け、より小さい合格モデルが見つかるたびに `best_feasible` を更新する
(段階4、「小さくする」)。前者は後者の第一段としてそのまま使われる。

責務の分け方(§5): 候補生成は§4.3の軸整列ブロック、島処理は§4.4、6条件の判定は
`acceptance.inspect` そのもの(このモジュールは6条件を再実装しない)。
"""
from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from itertools import product
from typing import Any, Optional

import numpy as np
from scipy import ndimage

from duocad import kernel

from . import acceptance
from .material import DEFAULT_ALLOWABLE_MPA, DEFAULT_YOUNGS_MODULUS_MPA

# `acceptance.connectivity_check` と同じ6近傍の定義(§4.4 冒頭)。島処理の合否と
# 最終の`connectivity_check`が別の近傍で数えると、ここで生かした成分が向こうで
# 複数成分に見えることがある —— 定義は1箇所(この値)に揃える。
NEIGHBOURHOOD_STRUCTURE = ndimage.generate_binary_structure(3, 1)


# ---------------------------------------------------------------------------
# 評価条件(§4.1「評価条件の識別子」)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SearchConditions:
    """1回の探索を通して固定される評価条件一式。`acceptance.inspect` への引数をそのまま束ねる
    (意味の正はそちら)。`table` は `anchor.AnchorTable` と同じ面/節点インタフェースを持てばよい。
    """

    grid: Any
    table: Any
    fixed_faces: tuple
    load_faces: tuple
    forces: tuple
    preserves: tuple
    excludes: tuple
    target_safety_factor: float
    allowable_mpa: float = DEFAULT_ALLOWABLE_MPA
    youngs_modulus_mpa: float = DEFAULT_YOUNGS_MODULUS_MPA
    percentile: Optional[float] = None
    solver: str = 'cg'
    volume_atol: float = 1e-9
    volume_rtol: float = 1e-9
    bbox_cache: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'fixed_faces', tuple(int(f) for f in self.fixed_faces))
        object.__setattr__(self, 'load_faces', tuple(int(f) for f in self.load_faces))
        object.__setattr__(self, 'forces', tuple(tuple(float(x) for x in row) for row in self.forces))
        object.__setattr__(self, 'preserves', tuple(self.preserves))
        object.__setattr__(self, 'excludes', tuple(self.excludes))

    def inspect_kwargs(self, *, bbox_cache: Any = ...) -> dict:
        """`acceptance.inspect` へ展開する引数。`percentile` 未指定はそちら側の既定に委ねる
        (既定値をここへ複製すると、acceptance.py 側が既定を変えたときに黙って食い違う)。
        """
        cache = self.bbox_cache if bbox_cache is ... else bbox_cache
        kwargs = dict(table=self.table, fixed_faces=self.fixed_faces, load_faces=self.load_faces,
                      forces=self.forces, preserves=self.preserves, excludes=self.excludes,
                      target_safety_factor=self.target_safety_factor, allowable_mpa=self.allowable_mpa,
                      youngs_modulus_mpa=self.youngs_modulus_mpa, solver=self.solver,
                      volume_atol=self.volume_atol, volume_rtol=self.volume_rtol, bbox_cache=cache)
        if self.percentile is not None:
            kwargs['percentile'] = self.percentile
        return kwargs


def _body_signature(shape: Any, *, cache: Any) -> dict:
    raw = getattr(shape, 'wrapped', shape)
    bounds = kernel.bounding_box(raw, cache=cache)
    return dict(min=[round(float(v), 9) for v in bounds['min']],
                max=[round(float(v), 9) for v in bounds['max']],
                volume=round(float(kernel.volume(raw)), 9))


def evaluation_identifier(conditions: SearchConditions) -> str:
    """格子・固定空間の境界条件・物性・応力規則・検査許容差から評価条件の識別子を作る(§4.1)。

    形状ハッシュ(候補マスクのハッシュ)だけでは、違う入力で出た同じ形の合否を
    再利用してしまう。`visited` はこの識別子とマスクハッシュの組をキーにする。
    """
    payload = dict(
        grid=conditions.grid.as_dict(),
        fixed_faces=sorted(conditions.fixed_faces),
        load_faces=sorted(conditions.load_faces),
        forces=[list(row) for row in conditions.forces],
        preserves=[[name, _body_signature(shape, cache=conditions.bbox_cache)]
                   for name, shape in conditions.preserves],
        excludes=[[name, _body_signature(shape, cache=conditions.bbox_cache)]
                  for name, shape in conditions.excludes],
        target_safety_factor=float(conditions.target_safety_factor),
        allowable_mpa=float(conditions.allowable_mpa),
        youngs_modulus_mpa=float(conditions.youngs_modulus_mpa),
        percentile=None if conditions.percentile is None else float(conditions.percentile),
        solver=str(conditions.solver),
        volume_atol=float(conditions.volume_atol),
        volume_rtol=float(conditions.volume_rtol),
    )
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _mask_hash(mask: np.ndarray) -> str:
    """`acceptance.inspect` の `material_sha256` と同じ式(候補どうしの突き合わせに使う)。"""
    array = np.ascontiguousarray(np.asarray(mask, dtype=bool))
    return hashlib.sha256(array.tobytes()).hexdigest()


# ---------------------------------------------------------------------------
# 島処理(§4.4)
# ---------------------------------------------------------------------------
def _holds_all_conditions(component: np.ndarray, conditions: SearchConditions) -> bool:
    """この成分だけを残したとき、全固定面・荷重面の節点と各保持CADの重なりを満たすか。

    FEMより先に効く軽い前提審査(numpy比較 + CAD重なりだけ)。除外条件はここで見ない
    —— 材料は常に設計空間の部分集合なので、成分を絞っても崩れない(§2、find_initial_feasible
    の「入力矛盾」の判定と同じ前提)。
    """
    live = acceptance.active_nodes(component).ravel()
    for face in conditions.fixed_faces + conditions.load_faces:
        ids = conditions.table.node_ids([face])
        if ids.size == 0 or not bool(live[ids].all()):
            return False
    report = acceptance.region_check(component, conditions.grid, conditions.preserves, preserve=True,
                                      volume_atol=conditions.volume_atol, volume_rtol=conditions.volume_rtol,
                                      cache=conditions.bbox_cache)
    return report['status'] == 'pass'


def _touches_any_condition(component: np.ndarray, conditions: SearchConditions) -> bool:
    """`_holds_all_conditions` と違い、一部の条件に触れているだけで真(除去した島の記録用)。"""
    live = acceptance.active_nodes(component).ravel()
    for face in conditions.fixed_faces + conditions.load_faces:
        ids = conditions.table.node_ids([face])
        if ids.size and bool(live[ids].any()):
            return True
    return False


def resolve_islands(candidate: np.ndarray, conditions: SearchConditions) -> tuple[list, dict]:
    """連結成分ごとに条件成立を調べ、単独で成立する成分だけを候補として返す(§4.4)。

    **最大成分を選ばない。** 複数の成分が単独で成立すれば、それぞれ別候補として返す
    (呼び出し側が両方評価する)。最後に保持を塗り直して復活させることはしない。
    """
    candidate = np.ascontiguousarray(candidate, dtype=bool)
    labels, count = ndimage.label(candidate, structure=NEIGHBOURHOOD_STRUCTURE)
    survivors, removed = [], []
    for label_id in range(1, count + 1):
        component = np.ascontiguousarray(labels == label_id)
        (survivors if _holds_all_conditions(component, conditions) else removed).append(component)
    diagnostics = dict(
        components=count,
        surviving_components=len(survivors),
        removed_islands=[
            dict(cells=int(comp.sum()), volume_mm3=float(comp.sum()) * conditions.grid.element_volume,
                 touches_condition=_touches_any_condition(comp, conditions))
            for comp in removed
        ],
    )
    return survivors, diagnostics


# ---------------------------------------------------------------------------
# 候補生成(§4.3 軸整列ブロック)
# ---------------------------------------------------------------------------
def block_sizes(shape: tuple[int, int, int]) -> tuple[int, ...]:
    """セル単位の 1, 2, 4, ... の決定的な系列。設計空間の最長辺を上限にする。"""
    limit = max(int(n) for n in shape)
    sizes, n = [], 1
    while n <= limit:
        sizes.append(n)
        n *= 2
    return tuple(sizes)


def _block_starts(shape: tuple[int, int, int], size: int):
    """非重複タイル張りのブロック開始添字。軸昇順(i,j,k)の決定的な順序(§4.3)。"""
    return product(*(range(0, n, size) for n in shape))


def repair_candidates(current: np.ndarray, design_space: np.ndarray, size: int):
    """1つのブロック大きさについて削除・追加・置換の候補を、削除優先で生成する(§4.3)。

    削除はブロックと現在の材料の重なり。追加は「設計空間内の、既存材料と面で接する
    空セル群」(§4.3)に限る —— 材料から離れた場所への新設は生成しない。
    境界のブロックは配列の端で自然に切り詰まる(スライスが配列を超えない)。
    """
    current = np.asarray(current, dtype=bool)
    design_space = np.asarray(design_space, dtype=bool)
    adjacent_to_material = ndimage.binary_dilation(current, structure=NEIGHBOURHOOD_STRUCTURE) & ~current
    deletes, adds, replaces = [], [], []
    for lo in _block_starts(current.shape, size):
        hi = tuple(min(l + size, n) for l, n in zip(lo, current.shape))
        region = tuple(slice(l, h) for l, h in zip(lo, hi))
        block = np.zeros(current.shape, dtype=bool)
        block[region] = True
        removable = block & current
        addable = block & design_space & adjacent_to_material
        has_removable, has_addable = removable.any(), addable.any()
        if has_removable:
            deletes.append(('delete', current & ~removable))
        if has_addable:
            adds.append(('add', current | addable))
        if has_removable and has_addable:
            replaces.append(('replace', (current & ~removable) | addable))
    # 各巡回に追加・置換の枠を確保する: 削除だけで候補を使い切らない(§4.3)。
    yield from deletes
    yield from adds
    yield from replaces


def _generate_repairs(current: np.ndarray, design_space: np.ndarray, sizes: tuple[int, ...]):
    """大きい削除から試し、失敗したら小さい領域も試す(§4.3)。`sizes` は降順で渡すこと。"""
    for size in sizes:
        yield from repair_candidates(current, design_space, size)


# ---------------------------------------------------------------------------
# 探索状態(§4.1)
# ---------------------------------------------------------------------------
@dataclass
class Trial:
    """1回の候補試行の記録(history の1行、§4.1)。`checks` は6条件の合否だけ
    (全文の report は best_feasible の方にだけ残し、履歴を膨らませない)。"""

    index: int
    parent: Optional[int]
    operation: str
    mask_hash: str
    before_mm3: float
    material_mm3: float
    removed_islands_mm3: float
    outcome: str  # 'ok' | 'infeasible' | 'unknown' | 'no_component'
    safety_factor: Optional[float] = None
    checks: Optional[dict] = None
    note: Optional[str] = None


@dataclass
class FeasibleModel:
    """`best_feasible`(§4.1)。`report` はキャッシュ無しの最終再検査(§3)の結果。"""

    mask: np.ndarray
    before_mask: np.ndarray
    report: dict
    trial_index: int

    @property
    def mask_hash(self) -> str:
        return self.report['material_sha256']

    @property
    def material_mm3(self) -> float:
        return self.report['volumes']['material_mm3']

    @property
    def safety_factor(self) -> float:
        return self.report['checks']['safety']['safety_factor']


@dataclass
class SearchResult:
    """探索1回の戻り値。`status` と `termination` は別欄(§4.6:「形状の合否とは別の欄にする」)。"""

    status: str  # 'feasible' | 'no_feasible_found' | 'input_contradiction' | 'resolution_insufficient'
    termination: str
    best_feasible: Optional[FeasibleModel]
    history: list
    evaluations: int
    conditions_identifier: str
    diagnostics: dict = field(default_factory=dict)


def _record_trial(history: list, parent: Optional[int], operation: str, report: dict) -> Trial:
    """`acceptance.inspect` の1回の戻り値を history の1行(§4.1)へ写す(append はしない)。"""
    safety = report['checks']['safety']
    return Trial(index=len(history), parent=parent, operation=operation,
                 mask_hash=report['material_sha256'], before_mm3=report['volumes']['before_mm3'],
                 material_mm3=report['volumes']['material_mm3'],
                 removed_islands_mm3=report['volumes']['removed_islands_mm3'],
                 outcome=('ok' if report['status'] == 'pass' else
                          'unknown' if report['status'] == 'unknown' else 'infeasible'),
                 safety_factor=safety.get('safety_factor'),
                 checks={name: check.get('status') for name, check in report['checks'].items()})


def _evaluate_survivor(survivor: np.ndarray, before: np.ndarray, conditions: SearchConditions,
                       *, parent: Optional[int], operation: str, history: list) -> tuple[dict, Trial]:
    """1個の生存成分を検査し、history に記録する(段階3・4共通、§4.5の解法再試行を含む)。

    計算失敗(`unknown`、原因`evaluation_error`)は、同じマスク・条件のまま一度だけ
    別解法で再試行する(§4.5「解法変更による再試行は有限の規則にする」)。それでも
    `unknown` ならそのまま返す —— 幾何修復の生成要否は呼び出し側が判断する。
    """
    report = acceptance.inspect(survivor, before, conditions.grid, **conditions.inspect_kwargs())
    trial = _record_trial(history, parent, operation, report)
    history.append(trial)
    if report['status'] == 'unknown' and report['checks']['safety'].get('reason') == 'evaluation_error':
        alternate = 'direct' if conditions.solver != 'direct' else 'cg'
        retry_kwargs = conditions.inspect_kwargs()
        retry_kwargs['solver'] = alternate
        retry_report = acceptance.inspect(survivor, before, conditions.grid, **retry_kwargs)
        trial = _record_trial(history, trial.index, f'solver_retry:{alternate}', retry_report)
        trial.note = f'同じマスク・条件で解法を{conditions.solver}→{alternate}に変えて再試行'
        history.append(trial)
        report = retry_report
    return report, trial


# ---------------------------------------------------------------------------
# 初期合格探索(§4.2)
# ---------------------------------------------------------------------------
def find_initial_feasible(design_space_mask: np.ndarray, conditions: SearchConditions, *,
                           max_evaluations: int = 200) -> SearchResult:
    """設計空間の全材料マスクから出発し、最初の6条件合格モデルを探す(§4.2、§8 段階3)。

    返却は必ず `best_feasible` から行い、`unknown`(計算失敗)や安全率不足で
    最良モデルを失わない(§4.1)。修復も通常の候補生成・評価・履歴・予算の対象で、
    入れ子の修復手続きは無い —— 幾何不良でも応力不足でも同じ `repair_candidates` を使う
    (§4.2「修復処理を無限に入れ子にしない」)。

    `max_evaluations` は生の候補(frontierから1回取り出す単位)の予算。1つの生の候補が
    複数の生存成分・解法再試行を持つ場合、`acceptance.inspect` の呼び出し回数はこれより
    多くなる(history の行数で見ること)。
    """
    design_space_mask = np.ascontiguousarray(design_space_mask, dtype=bool)
    identifier = evaluation_identifier(conditions)

    # 解像度不足: 拘束・荷重面がこの要素サイズで1つも節点を持たない(探索では直せない)。
    starved = sorted((set(conditions.fixed_faces) | set(conditions.load_faces))
                      & set(conditions.table.empty_faces()))
    if starved:
        return SearchResult(status='resolution_insufficient', termination='resolution_insufficient',
                             best_feasible=None, history=[], evaluations=0,
                             conditions_identifier=identifier, diagnostics=dict(empty_faces=starved))

    # 入力矛盾: 材料は常に設計空間の部分集合(§2)なので、保持との重なりは材料を減らすほど
    # 減るかそのまま。設計空間そのもの(取れる最大の材料)で既に重なりが足りなければ、
    # どの候補を試しても直らない —— 探索で確定させず、ここで判定する。
    # (除外条件は逆方向〈材料を減らすほど満たしやすくなる〉なので、この判定は使えない。)
    preserve_on_full = acceptance.region_check(design_space_mask, conditions.grid, conditions.preserves,
                                                preserve=True, volume_atol=conditions.volume_atol,
                                                volume_rtol=conditions.volume_rtol, cache=conditions.bbox_cache)
    if preserve_on_full['status'] == 'fail':
        return SearchResult(status='input_contradiction', termination='input_contradiction',
                             best_feasible=None, history=[], evaluations=0,
                             conditions_identifier=identifier,
                             diagnostics=dict(preserve_on_full_design_space=preserve_on_full))

    sizes = tuple(reversed(block_sizes(design_space_mask.shape)))  # 大きい削除から試す(§4.3)
    # 「無理な巡回」の候補(生の候補)と「実際に評価した」候補(島除去後)は別の集合にする
    # ——生の候補が単一成分ならこの2つは同じマスクになるので、同じ集合に入れると
    # 島除去後の評価そのものが「もう見た」判定で丸ごとスキップされてしまう。
    visited_candidates: set[tuple[str, str]] = set()
    visited_survivors: set[tuple[str, str]] = set()
    history: list[Trial] = []
    # frontier は「島除去より前」の生マスクだけを積む。**その候補自身の `before`
    # (=島除去前)は常にその候補自身であって、生成元(親)の候補ではない** ——
    # add/replace は親の材料の外へ広がるので、親を`before`に使うと
    # `acceptance.inspect`の`material⊆before`契約(§2)を破って例外になる。
    frontier: deque = deque([(design_space_mask, 'initial', None)])
    evaluations = 0

    while frontier:
        if evaluations >= max_evaluations:
            return SearchResult(status='no_feasible_found', termination='evaluation_limit',
                                 best_feasible=None, history=history, evaluations=evaluations,
                                 conditions_identifier=identifier)
        candidate, operation, parent = frontier.popleft()
        raw_hash = _mask_hash(candidate)
        raw_key = (raw_hash, identifier)
        if raw_key in visited_candidates:
            continue
        visited_candidates.add(raw_key)
        evaluations += 1
        before = candidate  # この候補自身が、これから求める成分の「島除去前」

        survivors, island_diag = resolve_islands(candidate, conditions)
        if not survivors:
            before_mm3 = float(candidate.sum()) * conditions.grid.element_volume
            trial = Trial(index=len(history), parent=parent, operation=operation, mask_hash=raw_hash,
                          before_mm3=before_mm3, material_mm3=0.0, removed_islands_mm3=before_mm3,
                          outcome='no_component',
                          note=f'{island_diag["components"]} 個の連結成分のいずれも固定面・荷重面・'
                               '保持の全条件を単独で満たさない')
            history.append(trial)
            for op, next_mask in _generate_repairs(candidate, design_space_mask, sizes):
                frontier.append((next_mask, op, trial.index))
            continue

        for survivor in survivors:
            survivor_key = (_mask_hash(survivor), identifier)
            if survivor_key in visited_survivors:
                continue
            visited_survivors.add(survivor_key)

            report, trial = _evaluate_survivor(survivor, before, conditions, parent=parent,
                                               operation=operation, history=history)

            if report['status'] != 'pass':
                if report['status'] != 'unknown':  # 計算失敗を不合格の証拠にしない(§4.5)
                    for op, next_mask in _generate_repairs(survivor, design_space_mask, sizes):
                        frontier.append((next_mask, op, trial.index))
                continue

            # 最良合格モデルをキャッシュなしで再検査する(§3)。ここが失敗したら成功を返さず、
            # 候補と診断を残して探索を続ける(§4.6「最終再検査が失敗したら成功を返さない」)。
            final_report = acceptance.inspect(survivor, before, conditions.grid, history=list(history),
                                              converged=True, max_evaluations=max_evaluations,
                                              **conditions.inspect_kwargs(bbox_cache=None))
            if final_report['status'] != 'pass':
                trial.note = 'キャッシュ無しの最終再検査が不合格/unknownだったため候補から外した'
                trial.outcome = 'unknown' if final_report['status'] == 'unknown' else 'infeasible'
                if trial.outcome != 'unknown':  # 計算失敗を不合格の証拠にしない(§4.5)。他は通常どおり修復対象。
                    for op, next_mask in _generate_repairs(survivor, design_space_mask, sizes):
                        frontier.append((next_mask, op, trial.index))
                continue

            best = FeasibleModel(mask=survivor, before_mask=before, report=final_report,
                                 trial_index=trial.index)
            return SearchResult(status='feasible', termination='initial_feasible_found',
                                best_feasible=best, history=history, evaluations=evaluations,
                                conditions_identifier=identifier)

    return SearchResult(status='no_feasible_found', termination='neighbourhood_exhausted',
                         best_feasible=None, history=history, evaluations=evaluations,
                         conditions_identifier=identifier)


# ---------------------------------------------------------------------------
# 削除・追加・置換による体積削減(§4.3、§8 段階4)
# ---------------------------------------------------------------------------
# 既定値は小型題材(tests/test_binary_search.py の detour 系フィクスチャ)で決めた1組
# (docs/PLAN_BINARY_STRESS_SEARCH.md §4.3 追記に採用理由と観測した試行数を記録)。
DEFAULT_FRONTIER_CAPACITY = 48
DEFAULT_MAX_CONSECUTIVE_ADDS = 2


def _push_children(frontier: deque, current: np.ndarray, design_space_mask: np.ndarray,
                   sizes: tuple[int, ...], parent_trial_index: Optional[int], streak: int,
                   max_consecutive_adds: int) -> None:
    """`current` から生成した削除・追加・置換の子候補をfrontierへ積む。

    `streak` は `current` に至るまでの**連続する**'add'操作の回数。'add'の子は
    streakを+1して積むが、それが上限を超えるなら積まない(§4.3「frontierの…追加の
    連続回数…を明示的な設定にする」——支えの追加(§4.3項2)は許すが、体積を増やし
    続けるだけの枝が予算を無限に消費しないようにする)。'delete'・'replace'の子は
    常にstreakを0へ戻して積む(体積を増やさない操作なので回数を数えない)。
    """
    for op, next_mask in _generate_repairs(current, design_space_mask, sizes):
        next_streak = streak + 1 if op == 'add' else 0
        if op == 'add' and next_streak > max_consecutive_adds:
            continue
        frontier.append((next_mask, op, parent_trial_index, next_streak))


def _is_smaller_feasible(candidate_report: dict, current_best: FeasibleModel, *, volume_atol: float) -> bool:
    """新しい合格候補が `best_feasible` を置き換えるべきか(§4.1)。

    体積が真に小さければ置換。同体積(許容差以内)なら安全率が高い方、それも同値なら
    マスクハッシュの辞書順で決定的に選ぶ(乱数・挿入順に依存させない)。
    """
    volume = candidate_report['volumes']['material_mm3']
    current_volume = current_best.material_mm3
    if volume < current_volume - volume_atol:
        return True
    if volume > current_volume + volume_atol:
        return False
    safety = candidate_report['checks']['safety']['safety_factor']
    current_safety = current_best.safety_factor
    if safety > current_safety + 1e-9:
        return True
    if safety < current_safety - 1e-9:
        return False
    return candidate_report['material_sha256'] < current_best.mask_hash


def _reduce_volume(initial: SearchResult, design_space_mask: np.ndarray, conditions: SearchConditions,
                   *, max_evaluations: int, frontier_capacity: int, max_consecutive_adds: int,
                   is_cancelled) -> SearchResult:
    """`initial.best_feasible` を出発点に、削除・追加・置換で体積を減らし続ける(§4.3、段階4)。

    `initial` は既に `status == 'feasible'` であること(呼び出し側の `find_best_feasible`
    が保証する)。返却は常に `status == 'feasible'`(悪化しても初期合格モデルへ戻せる)。
    frontierは**評価前の生マスク**を持つ(段階3と同じ設計 —— `max_evaluations` は
    frontierから取り出した生候補の数で数える)。容量 `frontier_capacity` を超えたら
    最も古い(＝一番先に生成された)候補から捨てる(`deque(maxlen=...)`、完全に決定的)。

    **既知の簡略化**: §4.3が求める「幾何合格・応力評価成功・目標達成・体積・安全率の余裕」
    による優先度付けはしていない(単純なFIFO)。体積昇順のような安価な事前指標だけで
    優先度を付ける設計を試したところ、'add'系候補が常に'delete'系より低優先度になり
    frontier容量を増やしても改善が見つからなくなる(FIFOより悪化する)ことを実測したため、
    今回は見送った(`docs/PLAN_BINARY_STRESS_SEARCH.md` §4.3 追記に detail)。
    """
    identifier = initial.conditions_identifier
    history = list(initial.history)
    evaluations = initial.evaluations
    best = initial.best_feasible
    sizes = tuple(reversed(block_sizes(design_space_mask.shape)))
    visited_candidates: set[tuple[str, str]] = set()
    visited_survivors: set[tuple[str, str]] = {(best.mask_hash, identifier)}
    frontier: deque = deque(maxlen=frontier_capacity)
    _push_children(frontier, best.mask, design_space_mask, sizes, best.trial_index, 0, max_consecutive_adds)

    termination = 'neighbourhood_exhausted'
    while frontier:
        if is_cancelled is not None and is_cancelled():
            termination = 'cancelled'
            break
        if evaluations >= max_evaluations:
            termination = 'evaluation_limit'
            break
        candidate, operation, parent, streak = frontier.popleft()
        raw_key = (_mask_hash(candidate), identifier)
        if raw_key in visited_candidates:
            continue
        visited_candidates.add(raw_key)
        evaluations += 1
        before = candidate

        survivors, _island_diag = resolve_islands(candidate, conditions)
        if not survivors:
            before_mm3 = float(candidate.sum()) * conditions.grid.element_volume
            trial = Trial(index=len(history), parent=parent, operation=operation, mask_hash=raw_key[0],
                          before_mm3=before_mm3, material_mm3=0.0, removed_islands_mm3=before_mm3,
                          outcome='no_component',
                          note='いずれの連結成分も固定面・荷重面・保持の全条件を単独で満たさない')
            history.append(trial)
            _push_children(frontier, candidate, design_space_mask, sizes, trial.index, streak,
                          max_consecutive_adds)
            continue

        for survivor in survivors:
            survivor_key = (_mask_hash(survivor), identifier)
            if survivor_key in visited_survivors:
                continue
            visited_survivors.add(survivor_key)

            report, trial = _evaluate_survivor(survivor, before, conditions, parent=parent,
                                               operation=operation, history=history)

            if report['status'] == 'unknown':
                continue  # 計算失敗は不合格の証拠にしない(§4.5)。修復候補も積まない。

            if report['status'] != 'pass':
                _push_children(frontier, survivor, design_space_mask, sizes, trial.index, streak,
                              max_consecutive_adds)
                continue

            if _is_smaller_feasible(report, best, volume_atol=conditions.volume_atol):
                # 最良合格モデルをキャッシュなしで再検査する(§3)。失敗したら不採用のまま
                # 従前の best_feasible を保持して探索を続ける(§4.6)。
                final_report = acceptance.inspect(survivor, before, conditions.grid, history=list(history),
                                                  converged=True, max_evaluations=max_evaluations,
                                                  **conditions.inspect_kwargs(bbox_cache=None))
                if final_report['status'] == 'pass':
                    best = FeasibleModel(mask=survivor, before_mask=before, report=final_report,
                                         trial_index=trial.index)
                else:
                    trial.note = ('キャッシュ無しの最終再検査が不合格/unknownだったため'
                                  '体積改善は不採用(従前のbest_feasibleを保持)')

            # 合格した生存成分は、best_feasibleを更新したか否かに関わらず子候補を生成する
            # —— 一時的な体積増加(add)を経由してから初めて縮む形状があるため
            # (§4.3「支えを作り替える探索の足場として、体積増加もfrontier内では許す」)。
            _push_children(frontier, survivor, design_space_mask, sizes, trial.index, streak,
                          max_consecutive_adds)

    return SearchResult(status='feasible', termination=termination, best_feasible=best, history=history,
                         evaluations=evaluations, conditions_identifier=identifier)


def find_best_feasible(design_space_mask: np.ndarray, conditions: SearchConditions, *,
                       max_evaluations: int = 1000,
                       frontier_capacity: int = DEFAULT_FRONTIER_CAPACITY,
                       max_consecutive_adds: int = DEFAULT_MAX_CONSECUTIVE_ADDS,
                       is_cancelled=None) -> SearchResult:
    """初期合格探索(段階3)に続けて削除・追加・置換で体積を減らす、単一の探索入口(§3、段階4)。

    まず `find_initial_feasible` で最初の6条件合格を探し、見つからなければその
    `SearchResult`(`no_feasible_found` / `input_contradiction` / `resolution_insufficient`)を
    そのまま返す(改善しようがないので段階4は何もしない)。見つかれば、そこから
    `_reduce_volume` で体積削減を続ける。`max_evaluations` は**両段階を通した1本の予算**
    (§4.1「評価条件の識別子」と同じ思想で、初期探索と削減探索を別予算にしない)。

    `is_cancelled` は引数無しで呼べる callable。真を返した時点の `best_feasible` を
    キャッシュ無し再検査済みのまま返す(§4.6「中断時は合格チェックポイントを記録する」)。
    削減段階の各frontier取り出しの前にだけ確認する —— `find_initial_feasible` 自体は
    中断に対応していない(段階3の契約のまま、段階4はそこへ手を入れない)。
    """
    identifier = evaluation_identifier(conditions)
    if is_cancelled is not None and is_cancelled():
        return SearchResult(status='no_feasible_found', termination='cancelled', best_feasible=None,
                             history=[], evaluations=0, conditions_identifier=identifier)
    initial = find_initial_feasible(design_space_mask, conditions, max_evaluations=max_evaluations)
    if initial.status != 'feasible':
        return initial
    return _reduce_volume(initial, design_space_mask, conditions, max_evaluations=max_evaluations,
                          frontier_capacity=frontier_capacity, max_consecutive_adds=max_consecutive_adds,
                          is_cancelled=is_cancelled)
