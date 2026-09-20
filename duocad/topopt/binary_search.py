"""初期合格探索(削除・追加・置換)と、ひずみエネルギーの順位で削る体積削減(計画書§8 段階3・段階4
`docs/PLAN_BINARY_STRESS_SEARCH.md`。入口検査と候補管理は `docs/FIX_BINARY_SEARCH_INITIALIZATION.md`)。

`find_initial_feasible` は設計空間の全材料マスクから出発し、最初に6条件
(`acceptance.inspect`)へ合格する候補を見つけて `best_feasible` に確定するところまで
(段階3、「見つける」だけ)。`find_best_feasible` はそこから、合格形状を解いた変位の要素エネルギーが
低い材料を、安全率の余裕に応じた割合でまとめて削り、6条件に合格するたびに `best_feasible` を
更新する(段階4、「小さくする」。計画書§11・§12)。前者は後者の第一段としてそのまま使われる。

責務の分け方(§5): 候補生成は§4.3の軸整列ブロック、島処理は§4.4、6条件の判定は
`acceptance.inspect` そのもの(このモジュールは6条件を再実装しない)。

段階3の候補の持ち方(修正C): frontier は**親マスク**を容量以内で保持し、子候補は
`(操作, ブロック領域)` の記述として親から1つずつ遅延生成して評価の直前にだけマスクにする。
生成した候補を配列のまま溜めない(保持マスク数は `frontier_capacity` + 最良合格 + 作業中の
数個で、評価回数に比例しない)。段階4は合格形状1つと試行中の候補だけを持つ。
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import deque
from dataclasses import dataclass, field, replace
from itertools import product
from typing import Any, Iterator, Optional

import numpy as np
from scipy import ndimage

from duocad import kernel

from . import acceptance, anchor, min_width
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
    節点だけへ対応付けたもの)であること。`mapping` はその検証結果(`anchor.condition_mapping`)。
    入口は `table` が `anchor.AnchorTable` なら渡された `mapping` の有無に関わらず検証をやり直し、
    どちらかが不成立なら探索しない(`find_initial_feasible`)。確定後は探索中に一切再配分しない。
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
    #: 保持・除外CADと格子セルの重なり体積の前計算(`acceptance.RegionOverlaps`)。探索1回のあいだ格子と
    #: 条件は固定なので、この探索が1つ持ち、探索が終わったら捨てる。`None` なら候補ごとにブーリアンを取る旧経路。
    region_overlaps: Any = field(default_factory=acceptance.RegionOverlaps)
    #: 最小幅の規則の**対象から外す**セル(保持ソリッドの占有。設計空間と同じ形の bool 配列、`DesignSpace.keep`)。
    #: 外すのは「そのセル自身が m 立方に被覆されていなくても違反にしない」だけで、材料としては、探索のマスクから削られた
    #: あとも他のセルの立方に数える(最終形状では保持の実形状が union で戻る)。
    #: `None` は外さない。評価(FEM・6条件)には使わないので `evaluation_identifier` に入れない。
    keep_cells: Any = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, 'fixed_faces', tuple(int(f) for f in self.fixed_faces))
        object.__setattr__(self, 'load_faces', tuple(int(f) for f in self.load_faces))
        object.__setattr__(self, 'forces', tuple(tuple(float(x) for x in row) for row in self.forces))
        object.__setattr__(self, 'preserves', tuple(self.preserves))
        object.__setattr__(self, 'excludes', tuple(self.excludes))

    def inspect_kwargs(self, *, bbox_cache: Any = ...) -> dict:
        """`acceptance.inspect` へ展開する引数。`percentile` 未指定はそちら側の既定に委ねる
        (既定値をここへ複製すると、acceptance.py 側が既定を変えたときに黙って食い違う)。

        `bbox_cache` を明示して外す呼び出しは「キャッシュ無しの再検査」(§3)なので、`region_overlaps`
        の前計算も一緒に外し、保持・除外は候補ごとのブーリアンで取り直す。
        """
        cache = self.bbox_cache if bbox_cache is ... else bbox_cache
        overlaps = self.region_overlaps if bbox_cache is ... else None
        kwargs = dict(table=self.table, fixed_faces=self.fixed_faces, load_faces=self.load_faces,
                      forces=self.forces, preserves=self.preserves, excludes=self.excludes,
                      target_safety_factor=self.target_safety_factor, allowable_mpa=self.allowable_mpa,
                      youngs_modulus_mpa=self.youngs_modulus_mpa, solver=self.solver,
                      volume_atol=self.volume_atol, volume_rtol=self.volume_rtol, bbox_cache=cache,
                      region_overlaps=overlaps)
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
                                      cache=conditions.bbox_cache, overlaps=conditions.region_overlaps)
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


def _touches_empty(mask: np.ndarray, region: tuple) -> bool:
    """ブロックが材料の表面に接するか: ブロック内に空セルがあるか、6面のどれかの外側が空(格子の外を含む)か。

    偽なら、ブロックは全て材料で、周りも全て材料(内部のブロック)。空でないブロックを空にすると閉じた空洞になる。
    """
    if not mask[region].all():
        return True
    for axis, span in enumerate(region):
        if span.start == 0 or span.stop == mask.shape[axis]:
            return True
        for edge in (span.start - 1, span.stop):
            layer = region[:axis] + (slice(edge, edge + 1),) + region[axis + 1:]
            if not mask[layer].all():
                return True
    return False


def _op_scan(mask: np.ndarray, design_space: np.ndarray, size: int, op: str):
    """1つの操作・1つのブロック大きさのブロックを1つずつ調べ、有効なら候補記述
    `(op, region, addable)`、無効なら None を返す(調べたブロックごとに必ず1つ出す)。

    有効性(削除は材料が要り、材料の表面に接すること〈内部のブロックを空にすると閉じた空洞ができ、
    水密の検査で必ず落ちる〉、追加は接する空セルが要る、置換は両方)はブロックの範囲だけを見て
    決め、マスクは作らない。`addable` はブロック大の小配列(削除では None)。最小幅は見ない
    (削除・置換の巻き添えは `_materialize` が子マスクを作るときに一緒に空にする)。
    """
    for lo in _block_starts(mask.shape, size):
        region = _block_region(lo, size, mask.shape)
        if op == 'delete':
            yield ('delete', region, None) if mask[region].any() and _touches_empty(mask, region) else None
            continue
        addable = _local_addable(mask, design_space, region)
        if not addable.any():
            yield None
        elif op == 'add':
            yield ('add', region, addable)
        else:
            yield ('replace', region, addable) if mask[region].any() else None


def _scan_descriptors(mask: np.ndarray, design_space: np.ndarray, sizes: tuple[int, ...],
                      *, allow_add: bool = True) -> Iterator[Optional[tuple]]:
    """`candidate_descriptors` と同じ順序で、無効なブロックも None として1つずつ出す。

    frontier はこちらを引き、None も走査予算に数えて中断を確認する —— 有効な候補が遠いとき、
    次の候補を返すまでに何万ブロックも予算・中断の外で調べることがない。
    巡回の手番は有効な候補を出したときだけ次の操作へ移る(None では移らない)ので、順序は
    無効なブロックを数えない場合と同じ。
    """
    ops = ('delete', 'add', 'replace') if allow_add else ('delete', 'replace')
    for size in sizes:
        alive = deque(_op_scan(mask, design_space, size, op) for op in ops)
        while alive:
            try:
                descriptor = next(alive[0])
            except StopIteration:
                alive.popleft()
                continue
            if descriptor is not None:
                alive.rotate(-1)
            yield descriptor


def candidate_descriptors(mask: np.ndarray, design_space: np.ndarray, sizes: tuple[int, ...],
                          *, allow_add: bool = True) -> Iterator[tuple]:
    """親 `mask` の子候補の記述を、大きいブロックから、各大きさで削除→追加→置換を1つずつ
    巡回して生成する(§4.3「削除優先で、各巡回に追加・置換の枠を確保する」、修正C §5.2)。

    操作別の生成器を巡回するので、削除候補が何万あっても追加・置換が後回しにならない。
    `allow_add=False` なら追加は生成しない(連続追加の上限。置換は体積を増やさないので残す)。
    """
    return (descriptor for descriptor in
            _scan_descriptors(mask, design_space, sizes, allow_add=allow_add)
            if descriptor is not None)


def _materialize(parent: np.ndarray, design_space: np.ndarray, op: str, region: tuple,
                 addable: Optional[np.ndarray], *, min_width_cells: int = 1,
                 exempt: Optional[np.ndarray] = None) -> np.ndarray:
    """候補記述から子マスクを作る(親は複製し、in-place で変えない)。評価の直前にだけ呼ぶ。

    `min_width_cells`(m)>1 のとき、削除・置換は「射影つき」: ブロックを空にした(置換は `addable` を足した)後に m 立方の
    被覆を失うセル(`min_width.lost_coverage`。`exempt` は被覆を問われない)も一緒に空にする。子は親が規則を満たすなら
    ブロックの周りで規則を満たす(足した `addable` が m 立方に入らなければ、そのセルも空にする)。追加は射影しない。
    """
    child = np.array(parent, dtype=bool, copy=True)
    if op == 'delete':
        child[region] = False
    elif op == 'add':
        child[region] |= addable
    elif op == 'replace':
        child[region] = addable
    else:
        raise ValueError(f'未知の操作 {op!r}')
    if min_width_cells > 1 and op != 'add':
        origin, lost = min_width.lost_coverage(parent, region, int(min_width_cells), exempt,
                                               refill=addable if op == 'replace' else None)
        child[tuple(slice(o, o + n) for o, n in zip(origin, lost.shape))][lost] = False
    return child


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
    """`best_feasible`(§4.1)。`report` はキャッシュ無しの検査(§3)の結果。

    `energy` はその検査で解いた変位の要素エネルギー(`acceptance.inspect` の `fields`。段階4の順位用)。
    `displacement` はその変位そのもの(ndof,)で、段階4が次の候補の CG の初期値に使う。**1本だけ持つ**
    (2.4mm で約22MB。評価のたびに入れ替え、採用形のものだけ残す)。
    """

    #: **読み取り専用**(`setflags(write=False)` 済み)。呼び出し側は書き換えず複製すること。
    mask: np.ndarray
    before_mask: np.ndarray
    report: dict
    trial_index: int
    energy: Optional[np.ndarray] = field(default=None, repr=False)
    displacement: Optional[np.ndarray] = field(default=None, repr=False)

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

    `termination` の語彙: 段階3は `initial_feasible_found` / `neighbourhood_exhausted`(frontier を捨てずに
    使い切った)/ `frontier_truncated`(容量超過で親を捨てたことがあり、近傍を尽くしたとは言えない)/
    `generation_limit`(候補ブロックの走査予算)、段階4は `converged`(1セルずつでも削れなくなった)/
    `evaluation_error`(出発形状のエネルギーが取れない、または採用形状の保持・除外が前計算なしの再検査で
    通らず段階3の合格形状へ戻した)、共通で `evaluation_limit` / `cancelled` /
    `resolution_insufficient` / `boundary_mapping_failed` / `input_contradiction`。
    段階3の `diagnostics['frontier']` / `['candidates']` に保持数の実測・除外・生成数、段階4の
    `diagnostics['reduction']` に試行ごとの削減割合・後退倍率・落ちた条件と止まった理由が入る。
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
                       *, parent: Optional[int], operation: str, history: list,
                       fields: Optional[dict] = None, overrides: Optional[dict] = None,
                       counter: Optional[dict] = None, u0: Optional[np.ndarray] = None) -> tuple[dict, Trial]:
    """1個の生存成分を検査し、history に記録する(段階3・4共通、§4.5の解法再試行を含む)。

    計算失敗(`unknown`、原因`evaluation_error`)は、同じマスク・条件のまま一度だけ
    別解法で再試行する(§4.5「解法変更による再試行は有限の規則にする」)。それでも
    `unknown` ならそのまま返す —— 幾何修復の生成要否は呼び出し側が判断する。
    `fields` / `overrides` / `u0` は `acceptance.inspect` へそのまま渡す。`counter` を渡すと、
    FEMまで進んだ検査の回数を `counter['fem_solves']` に、`u0` を渡した検査が初期値に使えたか捨てたか
    (`fields['warm_start']`)を `counter['warm_start_used']` / `['warm_start_fallbacks']` に足す。
    """
    kwargs = {**conditions.inspect_kwargs(), **(overrides or {})}

    def evaluate(solver: str, label: str, parent_index: Optional[int]) -> tuple:
        if fields is not None:
            fields.clear()
        report = acceptance.inspect(survivor, before, conditions.grid, **{**kwargs, 'solver': solver},
                                    fields=fields, u0=u0)
        if counter is not None and report['checks']['safety'].get('reason') != 'prerequisites_failed':
            counter['fem_solves'] += 1
            if u0 is not None and fields is not None:
                warm_start = fields.get('warm_start', '')
                counter['warm_start_used'] += warm_start == 'used'
                counter['warm_start_fallbacks'] += warm_start.startswith('fell_back')
        trial = _record_trial(history, parent_index, label, report)
        history.append(trial)
        return report, trial

    report, trial = evaluate(conditions.solver, operation, parent)
    if report['status'] == 'unknown' and report['checks']['safety'].get('reason') == 'evaluation_error':
        alternate = 'direct' if conditions.solver != 'direct' else 'cg'
        report, trial = evaluate(alternate, f'solver_retry:{alternate}', trial.index)
        trial.note = f'同じマスク・条件で解法を{conditions.solver}→{alternate}に変えて再試行'
    return report, trial


# ---------------------------------------------------------------------------
# frontier と予算(修正C §5)
# ---------------------------------------------------------------------------
# 既定値は小型題材(tests/test_binary_search.py の detour 系フィクスチャ)で決めた1組
# (docs/PLAN_BINARY_STRESS_SEARCH.md §4.3 追記に採用理由と観測した試行数を記録)。
DEFAULT_FRONTIER_CAPACITY = 48
DEFAULT_MAX_CONSECUTIVE_ADDS = 2
#: 走査予算の既定の下限(評価1回あたり)。小型題材(格子のセル数がこれより少ない)で決めた値
#: (docs/PLAN_BINARY_STRESS_SEARCH.md §4.3 追記)。無効・重複候補だけで評価数が増えない場合にも
#: 走査を止めるための上限で、評価予算とは別に数える(`_candidate_budget`)。
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
        """先頭の親でブロックを1つ調べた結果 `(node, descriptor)`。無効なブロックなら descriptor は
        None で、親は手番を保つ(有効な子を出したときだけ末尾へ回す)。尽きた親は退役させる。空なら None。"""
        while self._nodes:
            node = self._nodes[0]
            try:
                descriptor = next(node.children)
            except StopIteration:
                self._nodes.popleft()
                self.retired += 1
                continue
            if descriptor is not None:
                self._nodes.rotate(-1)
            return node, descriptor
        return None

    def summary(self) -> dict:
        return dict(capacity=self.capacity, peak_retained=self.peak, retained=len(self._nodes),
                    evictions=self.evictions, evicted_trials=list(self.evicted_trials), retired=self.retired)


@dataclass
class _Budget:
    """評価予算(生の候補を評価した回数)と走査予算(親から調べた候補ブロックの数。無効なブロック・
    重複も数える)。`generated` はそのうちマスクにした数、`duplicates` はさらにその中の重複。"""

    max_evaluations: int
    max_candidates: int
    evaluations: int = 0
    scanned: int = 0
    generated: int = 0
    duplicates: int = 0

    def summary(self) -> dict:
        return dict(scanned=self.scanned, generated=self.generated, duplicates=self.duplicates,
                    limit=self.max_candidates, evaluations=self.evaluations, evaluation_limit=self.max_evaluations)


def _candidate_budget(max_evaluations: int, max_candidates: Optional[int], shape: tuple) -> int:
    """走査予算。既定は評価1回あたり `max(DEFAULT_CANDIDATES_PER_EVALUATION, 格子のセル数)`。

    セル数は最小ブロック(1セル)で格子を1周調べる数で、合計が「評価予算 × 1周ぶん」を超えたら止める。
    製品の中断は `threading.Event.is_set`(実測 約20 ns/回)で、ブロックごとに呼んでも走査(約10 µs/ブロック)の1%未満。
    """
    if max_candidates is not None:
        return int(max_candidates)
    return max(DEFAULT_CANDIDATES_PER_EVALUATION, int(np.prod(shape))) * int(max_evaluations)


def _streak_after(op: str, streak: int) -> int:
    """`op` を経た後の連続する'add'の回数('delete'・'replace' は0へ戻す。§4.3)。"""
    return streak + 1 if op == 'add' else 0


def _node(mask: np.ndarray, trial_index: int, streak: int, design_space_mask: np.ndarray,
          sizes: tuple[int, ...], max_consecutive_adds: int) -> _Node:
    mask.setflags(write=False)
    return _Node(mask=mask, trial_index=trial_index, streak=streak,
                 children=_scan_descriptors(mask, design_space_mask, sizes, allow_add=streak < max_consecutive_adds))


def _pull(frontier: _Frontier, design_space_mask: np.ndarray, budget: _Budget, visited: set,
          identifier: str, is_cancelled, *, min_width_cells: int = 1,
          exempt: Optional[np.ndarray] = None) -> Any:
    """次の未評価候補 `(mask, op, parent_node)` か、終了理由の文字列。

    ブロックを1つ調べる前ごとに中断と走査予算を見る(無効なブロック・重複も予算を使う)。
    重複(既に見たマスク)は評価数を増やさない。重複は射影(`_materialize`)の後の子で判定する —— 別のブロックから
    同じ子ができる(2セル厚の板の上面のセルと下面のセルは同じ貫通柱になる)ので、射影の前では二度解く。
    """
    while True:
        if is_cancelled is not None and is_cancelled():
            return 'cancelled'
        if budget.scanned >= budget.max_candidates:
            return 'generation_limit'
        pulled = frontier.next_descriptor()
        if pulled is None:
            return 'frontier_truncated' if frontier.evictions else 'neighbourhood_exhausted'
        budget.scanned += 1
        node, descriptor = pulled
        if descriptor is None:
            continue
        op, region, addable = descriptor
        budget.generated += 1
        child = _materialize(node.mask, design_space_mask, op, region, addable,
                             min_width_cells=min_width_cells, exempt=exempt)
        key = (acceptance.material_sha256(child), identifier)
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
                           min_width_cells: int = 1,
                           is_cancelled=None) -> SearchResult:
    """設計空間の全材料マスクから出発し、最初の6条件合格モデルを探す(§4.2、§8 段階3)。

    入口で探索では直せない状態を確定させ、候補生成もFEMも始めない:
    `resolution_insufficient`(条件面に節点が無い)、`boundary_mapping_failed`(確定表の節点が
    全設計空間の材料にすら接しない〈修正B〉か、表の検証 `anchor.condition_mapping` が不成立 ——
    対応不能標本・作用位置のずれ等。渡された `conditions.mapping` とここでの再検証のどちらでも止まる)、
    `input_contradiction`(全設計空間でも保持と重ならない、または設計空間に閉じた空洞がある。
    後者は `diagnostics['closed_voids']` に空洞の数と大きい順の `cells` / `representative_cell`)。

    返却は必ず `best_feasible` から行い、`unknown`(計算失敗)や安全率不足で
    最良モデルを失わない(§4.1)。修復も通常の候補生成・評価・履歴・予算の対象で、
    入れ子の修復手続きは無い —— 幾何不良でも応力不足でも同じ候補生成を使う
    (§4.2「修復処理を無限に入れ子にしない」)。

    `max_evaluations` は生の候補(frontierから1回取り出す単位)の予算。1つの生の候補が
    複数の生存成分・解法再試行を持つ場合、`acceptance.inspect` の呼び出し回数はこれより
    多くなる(history の行数で見ること)。`max_candidates` は候補ブロックの走査予算(無効なブロック・
    重複も数える。既定は `_candidate_budget`)。`is_cancelled` はブロックを1つ調べる前ごとと評価前に見る。

    `min_width_cells`(m、既定1=制約なし)>1 のとき、設計空間をまず m 立方の opening で絞り、
    入口検査も絞った空間に対して行う(絞った結果で保持・拘束面が成り立たなければ、上の入口の状態で止まる)。
    **`conditions.keep_cells`(保持ソリッド由来のセル)は絞らずに残す** —— 規則の対象外だが材料には数える。
    以後、削除・置換の子は被覆を失うセルも空にした形(`_materialize`)になり、追加は射影しないので、合格を返す候補は毎回FEMの前に
    `_mask_satisfies_min_width` を通る。絞ったセル数は `diagnostics['min_width']['trimmed_cells']`
    (`find_best_feasible` が段階4まで進んだ結果では `diagnostics['initial']['min_width']`)。
    """
    design_space_mask = np.array(design_space_mask, dtype=bool, copy=True)
    if min_width_cells <= 1:
        return _find_initial_feasible(design_space_mask, conditions, max_evaluations=max_evaluations,
                                      frontier_capacity=frontier_capacity, max_consecutive_adds=max_consecutive_adds,
                                      max_candidates=max_candidates, min_width_cells=1, is_cancelled=is_cancelled)
    # 実測(hanger 2.4mm・m=3): 設計空間の縁に m 立方の入らないセルが残る(58/377,075)。射影(`_materialize`)は
    # 親が規則を満たす前提の差分計算で、違反したまま始めると違反セルの除去のたびに評価を払う(合成題材 6×3×3・m=2 で
    # 評価23回、絞ると1回)。以降は m 立方の材料だけのブロックに属するセルだけを設計空間として扱う(検査もこの空間に対して行う)。
    opened = min_width.cube_opening(design_space_mask, int(min_width_cells))
    exempt = _min_width_exempt(conditions, design_space_mask.shape, min_width_cells)
    if exempt is not None:
        opened |= design_space_mask & exempt
    result = _find_initial_feasible(opened, conditions, max_evaluations=max_evaluations,
                                    frontier_capacity=frontier_capacity, max_consecutive_adds=max_consecutive_adds,
                                    max_candidates=max_candidates, min_width_cells=min_width_cells,
                                    is_cancelled=is_cancelled)
    result.diagnostics = dict(result.diagnostics or {}, min_width=dict(
        cells=int(min_width_cells), design_cells=int(design_space_mask.sum()),
        trimmed_cells=int(design_space_mask.sum() - opened.sum())))
    return result


def _find_initial_feasible(design_space_mask: np.ndarray, conditions: SearchConditions, *,
                           max_evaluations: int, frontier_capacity: int, max_consecutive_adds: int,
                           max_candidates: Optional[int], min_width_cells: int, is_cancelled) -> SearchResult:
    """`find_initial_feasible` の本体。`design_space_mask` は最小幅で絞った後の空間(読み取り専用にして使う)。"""
    design_space_mask.setflags(write=False)
    identifier = evaluation_identifier(conditions)
    exempt = _min_width_exempt(conditions, design_space_mask.shape, min_width_cells)

    # 解像度不足: 拘束・荷重面がこの要素サイズで1つも節点を持たない(探索では直せない)。
    starved = sorted((set(conditions.fixed_faces) | set(conditions.load_faces))
                      & set(int(f) for f in conditions.table.empty_faces()))
    if starved:
        return SearchResult(status='resolution_insufficient', termination='resolution_insufficient',
                             best_feasible=None, history=[], evaluations=0,
                             conditions_identifier=identifier, diagnostics=dict(empty_faces=starved))

    # 境界条件の対応付け: 対応不能標本・作用位置のずれは候補の削除・追加・置換では直らない。
    # 製品入口の検証結果(`conditions.mapping`)だけを信じず、探索が実際に引く表を全設計空間で
    # 検証し直す —— 直接APIで不成立の表や検証の無い表を渡されても、ここで止まる。
    entry_mapping = None
    if isinstance(conditions.table, anchor.AnchorTable):
        entry_mapping = anchor.condition_mapping(conditions.table, conditions.fixed_faces, conditions.load_faces,
                                                 conditions.forces, material=design_space_mask)
    # 確定表の節点が全設計空間の材料に接しないなら、どの候補でも接しない(修正B §4)。
    # 同じ面/節点インタフェースだけを持つ表でも見られる、対応付けの必要条件。
    coverage = uncovered_condition_nodes(design_space_mask, conditions)
    uncovered = [row for row in coverage if row['uncovered_nodes']]
    failed = [report for report in (conditions.mapping, entry_mapping)
              if isinstance(report, dict) and report.get('status') != 'pass']
    if uncovered or failed:
        stats = getattr(conditions.table, 'stats', None) or {}
        return SearchResult(status='boundary_mapping_failed', termination='boundary_mapping_failed',
                             best_feasible=None, history=[], evaluations=0,
                             conditions_identifier=identifier,
                             diagnostics=dict(uncovered_condition_nodes=uncovered,
                                              table_mapping=stats.get('mapping', 'unknown'),
                                              mapped=conditions.mapping is not None,
                                              mapping=conditions.mapping, entry_mapping=entry_mapping))

    # 入力矛盾: 材料は常に設計空間の部分集合(§2)なので、保持との重なりは材料を減らすほど
    # 減るかそのまま。設計空間そのもの(取れる最大の材料)で既に重なりが足りなければ、
    # どの候補を試しても直らない —— 探索で確定させず、ここで判定する。
    # (除外条件は逆方向〈材料を減らすほど満たしやすくなる〉なので、この判定は使えない。)
    preserve_on_full = acceptance.region_check(design_space_mask, conditions.grid, conditions.preserves,
                                                preserve=True, volume_atol=conditions.volume_atol,
                                                volume_rtol=conditions.volume_rtol, cache=conditions.bbox_cache,
                                                overlaps=conditions.region_overlaps)
    if preserve_on_full['status'] == 'fail':
        return SearchResult(status='input_contradiction', termination='input_contradiction',
                             best_feasible=None, history=[], evaluations=0,
                             conditions_identifier=identifier,
                             diagnostics=dict(preserve_on_full_design_space=preserve_on_full))

    # 設計空間そのものに閉じた空洞がある入力(除外領域が塊に包まれている等)は、空洞のある全材料が
    # 6条件の水密で落ちるので、探索を始めずに矛盾として止める。削って空洞を外へ通す候補は原理上ありうるが、
    # 通路の幅は最小幅の制約と一体の話で扱わない(2026-09-20 本人承認。docs/TOPOPT.md「中空構造を許容しない」)。
    voids = acceptance.closed_voids(design_space_mask)
    if voids:
        return SearchResult(status='input_contradiction', termination='input_contradiction',
                             best_feasible=None, history=[], evaluations=0,
                             conditions_identifier=identifier,
                             diagnostics=dict(closed_voids=dict(
                                 count=acceptance.void_components(design_space_mask) - 1, largest=voids)))

    if is_cancelled is not None and is_cancelled():
        return SearchResult(status='no_feasible_found', termination='cancelled', best_feasible=None,
                             history=[], evaluations=0, conditions_identifier=identifier)

    sizes = tuple(reversed(block_sizes(design_space_mask.shape)))  # 大きい削除から試す(§4.3)
    budget = _Budget(max_evaluations=int(max_evaluations),
                     max_candidates=_candidate_budget(max_evaluations, max_candidates, design_space_mask.shape))
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
        raw_hash = acceptance.material_sha256(candidate)
        survivors, island_diag = resolve_islands(candidate, conditions)
        if not survivors:
            trial = _no_component_trial(history, candidate, raw_hash, parent, operation, conditions, island_diag)
            frontier.push(_node(candidate, trial.index, streak, design_space_mask, sizes, max_consecutive_adds))
            return None
        for survivor in survivors:
            survivor_key = (acceptance.material_sha256(survivor), identifier)
            if survivor_key in visited_survivors:
                continue
            visited_survivors.add(survivor_key)
            # 射影(`peel`・`_materialize`)は入力が規則を満たすことが前提で、追加(`add`)は射影しないので、
            # 合格を返す候補は毎回大域に検査する。FEMの結果に依らないので、FEMの前に落とす。違反は確定しているので、
            # FEMが計算失敗(`unknown`)になりえた候補も親として積む(不合格の証拠にしない対象は FEM の結果だけ)。
            if min_width_cells > 1 and not _mask_satisfies_min_width(survivor, min_width_cells, exempt):
                trial = _min_width_rejection_trial(history, survivor, candidate, parent, operation, conditions)
                frontier.push(_node(survivor, trial.index, streak, design_space_mask, sizes, max_consecutive_adds))
                continue
            report, trial = _evaluate_survivor(survivor, candidate, conditions, parent=parent,
                                               operation=operation, history=history)
            if report['status'] != 'pass':
                if report['status'] != 'unknown':  # 計算失敗を不合格の証拠にしない(§4.5)
                    frontier.push(_node(survivor, trial.index, streak, design_space_mask, sizes, max_consecutive_adds))
                continue
            # 最良合格モデルをキャッシュなしで再検査する(§3)。ここが失敗したら成功を返さず、
            # 候補と診断を残して探索を続ける(§4.6「最終再検査が失敗したら成功を返さない」)。
            energy_fields: dict = {}
            final_report = acceptance.inspect(survivor, candidate, conditions.grid, history=list(history),
                                              converged=True, max_evaluations=max_evaluations, fields=energy_fields,
                                              **conditions.inspect_kwargs(bbox_cache=None))
            if final_report['status'] != 'pass':
                trial.note = 'キャッシュ無しの最終再検査が不合格/unknownだったため候補から外した'
                trial.outcome = 'unknown' if final_report['status'] == 'unknown' else 'infeasible'
                if trial.outcome != 'unknown':  # 計算失敗を不合格の証拠にしない(§4.5)。他は通常どおり修復対象。
                    frontier.push(_node(survivor, trial.index, streak, design_space_mask, sizes, max_consecutive_adds))
                continue
            survivor.setflags(write=False)
            best = FeasibleModel(mask=survivor, before_mask=candidate, report=final_report,
                                 trial_index=trial.index, energy=energy_fields.get('element_energy'),
                                 displacement=energy_fields.get('displacement'))
            return SearchResult(status='feasible', termination='initial_feasible_found',
                                best_feasible=best, history=history, evaluations=budget.evaluations,
                                conditions_identifier=identifier, diagnostics=_diagnostics(frontier, budget))
        return None

    visited_candidates.add((acceptance.material_sha256(design_space_mask), identifier))
    found = consider(design_space_mask, 'initial', None, 0)
    if found is not None:
        return found
    while True:
        if budget.evaluations >= budget.max_evaluations:
            termination = 'evaluation_limit'
            break
        pulled = _pull(frontier, design_space_mask, budget, visited_candidates, identifier, is_cancelled,
                       min_width_cells=min_width_cells, exempt=exempt)
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
# 段階4: ひずみエネルギーの順位で削る体積削減(docs/PLAN_BINARY_STRESS_SEARCH.md §11)
# ---------------------------------------------------------------------------
#: 1回に削る割合 r の既定。r = min(DEFAULT_MAX_STEP_RATIO, 1 − (目標安全率 / 現在の安全率)^θ) × 後退倍率。
#: θ は「安全率が体積に比例するなら、次の形の安全率を現在と目標の間のどこへ置くか」(対数で見た位置。
#: 0.5 は幾何平均)。上限 0.2 と θ=0.5 は hanger 4.8mm の比較で決めた(計画書 §11)。
DEFAULT_STEP_EXPONENT = 0.5
DEFAULT_MAX_STEP_RATIO = 0.2
#: 削る順位に使うエネルギーをならす近傍の一辺(セル)。3 は自身と26近傍の材料平均。
ENERGY_FILTER_SIZE = 3
#: 削ったセルが1つで安全率が落ちたとき、順位から外して次のセルを試すのを何セル続けたら収束とみなすか(`None` は順位が尽きるまで)。
#: 16 は hanger 4.8mm で 1・4・16・無制限を測り、最終体積が最小(16 と無制限は同じ試行列で並び、小さい方を採った)だったから。
#: 段階4のFEMは 1 の 33 回に対し 999 回(予算切れ)まで増える(計画書 §12.7)。
DEFAULT_SINGLE_CELL_PATIENCE: Optional[int] = 16
_CORNERS = tuple(product(range(2), repeat=3))


def step_ratio(safety_factor: float, target: float, *, exponent: float = DEFAULT_STEP_EXPONENT,
               max_ratio: float = DEFAULT_MAX_STEP_RATIO) -> float:
    """安全率の余裕から決める1回の削減割合(後退倍率を掛ける前)。目標ちょうどで 0。

    `exponent=math.inf` なら余裕に関わらず `max_ratio`(固定割合)になる。
    """
    if not (safety_factor > 0 and target > 0 and 0 < max_ratio <= 1 and exponent > 0):
        raise ValueError(f'安全率・目標は正、max_ratio は (0,1]、exponent は正であること'
                         f'(安全率 {safety_factor!r} / 目標 {target!r} / max_ratio {max_ratio!r} / exponent {exponent!r})')
    gap = min(1.0, float(target) / float(safety_factor))
    return min(float(max_ratio), 1.0 - gap ** float(exponent))


def ranked_cells(mask: np.ndarray, energy: np.ndarray) -> np.ndarray:
    """材料セルの平坦添字を、近傍の材料でならしたエネルギーの昇順に並べる(同値は添字順)。

    ならすのは、1セルごとの値のばらつきで順位が入れ替わるのを抑えるため。
    """
    material = np.asarray(mask, dtype=bool)
    total = ndimage.uniform_filter(np.where(material, energy, 0.0), size=ENERGY_FILTER_SIZE, mode='constant')
    count = ndimage.uniform_filter(material.astype(float), size=ENERGY_FILTER_SIZE, mode='constant')
    flat = np.flatnonzero(material)
    smoothed = total.ravel()[flat] / count.ravel()[flat]
    return flat[np.argsort(smoothed, kind='stable')]


def _anchor_node_ids(conditions: SearchConditions) -> np.ndarray:
    ids = [_face_node_ids(conditions.table, face) for face in conditions.fixed_faces + conditions.load_faces]
    return np.unique(np.concatenate(ids)) if ids else np.zeros(0, dtype=np.int64)


#: 3×3×3 の窓の中心セルから見た6近傍の位置。
_WINDOW_NEIGHBOURS = ((0, 1, 1), (2, 1, 1), (1, 0, 1), (1, 2, 1), (1, 1, 0), (1, 1, 2))
#: 窓の中心セルの8頂点(頂点格子の添字)。窓の外周の頂点は窓の外を空と見るので使えない。
_WINDOW_CENTRE_VERTICES = (slice(1, 3),) * 3


@dataclass
class PeelStats:
    """`peel` の実績。`picked` は順位から選んで抜いた手の数(一口が複数セルでも1手)、`casualties` は手のたびに
    一緒に抜いた巻き添え(m=1 では常に0)、`islands` は切り離されて一緒に消したセル。実際に減ったセル数は
    `picked + casualties + islands`。`skipped_*` は飛ばした理由ごとの数(支え・表面・多様体・連結性の順に判定し、
    最初に落ちた理由で数える。m>1 の手は巻き添えを含む一口に対して判定する)。
    `skipped_thickness` は厚みを理由に手を却下しなくなったので常に0。"""

    picked: int = 0
    casualties: int = 0
    islands: int = 0
    skipped_support: int = 0
    skipped_surface: int = 0
    skipped_manifold: int = 0
    skipped_thickness: int = 0
    skipped_connectivity: int = 0
    already_removed: int = 0
    global_checks: int = 0
    seconds: float = 0.0
    #: 順位から選んだセル(手の先頭。巻き添えは含まない)の平坦添字(記録には出さない)。
    picked_cells: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64), repr=False)

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in self.__dataclass_fields__ if name != 'picked_cells'}


def _neighbours_stay_connected(window: np.ndarray) -> Optional[bool]:
    """中心を抜いた 3×3×3 の窓で、中心の材料6近傍が6連結か(材料の6近傍が無ければ None)。

    真なら、抜く前に連結だったマスクは抜いた後も連結(十分条件。偽でも割れるとは限らない)。
    """
    members = [p for p in _WINDOW_NEIGHBOURS if window[p]]
    if not members:
        return None
    if len(members) == 1:
        return True
    labels, _ = ndimage.label(window, structure=NEIGHBOURHOOD_STRUCTURE)
    return len({int(labels[p]) for p in members}) == 1


def _regular_after_removal(window: np.ndarray) -> bool:
    """中心を抜いた窓で、中心セルの8頂点が全て境界の多様体条件(`acceptance.REGULAR_VERTEX`)を満たすか。"""
    bad, _ = acceptance.irregular_vertices(window)
    return not bad[_WINDOW_CENTRE_VERTICES].any()


def _min_width_exempt(conditions: SearchConditions, shape: tuple, min_width_cells: int) -> Optional[np.ndarray]:
    """最小幅の規則の対象外のセル(`conditions.keep_cells`)。規則が無効(`m<=1`)か指定が無ければ `None`。"""
    if min_width_cells <= 1 or conditions.keep_cells is None:
        return None
    keep = np.asarray(conditions.keep_cells, dtype=bool)
    if keep.shape != tuple(shape):
        raise ValueError(f'keep_cells の形 {keep.shape} が設計空間の形 {tuple(shape)} と違う')
    return keep


def keep_cells_without_room(design_space: np.ndarray, keep: np.ndarray, min_width_cells: int,
                            obstacle: Optional[np.ndarray] = None) -> dict:
    """設計空間の opening(m 立方)に入らない保持セル —— どんな形を選んでも m 立方で覆えず、最小幅の保証が効かない場所。

    その周りの空きセル(26近傍のうち設計空間でないセル。格子の外も数える)を、除外ソリッドの占有(`obstacle`)と
    それ以外(設計空間の端・凸包の外・格子の外)に分けて数える。`{cells, around_exclude, around_outside}`。
    """
    m = int(min_width_cells)
    design = np.asarray(design_space, dtype=bool)
    if m <= 1:
        return dict(cells=0, around_exclude=0, around_outside=0)
    stranded = np.asarray(keep, dtype=bool) & design & ~min_width.cube_opening(design, m)
    cells = int(stranded.sum())
    if not cells:
        return dict(cells=0, around_exclude=0, around_outside=0)
    ring = ndimage.binary_dilation(np.pad(stranded, 1), structure=np.ones((3, 3, 3), dtype=bool)) & ~np.pad(design, 1)
    excluded = np.zeros_like(ring) if obstacle is None else np.pad(np.asarray(obstacle, dtype=bool), 1)
    return dict(cells=cells, around_exclude=int((ring & excluded).sum()), around_outside=int((ring & ~excluded).sum()))


def _mask_satisfies_min_width(mask: np.ndarray, min_width_cells: int, exempt: Optional[np.ndarray] = None) -> bool:
    """`mask` の全材料セル(`exempt` を除く)が m 立方の材料だけのブロックのどれかに属するか(大域判定)。
    `exempt` は `mask` にあるかどうかに関わらず材料に数える(`min_width.lost_coverage` と同じ扱い)。

    段階3の射影(`_materialize`)は親が既に規則を満たす前提の差分計算で、追加は射影しないので、`find_initial_feasible` は
    合格を返す候補ごとにこれで大域に確かめる。
    """
    if min_width_cells <= 1:
        return True
    mask = np.asarray(mask, dtype=bool)
    if exempt is None:
        return not (mask & ~min_width.cube_opening(mask, int(min_width_cells))).any()
    return not (mask & ~exempt & ~min_width.cube_opening(mask | exempt, int(min_width_cells))).any()


def peel(mask: np.ndarray, order: np.ndarray, count: int, anchor_ids: np.ndarray, *,
        min_width_cells: int = 1, exempt: Optional[np.ndarray] = None) -> tuple[np.ndarray, PeelStats]:
    """`order` を先頭から1回だけ歩き、各セルを**その時点のマスク**で判定して通ったらその場で消す。

    `count` 個(巻き添え・島として消えたぶんも数える。最後の一口が超えることがある)消えるか順位が尽きたら止める。
    `mask` が「1成分・全頂点が正則・条件節点が全て支えられている・閉じた空洞が無い・(`min_width_cells>1` なら)全材料セルが
    m 立方の材料だけのブロックに属する」なら、返すマスクも同じ —— 各手の判定はこの不変条件を保つ。

    **最小幅(`min_width_cells`=m>1)**: 手は「セル x + 巻き添え」の一口で、巻き添えは x を抜いた後に m 立方の被覆を失う
    材料セル(`min_width.casualties`。`exempt` のセルは被覆を問われないので巻き添えにならず、材料としては他のセルの立方に数える)。
    一口を抜いた形は(`exempt` が無ければ)opening(mask − {x}) に等しく(opening は冪等)、厚みの不変条件は構成上保たれるので、
    厚みを理由に手を却下しない。
    巻き添えが無い手は m=1 と同じ1セルの判定。以下は一口全体に対して判定する:

    - 支え: 条件節点に接する材料の最後の1セルは抜かない(巻き添えにそのセルが入る手も却下する)。
    - 多様体: 抜いた後の、一口に接する頂点が全て正則(1セルは3×3×3 の窓、複数セルは `min_width.bite_verdict`)。
    - 連結: 一口に接する材料が窓の中で連結なら通す。そうでなければマスク全体を数え直し、条件節点を支える
      セルが全て同じ1成分にあるときだけ通して、他の成分(島)を一緒に消す。島は保持成分と辺・頂点でも
      接しない(下記コメント)ので、両方を跨ぐ m 立方の材料だけのブロックは無く、島の除去は厚みの
      不変条件を壊さない。
    - 表面: x が空の6近傍(格子の外を含む)を持たなければ抜かない(内部に空洞を作らない。中空構造を許容しない製品制約)。
      一口の6連結成分もそれぞれ既存の空につながっていること。抜いた後の一口は既存の空の成分に属し、島として消える成分も隣接する空につながる。
      閉じた空洞の無い `mask` からは閉じた空洞の無い候補ができる(`acceptance.boundary_check` の `void_components` が落ちない根拠)。
    """
    started = time.perf_counter()
    mask = np.asarray(mask, dtype=bool)
    shape = mask.shape
    node_shape = tuple(n + 1 for n in shape)
    support = np.zeros(node_shape, dtype=np.int32)
    is_anchor = np.zeros(node_shape, dtype=bool)
    is_anchor.ravel()[anchor_ids] = True
    touches = np.zeros(shape, dtype=bool)
    for corner in _CORNERS:
        window = tuple(slice(c, c + n) for c, n in zip(corner, shape))
        support[window] += mask
        touches |= is_anchor[window]
    support_flat, anchor_flat, touches_flat = support.ravel(), is_anchor.ravel(), touches.ravel()
    plane, row = shape[1] * shape[2], shape[2]
    node_plane, node_row = node_shape[1] * node_shape[2], node_shape[2]

    work = np.pad(mask, 1)
    inner = work[1:-1, 1:-1, 1:-1]
    stats = PeelStats()
    picked: list[int] = []
    for cell in order.tolist():
        if stats.picked + stats.casualties + stats.islands >= count:
            break
        i, rest = divmod(cell, plane)
        j, k = divmod(rest, row)
        if not work[i + 1, j + 1, k + 1]:
            stats.already_removed += 1
            continue
        corners = None
        if touches_flat[cell]:
            corners = [(i + a) * node_plane + (j + b) * node_row + (k + c) for a, b, c in _CORNERS]
            if any(anchor_flat[n] and support_flat[n] <= 1 for n in corners):
                stats.skipped_support += 1
                continue
        window = work[i:i + 3, j:j + 3, k:k + 3].copy()
        if all(window[p] for p in _WINDOW_NEIGHBOURS):
            stats.skipped_surface += 1
            continue
        bite = None
        if min_width_cells > 1:
            extra = min_width.casualties(inner, (i, j, k), min_width_cells, exempt)
            if len(extra):
                bite = np.vstack(([[i, j, k]], extra))
        if bite is None:
            cleared = (i, j, k)
            window[1, 1, 1] = False
            if not _regular_after_removal(window):
                stats.skipped_manifold += 1
                continue
            connected = _neighbours_stay_connected(window)
            if connected is None:
                stats.skipped_connectivity += 1
                continue
        else:
            cleared = tuple(bite.T)
            for ci, cj, ck in extra[touches[extra[:, 0], extra[:, 1], extra[:, 2]]]:
                corners = (corners or []) + [(ci + a) * node_plane + (cj + b) * node_row + (ck + c)
                                             for a, b, c in _CORNERS]
            if corners is not None:
                dropped: dict = {}
                for n in corners:
                    if anchor_flat[n]:
                        dropped[n] = dropped.get(n, 0) + 1
                if any(support_flat[n] <= d for n, d in dropped.items()):
                    stats.skipped_support += 1
                    continue
            reason, connected = min_width.bite_verdict(work, bite, NEIGHBOURHOOD_STRUCTURE)
            if reason is not None:
                setattr(stats, f'skipped_{reason}', getattr(stats, f'skipped_{reason}') + 1)
                continue
        island_cells = 0
        inner[cleared] = False
        if not connected:
            stats.global_checks += 1
            labels, _ = ndimage.label(inner, structure=NEIGHBOURHOOD_STRUCTURE)
            holders = np.unique(labels[touches & inner])
            if anchor_ids.size:
                keep = int(holders[0]) if holders.size == 1 else 0
            else:
                keep = 1 if labels.max() == 1 else 0
            if keep == 0:
                inner[cleared] = True
                stats.skipped_connectivity += 1
                continue
            islands = (labels > 0) & (labels != keep)
            island_cells = int(islands.sum())
            # 島は条件節点に接するセルを持たないので、支え数はそのままでよい。島と保持成分は辺・頂点でも接しない
            # (接していれば、切り離した一口の頂点の正則判定で落ちている)ので、島を消しても頂点は正則のまま。
            inner[islands] = False
        if corners is not None:
            for n in corners:
                support_flat[n] -= 1
        stats.picked += 1
        stats.casualties += 0 if bite is None else len(bite) - 1
        stats.islands += island_cells
        picked.append(cell)
    stats.picked_cells = np.asarray(picked, dtype=np.int64)
    stats.seconds = time.perf_counter() - started
    return np.ascontiguousarray(inner), stats


def _recheck_regions_without_precompute(best: FeasibleModel, seed: FeasibleModel,
                                        conditions: SearchConditions) -> tuple[FeasibleModel, str]:
    """段階4が更新した `best` の保持・除外を、前計算(`region_overlaps`)を使わずブーリアンで取り直す(§3)。

    段階4の評価は前計算から引くので、報告済みの合格は「前計算が旧経路と同じ答えを出す」ことに
    乗っている。採用形状だけは旧経路で確かめ、通れば保持・除外の報告を旧経路の値に差し替えて返す。
    通らなければ、段階3でキャッシュ無しに再検査済みの `seed` を返す(呼び出し側が `best` との
    同一性で食い違いを見分ける)。`best` が `seed` のままなら何もしない(検査済み)。
    """
    if best is seed:
        return best, 'not_needed'
    rows = {}
    for name, bodies, preserve in (('preserve', conditions.preserves, True), ('exclude', conditions.excludes, False)):
        rows[name] = acceptance.region_check(best.mask, conditions.grid, bodies, preserve=preserve,
                                             volume_atol=conditions.volume_atol, volume_rtol=conditions.volume_rtol)
        if rows[name]['status'] != 'pass':
            return seed, f'failed:{name}:{rows[name]["status"]}'
    report = dict(best.report, checks=dict(best.report['checks'], **rows))
    return replace(best, report=report), 'pass'


def _missing_conditions(mask: np.ndarray, conditions: SearchConditions) -> list[str]:
    """`mask` が満たしていない条件(節点が材料から離れた固定面・荷重面と、保持の重なり)の名前。"""
    live = acceptance.active_nodes(mask).ravel()
    missing = []
    for side, faces in (('fixed', conditions.fixed_faces), ('load', conditions.load_faces)):
        for face in faces:
            ids = _face_node_ids(conditions.table, face)
            if ids.size == 0 or not bool(live[ids].all()):
                missing.append(f'{side}_face_{int(face)}')
    report = acceptance.region_check(mask, conditions.grid, conditions.preserves, preserve=True,
                                     volume_atol=conditions.volume_atol, volume_rtol=conditions.volume_rtol,
                                     cache=conditions.bbox_cache, overlaps=conditions.region_overlaps)
    if report['status'] != 'pass':
        missing.append('preserve')
    return missing


def _geometry_rejection(candidate: np.ndarray, survivors: list, island_diag: dict,
                        conditions: SearchConditions) -> Optional[dict]:
    """`peel` の候補が「1成分で、その成分が全条件を単独で満たす」ときは None。満たさなければ理由。

    連結性(`components` が1でない)は `peel` の不変条件の破れ。保持の重なりは `peel` が見ないので、
    ここで落ちうる。
    """
    if island_diag['components'] == 1 and len(survivors) == 1:
        return None
    return dict(components=island_diag['components'], surviving_components=len(survivors),
                missing=_missing_conditions(candidate, conditions), removed_islands=island_diag['removed_islands'])


def _min_width_rejection_trial(history: list, survivor: np.ndarray, candidate: np.ndarray, parent: Optional[int],
                               operation: str, conditions: SearchConditions) -> Trial:
    volume = conditions.grid.element_volume
    trial = Trial(index=len(history), parent=parent, operation=operation,
                  mask_hash=acceptance.material_sha256(survivor), before_mm3=float(candidate.sum()) * volume,
                  material_mm3=float(survivor.sum()) * volume,
                  removed_islands_mm3=float(candidate.sum() - survivor.sum()) * volume,
                  outcome='infeasible', checks=dict(min_width='fail'),
                  note='FEMの前に最小幅の不変条件(m立方の材料だけのブロックに全材料セルが属する)で不合格')
    history.append(trial)
    return trial


def _geometry_rejection_trial(history: list, candidate: np.ndarray, parent: Optional[int], operation: str,
                              conditions: SearchConditions, rejection: dict) -> Trial:
    volume = float(candidate.sum()) * conditions.grid.element_volume
    trial = Trial(index=len(history), parent=parent, operation=operation,
                  mask_hash=acceptance.material_sha256(candidate), before_mm3=volume, material_mm3=volume,
                  removed_islands_mm3=0.0, outcome='infeasible', checks=dict(geometry='fail'),
                  note=f'FEMの前に幾何で不合格(成分 {rejection["components"]}・欠けた条件 {rejection["missing"]})')
    history.append(trial)
    return trial


def _reduce_volume(initial: SearchResult, design_space_mask: np.ndarray, conditions: SearchConditions,
                   *, max_evaluations: int, is_cancelled,
                   step_exponent: float = DEFAULT_STEP_EXPONENT,
                   max_step_ratio: float = DEFAULT_MAX_STEP_RATIO,
                   single_cell_patience: Optional[int] = DEFAULT_SINGLE_CELL_PATIENCE,
                   min_width_cells: int = 1, warm_start: bool = False) -> SearchResult:
    """`initial.best_feasible` から、ひずみエネルギーの低い材料をまとめて削り続ける(段階4)。

    合格形状は1つだけ持つ。1回の試行: 合格形状を解いた変位の要素エネルギー(近傍でならす)の低い順に
    `k = max(1, ⌊r × 後退倍率 × 材料セル数⌋)` セルを、`peel` が局所の不変条件で判定しながら削る
    (`step_ratio`。最小幅 m>1 では手ごとに巻き添えも一緒に抜き、`k` は巻き添えも数える)。候補は構成上
    「1成分・全頂点が正則・全条件節点に支えあり」で、島処理
    (`resolve_islands`)は検算 → 6条件(FEM)。合格すればその形状とエネルギーへ進み、
    後退倍率を2倍に戻す(上限1)。不合格・計算失敗なら形状を変えず、その形の内容ハッシュを `rejected` に入れる。
    順位から選んだ手が2つ以上なら後退倍率を半分にして同じ順位から歩き直す(再求解しない)。1つだけなら、
    その手のセルを**この形状の間だけ**順位から外して次のセルを試す(採用で形が変わったら順位を作り直すので解除)。
    `rejected` の形がもう一度できたら(順位が尽きて `k` を減らしたとき、別のセルの巻き添えが同じ一口になったとき)
    FEMを解かずに、同じ手順で次へ進む。安全率で落ちたセルが
    採用を挟まず `single_cell_patience` 個に達したら `stop_reason='safety_at_single_cell'` で `converged`
    (`None` は数えない)。計算失敗・保持の重なりの不合格は数えない。順位を歩いて1セルも抜けなければ
    `converged`(`no_removable_cell`)。
    どの経路でも有限回で止まる: 採用のたびに材料が1セル以上減る。採用の間では、k ≥ 2 の不合格は後退倍率を
    半分にして k を(有限回で k=1 まで)下げ、k=1 の不合格は順位を1セル縮めるので、順位は尽きる。
    止まった理由は `diagnostics['reduction']['stop_reason']`、試行ごとの `peel` の実績・k・r・後退倍率・
    落ちた条件は `['steps']`、検算で落ちた候補は `['geometry_rejections']`。

    評価予算は両段階を通した1本なので `initial.evaluations` から数える(削る候補1つが1回)。
    評価は bbox キャッシュ無しで行うが、保持・除外の共通体積は前計算(`region_overlaps`)から引く。
    採用形状はこの関数を出る前に前計算を使わない旧経路で取り直す(`_recheck_regions_without_precompute`)。

    `warm_start` なら、直前の合格形状の変位(`FeasibleModel.displacement`)を次の候補の CG の初期値に渡す。
    **既定はオフ**: 採用形状の `mask_hash` が cold と一致しないことが実測されている(docs/TOPOPT_SPEED.md)。
    初期値を使えた回数と捨てた回数は `diagnostics['reduction']['warm_start_used']` / `['warm_start_fallbacks']`。
    """
    if single_cell_patience is not None and not (
            isinstance(single_cell_patience, int) and not isinstance(single_cell_patience, bool) and single_cell_patience >= 1):
        raise ValueError(f'single_cell_patience は 1 以上の整数か None(無制限): {single_cell_patience!r}')
    identifier = initial.conditions_identifier
    history = list(initial.history)
    best = initial.best_feasible
    evaluations = int(initial.evaluations)
    target = float(conditions.target_safety_factor)
    anchor_ids = _anchor_node_ids(conditions)
    exempt = _min_width_exempt(conditions, best.mask.shape, min_width_cells)
    steps: list[dict] = []
    reduction = dict(step_exponent=float(step_exponent), max_step_ratio=float(max_step_ratio),
                     filter_size=ENERGY_FILTER_SIZE, fem_solves=0, min_width_cells=int(min_width_cells),
                     warm_start=bool(warm_start), warm_start_used=0, warm_start_fallbacks=0,
                     peel_calls=0, peel_seconds=0.0, peel_seconds_max=0.0, repeated_candidates=0,
                     single_cell_patience=single_cell_patience, safety_excluded_cells=0, adopted_after_exclusion=0,
                     geometry_rejections=[], stop_reason=None, steps=steps)

    def result(termination: str) -> SearchResult:
        final, verdict = _recheck_regions_without_precompute(best, initial.best_feasible, conditions)
        reduction['final_region_recheck'] = verdict
        if verdict.startswith('failed'):
            termination = 'evaluation_error'
        return SearchResult(status='feasible', termination=termination, best_feasible=final, history=history,
                            evaluations=evaluations, conditions_identifier=identifier,
                            diagnostics=dict(initial=initial.diagnostics, reduction=reduction))

    energy = best.energy
    if energy is None:
        fields: dict = {}
        acceptance.inspect(best.mask, best.before_mask, conditions.grid, fields=fields,
                           **conditions.inspect_kwargs(bbox_cache=None))
        reduction['fem_solves'] += 1
        energy = fields.get('element_energy')
        if energy is None:
            reduction['stop_reason'] = 'seed_energy_unavailable'
            return result('evaluation_error')

    multiplier = 1.0
    order = ranked_cells(best.mask, energy)
    rejected: set[str] = set()
    safety_streak = 0
    while True:
        if evaluations >= max_evaluations:
            return result('evaluation_limit')
        if is_cancelled is not None and is_cancelled():
            return result('cancelled')
        gap_ratio = step_ratio(best.safety_factor, target, exponent=step_exponent, max_ratio=max_step_ratio)
        requested = max(1, int(gap_ratio * multiplier * int(best.mask.sum())))
        candidate, peeled = peel(best.mask, order, requested, anchor_ids, min_width_cells=min_width_cells,
                                 exempt=exempt)
        reduction['peel_calls'] += 1
        reduction['peel_seconds'] += peeled.seconds
        reduction['peel_seconds_max'] = max(reduction['peel_seconds_max'], peeled.seconds)
        if peeled.picked == 0:
            reduction['stop_reason'] = 'no_removable_cell'
            return result('converged')
        candidate.setflags(write=False)
        key = acceptance.material_sha256(candidate)
        if key in rejected:
            # 不合格が分かっている候補を解き直さない(順位が尽きて k を減らしても、別のセルの巻き添えが同じ一口になっても、同じ形になる)。
            reduction['repeated_candidates'] += 1
            if peeled.picked > 1:
                multiplier /= 2.0
            else:
                order = order[~np.isin(order, peeled.picked_cells)]
            continue
        removed = int(best.mask.sum() - candidate.sum())
        evaluations += 1
        operation = f'remove:{removed}'
        step = dict(requested_cells=requested, peel=peeled.as_dict(), removed_cells=removed,
                    step_ratio=gap_ratio * multiplier, gap_step_ratio=gap_ratio, multiplier=multiplier,
                    safety_factor_before=best.safety_factor, material_mm3_before=best.material_mm3)
        steps.append(step)

        accepted, failed, safety_failed = None, [], False
        survivors, island_diag = resolve_islands(candidate, conditions)
        rejection = _geometry_rejection(candidate, survivors, island_diag, conditions)
        if rejection is not None:
            reduction['geometry_rejections'].append(dict(step=len(steps) - 1, **rejection))
            trial = _geometry_rejection_trial(history, candidate, best.trial_index, operation, conditions, rejection)
            failed.append('geometry')
        else:
            fields = {}
            report, trial = _evaluate_survivor(survivors[0], candidate, conditions, parent=best.trial_index,
                                               operation=operation, history=history, fields=fields,
                                               overrides=dict(bbox_cache=None, history=history),
                                               counter=reduction, u0=best.displacement if warm_start else None)
            if report['status'] == 'pass' and 'element_energy' in fields:
                accepted = (survivors[0], report, fields['element_energy'])
            else:
                failed.extend(name for name, check in report['checks'].items() if check.get('status') != 'pass')
                safety_failed = report['checks']['safety'].get('status') == 'fail'
        step.update(trials=[trial.index], outcome='accepted' if accepted is not None else 'rejected',
                    failed=sorted(set(failed)))

        if accepted is not None:
            survivor, report, energy = accepted
            survivor = np.array(survivor, dtype=bool, copy=True)
            survivor.setflags(write=False)
            best = FeasibleModel(mask=survivor, before_mask=candidate, report=report, trial_index=trial.index,
                                 energy=energy, displacement=fields.get('displacement') if warm_start else None)
            step.update(safety_factor=best.safety_factor, material_mm3=best.material_mm3)
            multiplier = min(1.0, 2.0 * multiplier)
            order = ranked_cells(best.mask, energy)
            rejected.clear()
            reduction['adopted_after_exclusion'] += int(safety_streak > 0)
            safety_streak = 0
        else:
            rejected.add(key)
            if peeled.picked > 1:
                multiplier /= 2.0
            else:
                if safety_failed:
                    safety_streak += 1
                    if single_cell_patience is not None and safety_streak >= single_cell_patience:
                        reduction['stop_reason'] = 'safety_at_single_cell'
                        return result('converged')
                    reduction['safety_excluded_cells'] += 1
                order = order[~np.isin(order, peeled.picked_cells)]


def find_best_feasible(design_space_mask: np.ndarray, conditions: SearchConditions, *,
                       max_evaluations: int = 1000,
                       frontier_capacity: int = DEFAULT_FRONTIER_CAPACITY,
                       max_consecutive_adds: int = DEFAULT_MAX_CONSECUTIVE_ADDS,
                       max_candidates: Optional[int] = None,
                       step_exponent: float = DEFAULT_STEP_EXPONENT,
                       max_step_ratio: float = DEFAULT_MAX_STEP_RATIO,
                       single_cell_patience: Optional[int] = DEFAULT_SINGLE_CELL_PATIENCE,
                       min_width_cells: int = 1,
                       warm_start: bool = False,
                       is_cancelled=None) -> SearchResult:
    """初期合格探索(段階3)に続けて、ひずみエネルギーの低い材料を削って体積を減らす単一の探索入口(§3)。

    まず `find_initial_feasible` で最初の6条件合格を探し、見つからなければその
    `SearchResult`(`no_feasible_found` / `input_contradiction` / `resolution_insufficient` /
    `boundary_mapping_failed`)をそのまま返す(改善しようがないので段階4は何もしない)。見つかれば、
    そこから `_reduce_volume` で体積削減を続ける。`max_evaluations` は**両段階を通した1本の予算**
    (§4.1「評価条件の識別子」と同じ思想で、初期探索と削減探索を別予算にしない)。
    `frontier_capacity` / `max_consecutive_adds` / `max_candidates` は段階3だけ、
    `step_exponent` / `max_step_ratio` / `single_cell_patience` / `warm_start` は段階4だけが使う(`step_ratio` / `_reduce_volume`)。
    `min_width_cells`(m、既定1=制約なし)は両段階が使う —— 段階3・段階4とも、削除で被覆を失うセルを同じ一手で一緒に抜く
    (`_materialize`・`peel` docstring)ので、厚みを理由に削除の候補を捨てない(段階3の追加は射影せず、規則を破る追加は評価の前に
    大域判定で落とす)。設計空間はまず m 立方の opening で絞る(縁に m 立方の入らない材料が残る設計空間から始めると、
    違反セルの除去に評価を払う)。入口検査は絞った空間に対して行う。
    `conditions.keep_cells`(保持ソリッド由来のセル)は規則の対象外で、絞りからも外れる(材料としては数える)。

    `is_cancelled` は引数無しで呼べる callable。段階3はブロックを1つ調べる前ごとと評価前、段階4は
    削る候補を作る前ごとに見る。真を返した時点の `best_feasible`(キャッシュ無しで検査済み)を返す
    (§4.6「中断時は合格チェックポイントを記録する」)。初期合格の前に中断されれば `no_feasible_found` / `cancelled`。
    """
    identifier = evaluation_identifier(conditions)
    if is_cancelled is not None and is_cancelled():
        return SearchResult(status='no_feasible_found', termination='cancelled', best_feasible=None,
                             history=[], evaluations=0, conditions_identifier=identifier)
    initial = find_initial_feasible(design_space_mask, conditions, max_evaluations=max_evaluations,
                                    frontier_capacity=frontier_capacity, max_consecutive_adds=max_consecutive_adds,
                                    max_candidates=max_candidates, min_width_cells=min_width_cells,
                                    is_cancelled=is_cancelled)
    if initial.status != 'feasible':
        return initial
    return _reduce_volume(initial, design_space_mask, conditions, max_evaluations=max_evaluations,
                          is_cancelled=is_cancelled, step_exponent=step_exponent, max_step_ratio=max_step_ratio,
                          single_cell_patience=single_cell_patience, min_width_cells=min_width_cells,
                          warm_start=warm_start)
