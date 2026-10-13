"""全材料の評価と、ひずみエネルギーの順位で削る体積削減(段階3・段階4。
`docs/TOPOPT.md`「二値探索の方式と契約」。入口検査は同「回帰検査の対応」)。

`find_initial_feasible` は入口検査のあと全材料マスクを1回だけ6条件(`acceptance.inspect`)に掛け、
合格すれば `best_feasible` に確定する(段階3)。合格しなければ落ちた検査を返す。
`find_best_feasible` はそこから、要素エネルギーの低い材料を安全率の余裕に応じた割合で削り、
6条件に合格するたびに `best_feasible` を更新する(段階4。`docs/TOPOPT.md`「段階4: ひずみエネルギーの順位で削る」)。
6条件の判定は `acceptance.inspect` に任せ再実装しない。保持するマスクは合格形状1つと試行中の候補だけ。
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field, replace
from itertools import product
from typing import Any, Optional

import numpy as np
from scipy import ndimage

from duocad import kernel

from . import acceptance, anchor, min_width, neck, voxelize
from .acceptance import NEIGHBOURHOOD_STRUCTURE
from .material import DEFAULT_ALLOWABLE_MPA, DEFAULT_YOUNGS_MODULUS_MPA


# ---------------------------------------------------------------------------
# 評価条件(§4.1「評価条件の識別子」)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SearchConditions:
    """1回の探索を通して固定される評価条件一式(`acceptance.inspect` への引数を束ねる。意味の正はそちら)。

    `table` は**探索用の確定表**(`anchor.build_anchors(..., allowed_nodes=...)` で材料に接する節点だけへ
    対応付けたもの)。`mapping` はその検証結果(`anchor.condition_mapping`)。入口は `table` が
    `anchor.AnchorTable` なら `mapping` の有無に関わらず検証をやり直し、不成立なら探索しない。
    確定後は探索中に再配分しない。
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
    solver: str = 'auto'  # 'auto' は fem.choose_solver が候補ごとに cg / direct を選ぶ
    bbox_cache: Any = None
    mapping: Any = None
    #: 保持・除外CADと格子セルの重なり体積の前計算(`acceptance.RegionOverlaps`)。`None` なら候補ごとにブーリアンを取る。
    region_overlaps: Any = field(default_factory=acceptance.RegionOverlaps)
    #: 保持ソリッド由来のセル(設計空間と同じ形の bool 配列、`DesignSpace.keep`。設計空間に含まれること)。
    #: どの候補でも材料のまま(`keep ⊆ mask`)。最小幅の規則はこれらのセル自身を違反にしない(他のセルの立方には材料として数える)。
    #: `None` は制約なし。`member_volume_mm3` を使うときは部材域から外すセルとして安全率の入力にもなり、そのときだけ `evaluation_identifier` に入れる。
    keep_cells: Any = field(default=None, compare=False, repr=False)
    #: 安全率の代表応力の規則。`None` は材料全体の `percentile` 点、値を渡すと部材域(`acceptance.member_region`)の
    #: 固定物理体積の上位値で、この体積 V [mm³] までの超過を許す。
    member_volume_mm3: Optional[float] = None
    #: 表面ピークの却下判定(docs/TOPOPT_PEAK_GATE.md)。`None` は判定しない。値 ε を渡すと段階4の開始時に全材料の
    #: セルごとの6面の最大 B を測り、部材域の露出面に `max(許容 ÷ 目標, (1+ε)·B)` を課す。`member_volume_mm3` が要る。
    peak_epsilon: Optional[float] = None
    #: `peak_epsilon` から段階4が作るセルごとの上限 [MPa]。外から渡さない。
    peak_limit_mpa: Any = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, 'fixed_faces', tuple(int(f) for f in self.fixed_faces))
        object.__setattr__(self, 'load_faces', tuple(int(f) for f in self.load_faces))
        object.__setattr__(self, 'forces', tuple(tuple(float(x) for x in row) for row in self.forces))
        object.__setattr__(self, 'preserves', tuple(self.preserves))
        object.__setattr__(self, 'excludes', tuple(self.excludes))

    def inspect_kwargs(self, *, bbox_cache: Any = ...) -> dict:
        """`acceptance.inspect` へ展開する引数。`percentile` 未指定は acceptance 側の既定に委ねる。

        `bbox_cache` を明示して外す呼び出しは「キャッシュ無しの再検査」(§3)なので、`region_overlaps` の前計算も外す。
        """
        cache = self.bbox_cache if bbox_cache is ... else bbox_cache
        overlaps = self.region_overlaps if bbox_cache is ... else None
        kwargs = dict(table=self.table, fixed_faces=self.fixed_faces, load_faces=self.load_faces,
                      forces=self.forces, preserves=self.preserves, excludes=self.excludes,
                      target_safety_factor=self.target_safety_factor, allowable_mpa=self.allowable_mpa,
                      youngs_modulus_mpa=self.youngs_modulus_mpa, solver=self.solver, bbox_cache=cache,
                      region_overlaps=overlaps)
        if self.percentile is not None:
            kwargs['percentile'] = self.percentile
        if self.member_volume_mm3 is not None:
            kwargs['member_volume_mm3'] = float(self.member_volume_mm3)
            kwargs['keep_cells'] = self.keep_cells
        if self.peak_limit_mpa is not None:
            kwargs['peak_limit_mpa'] = self.peak_limit_mpa
        return kwargs


def _body_signature(shape: Any, *, cache: Any) -> dict:
    raw = voxelize.unwrap(shape)
    bounds = kernel.bounding_box(raw, cache=cache)
    return dict(min=[round(float(v), 9) for v in bounds['min']],
                max=[round(float(v), 9) for v in bounds['max']],
                volume=round(float(kernel.volume(raw)), 9))


def _boundary_signature(conditions: SearchConditions) -> dict:
    """確定した節点・拘束自由度・節点荷重と対応付け規則(面添字だけでは別の表を区別できないため評価条件ハッシュに含める)。"""
    table = conditions.table
    faces: dict = {}
    for face in conditions.fixed_faces:
        ids = anchor.face_node_ids(table, face)
        entry = dict(nodes=hashlib.sha256(ids.tobytes()).hexdigest(), count=int(ids.size))
        if ids.size:
            dofs = np.asarray(table.fixed_dofs([int(face)]), dtype=np.int64)
            entry['dofs'] = hashlib.sha256(dofs.tobytes()).hexdigest()
        faces[f'fixed_{int(face)}'] = entry
    for face, force in zip(conditions.load_faces, conditions.forces):
        ids = anchor.face_node_ids(table, face)
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
    """格子・境界条件・物性・応力規則・検査許容差から評価条件の識別子を作る(§4.1)。

    `visited` はこの識別子とマスクハッシュの組をキーにする(形状ハッシュだけでは別入力の同形の合否を再利用してしまう)。
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
        member_volume_mm3=None if conditions.member_volume_mm3 is None else float(conditions.member_volume_mm3),
        member_keep=(None if conditions.member_volume_mm3 is None or conditions.keep_cells is None
                     else hashlib.sha256(np.packbits(np.asarray(conditions.keep_cells, dtype=bool)).tobytes()).hexdigest()),
        solver=str(conditions.solver),
        volume_atol=acceptance.VOLUME_ATOL,
        volume_rtol=acceptance.VOLUME_RTOL,
    )
    if conditions.peak_epsilon is not None:  # 判定しない探索の識別子は変えない
        payload['peak_epsilon'] = float(conditions.peak_epsilon)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------------------
# 入口の必要条件(修正B §4): 確定表の全節点が全設計空間の材料に接するか
# ---------------------------------------------------------------------------
def uncovered_condition_nodes(material: np.ndarray, conditions: SearchConditions) -> list:
    """条件面ごとに、確定表の節点のうち `material` に接しないものを数える(設計空間で接しない節点はどの候補でも被覆できない)。"""
    live = acceptance.active_nodes(material).ravel()
    rows = []
    for side, faces in (('fixed', conditions.fixed_faces), ('load', conditions.load_faces)):
        for face in faces:
            ids, lost = anchor.face_coverage(conditions.table, face, live)
            rows.append(dict(condition=f'{side}_face_{int(face)}', side=side, face=int(face),
                             nodes=int(ids.size), uncovered_nodes=int(lost.size),
                             uncovered_node_ids=lost[:anchor.UNMAPPED_SAMPLE_RECORD].tolist()))
    return rows


# ---------------------------------------------------------------------------
# 島処理(§4.4)
# ---------------------------------------------------------------------------
def _keep_cells(conditions: SearchConditions, shape: tuple) -> Optional[np.ndarray]:
    """`conditions.keep_cells`(指定が無ければ `None`)。形が格子と違えば例外。"""
    if conditions.keep_cells is None:
        return None
    keep = np.asarray(conditions.keep_cells, dtype=bool)
    if keep.shape != tuple(shape):
        raise ValueError(f'keep_cells の形 {keep.shape} が設計空間の形 {tuple(shape)} と違う')
    return keep


def _holds_all_conditions(component: np.ndarray, conditions: SearchConditions) -> bool:
    """この成分だけを残したとき、全固定面・荷重面の節点と各保持CADの重なりを満たし、全保持セル(`keep_cells`)を含むか。

    FEMより先に効く軽い前提審査。除外条件は見ない(材料は常に設計空間の部分集合なので成分を絞っても崩れない)。
    """
    keep = _keep_cells(conditions, component.shape)
    if keep is not None and (keep & ~component).any():
        return False
    live = acceptance.active_nodes(component).ravel()
    for face in conditions.fixed_faces + conditions.load_faces:
        ids, lost = anchor.face_coverage(conditions.table, face, live)
        if ids.size == 0 or lost.size:
            return False
    report = acceptance.region_check(component, conditions.grid, conditions.preserves, preserve=True,
                                      cache=conditions.bbox_cache, overlaps=conditions.region_overlaps)
    return report['status'] == 'pass'


def _touches_any_condition(component: np.ndarray, conditions: SearchConditions) -> bool:
    """`_holds_all_conditions` と違い、一部の条件に触れているだけで真(除去した島の記録用)。"""
    live = acceptance.active_nodes(component).ravel()
    for face in conditions.fixed_faces + conditions.load_faces:
        ids, lost = anchor.face_coverage(conditions.table, face, live)
        if ids.size > lost.size:
            return True
    return False


def resolve_islands(candidate: np.ndarray, conditions: SearchConditions) -> tuple[list, dict]:
    """連結成分ごとに条件成立を調べ、単独で成立する成分だけを候補として返す(§4.4)。

    **最大成分を選ばない。** 複数の成分が単独で成立すれば、それぞれ別候補として返す。`keep_cells` があれば
    成立の条件に「全保持セルを含む」が加わる。除去した島は数値だけを残し、マスクは保持しない。
    """
    candidate = np.ascontiguousarray(candidate, dtype=bool)
    keep = _keep_cells(conditions, candidate.shape)
    labels, count = ndimage.label(candidate, structure=NEIGHBOURHOOD_STRUCTURE)
    survivors, removed = [], []
    for label_id in range(1, count + 1):
        component = np.ascontiguousarray(labels == label_id)
        if _holds_all_conditions(component, conditions):
            survivors.append(component)
        else:
            island = dict(cells=int(component.sum()),
                          volume_mm3=float(component.sum()) * conditions.grid.element_volume,
                          touches_condition=_touches_any_condition(component, conditions))
            if keep is not None:
                island['keep_cells'] = int((keep & component).sum())
            removed.append(island)
    diagnostics = dict(components=count, surviving_components=len(survivors), removed_islands=removed)
    return survivors, diagnostics


# ---------------------------------------------------------------------------
# 探索状態(§4.1)
# ---------------------------------------------------------------------------
@dataclass
class Trial:
    """1回の候補試行の記録(history の1行、§4.1)。`checks` は6条件の合否だけ(全文の report は best_feasible にだけ残す)。"""

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
    """`best_feasible`(§4.1)。`report` はキャッシュ無しの検査(§3)の結果、`energy` はその要素エネルギー(段階4の順位用)。"""

    #: **読み取り専用**(`setflags(write=False)` 済み)。呼び出し側は書き換えず複製すること。
    mask: np.ndarray
    before_mask: np.ndarray
    report: dict
    trial_index: int
    energy: Optional[np.ndarray] = field(default=None, repr=False)

    @property
    def mask_hash(self) -> str:
        return self.report['material_sha256']

    @property
    def material_mm3(self) -> float:
        return self.report['volumes']['material_mm3']

    @property
    def safety_factor(self) -> float:
        return self.report['checks']['safety']['safety_factor']


@dataclass(frozen=True)
class AcceptedShape:
    """段階3・4で合格形状になった形(`SearchResult.accepted`)。完成形検査に落ちたとき `topopt.complete.backtrack` が戻る先。
    マスクは1セル1ビットに詰めて持つ(hanger 2.4mm 格子で1形 約 0.1 MB)。"""

    trial_index: int
    safety_factor: float
    material_mm3: float
    shape: tuple
    packed: np.ndarray = field(repr=False)

    @classmethod
    def of(cls, model: "FeasibleModel") -> "AcceptedShape":
        mask = np.asarray(model.mask, dtype=bool)
        return cls(trial_index=int(model.trial_index), safety_factor=float(model.safety_factor),
                   material_mm3=float(model.material_mm3), shape=tuple(mask.shape), packed=np.packbits(mask.ravel()))

    def mask(self) -> np.ndarray:
        return np.unpackbits(self.packed, count=int(np.prod(self.shape))).astype(bool).reshape(self.shape)


@dataclass
class SearchResult:
    """探索1回の戻り値。`status` と `termination` は別欄(§4.6:「形状の合否とは別の欄にする」)。

    `termination` の語彙: 段階3は `initial_feasible_found` / `full_material_infeasible`(全材料が形として不合格)/
    `resolution_insufficient` / `boundary_mapping_failed` / `input_contradiction`、段階4は `converged`
    (1セルずつでも削れなくなった)/ `evaluation_limit`、共通で `cancelled` / `evaluation_error`(段階3: 全材料のFEMが
    計算失敗。段階4: 出発形状のエネルギーが取れない、候補の安全率が続けて取れない
    (`stop_reason='repeated_evaluation_error'`)、または採用形状の保持・除外が前計算なしの再検査で通らず
    段階3の合格形状へ戻した)。
    段階3の `diagnostics['full_material']` に全材料の島処理と落ちた検査、段階4の
    `diagnostics['reduction']` に試行ごとの削減割合・後退倍率・落ちた条件と止まった理由が入る。
    """

    status: str  # 'feasible' | 'no_feasible_found' | 'input_contradiction' | 'resolution_insufficient' | 'boundary_mapping_failed'
    termination: str
    best_feasible: Optional[FeasibleModel]
    history: list
    evaluations: int
    conditions_identifier: str
    diagnostics: dict = field(default_factory=dict)
    #: 合格形状になった順の形(段階3の出発形状から `best_feasible` まで)。段階4に入らなかった結果では空。
    accepted: list = field(default_factory=list)


def _record_trial(history: list, parent: Optional[int], operation: str, report: dict) -> Trial:
    """`acceptance.inspect` の戻り値を history の1行へ写す(append はしない)。"""
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
                       counter: Optional[dict] = None) -> tuple[dict, Trial]:
    """1個の生存成分を検査し、history に記録する(段階3・4共通)。

    計算失敗(`unknown`、原因`evaluation_error`)は**解法を変えて再試行しない**(収束しなければ
    `PreparedSystem.solve` がその場で直接解法まで解いているので、再試行は同じ分解の繰り返しになる)。
    `survivor` が全保持セルを含まなければ例外(黙って塗り直さない)。`fields` / `overrides` は
    `acceptance.inspect` へ渡す。`counter` を渡すと FEMまで進んだ検査の回数を `counter['fem_solves']` に足す。
    """
    keep = _keep_cells(conditions, survivor.shape)
    if keep is not None and not survivor[keep].all():
        raise RuntimeError(f'評価に回る候補が保持セルを {int((keep & ~survivor).sum())} 個欠いている(探索が保持セルを空にした)')
    if fields is not None:
        fields.clear()
    report = acceptance.inspect(survivor, before, conditions.grid,
                                **{**conditions.inspect_kwargs(), **(overrides or {})}, fields=fields)
    if counter is not None and report['checks']['safety'].get('reason') != 'prerequisites_failed':
        counter['fem_solves'] += 1
    trial = _record_trial(history, parent, operation, report)
    history.append(trial)
    return report, trial


def _no_component_trial(history: list, candidate: np.ndarray, raw_hash: str, parent: Optional[int],
                        operation: str, conditions: SearchConditions, island_diag: dict) -> Trial:
    before_mm3 = float(candidate.sum()) * conditions.grid.element_volume
    trial = Trial(index=len(history), parent=parent, operation=operation, mask_hash=raw_hash,
                  before_mm3=before_mm3, material_mm3=0.0, removed_islands_mm3=before_mm3,
                  outcome='no_component',
                  note=f'{island_diag["components"]} 個の連結成分のいずれも固定面・荷重面・'
                       '保持の全条件と全保持セルを単独で満たさない')
    history.append(trial)
    return trial


# ---------------------------------------------------------------------------
# 初期合格(§4.2): 入口検査と、全材料の1回の評価
# ---------------------------------------------------------------------------
def find_initial_feasible(design_space_mask: np.ndarray, conditions: SearchConditions, *,
                           max_evaluations: int = 200,
                           min_width_cells: int = 1,
                           is_cancelled=None) -> SearchResult:
    """設計空間の全材料マスクを1回だけ評価し、6条件に合格すればそれを最初の合格モデルにする(§4.2、段階3)。

    入口で、材料を減らしても直らない状態を確定させ、FEMを始めない: `resolution_insufficient`(条件面に節点が無い)、
    `boundary_mapping_failed`(確定表の節点が全設計空間の材料にすら接しない、または `anchor.condition_mapping` が不成立。
    渡された `conditions.mapping` とここでの再検証のどちらでも止まる)、`input_contradiction`(全設計空間でも
    保持と重ならない、または閉じた空洞がある。後者は `diagnostics['closed_voids']`)。

    全材料が合格しなければ `no_feasible_found`。`termination` は、条件を単独で満たす成分が無い・最小幅違反・
    6条件のどれかが不合格なら `full_material_infeasible`、FEMの計算失敗だけなら `evaluation_error`。
    落ちた検査は `diagnostics['full_material']`(`islands` と成分ごとの `attempts`)。
    評価は bbox キャッシュも保持・除外の前計算も使わない(§3。段階4の戻り先になる合格形状なので)。

    `max_evaluations` は報告(`report['search']`)に写すだけで、評価は常に1回。`is_cancelled` は評価の前に見る。

    `min_width_cells`(m)>1 のとき、設計空間をまず m 立方の opening で絞り、入口検査も評価も絞った空間に対して行う。
    **`keep_cells` は絞らずに残す**(規則の対象外だが材料には数える)。設計空間の外にはみ出していれば `ValueError`。
    保持セルが別成分に分かれる設計空間は `full_material_infeasible`。評価する成分はFEMの前に
    `_mask_satisfies_min_width` と芯の連結を通す(島の除去は規則を保つと限らないため)。
    絞ったセル数は `diagnostics['min_width']['trimmed_cells']`(段階4まで進んだ結果では `diagnostics['initial']['min_width']`)。
    """
    design_space_mask = np.array(design_space_mask, dtype=bool, copy=True)
    keep = _keep_cells(conditions, design_space_mask.shape)
    if keep is not None and (keep & ~design_space_mask).any():
        raise ValueError(f'keep_cells が設計空間の外にある({int((keep & ~design_space_mask).sum())} セル)。'
                         '設計空間は保持セルを含む(`DesignSpace.keep` の契約)')
    if min_width_cells <= 1:
        return _find_initial_feasible(design_space_mask, conditions, max_evaluations=max_evaluations,
                                      min_width_cells=1, is_cancelled=is_cancelled)
    # 実測(hanger 2.4mm・m=3): 設計空間の縁に m 立方の入らないセルが残る(58/377,075)。`peel` の射影は入力が規則を満たす前提。
    opened = min_width.cube_opening(design_space_mask, int(min_width_cells))
    exempt = _min_width_exempt(conditions, design_space_mask.shape, min_width_cells)
    if exempt is not None:
        opened |= design_space_mask & exempt
    result = _find_initial_feasible(opened, conditions, max_evaluations=max_evaluations,
                                    min_width_cells=min_width_cells, is_cancelled=is_cancelled)
    core = min_width.CoreConnectivity(int(min_width_cells), exempt)
    result.diagnostics = dict(result.diagnostics or {}, min_width=dict(
        cells=int(min_width_cells), core_adjacency=int(core.structure.sum()) - 1, design_cells=int(design_space_mask.sum()),
        trimmed_cells=int(design_space_mask.sum() - opened.sum()), design_core_groups=int(core.groups(opened))))
    return result


def _find_initial_feasible(design_space_mask: np.ndarray, conditions: SearchConditions, *,
                           max_evaluations: int, min_width_cells: int, is_cancelled) -> SearchResult:
    """`find_initial_feasible` の本体。`design_space_mask` は最小幅で絞った後の空間(読み取り専用にして使う)。"""
    design_space_mask.setflags(write=False)
    identifier = evaluation_identifier(conditions)
    exempt = _min_width_exempt(conditions, design_space_mask.shape, min_width_cells)
    core = min_width.CoreConnectivity(min_width_cells, exempt) if min_width_cells > 1 else None

    starved = sorted((set(conditions.fixed_faces) | set(conditions.load_faces))
                      & set(int(f) for f in conditions.table.empty_faces()))
    if starved:
        return SearchResult(status='resolution_insufficient', termination='resolution_insufficient',
                             best_feasible=None, history=[], evaluations=0,
                             conditions_identifier=identifier, diagnostics=dict(empty_faces=starved))

    # `conditions.mapping` だけを信じず、探索が実際に引く表を全設計空間で検証し直す。
    entry_mapping = None
    if isinstance(conditions.table, anchor.AnchorTable):
        entry_mapping = anchor.condition_mapping(conditions.table, conditions.fixed_faces, conditions.load_faces,
                                                 conditions.forces, material=design_space_mask)
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

    # 保持との重なりは材料を減らすほど減るので、設計空間で足りなければどの形でも直らない(除外条件は逆方向でこの判定は使えない)。
    preserve_on_full = acceptance.region_check(design_space_mask, conditions.grid, conditions.preserves,
                                                preserve=True, cache=conditions.bbox_cache,
                                                overlaps=conditions.region_overlaps)
    if preserve_on_full['status'] == 'fail':
        return SearchResult(status='input_contradiction', termination='input_contradiction',
                             best_feasible=None, history=[], evaluations=0,
                             conditions_identifier=identifier,
                             diagnostics=dict(preserve_on_full_design_space=preserve_on_full))

    # 閉じた空洞のある全材料は水密で落ちるので矛盾として止める(空洞を外へ通す形は扱わない。2026-09-20 本人承認。docs/TOPOPT.md「中空構造を許容しない」)。
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

    history: list[Trial] = []
    attempts: list[dict] = []
    survivors, island_diag = resolve_islands(design_space_mask, conditions)

    def result(status: str, termination: str, best: Optional[FeasibleModel] = None) -> SearchResult:
        return SearchResult(status=status, termination=termination, best_feasible=best, history=history,
                            evaluations=1, conditions_identifier=identifier,
                            diagnostics=dict(full_material=dict(islands=island_diag, attempts=attempts)))

    if not survivors:
        trial = _no_component_trial(history, design_space_mask, acceptance.material_sha256(design_space_mask),
                                    None, 'initial', conditions, island_diag)
        attempts.append(dict(trial=trial.index, status='fail', failed=['no_component'], checks=dict(
            no_component=dict(status='fail', missing=_missing_conditions(design_space_mask, conditions)))))
        return result('no_feasible_found', 'full_material_infeasible')
    for survivor in survivors:
        # 島の除去は規則を保つと限らない(mask に無い `exempt` セルを挟んだ立方に頼るセルが、島と一緒に被覆を失う)。
        if core is not None and not (_mask_satisfies_min_width(survivor, min_width_cells, exempt)
                                     and core.connected(survivor)):
            trial = _min_width_rejection_trial(history, survivor, design_space_mask, None, 'initial', conditions)
            attempts.append(dict(trial=trial.index, status='fail', failed=['min_width'],
                                 checks=dict(min_width=dict(status='fail', cells=int(min_width_cells)))))
            continue
        fields: dict = {}
        report, trial = _evaluate_survivor(
            survivor, design_space_mask, conditions, parent=None, operation='initial', history=history, fields=fields,
            overrides=dict(conditions.inspect_kwargs(bbox_cache=None), history=history, converged=True,
                           max_evaluations=max_evaluations))
        failing = {name: check for name, check in report['checks'].items() if check.get('status') != 'pass'}
        attempts.append(dict(trial=trial.index, status=report['status'], failed=list(failing), checks=failing))
        if report['status'] != 'pass':
            continue
        survivor.setflags(write=False)
        best = FeasibleModel(mask=survivor, before_mask=design_space_mask, report=report, trial_index=trial.index,
                             energy=fields.get('element_energy'))
        return result('feasible', 'initial_feasible_found', best)
    # 計算失敗を不合格の証拠にしない(§4.5)。
    only_unknown = all(attempt['status'] == 'unknown' for attempt in attempts)
    return result('no_feasible_found', 'evaluation_error' if only_unknown else 'full_material_infeasible')


# ---------------------------------------------------------------------------
# 段階4: ひずみエネルギーの順位で削る体積削減(docs/TOPOPT.md「段階4: ひずみエネルギーの順位で削る」)
# ---------------------------------------------------------------------------
#: 1回に削る割合 r = min(DEFAULT_MAX_STEP_RATIO, 1 − (目標安全率 / 現在の安全率)^θ) × 後退倍率。
#: 上限 0.2 と θ=0.5(幾何平均)は hanger 4.8mm の比較で決めた(docs/TOPOPT.md「段階4: ひずみエネルギーの順位で削る」)。
DEFAULT_STEP_EXPONENT = 0.5
DEFAULT_MAX_STEP_RATIO = 0.2
#: 削る順位に使うエネルギーをならす近傍の一辺(セル)。3 は自身と26近傍の材料平均。
ENERGY_FILTER_SIZE = 3
#: 削ったセルが1つで安全率が落ちたとき、順位から外して次のセルを試すのを、採用を挟まず何セル続けたら止めるか(`None` は順位が
#: 尽きるまで)。既定は `None`(本人決定 2026-10-12): 不合格の続きは削り切ったことの証拠にならない(dilution 180N・V=1セルで
#: 8 は評価 154・材料 +6.5% で発火し、尽きたのは評価 682。docs/TOPOPT_STOPPING.md 3節)。止める時点だけを決めて道を変えないので、
#: 値を振る実験のつまみとして残す。
DEFAULT_SINGLE_CELL_PATIENCE: Optional[int] = None
#: 合格形状の安全率が 目標 × (1 + これ) 以下の間に1セルの手が安全率で落ちたら止める(`None` は見ない)。既定は `None`(本人決定
#: 2026-10-12): V = 1 セルでは安全率が最も熱い1要素で決まり目標の近くに張り付くので、帯は削り切ったことの証拠にならない
#: (dilution 120N で評価 14 に帯に入り、そこから材料を 18,536 → 12,664 mm³ まで削れた。docs/TOPOPT_STOPPING.md 2節)。つまみとして残す。
DEFAULT_TARGET_TOLERANCE: Optional[float] = None
#: 安全率の**評価が落ちた**(`checks['safety']` が `unknown` かつ `reason='evaluation_error'`)候補が何回続いたら段階4を打ち切るか
#: (`None` は見ない)。`fail` や `prerequisites_failed` とは別に数える(こちらは判定そのものが出ていない)。
#: **4 は実測で決めていない暫定値**(2026-09-24)。1候補の求解に 35 秒かかる実測がある。
DEFAULT_EVALUATION_ERROR_PATIENCE: Optional[int] = 4
#: ピーク判定の探索で、採用形の露出面が上限のこの割合を超えるセル(と却下の最悪セル)から `PEAK_SKIP_RADIUS` セル以内を
#: 削る順位から外す。同じ高応力箇所の周りを何度も試して patience を使い切るのを避ける(dilution で却下 89 回中 73 回が
#: 同じ1か所、止まった時点で上位30手中12手が通った。docs/TOPOPT_PEAK_GATE.md)。**値は暫定で実測で決めていない。**
PEAK_HOT_RATIO = 0.9
PEAK_SKIP_RADIUS = 2
_CORNERS = tuple(product(range(2), repeat=3))


def step_ratio(safety_factor: float, target: float, *, exponent: float = DEFAULT_STEP_EXPONENT,
               max_ratio: float = DEFAULT_MAX_STEP_RATIO) -> float:
    """安全率の余裕から決める1回の削減割合(後退倍率を掛ける前)。目標ちょうどで 0。`exponent=math.inf` なら `max_ratio` 固定。"""
    if not (safety_factor > 0 and target > 0 and 0 < max_ratio <= 1 and exponent > 0):
        raise ValueError(f'安全率・目標は正、max_ratio は (0,1]、exponent は正であること'
                         f'(安全率 {safety_factor!r} / 目標 {target!r} / max_ratio {max_ratio!r} / exponent {exponent!r})')
    gap = min(1.0, float(target) / float(safety_factor))
    return min(float(max_ratio), 1.0 - gap ** float(exponent))


def ranked_cells(mask: np.ndarray, energy: np.ndarray) -> np.ndarray:
    """材料セルの平坦添字を、近傍の材料でならしたエネルギーの昇順に並べる(同値は添字順)。"""
    material = np.asarray(mask, dtype=bool)
    total = ndimage.uniform_filter(np.where(material, energy, 0.0), size=ENERGY_FILTER_SIZE, mode='constant')
    count = ndimage.uniform_filter(material.astype(float), size=ENERGY_FILTER_SIZE, mode='constant')
    flat = np.flatnonzero(material)
    smoothed = total.ravel()[flat] / count.ravel()[flat]
    return flat[np.argsort(smoothed, kind='stable')]


def _anchor_node_ids(conditions: SearchConditions) -> np.ndarray:
    ids = [anchor.face_node_ids(conditions.table, face) for face in conditions.fixed_faces + conditions.load_faces]
    return np.unique(np.concatenate(ids)) if ids else np.zeros(0, dtype=np.int64)


#: 3×3×3 の窓の中心セルから見た6近傍の位置。
_WINDOW_NEIGHBOURS = ((0, 1, 1), (2, 1, 1), (1, 0, 1), (1, 2, 1), (1, 1, 0), (1, 1, 2))
#: 窓の中心セルの8頂点(頂点格子の添字)。窓の外周の頂点は窓の外を空と見るので使えない。
_WINDOW_CENTRE_VERTICES = (slice(1, 3),) * 3


@dataclass
class PeelStats:
    """`peel` の実績。`picked` は順位から選んで抜いた手の数、`casualties` は手のたびに一緒に抜いた巻き添え(m=1 では常に0)、
    `islands` は切り離されて消したセル。実際に減ったセル数は `picked + casualties + islands`。
    `skipped_*` は飛ばした理由ごとの数(支え・表面・傷・多様体・連結性・くびれ・細い継ぎ目・芯の連結の順に判定し、最初に落ちた理由で数える)。
    `skipped_scar` は一口(起点 + 巻き添え)が傷(`scar`。段階4で安全率に落ちた1手の削除セル)と重なった数。
    `skipped_thickness` は常に0。`skipped_neck` は `neck.NeckJudge` がくびれと判定した数、`skipped_joint` は細い継ぎ目
    (`neck.NeckJudge.creates_thin_joint`)と判定した数、`neck_full_grid` は材料が格子全体まで
    広げないとつながらなかった判定の数、`skipped_core` は `min_width.CoreConnectivity` が切れるとした数(いずれも m=1 は常に0)。
    保護セル(`protected`)を含む成分を島として切り離す手は `skipped_connectivity` に数える。"""

    picked: int = 0
    casualties: int = 0
    islands: int = 0
    skipped_support: int = 0
    skipped_surface: int = 0
    skipped_manifold: int = 0
    skipped_thickness: int = 0
    skipped_connectivity: int = 0
    skipped_neck: int = 0
    skipped_core: int = 0
    skipped_scar: int = 0
    skipped_joint: int = 0
    already_removed: int = 0
    global_checks: int = 0
    #: 芯の連結の判定を 窓でつながる / 窓で割れる / 大域の数え直し で答えた回数(`min_width.CoreConnectivity.stats`)。
    core_window_true: int = 0
    core_window_false: int = 0
    core_global: int = 0
    neck_full_grid: int = 0
    seconds: float = 0.0
    #: 順位から選んだセル(手の先頭。巻き添えは含まない)の平坦添字。
    picked_cells: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64), repr=False)

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in self.__dataclass_fields__ if name != 'picked_cells'}


def _neighbours_stay_connected(window: np.ndarray) -> Optional[bool]:
    """中心を抜いた 3×3×3 の窓で、中心の材料6近傍が6連結か(無ければ None)。真は連結維持の十分条件(偽でも割れるとは限らない)。"""
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
    return None if min_width_cells <= 1 else _keep_cells(conditions, shape)


def keep_cells_without_room(design_space: np.ndarray, keep: np.ndarray, min_width_cells: int,
                            obstacle: Optional[np.ndarray] = None) -> dict:
    """設計空間の opening(m 立方)に入らない保持セル(最小幅の保証が効かない場所)と、その周りの空きセル
    (26近傍のうち設計空間でないセル。格子の外も数える)を除外ソリッドの占有(`obstacle`)とそれ以外に分けた数。
    `{cells, around_exclude, around_outside}`。
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
    """
    if min_width_cells <= 1:
        return True
    mask = np.asarray(mask, dtype=bool)
    if exempt is None:
        return not (mask & ~min_width.cube_opening(mask, int(min_width_cells))).any()
    return not (mask & ~exempt & ~min_width.cube_opening(mask | exempt, int(min_width_cells))).any()


def peel(mask: np.ndarray, order: np.ndarray, count: int, anchor_ids: np.ndarray, *,
        min_width_cells: int = 1, exempt: Optional[np.ndarray] = None,
        protected: Optional[np.ndarray] = None, scar: Optional[np.ndarray] = None) -> tuple[np.ndarray, PeelStats]:
    """`order` を先頭から1回だけ歩き、各セルを**その時点のマスク**で判定して通ったらその場で消す。

    `count` 個(巻き添え・島として消えたぶんも数える。最後の一口が超えることがある)消えるか順位が尽きたら止める。
    `mask` が「1成分・全頂点が正則・条件節点が全て支えられている・閉じた空洞が無い・(`min_width_cells>1` なら)全材料セルが
    m 立方の材料だけのブロックに属し、侵食した芯がつながる(S(m)。`min_width.CoreConnectivity`)」なら、返すマスクも同じ。
    同じ親から何度も削るときは `PeelWalk` を使う(結果は同じで、歩いた分を使い回す)。

    **保護セル(`protected`。`mask` に含まれること)は決して抜かない**: 順位に並んでいても飛ばし(数えない)、島として切り離す手も却下する
    (`skipped_connectivity`)。m>1 では巻き添えにも入らないよう、`protected` は `exempt` に含まれること(`ValueError`)。

    **最小幅(m>1)**: 手は「セル x + 巻き添え」の一口で、巻き添えは x を抜いた後に m 立方の被覆を失う材料セル
    (`min_width.casualties`。`exempt` は被覆を問われず、材料としては他のセルの立方に数える)。一口を抜いた形は
    厚みの不変条件を構成上保つので、厚みを理由に手を却下しない。以下は一口全体に対して判定する:

    - 支え: 条件節点に接する材料の最後の1セルは抜かない(巻き添えにそのセルが入る手も却下)。
    - 多様体: 抜いた後の、一口に接する頂点が全て正則(1セルは3×3×3 の窓、複数セルは `min_width.bite_verdict`)。
    - 連結: 一口に接する材料が窓の中で連結なら通す。そうでなければマスク全体を数え直し、条件節点を支えるセルが全て
      同じ1成分にあるときだけ通して、他の成分(島)を一緒に消す。`exempt` を材料に数える m≥3 では mask に無い `exempt` セルを
      挟んで島と保持成分が同じ m 立方に入りうる(m=2 や `exempt` 無しでは起きない)ので、島を消した形を大域で確かめ、
      崩れる手は却下する(`skipped_connectivity`)。
    - くびれ(m>1): 一口を抜くと、材料の前線をつなぎ直す最小の窓の中で芯の前線がつなぎ直されない手は抜かない(`skipped_neck`。
      `neck.NeckJudge`、docs/TOPOPT_MIN_WIDTH.md 2.2節)。S(m) は芯が大域でつながるかしか見ず輪の中のくびれを通すため(#120)。
    - 細い継ぎ目(m>1): 一口で共通の m 立方を失った6近傍の材料対の芯どうしが、対のすぐ周りでつながらない手は抜かない
      (`skipped_joint`。`neck.NeckJudge.creates_thin_joint`)。くびれの判定が窓の中の迂回路で通す1セル幅の接触面も断る。
    - 芯の連結(m>1): 一口(と島)を抜くと侵食した芯が割れる手は抜かない(`skipped_core`)。R(m) は m 立方の共有1セルの斜めの
      つなぎ目を許すので、厚みの不変条件だけでは芯は割れうる。窓で言えれば通し、言えなければ大域で数え直す(島を消すときは常に大域)。
    - 表面: x が空の6近傍(格子の外を含む)を持たなければ抜かない。一口の6連結成分もそれぞれ既存の空につながっていること
      (閉じた空洞を作らない。中空構造を許容しない製品制約で、`acceptance.boundary_check` の `void_components` が落ちない根拠)。
    - 傷(`scar`。格子と同じ形の bool 配列): 一口が傷のセルを1つでも含む手は抜かない(`skipped_scar`)。
    """
    return PeelWalk(mask, anchor_ids, min_width_cells=min_width_cells, exempt=exempt, protected=protected,
                    scar=scar).take(order, count)


#: `PeelStats` のうち歩きの途中で数える欄(`picked_cells`・`seconds` と、判定器の `stats` から写す欄を除く)。
_WALK_COUNTERS = ('picked', 'casualties', 'islands', 'skipped_support', 'skipped_surface', 'skipped_manifold',
                  'skipped_connectivity', 'skipped_neck', 'skipped_core', 'skipped_scar', 'skipped_joint', 'already_removed',
                  'global_checks')


@dataclass(frozen=True)
class _Pick:
    """歩きの1手。`removed` はこの手で消したセル(一口と島)の平坦添字、`total` はここまでに消えた数の累計。
    `before` / `after` はこの手を調べる直前・抜いた直後の数え(`PeelWalk._counters`)。"""

    position: int
    cell: int
    removed: np.ndarray
    total: int
    before: tuple
    after: tuple


class PeelWalk:
    """1つの親マスクに対する `peel` の歩き。歩いた手を覚え、同じ親への次の `take` で使い回す。

    段階4は不合格のたびに同じ親から削り直す: 要求数を半分にする(候補は前の歩きの接頭辞)か、抜いた1手を順位から外す
    (その手より前のセルは同じマスクで同じ判定)。`take` は前回の順位と比べ、食い違う位置より前の手を残して、その位置の状態から
    歩きを再開する。判定は真偽と整数だけで決まるので、**候補・`picked_cells`・`PeelStats` の数(`seconds` を除く)は毎回 `peel` を
    呼んだときと等しい**(`tests/test_peel_walk.py`)。親が変わったら作り直すこと。
    """

    def __init__(self, mask: np.ndarray, anchor_ids: np.ndarray, *, min_width_cells: int = 1,
                 exempt: Optional[np.ndarray] = None, protected: Optional[np.ndarray] = None,
                 scar: Optional[np.ndarray] = None) -> None:
        self.parent = np.asarray(mask, dtype=bool)
        self.protected = None if protected is None else np.asarray(protected, dtype=bool)
        if self.protected is not None and min_width_cells > 1 and (exempt is None or (self.protected & ~exempt).any()):
            raise ValueError('m>1 の peel では protected は exempt に含まれること(巻き添えに入らないため)')
        self.anchor_ids = anchor_ids
        self.min_width_cells = int(min_width_cells)
        self.exempt = exempt
        self.scar = None if scar is None or not np.any(scar) else np.asarray(scar, dtype=bool)
        shape = self.parent.shape
        self.node_shape = tuple(n + 1 for n in shape)
        self.is_anchor = np.zeros(self.node_shape, dtype=bool)
        self.is_anchor.ravel()[anchor_ids] = True
        self.touches = np.zeros(shape, dtype=bool)
        for corner in _CORNERS:
            self.touches |= self.is_anchor[tuple(slice(c, c + n) for c, n in zip(corner, shape))]
        self.core = min_width.CoreConnectivity(self.min_width_cells, exempt) if self.min_width_cells > 1 else None
        self.necks = None if self.core is None else neck.NeckJudge(self.core)
        self.order: Optional[np.ndarray] = None
        self.position = 0
        self.picks: list[_Pick] = []
        self.count = dict.fromkeys(_WALK_COUNTERS, 0)
        self.work = self.support = None

    def _zero_counters(self) -> tuple:
        return ((0,) * len(_WALK_COUNTERS),
                None if self.core is None else dict(window_true=0, window_false=0, global_count=0, max_margin=1),
                None if self.necks is None else dict(rejected=0, full_grid=0, thin_joint=0))

    def _counters(self) -> tuple:
        return (tuple(self.count.values()),
                None if self.core is None else dict(self.core.stats),
                None if self.necks is None else dict(self.necks.stats))

    def _restore(self, upto: int, position: int, counters: Optional[tuple]) -> None:
        """`picks[:upto]` を抜いた状態・順位の `position` から歩けるようにする(`counters` が None なら数えは0から)。"""
        del self.picks[upto:]
        mask = self.parent.copy()
        for pick in self.picks:
            mask.ravel()[pick.removed] = False
        # 支えの数は条件節点でだけ読む。抜いたセルのうち条件節点に接するものだけ減らす `_walk` の数え方と、条件節点では一致する
        # (条件節点に接しないセル・島は角に条件節点を持たない)。
        self.support = np.zeros(self.node_shape, dtype=np.int32)
        for corner in _CORNERS:
            self.support[tuple(slice(c, c + n) for c, n in zip(corner, mask.shape))] += mask
        self.work = np.pad(mask, 1)
        self.position = position
        counters = self._zero_counters() if counters is None else counters
        self.count = dict(zip(_WALK_COUNTERS, counters[0]))
        if self.core is not None:
            self.core.stats.update(counters[1])
            self.necks.stats.update(counters[2])

    def take(self, order: np.ndarray, count: int) -> tuple[np.ndarray, PeelStats]:
        """`peel(mask, order, count, ...)` と同じ戻り値。"""
        started = time.perf_counter()
        if count <= 0:
            return self._result(0, self._zero_counters(), started)
        # 写しで持つ(呼び出し側が同じ配列を書き換えても、前回の順位との食い違いを見落とさない)。
        order = np.array(order if self.protected is None else order[~self.protected.ravel()[order]], copy=True)
        if self.order is None:
            self._restore(0, 0, None)
        else:
            n = min(len(order), len(self.order))
            differ = np.flatnonzero(order[:n] != self.order[:n])
            first = int(differ[0]) if differ.size else n
            if first < self.position:
                keep = sum(1 for pick in self.picks if pick.position < first)
                if keep < len(self.picks) and self.picks[keep].position == first:
                    self._restore(keep, first, self.picks[keep].before)
                elif keep:
                    self._restore(keep, self.picks[keep - 1].position + 1, self.picks[keep - 1].after)
                else:
                    self._restore(0, 0, None)
        self.order = order
        for index, pick in enumerate(self.picks):
            if pick.total >= count:
                return self._result(index + 1, pick.after, started)
        self._walk(count)
        return self._result(len(self.picks), self._counters(), started)

    def _result(self, upto: int, counters: tuple, started: float) -> tuple[np.ndarray, PeelStats]:
        candidate = self.parent.copy()
        for pick in self.picks[:upto]:
            candidate.ravel()[pick.removed] = False
        stats = PeelStats(**dict(zip(_WALK_COUNTERS, counters[0])))
        stats.picked_cells = np.asarray([pick.cell for pick in self.picks[:upto]], dtype=np.int64)
        if counters[1] is not None:
            stats.core_window_true = int(counters[1]['window_true'])
            stats.core_window_false = int(counters[1]['window_false'])
            stats.core_global = int(counters[1]['global_count'])
            stats.neck_full_grid = int(counters[2]['full_grid'])
        stats.seconds = time.perf_counter() - started
        return candidate, stats

    def _walk(self, count: int) -> None:
        """今の状態から順位を歩き、消えた数の累計が `count` に届くか順位が尽きたら止める(`peel` の判定そのもの)。"""
        shape = self.parent.shape
        node_shape = self.node_shape
        order, work, touches, exempt, protected, scar = (self.order, self.work, self.touches, self.exempt, self.protected,
                                                         self.scar)
        support_flat, anchor_flat, touches_flat = self.support.ravel(), self.is_anchor.ravel(), touches.ravel()
        plane, row = shape[1] * shape[2], shape[2]
        node_plane, node_row = node_shape[1] * node_shape[2], node_shape[2]
        inner = work[1:-1, 1:-1, 1:-1]
        m, core, necks, anchor_ids, tally = self.min_width_cells, self.core, self.necks, self.anchor_ids, self.count
        total = tally['picked'] + tally['casualties'] + tally['islands']
        while self.position < len(order) and total < count:
            position = self.position
            self.position += 1
            cell = int(order[position])
            i, rest = divmod(cell, plane)
            j, k = divmod(rest, row)
            if not work[i + 1, j + 1, k + 1]:
                tally['already_removed'] += 1
                continue
            corners = None
            if touches_flat[cell]:
                corners = [(i + a) * node_plane + (j + b) * node_row + (k + c) for a, b, c in _CORNERS]
                if any(anchor_flat[n] and support_flat[n] <= 1 for n in corners):
                    tally['skipped_support'] += 1
                    continue
            window = work[i:i + 3, j:j + 3, k:k + 3].copy()
            if all(window[p] for p in _WINDOW_NEIGHBOURS):
                tally['skipped_surface'] += 1
                continue
            # ここまでの判定は数えを動かさずに通ってきたので、これがこのセルを調べる直前の数え(この先は大域の数え直しと判定器が動かす)。
            before = self._counters()
            bite = None
            if m > 1:
                extra = min_width.casualties(inner, (i, j, k), m, exempt)
                if len(extra):
                    bite = np.vstack(([[i, j, k]], extra))
            cleared = (i, j, k) if bite is None else tuple(bite.T)
            if scar is not None and scar[cleared].any():
                tally['skipped_scar'] += 1
                continue
            if bite is None:
                window[1, 1, 1] = False
                if not _regular_after_removal(window):
                    tally['skipped_manifold'] += 1
                    continue
                connected = _neighbours_stay_connected(window)
                if connected is None:
                    tally['skipped_connectivity'] += 1
                    continue
            else:
                for ci, cj, ck in extra[touches[extra[:, 0], extra[:, 1], extra[:, 2]]]:
                    corners = (corners or []) + [(ci + a) * node_plane + (cj + b) * node_row + (ck + c)
                                                 for a, b, c in _CORNERS]
                if corners is not None:
                    dropped: dict = {}
                    for n in corners:
                        if anchor_flat[n]:
                            dropped[n] = dropped.get(n, 0) + 1
                    if any(support_flat[n] <= d for n, d in dropped.items()):
                        tally['skipped_support'] += 1
                        continue
                reason, connected = min_width.bite_verdict(work, bite)
                if reason is not None:
                    tally[f'skipped_{reason}'] += 1
                    continue
            island_cells = 0
            inner[cleared] = False
            if not connected:
                tally['global_checks'] += 1
                labels, _ = ndimage.label(inner, structure=NEIGHBOURHOOD_STRUCTURE)
                holders = np.unique(labels[touches & inner])
                if anchor_ids.size:
                    keep = int(holders[0]) if holders.size == 1 else 0
                else:
                    keep = 1 if labels.max() == 1 else 0
                if keep == 0:
                    inner[cleared] = True
                    tally['skipped_connectivity'] += 1
                    continue
                islands = (labels > 0) & (labels != keep)
                if protected is not None and (islands & protected).any():
                    inner[cleared] = True
                    tally['skipped_connectivity'] += 1
                    continue
                island_cells = int(islands.sum())
                # 島と保持成分は m=2 では同じ m 立方に入らない(2×2×2 の全セルは互いに26近傍)。m≥3 は大域で確かめる。
                if exempt is not None and m > 2 and not _mask_satisfies_min_width(inner & ~islands, m, exempt):
                    inner[cleared] = True
                    tally['skipped_connectivity'] += 1
                    continue
                # 島は条件節点に接せず、保持成分と辺・頂点でも接しない(接すれば一口の頂点の正則判定で落ちている)。
                inner[islands] = False
            if necks is not None and necks.creates_neck(inner, np.array([[i, j, k]]) if bite is None else bite):
                inner[cleared] = True
                if island_cells:
                    inner[islands] = True
                tally['skipped_neck'] += 1
                continue
            if necks is not None and necks.creates_thin_joint(inner, np.array([[i, j, k]]) if bite is None else bite):
                inner[cleared] = True
                if island_cells:
                    inner[islands] = True
                tally['skipped_joint'] += 1
                continue
            if core is not None and not core.after_bite(inner, (i, j, k) if bite is None else bite, island_cells):
                inner[cleared] = True
                if island_cells:
                    inner[islands] = True
                tally['skipped_core'] += 1
                continue
            if corners is not None:
                for n in corners:
                    support_flat[n] -= 1
            removed = (np.array([cell], dtype=np.int64) if bite is None
                       else np.ravel_multi_index(tuple(bite.T), shape).astype(np.int64))
            if island_cells:
                removed = np.concatenate([removed, np.flatnonzero(islands)])
            tally['picked'] += 1
            tally['casualties'] += 0 if bite is None else len(bite) - 1
            tally['islands'] += island_cells
            total = tally['picked'] + tally['casualties'] + tally['islands']
            self.picks.append(_Pick(position=position, cell=cell, removed=removed, total=total,
                                    before=before, after=self._counters()))


def _recheck_regions_without_precompute(best: FeasibleModel, seed: FeasibleModel,
                                        conditions: SearchConditions) -> tuple[FeasibleModel, str]:
    """段階4が更新した `best` の保持・除外を、前計算(`region_overlaps`)を使わず直接ブーリアンで取り直す(§3)。

    通れば保持・除外の報告をその値に差し替えて返し、通らなければ段階3で再検査済みの `seed` を返す
    (呼び出し側が `best` との同一性で見分ける)。`best` が `seed` のままなら何もしない。
    """
    if best is seed:
        return best, 'not_needed'
    rows = {}
    for name, bodies, preserve in (('preserve', conditions.preserves, True), ('exclude', conditions.excludes, False)):
        rows[name] = acceptance.region_check(best.mask, conditions.grid, bodies, preserve=preserve)
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
            ids, lost = anchor.face_coverage(conditions.table, face, live)
            if ids.size == 0 or lost.size:
                missing.append(f'{side}_face_{int(face)}')
    report = acceptance.region_check(mask, conditions.grid, conditions.preserves, preserve=True,
                                     cache=conditions.bbox_cache, overlaps=conditions.region_overlaps)
    if report['status'] != 'pass':
        missing.append('preserve')
    return missing


def _geometry_rejection(candidate: np.ndarray, survivors: list, island_diag: dict,
                        conditions: SearchConditions) -> Optional[dict]:
    """`peel` の候補が「1成分で、その成分が全条件を単独で満たす」ときは None。満たさなければ理由
    (連結性は `peel` の不変条件の破れ。保持の重なりは `peel` が見ないのでここで落ちうる)。"""
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
                  note='FEMの前に最小幅の不変条件(m立方の材料だけのブロックに全材料セルが属する・侵食した芯がつながる)で不合格')
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


def _check_stage4_options(single_cell_patience, target_tolerance, evaluation_error_patience) -> None:
    """段階4の引数を検査する。段階3の前(`find_best_feasible` の入口)でも呼ぶのは、後だと全材料のFEMを解いてから落ち、段階3不合格なら不正値が黙って通るため。"""
    if single_cell_patience is not None and not (
            isinstance(single_cell_patience, int) and not isinstance(single_cell_patience, bool) and single_cell_patience >= 1):
        raise ValueError(f'single_cell_patience は 1 以上の整数か None(無制限): {single_cell_patience!r}')
    if target_tolerance is not None and not (
            isinstance(target_tolerance, (int, float)) and not isinstance(target_tolerance, bool)
            and math.isfinite(target_tolerance) and target_tolerance >= 0):
        raise ValueError(f'target_tolerance は 0 以上の有限の数か None(見ない): {target_tolerance!r}')
    if evaluation_error_patience is not None and not (
            isinstance(evaluation_error_patience, int) and not isinstance(evaluation_error_patience, bool)
            and evaluation_error_patience >= 1):
        raise ValueError(f'evaluation_error_patience は 1 以上の整数か None(数えない): {evaluation_error_patience!r}')


def _attach_peak_limit(best: FeasibleModel, conditions: SearchConditions, reduction: dict):
    """段階4の出発形のセルごとの6面の最大 B から、ピーク判定の上限 `max(許容 ÷ 目標, (1+ε)·B)` を作って条件に付ける。

    戻り値は (上限を付けた条件, 出発形の要素エネルギー)。B を測れなければ (None, None)。B は入口で1回だけ測り、
    採用のたびに更新しない(利用者の形が全材料で持つ値を超えて悪化させない、だけを約束する。docs/TOPOPT_PEAK_GATE.md)。
    """
    if conditions.member_volume_mm3 is None:
        raise ValueError('peak_epsilon には member_volume_mm3(部材域の判定)が要る')
    epsilon = float(conditions.peak_epsilon)
    if not (math.isfinite(epsilon) and epsilon >= 0.0):
        raise ValueError(f'peak_epsilon は有限の 0 以上であること(受け取った値: {conditions.peak_epsilon!r})')
    fields: dict = {'cell_face_max': None}
    acceptance.inspect(best.mask, best.before_mask, conditions.grid, fields=fields,
                       **conditions.inspect_kwargs(bbox_cache=None))
    reduction['fem_solves'] += 1
    base = fields.get('cell_face_max')
    if base is None:
        return None, None
    sigma_t = float(conditions.allowable_mpa) / float(conditions.target_safety_factor)
    limit = np.maximum(sigma_t, (1.0 + epsilon) * base)
    region = acceptance.member_region(best.mask, conditions.table, conditions.fixed_faces, conditions.load_faces,
                                      conditions.keep_cells)
    worst = np.unravel_index(int(np.argmax(np.where(region, base, -np.inf))), base.shape)
    reduction['peak_gate'] = dict(
        epsilon=epsilon, sigma_t_mpa=sigma_t, baseline_max_mpa=float(base[worst]),
        baseline_max_position_mm=[float(v) for v in np.asarray(conditions.grid.origin)
                                  + (np.asarray(worst, dtype=float) + 0.5) * conditions.grid.spacing],
        cells_above_sigma_t=int((region & (base > sigma_t)).sum()),
        rejections=0, rejections_with_safety_margin=0, single_cell_rejections=0, skipped_near_peaks=0,
        rejection_sites=[])
    return replace(conditions, peak_limit_mpa=limit), fields.get('element_energy')


def _reduce_volume(initial: SearchResult, conditions: SearchConditions,
                   *, max_evaluations: int, is_cancelled,
                   step_exponent: float = DEFAULT_STEP_EXPONENT,
                   max_step_ratio: float = DEFAULT_MAX_STEP_RATIO,
                   single_cell_patience: Optional[int] = DEFAULT_SINGLE_CELL_PATIENCE,
                   target_tolerance: Optional[float] = DEFAULT_TARGET_TOLERANCE,
                   evaluation_error_patience: Optional[int] = DEFAULT_EVALUATION_ERROR_PATIENCE,
                   min_width_cells: int = 1, on_evaluation=None, on_accept=None) -> SearchResult:
    """`initial.best_feasible` から、ひずみエネルギーの低い材料をまとめて削り続ける(段階4)。

    合格形状は1つだけ持つ。1回の試行: 合格形状の要素エネルギー(近傍でならす)の低い順に
    `k = max(1, ⌊r × 後退倍率 × 材料セル数⌋)` セルを `peel` で削る(`step_ratio`。m>1 では `k` は巻き添えも数える)。
    候補は島処理(`resolve_islands`)の検算 → 6条件(FEM)。合格すればその形状とエネルギーへ進み、後退倍率を2倍に戻す(上限1)。
    不合格・計算失敗なら形状を変えず、その形の内容ハッシュを `rejected` に入れる(同じ形は同じ答えなので採用で空にしない)。
    `rejected` の形がもう一度できたらFEMを解かずに同じ手順で次へ進む。`keep_cells` は `peel` の `protected` として渡す。
    順位から選んだ手が2つ以上なら後退倍率を半分にして同じ順位から歩き直す(再求解しない)。1つだけなら、
    その手のセルを順位から外して次のセルを試す。**安全率で落ちた1セルは採用をまたいで外したまま**にする(`poison`)。
    外したときの合格形状の安全率と不足分 `目標 − 候補の安全率` を覚え、その後の合格形状の安全率が不足分以上に上がった
    セルだけ順位へ戻す(代表応力がパーセンタイルなので削るほど安全率が上がることがある)。安全率以外で落ちた1セルは
    その形状の間だけ外す。
    **傷**: 安全率で落ちた1手は、その一口の削除セル(起点 + 巻き添え + 島)も覚え、以後の歩きで一口がそれと1セルでも重なる手を
    飛ばす(`peel` の `scar`)。起点だけを外す毒では、同じ部材をかじる別の起点の一口を一つずつ FEM で確かめていた
    (docs/TOPOPT_CONVERGENCE.md)。戻し方は毒と同じ。2手以上の不合格は傷にしない(どの手が悪いか分からない)。
    **終端の確かめ直し**: 順位を歩いて1手も抜けないとき、傷か毒が残っていれば両方を外して歩き直す。外した後に採用が無いまま
    また尽きたら止める(採用があれば探索を続け、次に尽きたらまた外す)。
    停止: 確かめ直しの後に順位を歩いて1手も抜けなければ `converged`(`no_removable_cell`)。既定ではこれだけが収束で、ほかは
    評価予算(`evaluation_limit`)・中断(`cancelled`)と、**評価が落ちた候補**(`checks['safety']` が `unknown` かつ
    `reason='evaluation_error'`。`prerequisites_failed` は形状の不合格なので入らない)が採用を挟まず
    `evaluation_error_patience` 回続いたときの `evaluation_error`(`repeated_evaluation_error`。合格形状は返す)。
    つまみ(既定 `None`): 安全率で落ちたセルが採用を挟まず `single_cell_patience` 個に達したら `safety_at_single_cell` で
    `converged`。合格形状の安全率が `目標 × (1 + target_tolerance)` 以下の間に1セルの手が安全率で落ちたら
    `within_target_tolerance` で `converged`。どちらも止める時点だけを決め、道を変えない。
    止まった理由は `diagnostics['reduction']['stop_reason']`、試行ごとの記録は `['steps']`、検算で落ちた候補は
    `['geometry_rejections']`、外したままのセル数と順位へ戻した回数は `['poison_cells']` / `['poison_released']`、
    傷は `['scars']`(`recorded` / `released`(安全率が上がって戻した)/ `remaining` / `terminal_rounds`(確かめ直しの回数)/
    `terminal_released`(確かめ直しで外した傷と毒の数)/ `accepted_after_terminal`(最初の確かめ直しより後の採用の数))。

    評価予算は両段階を通した1本(`initial.evaluations` から数える。削る候補1つが1回)。評価は bbox キャッシュ無しで行うが、
    保持・除外の共通体積は前計算から引く。採用形状は出る前に直接ブーリアンで取り直す(`_recheck_regions_without_precompute`)。

    `on_evaluation(event: dict)` は出発形状で1回(`outcome='initial'`)、以降は評価に数えた候補ごとに1回、合格形状を更新した後で呼ぶ。
    `event` は素の値だけ: `evaluation`(通算の評価回数)/ `outcome`(`'initial'` / `'accepted'` / `'rejected'`)/ `failed` /
    `candidate_safety_factor`(FEMを解かなかった候補は `None`)/ `candidate_cells` / `safety_factor`・`cells`(その時点の合格形状)/
    `target_safety_factor`。**例外を投げれば探索ごと落ちる**ので、呼び出し側で握ること。
    `on_accept(shape: AcceptedShape, evaluation: int)` は合格形状が増えるたびに(出発形状を含む)、その `on_evaluation` より前に
    呼ぶ。並びは `SearchResult.accepted` と同じだが、出る前の再検査に落ちると `accepted` は出発形状だけに縮む(`accepted` が正)。
    """
    _check_stage4_options(single_cell_patience, target_tolerance, evaluation_error_patience)
    identifier = initial.conditions_identifier
    history = list(initial.history)
    best = initial.best_feasible
    accepted_shapes = [AcceptedShape.of(best)]
    if on_accept is not None:
        on_accept(accepted_shapes[-1], int(initial.evaluations))
    evaluations = int(initial.evaluations)
    target = float(conditions.target_safety_factor)
    anchor_ids = _anchor_node_ids(conditions)
    exempt = _min_width_exempt(conditions, best.mask.shape, min_width_cells)
    protected = _keep_cells(conditions, best.mask.shape)
    steps: list[dict] = []
    # 安全率で落ちた1セル → (外したときの合格形状の安全率, 不足分。取れなければ None = 戻さない)。
    poison: dict[int, tuple[float, Optional[float]]] = {}
    # 安全率で落ちた1手の一口の削除セル(平坦添字)・記録したときの合格形状の安全率・不足分(戻し方は毒と同じ)。
    scars: list[tuple[np.ndarray, float, Optional[float]]] = []
    terminal_unconfirmed = False  # 確かめ直しで傷と毒を外した後、まだ採用が無い
    reduction = dict(step_exponent=float(step_exponent), max_step_ratio=float(max_step_ratio),
                     filter_size=ENERGY_FILTER_SIZE, fem_solves=0, min_width_cells=int(min_width_cells),
                     peel_calls=0, peel_seconds=0.0, peel_seconds_max=0.0, repeated_candidates=0,
                     single_cell_patience=single_cell_patience, target_tolerance=target_tolerance,
                     evaluation_error_patience=evaluation_error_patience, evaluation_errors=0,
                     safety_excluded_cells=0, adopted_after_exclusion=0, poison_cells=0, poison_released=0,
                     scars=dict(recorded=0, released=0, remaining=0, terminal_rounds=0, terminal_released=0,
                                accepted_after_terminal=0),
                     geometry_rejections=[], stop_reason=None, steps=steps)
    # 形のくびれの静的診断(探索の判定ではない。docs/TOPOPT_MIN_WIDTH.md 2.2節)。
    static_core = min_width.CoreConnectivity(min_width_cells, exempt) if min_width_cells > 1 else None
    if static_core is not None:
        reduction['static_necks'] = dict(start=neck.static_necks(best.mask, static_core))

    def result(termination: str) -> SearchResult:
        final, verdict = _recheck_regions_without_precompute(best, initial.best_feasible, conditions)
        reduction['final_region_recheck'] = verdict
        if static_core is not None:
            reduction['static_necks']['result'] = neck.static_necks(final.mask, static_core)
        reduction['poison_cells'] = len(poison)
        reduction['scars']['remaining'] = len(scars)
        shapes = accepted_shapes
        if verdict.startswith('failed'):
            termination = 'evaluation_error'
            shapes = accepted_shapes[:1]  # 段階3の合格形状へ戻した
        return SearchResult(status='feasible', termination=termination, best_feasible=final, history=history,
                            evaluations=evaluations, conditions_identifier=identifier,
                            diagnostics=dict(initial=initial.diagnostics, reduction=reduction), accepted=shapes)

    def notify(outcome: str, failed: list, candidate_safety_factor: Optional[float], candidate_cells: int) -> None:
        if on_evaluation is not None:
            on_evaluation(dict(evaluation=evaluations, outcome=outcome, failed=sorted(set(failed)),
                               candidate_safety_factor=None if candidate_safety_factor is None else float(candidate_safety_factor),
                               candidate_cells=int(candidate_cells),
                               safety_factor=float(best.safety_factor), cells=int(best.mask.sum()),
                               target_safety_factor=target))

    notify('initial', [], float(best.safety_factor), int(best.mask.sum()))
    energy = best.energy
    if conditions.peak_epsilon is not None:
        gated, energy = _attach_peak_limit(best, conditions, reduction)
        if gated is None:
            reduction['stop_reason'] = 'peak_baseline_unavailable'
            return result('evaluation_error')
        conditions = gated
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
    rejected: set[str] = set()
    walk: Optional[PeelWalk] = None  # 今の合格形状からの歩き。採用で作り直す

    def ranking() -> np.ndarray:
        released = [cell for cell, (base, shortfall) in poison.items()
                    if shortfall is not None and best.safety_factor - base >= shortfall]
        for cell in released:
            del poison[cell]
        reduction['poison_released'] += len(released)
        kept = [scar for scar in scars if not (scar[2] is not None and best.safety_factor - scar[1] >= scar[2])]
        reduction['scars']['released'] += len(scars) - len(kept)
        scars[:] = kept
        order = ranked_cells(best.mask, energy)
        if poison:
            order = order[~np.isin(order, list(poison))]
        return away_from_peaks(order)

    peak_near = None  # 採用形の高応力箇所の近く(ピーク判定のときだけ)

    def away_from_peaks(order: np.ndarray) -> np.ndarray:
        if peak_near is None:
            return order
        keep_order = ~peak_near.ravel()[order]
        reduction['peak_gate']['skipped_near_peaks'] += int((~keep_order).sum())
        return order[keep_order]

    def near(cells: np.ndarray) -> np.ndarray:
        return ndimage.binary_dilation(cells, structure=np.ones((3, 3, 3), bool), iterations=PEAK_SKIP_RADIUS)

    def scar_mask() -> Optional[np.ndarray]:
        if not scars:
            return None
        mask = np.zeros(best.mask.size, dtype=bool)
        for cells, _, _ in scars:
            mask[cells] = True
        return mask.reshape(best.mask.shape)

    order = ranking()
    safety_streak = 0
    peak_streak = 0
    error_streak = 0
    while True:
        if evaluations >= max_evaluations:
            return result('evaluation_limit')
        if is_cancelled is not None and is_cancelled():
            return result('cancelled')
        gap_ratio = step_ratio(best.safety_factor, target, exponent=step_exponent, max_ratio=max_step_ratio)
        requested = max(1, int(gap_ratio * multiplier * int(best.mask.sum())))
        if walk is None:
            walk = PeelWalk(best.mask, anchor_ids, min_width_cells=min_width_cells, exempt=exempt, protected=protected,
                            scar=scar_mask())
        candidate, peeled = walk.take(order, requested)
        reduction['peel_calls'] += 1
        reduction['peel_seconds'] += peeled.seconds
        reduction['peel_seconds_max'] = max(reduction['peel_seconds_max'], peeled.seconds)
        if peeled.picked == 0:
            if (scars or poison) and not terminal_unconfirmed:
                # 終端の確かめ直し: 傷と毒は同じ親で FEM に落ちた手からの推定を含むので、外して今の形で歩き直す。
                reduction['scars']['terminal_rounds'] += 1
                reduction['scars']['terminal_released'] += len(scars) + len(poison)
                scars.clear()
                poison.clear()
                terminal_unconfirmed = True
                order = ranking()
                walk = None
                continue
            reduction['stop_reason'] = 'no_removable_cell'
            return result('converged')
        candidate.setflags(write=False)
        key = acceptance.material_sha256(candidate)
        if key in rejected:
            # 不合格が分かっている候補を解き直さない。
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

        accepted, failed, safety_failed, safety_unknown, peak_failed = None, [], False, False, False
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
                                               counter=reduction)
            if report['status'] == 'pass' and 'element_energy' in fields:
                accepted = (survivors[0], report, fields['element_energy'])
            else:
                failed.extend(name for name, check in report['checks'].items() if check.get('status') != 'pass')
                safety_failed = report['checks']['safety'].get('status') == 'fail'
                # `prerequisites_failed` は形状の不合格なので数えない(混ぜると幾何で落ちる候補が続いただけで打ち切る)。
                safety_unknown = (report['checks']['safety'].get('status') == 'unknown'
                                  and report['checks']['safety'].get('reason') == 'evaluation_error')
                peak = report['checks'].get('peak')
                peak_failed = peak is not None and peak.get('status') == 'fail'
                if peak_failed:
                    gate = reduction['peak_gate']
                    gate['rejections'] += 1
                    sf = report['checks']['safety'].get('safety_factor')
                    margin = not safety_failed and sf is not None and sf >= target * 1.1
                    gate['rejections_with_safety_margin'] += int(margin)
                    if len(gate['rejection_sites']) < 200:
                        gate['rejection_sites'].append(dict(
                            step=len(steps) - 1, picked=int(peeled.picked), safety_factor=sf, safety_margin=margin,
                            worst_ratio=peak['worst_ratio'], worst_mpa=peak['worst_mpa'],
                            worst_position_mm=peak['worst_position_mm']))
        step.update(trials=[trial.index], outcome='accepted' if accepted is not None else 'rejected',
                    failed=sorted(set(failed)))

        if accepted is not None:
            survivor, report, energy = accepted
            survivor = np.array(survivor, dtype=bool, copy=True)
            survivor.setflags(write=False)
            best = FeasibleModel(mask=survivor, before_mask=candidate, report=report, trial_index=trial.index,
                                 energy=energy)
            accepted_shapes.append(AcceptedShape.of(best))
            if on_accept is not None:
                on_accept(accepted_shapes[-1], evaluations)
            walk = None
            step.update(safety_factor=best.safety_factor, material_mm3=best.material_mm3)
            multiplier = min(1.0, 2.0 * multiplier)
            if conditions.peak_limit_mpa is not None and 'peak_ratio' in fields:
                peak_near = near(fields['peak_ratio'] >= PEAK_HOT_RATIO)
            order = ranking()
            reduction['adopted_after_exclusion'] += int(safety_streak > 0)
            reduction['scars']['accepted_after_terminal'] += int(reduction['scars']['terminal_rounds'] > 0)
            terminal_unconfirmed = False
            safety_streak = 0
            peak_streak = 0
            error_streak = 0
            notify('accepted', failed, best.safety_factor, int(best.mask.sum()))
        else:
            rejected.add(key)
            notify('rejected', failed, trial.safety_factor,
                   int((candidate if rejection is not None else survivors[0]).sum()))
            if safety_unknown:
                # 判定が取れていないので `single_cell_patience` は減らない。別の打ち切りが無いと予算を使い切るまで走る(1候補35秒でも約24時間)。
                reduction['evaluation_errors'] += 1
                error_streak += 1
                if evaluation_error_patience is not None and error_streak >= evaluation_error_patience:
                    reduction['stop_reason'] = 'repeated_evaluation_error'
                    return result('evaluation_error')
            else:
                error_streak = 0
            if peeled.picked > 1:
                multiplier /= 2.0
            else:
                if safety_failed:
                    if target_tolerance is not None and best.safety_factor <= target * (1.0 + target_tolerance):
                        reduction['stop_reason'] = 'within_target_tolerance'
                        return result('converged')
                    safety_streak += 1
                    if single_cell_patience is not None and safety_streak >= single_cell_patience:
                        reduction['stop_reason'] = 'safety_at_single_cell'
                        return result('converged')
                    reduction['safety_excluded_cells'] += 1
                    candidate_sf = trial.safety_factor
                    shortfall = (target - float(candidate_sf)
                                 if candidate_sf is not None and math.isfinite(candidate_sf) and candidate_sf < target
                                 else None)
                    poison[int(peeled.picked_cells[0])] = (float(best.safety_factor), shortfall)
                    scars.append((np.flatnonzero(best.mask.ravel() & ~candidate.ravel()), float(best.safety_factor),
                                  shortfall))
                    reduction['scars']['recorded'] += 1
                    walk = None
                elif peak_failed:
                    # ピークだけで落ちた1セルは、この形の間だけ順位から外す(採用で順位を作り直すと戻る)。
                    peak_streak += 1
                    reduction['peak_gate']['single_cell_rejections'] += 1
                    if single_cell_patience is not None and peak_streak >= single_cell_patience:
                        reduction['stop_reason'] = 'peak_at_single_cell'
                        return result('converged')
                order = order[~np.isin(order, peeled.picked_cells)]
            if peak_failed:
                worst = np.zeros(best.mask.shape, dtype=bool)
                worst[tuple(peak['worst_cell'])] = True
                peak_near = near(worst) if peak_near is None else peak_near | near(worst)
                order = away_from_peaks(order)


def find_best_feasible(design_space_mask: np.ndarray, conditions: SearchConditions, *,
                       max_evaluations: int = 1000,
                       step_exponent: float = DEFAULT_STEP_EXPONENT,
                       max_step_ratio: float = DEFAULT_MAX_STEP_RATIO,
                       single_cell_patience: Optional[int] = DEFAULT_SINGLE_CELL_PATIENCE,
                       target_tolerance: Optional[float] = DEFAULT_TARGET_TOLERANCE,
                       evaluation_error_patience: Optional[int] = DEFAULT_EVALUATION_ERROR_PATIENCE,
                       min_width_cells: int = 1,
                       is_cancelled=None, on_evaluation=None, on_accept=None) -> SearchResult:
    """全材料の評価(段階3)に続けて、ひずみエネルギーの低い材料を削って体積を減らす単一の探索入口(§3)。

    `find_initial_feasible` が合格しなければその `SearchResult` をそのまま返し、合格すれば `_reduce_volume` で
    体積削減を続ける。`max_evaluations` は**両段階を通した1本の予算**(全材料の評価が1回、残りが段階4の候補)。
    `step_exponent` / `max_step_ratio` / `single_cell_patience` / `target_tolerance` / `evaluation_error_patience` は段階4だけが使う。
    `min_width_cells` は両段階が使う(段階3は設計空間を絞り、段階4は被覆を失うセルを同じ一手で抜く)。
    `keep_cells` はどちらの段階でも空にならず、最小幅の規則の対象外で絞りからも外れる(材料としては数える)。

    `is_cancelled` は引数無しで呼べる callable。段階3は評価の前、段階4は候補を作る前ごとに見る。真を返した時点の
    `best_feasible` を返す(§4.6)。初期合格の前に中断されれば `no_feasible_found` / `cancelled`。
    `on_evaluation` / `on_accept` は段階4へそのまま渡す(`_reduce_volume` docstring)。全材料が不合格なら呼ばれない。
    """
    _check_stage4_options(single_cell_patience, target_tolerance, evaluation_error_patience)
    identifier = evaluation_identifier(conditions)
    if is_cancelled is not None and is_cancelled():
        return SearchResult(status='no_feasible_found', termination='cancelled', best_feasible=None,
                             history=[], evaluations=0, conditions_identifier=identifier)
    initial = find_initial_feasible(design_space_mask, conditions, max_evaluations=max_evaluations,
                                    min_width_cells=min_width_cells, is_cancelled=is_cancelled)
    if initial.status != 'feasible':
        return initial
    return _reduce_volume(initial, conditions, max_evaluations=max_evaluations,
                          is_cancelled=is_cancelled, step_exponent=step_exponent, max_step_ratio=max_step_ratio,
                          single_cell_patience=single_cell_patience, target_tolerance=target_tolerance,
                          evaluation_error_patience=evaluation_error_patience,
                          min_width_cells=min_width_cells, on_evaluation=on_evaluation, on_accept=on_accept)
