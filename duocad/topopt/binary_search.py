"""初期合格探索と削除・追加・置換による体積削減(計画書§8 段階3・段階4
`docs/PLAN_BINARY_STRESS_SEARCH.md`。入口検査と候補管理は `docs/FIX_BINARY_SEARCH_INITIALIZATION.md`)。

`find_initial_feasible` は設計空間の全材料マスクから出発し、最初に6条件
(`acceptance.inspect`)へ合格する候補を見つけて `best_feasible` に確定するところまで
(段階3、「見つける」だけ)。`find_best_feasible` はそこから削除・追加・置換の巡回で
体積を減らし続け、より小さい合格モデルが見つかるたびに `best_feasible` を更新する
(段階4、「小さくする」)。前者は後者の第一段としてそのまま使われる。

責務の分け方(§5): 候補生成は§4.3の軸整列ブロック、島処理は§4.4、6条件の判定は
`acceptance.inspect` そのもの(このモジュールは6条件を再実装しない)。

候補の持ち方(修正C): frontier は**親マスク**を容量以内で保持し、子候補は
`(操作, ブロック領域)` の記述として親から1つずつ遅延生成して評価の直前にだけマスクにする。
生成した候補を配列のまま溜めない(保持マスク数は `frontier_capacity` + 最良合格 + 作業中の
数個で、評価回数に比例しない)。両段階が同じ frontier・予算・中断の契約を使う。
"""
from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from itertools import product
from typing import Any, Iterator, Optional

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

    `table` は**探索用の確定表**(`anchor.build_anchors(..., allowed_nodes=...)` で材料に接する
    節点だけへ対応付けたもの)であること。`mapping` はその検証結果(`anchor.condition_mapping`)で、
    無ければ未対応付けの表として入口の診断に書かれる。確定後は探索中に一切再配分しない。
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
    mapping: Any = None

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


def _face_node_ids(table: Any, face: int) -> np.ndarray:
    """面の節点(空の面は空配列。`AnchorTable.node_ids` は空の面で例外を出すので先に見る)。"""
    if int(face) in set(int(f) for f in table.empty_faces()):
        return np.zeros(0, dtype=np.int64)
    return np.asarray(table.node_ids([int(face)]), dtype=np.int64)


def _boundary_signature(conditions: SearchConditions) -> dict:
    """確定した節点・拘束自由度・節点荷重と対応付け規則(修正A §3.3「評価条件ハッシュに含める」)。

    面添字だけでは、同じ面添字でも別の表(幾何の表と材料に対応付けた確定表)を区別できない。
    """
    table = conditions.table
    faces: dict = {}
    for face in conditions.fixed_faces:
        ids = _face_node_ids(table, face)
        entry = dict(nodes=hashlib.sha256(ids.tobytes()).hexdigest(), count=int(ids.size))
        if ids.size:
            dofs = np.asarray(table.fixed_dofs([int(face)]), dtype=np.int64)
            entry['dofs'] = hashlib.sha256(dofs.tobytes()).hexdigest()
        faces[f'fixed_{int(face)}'] = entry
    for face, force in zip(conditions.load_faces, conditions.forces):
        ids = _face_node_ids(table, face)
        entry = dict(nodes=hashlib.sha256(ids.tobytes()).hexdigest(), count=int(ids.size))
        if ids.size:
            vector = np.asarray(table.load_vector([int(face)], force), dtype=float)
            nonzero = np.flatnonzero(vector)
            digest = hashlib.sha256(nonzero.astype(np.int64).tobytes())
            digest.update(np.round(vector[nonzero], 12).tobytes())
            entry['load'] = digest.hexdigest()
        faces[f'load_{int(face)}'] = entry
    stats = getattr(table, 'stats', None) or {}
    rule = dict(mapping=stats.get('mapping', 'unknown'), radius_ratio=stats.get('radius_ratio'),
                sample_ratio=stats.get('sample_ratio'))
    if isinstance(conditions.mapping, dict):
        rule['mapping_rule'] = conditions.mapping.get('rule')
    return dict(faces=faces, rule=rule)


def evaluation_identifier(conditions: SearchConditions) -> str:
    """格子・固定空間の境界条件(確定した節点・荷重・拘束と対応付け規則)・物性・応力規則・
    検査許容差から評価条件の識別子を作る(§4.1)。

    形状ハッシュ(候補マスクのハッシュ)だけでは、違う入力で出た同じ形の合否を
    再利用してしまう。`visited` はこの識別子とマスクハッシュの組をキーにする。
    """
    payload = dict(
        grid=conditions.grid.as_dict(),
        fixed_faces=sorted(conditions.fixed_faces),
        load_faces=sorted(conditions.load_faces),
        forces=[list(row) for row in conditions.forces],
        boundary=_boundary_signature(conditions),
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
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _mask_hash(mask: np.ndarray) -> str:
    """`acceptance.inspect` の `material_sha256` と同じ式(候補どうしの突き合わせに使う)。"""
    array = np.ascontiguousarray(np.asarray(mask, dtype=bool))
    return hashlib.sha256(array.tobytes()).hexdigest()


# ---------------------------------------------------------------------------
# 入口の必要条件(修正B §4): 確定表の全節点が全設計空間の材料に接するか
# ---------------------------------------------------------------------------
def uncovered_condition_nodes(material: np.ndarray, conditions: SearchConditions) -> list:
    """条件面ごとに、確定表の節点のうち `material` に接しないものを数える。

    候補Mは設計空間Dの部分集合なので、Mに接する節点はDに接する節点の部分集合。Dで接しない
    固定節点はD内の削除・追加・置換では被覆できない —— 探索前にここで判定する。
    """
    live = acceptance.active_nodes(material).ravel()
    rows = []
    for side, faces in (('fixed', conditions.fixed_faces), ('load', conditions.load_faces)):
        for face in faces:
            ids = _face_node_ids(conditions.table, face)
            lost = ids[~live[ids]] if ids.size else ids
            rows.append(dict(condition=f'{side}_face_{int(face)}', side=side, face=int(face),
                             nodes=int(ids.size), uncovered_nodes=int(lost.size),
                             uncovered_node_ids=lost[:20].tolist()))
    return rows


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
    除去した島は数値(セル数・体積・条件に触れていたか)だけを残し、マスクは保持しない。
    """
    candidate = np.ascontiguousarray(candidate, dtype=bool)
    labels, count = ndimage.label(candidate, structure=NEIGHBOURHOOD_STRUCTURE)
    survivors, removed = [], []
    for label_id in range(1, count + 1):
        component = np.ascontiguousarray(labels == label_id)
        if _holds_all_conditions(component, conditions):
            survivors.append(component)
        else:
            removed.append(dict(cells=int(component.sum()),
                                volume_mm3=float(component.sum()) * conditions.grid.element_volume,
                                touches_condition=_touches_any_condition(component, conditions)))
    diagnostics = dict(components=count, surviving_components=len(survivors), removed_islands=removed)
    return survivors, diagnostics


# ---------------------------------------------------------------------------
# 候補生成(§4.3 軸整列ブロック。修正C §5.1 遅延生成)
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


def _block_region(lo: tuple, size: int, shape: tuple) -> tuple:
    """境界のブロックは配列の端で自然に切り詰まる(スライスが配列を超えない)。"""
    return tuple(slice(int(l), min(int(l) + size, int(n))) for l, n in zip(lo, shape))


def _local_addable(mask: np.ndarray, design_space: np.ndarray, region: tuple) -> np.ndarray:
    """ブロック内の「設計空間内で既存材料と面で接する空セル」(§4.3 追加)。

    ブロックの周囲1セル(面で接する相手はそこにしか無い)だけを見て膨張させる。全格子の膨張を
    候補ごとに払わない。
    """
    halo = tuple(slice(max(s.start - 1, 0), min(s.stop + 1, n)) for s, n in zip(region, mask.shape))
    sub = mask[halo]
    adjacent = ndimage.binary_dilation(sub, structure=NEIGHBOURHOOD_STRUCTURE) & ~sub
    inner = tuple(slice(s.start - h.start, s.stop - h.start) for s, h in zip(region, halo))
    return adjacent[inner] & design_space[region]


def _op_descriptors(mask: np.ndarray, design_space: np.ndarray, size: int, op: str):
    """1つの操作・1つのブロック大きさの有効な候補記述 `(op, region, addable)` を順に生成する。

    有効性(削除は材料が要る、追加は接する空セルが要る、置換は両方)はブロックの範囲だけを見て
    決め、マスクは作らない。`addable` はブロック大の小配列(削除では None)。
    """
    for lo in _block_starts(mask.shape, size):
        region = _block_region(lo, size, mask.shape)
        if op == 'delete':
            if mask[region].any():
                yield ('delete', region, None)
            continue
        addable = _local_addable(mask, design_space, region)
        if not addable.any():
            continue
        if op == 'add':
            yield ('add', region, addable)
        elif op == 'replace' and mask[region].any():
            yield ('replace', region, addable)


def candidate_descriptors(mask: np.ndarray, design_space: np.ndarray, sizes: tuple[int, ...],
                          *, allow_add: bool = True) -> Iterator[tuple]:
    """親 `mask` の子候補の記述を、大きいブロックから、各大きさで削除→追加→置換を1つずつ
    巡回して生成する(§4.3「削除優先で、各巡回に追加・置換の枠を確保する」、修正C §5.2)。

    操作別の生成器を巡回するので、削除候補が何万あっても追加・置換が後回しにならない。
    `allow_add=False` なら追加は生成しない(連続追加の上限。置換は体積を増やさないので残す)。
    """
    ops = ('delete', 'add', 'replace') if allow_add else ('delete', 'replace')
    for size in sizes:
        alive = [_op_descriptors(mask, design_space, size, op) for op in ops]
        while alive:
            for generator in list(alive):
                try:
                    yield next(generator)
                except StopIteration:
                    alive.remove(generator)


def _materialize(parent: np.ndarray, design_space: np.ndarray, op: str, region: tuple,
                 addable: Optional[np.ndarray]) -> np.ndarray:
    """候補記述から子マスクを作る(親は複製し、in-place で変えない)。評価の直前にだけ呼ぶ。"""
    child = np.array(parent, dtype=bool, copy=True)
    if op == 'delete':
        child[region] = False
    elif op == 'add':
        child[region] |= addable
    elif op == 'replace':
        child[region] = addable
    else:
        raise ValueError(f'未知の操作 {op!r}')
    return child


def repair_candidates(current: np.ndarray, design_space: np.ndarray, size: int):
    """1つのブロック大きさについて削除・追加・置換の候補 `(op, mask)` を遅延生成する(§4.3)。

    削除はブロックと現在の材料の重なり。追加は「設計空間内の、既存材料と面で接する
    空セル群」(§4.3)に限る —— 材料から離れた場所への新設は生成しない。
    マスクは1つ要求されるたびに1つだけ作る(全候補を配列で先に作らない)。
    """
    current = np.asarray(current, dtype=bool)
    design_space = np.asarray(design_space, dtype=bool)
    for op, region, addable in candidate_descriptors(current, design_space, (int(size),)):
        yield op, _materialize(current, design_space, op, region, addable)


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
    """探索1回の戻り値。`status` と `termination` は別欄(§4.6:「形状の合否とは別の欄にする」)。

    `termination` の語彙: `initial_feasible_found` / `neighbourhood_exhausted`(frontier を捨てずに
    使い切った)/ `frontier_truncated`(容量超過で親を捨てたことがあり、近傍を尽くしたとは言えない)/
    `evaluation_limit` / `generation_limit`(候補記述の予算)/ `cancelled` /
    `resolution_insufficient` / `boundary_mapping_failed` / `input_contradiction`。
    `diagnostics['frontier']` / `['candidates']` に保持数の実測・除外・生成数が入る。
    """

    status: str  # 'feasible' | 'no_feasible_found' | 'input_contradiction' | 'resolution_insufficient' | 'boundary_mapping_failed'
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
# frontier と予算(修正C §5)
# ---------------------------------------------------------------------------
# 既定値は小型題材(tests/test_binary_search.py の detour 系フィクスチャ)で決めた1組
# (docs/PLAN_BINARY_STRESS_SEARCH.md §4.3 追記に採用理由と観測した試行数を記録)。
DEFAULT_FRONTIER_CAPACITY = 48
DEFAULT_MAX_CONSECUTIVE_ADDS = 2
#: 候補記述の予算(生成側)の既定 = この値 × 評価予算。無効・重複候補だけで評価数が増えない
#: 場合にも生成を止めるための上限で、評価予算とは別に数える(修正C §5.2)。
DEFAULT_CANDIDATES_PER_EVALUATION = 64
#: 診断に残す除外した親の試行番号の上限(記録の大きさを抑えるためだけの値)。
EVICTION_RECORD = 50


@dataclass
class _Node:
    """frontier に保持する親。`mask` は読み取り専用で、子は必ず複製から作る。"""

    mask: np.ndarray
    trial_index: int
    streak: int
    children: Iterator


class _Frontier:
    """親マスクを `capacity` 以内で保持し、親を巡回して子記述を1つずつ取り出す。

    取り出しは**親の round-robin**(先頭の親から次の子を1つ取り、その親を末尾へ回す)。
    1つの親の子が何万あっても、他の親(直前に見つけた小さい合格モデルなど)の子が順番を
    待ち続けない。容量を超えて積むときは**最も古い親(試行番号が最小)**を捨てる —— 積んだ後に
    まとめて切り詰めるのではなく、その1回の push でだけ保持数が動く。捨てた回数と試行番号は
    診断に残り、捨てたことがある探索は「近傍を尽くした」と表示しない。
    """

    def __init__(self, capacity: int) -> None:
        if int(capacity) < 1:
            raise ValueError(f'frontier_capacity は1以上であること(いま {capacity!r})')
        self.capacity = int(capacity)
        self._nodes: deque = deque()
        self.peak = 0
        self.evictions = 0
        self.evicted_trials: list[int] = []
        self.retired = 0

    def __len__(self) -> int:
        return len(self._nodes)

    def push(self, node: _Node) -> None:
        if len(self._nodes) >= self.capacity:
            oldest = min(range(len(self._nodes)), key=lambda i: self._nodes[i].trial_index)
            victim = self._nodes[oldest]
            del self._nodes[oldest]
            self.evictions += 1
            if len(self.evicted_trials) < EVICTION_RECORD:
                self.evicted_trials.append(int(victim.trial_index))
        self._nodes.append(node)
        self.peak = max(self.peak, len(self._nodes))

    def next_descriptor(self) -> Optional[tuple]:
        """先頭の親の次の子記述 `(node, descriptor)`。尽きた親は退役させる。空なら None。"""
        while self._nodes:
            node = self._nodes[0]
            try:
                descriptor = next(node.children)
            except StopIteration:
                self._nodes.popleft()
                self.retired += 1
                continue
            self._nodes.rotate(-1)
            return node, descriptor
        return None

    def summary(self) -> dict:
        return dict(capacity=self.capacity, peak_retained=self.peak, retained=len(self._nodes),
                    evictions=self.evictions, evicted_trials=list(self.evicted_trials), retired=self.retired)


@dataclass
class _Budget:
    """評価予算(生の候補を評価した回数)と生成予算(マスクにした候補記述の数。重複も数える)。"""

    max_evaluations: int
    max_candidates: int
    evaluations: int = 0
    generated: int = 0
    duplicates: int = 0

    def summary(self) -> dict:
        return dict(generated=self.generated, duplicates=self.duplicates, limit=self.max_candidates,
                    evaluations=self.evaluations, evaluation_limit=self.max_evaluations)


def _candidate_budget(max_evaluations: int, max_candidates: Optional[int]) -> int:
    return int(max_candidates) if max_candidates is not None else DEFAULT_CANDIDATES_PER_EVALUATION * int(max_evaluations)


def _streak_after(op: str, streak: int) -> int:
    """`op` を経た後の連続する'add'の回数('delete'・'replace' は0へ戻す。§4.3)。"""
    return streak + 1 if op == 'add' else 0


def _node(mask: np.ndarray, trial_index: int, streak: int, design_space_mask: np.ndarray,
          sizes: tuple[int, ...], max_consecutive_adds: int) -> _Node:
    mask.setflags(write=False)
    return _Node(mask=mask, trial_index=trial_index, streak=streak,
                 children=candidate_descriptors(mask, design_space_mask, sizes,
                                                allow_add=streak < max_consecutive_adds))


def _pull(frontier: _Frontier, design_space_mask: np.ndarray, budget: _Budget, visited: set,
          identifier: str, is_cancelled) -> Any:
    """次の未評価候補 `(mask, op, parent_node)` か、終了理由の文字列。

    生成中・評価前に中断を見る。重複(既に見たマスク)は評価数を増やさず、生成予算だけを消費する。
    """
    while True:
        if is_cancelled is not None and is_cancelled():
            return 'cancelled'
        pulled = frontier.next_descriptor()
        if pulled is None:
            return 'frontier_truncated' if frontier.evictions else 'neighbourhood_exhausted'
        if budget.generated >= budget.max_candidates:
            return 'generation_limit'
        node, (op, region, addable) = pulled
        budget.generated += 1
        child = _materialize(node.mask, design_space_mask, op, region, addable)
        key = (_mask_hash(child), identifier)
        if key in visited:
            budget.duplicates += 1
            continue
        visited.add(key)
        return child, op, node


def _diagnostics(frontier: _Frontier, budget: _Budget, **extra: Any) -> dict:
    return dict(frontier=frontier.summary(), candidates=budget.summary(), **extra)


def _no_component_trial(history: list, candidate: np.ndarray, raw_hash: str, parent: Optional[int],
                        operation: str, conditions: SearchConditions, island_diag: dict) -> Trial:
    before_mm3 = float(candidate.sum()) * conditions.grid.element_volume
    trial = Trial(index=len(history), parent=parent, operation=operation, mask_hash=raw_hash,
                  before_mm3=before_mm3, material_mm3=0.0, removed_islands_mm3=before_mm3,
                  outcome='no_component',
                  note=f'{island_diag["components"]} 個の連結成分のいずれも固定面・荷重面・'
                       '保持の全条件を単独で満たさない')
    history.append(trial)
    return trial


# ---------------------------------------------------------------------------
# 初期合格探索(§4.2)
# ---------------------------------------------------------------------------
def find_initial_feasible(design_space_mask: np.ndarray, conditions: SearchConditions, *,
                           max_evaluations: int = 200,
                           frontier_capacity: int = DEFAULT_FRONTIER_CAPACITY,
                           max_consecutive_adds: int = DEFAULT_MAX_CONSECUTIVE_ADDS,
                           max_candidates: Optional[int] = None,
                           is_cancelled=None) -> SearchResult:
    """設計空間の全材料マスクから出発し、最初の6条件合格モデルを探す(§4.2、§8 段階3)。

    入口で探索では直せない状態を確定させ、候補生成もFEMも始めない:
    `resolution_insufficient`(条件面に節点が無い)、`boundary_mapping_failed`(確定表の節点が
    全設計空間の材料にすら接しない。修正B)、`input_contradiction`(全設計空間でも保持と重ならない)。

    返却は必ず `best_feasible` から行い、`unknown`(計算失敗)や安全率不足で
    最良モデルを失わない(§4.1)。修復も通常の候補生成・評価・履歴・予算の対象で、
    入れ子の修復手続きは無い —— 幾何不良でも応力不足でも同じ候補生成を使う
    (§4.2「修復処理を無限に入れ子にしない」)。

    `max_evaluations` は生の候補(frontierから1回取り出す単位)の予算。1つの生の候補が
    複数の生存成分・解法再試行を持つ場合、`acceptance.inspect` の呼び出し回数はこれより
    多くなる(history の行数で見ること)。`max_candidates` は候補記述の予算(既定は
    `DEFAULT_CANDIDATES_PER_EVALUATION × max_evaluations`)。`is_cancelled` は生成中・評価前に見る。
    """
    design_space_mask = np.array(design_space_mask, dtype=bool, copy=True)
    design_space_mask.setflags(write=False)
    identifier = evaluation_identifier(conditions)

    # 解像度不足: 拘束・荷重面がこの要素サイズで1つも節点を持たない(探索では直せない)。
    starved = sorted((set(conditions.fixed_faces) | set(conditions.load_faces))
                      & set(int(f) for f in conditions.table.empty_faces()))
    if starved:
        return SearchResult(status='resolution_insufficient', termination='resolution_insufficient',
                             best_feasible=None, history=[], evaluations=0,
                             conditions_identifier=identifier, diagnostics=dict(empty_faces=starved))

    # 境界条件の対応付け: 確定表の節点が全設計空間の材料に接しないなら、どの候補でも接しない
    # (修正B §4)。表が材料に対応付けた確定表か、材料を見ない幾何の表かも診断に書く。
    coverage = uncovered_condition_nodes(design_space_mask, conditions)
    uncovered = [row for row in coverage if row['uncovered_nodes']]
    if uncovered:
        stats = getattr(conditions.table, 'stats', None) or {}
        return SearchResult(status='boundary_mapping_failed', termination='boundary_mapping_failed',
                             best_feasible=None, history=[], evaluations=0,
                             conditions_identifier=identifier,
                             diagnostics=dict(uncovered_condition_nodes=uncovered,
                                              table_mapping=stats.get('mapping', 'unknown'),
                                              mapped=conditions.mapping is not None,
                                              mapping=conditions.mapping))

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

    if is_cancelled is not None and is_cancelled():
        return SearchResult(status='no_feasible_found', termination='cancelled', best_feasible=None,
                             history=[], evaluations=0, conditions_identifier=identifier)

    sizes = tuple(reversed(block_sizes(design_space_mask.shape)))  # 大きい削除から試す(§4.3)
    budget = _Budget(max_evaluations=int(max_evaluations),
                     max_candidates=_candidate_budget(max_evaluations, max_candidates))
    frontier = _Frontier(frontier_capacity)
    # 「生の候補」と「実際に評価した」候補(島除去後)は別の集合にする —— 生の候補が単一成分なら
    # この2つは同じマスクになるので、同じ集合に入れると島除去後の評価そのものが
    # 「もう見た」判定で丸ごとスキップされてしまう。
    visited_candidates: set[tuple[str, str]] = set()
    visited_survivors: set[tuple[str, str]] = set()
    history: list[Trial] = []

    def consider(candidate: np.ndarray, operation: str, parent: Optional[int], streak: int) -> Optional[SearchResult]:
        """生の候補1つを島除去→評価し、不合格なら親として積む。合格が確定したら結果を返す。

        その候補自身の `before`(=島除去前)は常にその候補自身であって、生成元(親)ではない ——
        add/replace は親の材料の外へ広がるので、親を `before` に使うと `acceptance.inspect` の
        `material⊆before` 契約(§2)を破る。
        """
        candidate.setflags(write=False)
        budget.evaluations += 1
        raw_hash = _mask_hash(candidate)
        survivors, island_diag = resolve_islands(candidate, conditions)
        if not survivors:
            trial = _no_component_trial(history, candidate, raw_hash, parent, operation, conditions, island_diag)
            frontier.push(_node(candidate, trial.index, streak, design_space_mask, sizes, max_consecutive_adds))
            return None
        for survivor in survivors:
            survivor_key = (_mask_hash(survivor), identifier)
            if survivor_key in visited_survivors:
                continue
            visited_survivors.add(survivor_key)
            report, trial = _evaluate_survivor(survivor, candidate, conditions, parent=parent,
                                               operation=operation, history=history)
            if report['status'] != 'pass':
                if report['status'] != 'unknown':  # 計算失敗を不合格の証拠にしない(§4.5)
                    frontier.push(_node(survivor, trial.index, streak, design_space_mask, sizes, max_consecutive_adds))
                continue
            # 最良合格モデルをキャッシュなしで再検査する(§3)。ここが失敗したら成功を返さず、
            # 候補と診断を残して探索を続ける(§4.6「最終再検査が失敗したら成功を返さない」)。
            final_report = acceptance.inspect(survivor, candidate, conditions.grid, history=list(history),
                                              converged=True, max_evaluations=max_evaluations,
                                              **conditions.inspect_kwargs(bbox_cache=None))
            if final_report['status'] != 'pass':
                trial.note = 'キャッシュ無しの最終再検査が不合格/unknownだったため候補から外した'
                trial.outcome = 'unknown' if final_report['status'] == 'unknown' else 'infeasible'
                if trial.outcome != 'unknown':  # 計算失敗を不合格の証拠にしない(§4.5)。他は通常どおり修復対象。
                    frontier.push(_node(survivor, trial.index, streak, design_space_mask, sizes, max_consecutive_adds))
                continue
            survivor.setflags(write=False)
            best = FeasibleModel(mask=survivor, before_mask=candidate, report=final_report,
                                 trial_index=trial.index)
            return SearchResult(status='feasible', termination='initial_feasible_found',
                                best_feasible=best, history=history, evaluations=budget.evaluations,
                                conditions_identifier=identifier, diagnostics=_diagnostics(frontier, budget))
        return None

    visited_candidates.add((_mask_hash(design_space_mask), identifier))
    found = consider(design_space_mask, 'initial', None, 0)
    if found is not None:
        return found
    while True:
        if budget.evaluations >= budget.max_evaluations:
            termination = 'evaluation_limit'
            break
        pulled = _pull(frontier, design_space_mask, budget, visited_candidates, identifier, is_cancelled)
        if isinstance(pulled, str):
            termination = pulled
            break
        candidate, operation, node = pulled
        found = consider(candidate, operation, node.trial_index, _streak_after(operation, node.streak))
        if found is not None:
            return found
    return SearchResult(status='no_feasible_found', termination=termination, best_feasible=None,
                         history=history, evaluations=budget.evaluations, conditions_identifier=identifier,
                         diagnostics=_diagnostics(frontier, budget))


# ---------------------------------------------------------------------------
# 削除・追加・置換による体積削減(§4.3、§8 段階4)
# ---------------------------------------------------------------------------
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
                   max_candidates: Optional[int] = None, is_cancelled) -> SearchResult:
    """`initial.best_feasible` を出発点に、削除・追加・置換で体積を減らし続ける(§4.3、段階4)。

    `initial` は既に `status == 'feasible'` であること(呼び出し側の `find_best_feasible`
    が保証する)。返却は常に `status == 'feasible'`(悪化しても初期合格モデルへ戻せる)。
    frontier・予算・中断は `find_initial_feasible` と同じ契約(`_Frontier` / `_Budget` / `_pull`)。
    評価予算は両段階を通した1本なので `initial.evaluations` から数え続ける。

    **既知の簡略化**: §4.3が求める「幾何合格・応力評価成功・目標達成・体積・安全率の余裕」
    による優先度付けはしていない(親の round-robin と、各親の中の大きいブロック優先・
    削除→追加→置換の巡回だけ)。
    """
    identifier = initial.conditions_identifier
    history = list(initial.history)
    best = initial.best_feasible
    sizes = tuple(reversed(block_sizes(design_space_mask.shape)))
    design_space_mask = np.array(design_space_mask, dtype=bool, copy=True)
    design_space_mask.setflags(write=False)
    started = initial.diagnostics.get('candidates', {}) if isinstance(initial.diagnostics, dict) else {}
    budget = _Budget(max_evaluations=int(max_evaluations),
                     max_candidates=_candidate_budget(max_evaluations, max_candidates),
                     evaluations=int(initial.evaluations),
                     generated=int(started.get('generated', 0)), duplicates=int(started.get('duplicates', 0)))
    frontier = _Frontier(frontier_capacity)
    visited_candidates: set[tuple[str, str]] = set()
    visited_survivors: set[tuple[str, str]] = {(best.mask_hash, identifier)}
    seed = np.array(best.mask, dtype=bool, copy=True)
    frontier.push(_node(seed, best.trial_index, 0, design_space_mask, sizes, max_consecutive_adds))

    while True:
        if budget.evaluations >= budget.max_evaluations:
            termination = 'evaluation_limit'
            break
        pulled = _pull(frontier, design_space_mask, budget, visited_candidates, identifier, is_cancelled)
        if isinstance(pulled, str):
            termination = pulled
            break
        candidate, operation, node = pulled
        streak = _streak_after(operation, node.streak)
        parent = node.trial_index
        candidate.setflags(write=False)
        budget.evaluations += 1

        survivors, island_diag = resolve_islands(candidate, conditions)
        if not survivors:
            trial = _no_component_trial(history, candidate, _mask_hash(candidate), parent, operation,
                                        conditions, island_diag)
            frontier.push(_node(candidate, trial.index, streak, design_space_mask, sizes, max_consecutive_adds))
            continue

        for survivor in survivors:
            survivor_key = (_mask_hash(survivor), identifier)
            if survivor_key in visited_survivors:
                continue
            visited_survivors.add(survivor_key)

            report, trial = _evaluate_survivor(survivor, candidate, conditions, parent=parent,
                                               operation=operation, history=history)

            if report['status'] == 'unknown':
                continue  # 計算失敗は不合格の証拠にしない(§4.5)。修復候補も積まない。

            if report['status'] != 'pass':
                frontier.push(_node(survivor, trial.index, streak, design_space_mask, sizes, max_consecutive_adds))
                continue

            if _is_smaller_feasible(report, best, volume_atol=conditions.volume_atol):
                # 最良合格モデルをキャッシュなしで再検査する(§3)。失敗したら不採用のまま
                # 従前の best_feasible を保持して探索を続ける(§4.6)。
                final_report = acceptance.inspect(survivor, candidate, conditions.grid, history=list(history),
                                                  converged=True, max_evaluations=max_evaluations,
                                                  **conditions.inspect_kwargs(bbox_cache=None))
                if final_report['status'] == 'pass':
                    checkpoint = np.array(survivor, dtype=bool, copy=True)
                    checkpoint.setflags(write=False)
                    best = FeasibleModel(mask=checkpoint, before_mask=candidate, report=final_report,
                                         trial_index=trial.index)
                else:
                    trial.note = ('キャッシュ無しの最終再検査が不合格/unknownだったため'
                                  '体積改善は不採用(従前のbest_feasibleを保持)')

            # 合格した生存成分は、best_feasibleを更新したか否かに関わらず子候補を生成する
            # —— 一時的な体積増加(add)を経由してから初めて縮む形状があるため
            # (§4.3「支えを作り替える探索の足場として、体積増加もfrontier内では許す」)。
            frontier.push(_node(survivor, trial.index, streak, design_space_mask, sizes, max_consecutive_adds))

    return SearchResult(status='feasible', termination=termination, best_feasible=best, history=history,
                         evaluations=budget.evaluations, conditions_identifier=identifier,
                         diagnostics=_diagnostics(frontier, budget, initial=initial.diagnostics))


def find_best_feasible(design_space_mask: np.ndarray, conditions: SearchConditions, *,
                       max_evaluations: int = 1000,
                       frontier_capacity: int = DEFAULT_FRONTIER_CAPACITY,
                       max_consecutive_adds: int = DEFAULT_MAX_CONSECUTIVE_ADDS,
                       max_candidates: Optional[int] = None,
                       is_cancelled=None) -> SearchResult:
    """初期合格探索(段階3)に続けて削除・追加・置換で体積を減らす、単一の探索入口(§3、段階4)。

    まず `find_initial_feasible` で最初の6条件合格を探し、見つからなければその
    `SearchResult`(`no_feasible_found` / `input_contradiction` / `resolution_insufficient` /
    `boundary_mapping_failed`)をそのまま返す(改善しようがないので段階4は何もしない)。見つかれば、
    そこから `_reduce_volume` で体積削減を続ける。`max_evaluations` は**両段階を通した1本の予算**
    (§4.1「評価条件の識別子」と同じ思想で、初期探索と削減探索を別予算にしない)。
    `frontier_capacity` / `max_consecutive_adds` / `max_candidates` も両段階で同じ値を使う。

    `is_cancelled` は引数無しで呼べる callable。両段階とも生成中・評価前に見る。真を返した
    時点の `best_feasible` をキャッシュ無し再検査済みのまま返す(§4.6「中断時は合格チェック
    ポイントを記録する」)。初期合格の前に中断されれば `no_feasible_found` / `cancelled`。
    """
    identifier = evaluation_identifier(conditions)
    if is_cancelled is not None and is_cancelled():
        return SearchResult(status='no_feasible_found', termination='cancelled', best_feasible=None,
                             history=[], evaluations=0, conditions_identifier=identifier)
    initial = find_initial_feasible(design_space_mask, conditions, max_evaluations=max_evaluations,
                                    frontier_capacity=frontier_capacity, max_consecutive_adds=max_consecutive_adds,
                                    max_candidates=max_candidates, is_cancelled=is_cancelled)
    if initial.status != 'feasible':
        return initial
    return _reduce_volume(initial, design_space_mask, conditions, max_evaluations=max_evaluations,
                          frontier_capacity=frontier_capacity, max_consecutive_adds=max_consecutive_adds,
                          max_candidates=max_candidates, is_cancelled=is_cancelled)
