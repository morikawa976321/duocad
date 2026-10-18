"""タスク4: 20%削除・5%削除・適応歩幅＋復帰・再配置の4探索方式を公平に比較する実験用実装。

**製品コードは変更しない**(`docs/TOPOPT_IDLE_MATERIAL.md`)。既存のFEM(`duocad.topopt.fem`/`stress`)・
幾何検査(`duocad.topopt.acceptance`/`binary_search.peel`/`resolve_islands`)・力学条件
(`optimize_run.build_conditions`/`_build_search_conditions`)は共用する。評価器は03bが凍結した
版をそのまま使う(`stress_metrics.py` M5b=`metric_exceedance_integral`〈合否ゲート〉・
M3=`metric_zone_percentile`〈部位別診断〉・M7b=`metric_ks_aggregation`〈探索補助〉)。

## 4方式の入口
- `run_fixed_step(ctx, config)`: **製品コードそのもの**(`binary_search.find_best_feasible`)を
  `max_step_ratio` だけ変えて呼ぶ。`method="step20"`(既定0.2, 製品既定)/`"step05"`(0.05)。
  安全率ゲートは製品どおり(パーセンタイル)。
- `run_adaptive(ctx, config)`: 新規。歩幅は安全率余裕(`step_ratio`)と実削除体積(巻き添え/島込みの
  実削除セルの要素エネルギー、一次予測の基準)から決める。**部位別応力(M3)・compliance C・最大変位**
  は候補ごとの一手あたり変化を監視し、いずれかが許容を超えて悪化(急変)または一次予測との乖離が
  大きければロールバック(後退倍率を半減して同じ基点から再評価)する。停滞したら
  最軽量合格形へ復帰し別順序で再開する。安全率ゲートは `AdaptiveConfig.gate`
  (`"m5b_raw"`〈既定。03bの凍結定義〉/`"m5b_target"`/`"percentile"`)。
- `run_reallocation(ctx, config)`: 新規。`run_adaptive` の削減フェーズを内側に持ち、削減が停滞したら
  同体積付近で追加→削減のサイクルへ入る。追加候補は初期全材料形・実行内履歴・外部候補
  (`ReallocationConfig.external_candidate_masks`)の和集合から、現在形へ再結合する
  連結成分ごと(＝失った部材の単位)を長い順に提案する。

## 評価器
`score_mask(mask, ctx, *, m5b_tolerance_mpa_mm3=0.0, m3_percentile=99.5, ks_rho=8.0)` が候補1つを
1回のFEM(`_fem_probe.evaluate_full`)で採点する(`CandidateScore`)。
"""
from __future__ import annotations

import dataclasses
import json
import sys
import time
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import numpy as np  # noqa: E402
from scipy import ndimage  # noqa: E402

from duocad import optimize_run  # noqa: E402
from duocad.topopt import acceptance, binary_search, design_space as ds, fem, min_width, stress  # noqa: E402

import _common  # noqa: E402
import _fem_probe  # noqa: E402
import refine_shape  # noqa: E402
import stress_metrics as sm  # noqa: E402

NEIGHBOURHOOD_STRUCTURE = acceptance.NEIGHBOURHOOD_STRUCTURE


# ---------------------------------------------------------------------------
# 文脈構築(4方式が共有する: 力学条件・設計空間・アンカー表)
# ---------------------------------------------------------------------------
@dataclass
class SearchContext:
    """1つの(モデル, 荷重, 要素サイズ, 最小幅)条件を通して固定される探索文脈。
    `metric_eval.solve_shape` と同じ条件構築パイプラインを使う(単一の真実源。`build_context` 参照)。"""

    model_path: Path
    load_n: float
    element_size_mm: float
    min_width_mm: float
    cond: Any  # binary_search.SearchConditions
    space: Any  # design_space.DesignSpace
    table: Any
    min_width_cells: int

    @property
    def grid(self):
        return self.space.grid


def build_context(model_path: Path, *, load_n: float, element_size_mm: float, min_width_mm: float) -> SearchContext:
    """`metric_eval.solve_shape` と同じ手順で `cond`/`space`/`table` を組む。
    保存済み形状ではなく**新規に探索する**ので設計空間全体(`space.mask`)が要り、
    `_build_search_conditions` で止めて投影・保存マスクの読み込みは行わない。
    `min_width_mm` は `optimize_run._resolve_min_width` で `min_width_cells` へ解決し、以後の
    `find_initial_feasible`/`find_best_feasible`/削減ループへ明示的に渡す
    (`00_COMPACT_MODELS.md` 3-1節の罠: 渡し忘れると既定 `min_width_cells=1`〈制約なし〉で走る)。
    """
    doc = refine_shape.open_session(model_path).document
    refine_shape._set_load_doc(doc, load_n)
    bundle = optimize_run.build_conditions(doc)
    space = ds.derive(bundle.conditions, spacing=element_size_mm)
    table, mapping = optimize_run._map_boundary_conditions(space, bundle)
    cond = optimize_run._build_search_conditions(
        doc, space, bundle, optimize_run.OptimizeOptions(element_size_mm=element_size_mm),
        table=table, mapping=mapping,
    )
    cond = dataclasses.replace(cond, member_volume_mm3=None)  # この研究は材料全体の99.5%点(2026-10-09 より前の製品)を対象にする
    resolved = optimize_run._resolve_min_width(space.grid.spacing, min_width_mm)
    return SearchContext(model_path=model_path, load_n=load_n, element_size_mm=element_size_mm,
                         min_width_mm=min_width_mm, cond=cond, space=space, table=table,
                         min_width_cells=int(resolved["cells"]))


# ---------------------------------------------------------------------------
# 評価器(03bの凍結版をそのまま呼ぶ。§後続への契約②)
# ---------------------------------------------------------------------------
@dataclass
class CandidateScore:
    """候補マスク1つのFEM1回ぶんの採点。**`m5b_raw_*` と `m5b_target_*` の違いに注意**:
    `m5b_raw_*` は `cond.allowable_mpa`(材料の生の許容応力。例: 40MPa)をしきい値にする
    (03bが凍結した定義)。`m5b_target_*` は `cond.allowable_mpa / cond.target_safety_factor` を
    しきい値にした版で、製品の探索目標(方式1・2がパーセンタイルで守るSF)と**同じ安全余裕**を測る。
    **どちらを受理ゲートに使うかは `AdaptiveConfig.gate` が選ぶ**。`CandidateScore` は両方を
    常に計算して返すだけ(実測: dilution_smallの全材料形は `m5b_raw` では合格するが
    `m5b_target` では不合格〈192.9 MPa·mm³超過〉)。`safety_factor_product` は製品の
    パーセンタイル安全率で、ゲートには使わず比較・報告のため常に載せる。
    """

    material_mm3: float
    safety_factor_product: float
    m5b_raw_exceedance_mpa_mm3: float
    m5b_raw_passed: bool
    m5b_target_exceedance_mpa_mm3: float
    m5b_target_passed: bool
    m3_worst_zone: Optional[str]
    m3_worst_value: Optional[float]
    m3_passed: Optional[bool]
    m7b_ks_mpa: float
    compliance: float
    max_displacement_mm: float
    element_energy: np.ndarray = field(repr=False)
    von_mises_mpa: np.ndarray = field(repr=False)
    zones: dict = field(repr=False)
    mask_hash: str = ""

    def to_json(self) -> dict:
        return dict(material_mm3=self.material_mm3, safety_factor_product=self.safety_factor_product,
                    m5b_raw_exceedance_mpa_mm3=self.m5b_raw_exceedance_mpa_mm3, m5b_raw_passed=self.m5b_raw_passed,
                    m5b_target_exceedance_mpa_mm3=self.m5b_target_exceedance_mpa_mm3,
                    m5b_target_passed=self.m5b_target_passed,
                    m3_worst_zone=self.m3_worst_zone, m3_worst_value=self.m3_worst_value,
                    m3_passed=self.m3_passed, m7b_ks_mpa=self.m7b_ks_mpa, compliance=self.compliance,
                    max_displacement_mm=self.max_displacement_mm, mask_hash=self.mask_hash)


def score_mask(mask: np.ndarray, ctx: SearchContext, *, m5b_tolerance_mpa_mm3: float = 0.0,
              m3_percentile: float = 99.5, ks_rho: float = 8.0,
              zone_distance_mm: Optional[float] = None,
              probe: Optional[dict] = None) -> Optional[CandidateScore]:
    """`mask` を1回FEMで解き、M5b/M3/M7b・compliance・要素エネルギーまで一括で返す。
    FEM(残差検算含む)が失敗すれば `None`(幾何は合格・物理が不明の候補)。
    `_fem_probe.evaluate_full` の `_model`/`_density`/`_displacement` から `model.compliance` を
    直接呼び、`acceptance.safety_check` と同じ式で要素エネルギーを取る(2回目のFEM解を避ける。
    層をまたぐ契約: `fem.Model.compliance` の `ce` は全要素ぶん返る仕様を前提にしている)。
    `probe` は同じ `mask`・`ctx` で呼び出し側が既に解いた `evaluate_full` の結果(渡さなければここで解く)。
    """
    cond = ctx.cond
    if probe is None:
        probe = _fem_probe.evaluate_full(
            mask, ctx.grid, ctx.table, cond.fixed_faces, cond.load_faces, cond.forces,
            allowable_mpa=cond.allowable_mpa, youngs_modulus_mpa=cond.youngs_modulus_mpa,
            percentile=cond.percentile or stress.DEFAULT_PERCENTILE, solver=cond.solver,
        )
    if probe["status"] != "ok":
        return None
    model, density, displacement = probe["_model"], probe["_density"], probe["_displacement"]
    c, element_energy = model.compliance(density, stress.PENAL, displacement)
    element_energy = np.where(mask, element_energy, 0.0)

    dist = zone_distance_mm if zone_distance_mm is not None else 3.0 * ctx.grid.spacing
    fixed_boxes = _fem_probe.face_world_boxes(ctx.table, cond.fixed_faces)
    load_boxes = _fem_probe.face_world_boxes(ctx.table, cond.load_faces)
    zones = _fem_probe.zone_masks(mask, ctx.grid, fixed_world_boxes=fixed_boxes,
                                  load_world_boxes=load_boxes, distance_mm=dist)
    vm = probe["von_mises_mpa"]
    target_allowable = cond.allowable_mpa / float(cond.target_safety_factor)
    m5b_raw = sm.metric_exceedance_integral(vm, mask, spacing_mm=ctx.grid.spacing, allowable_mpa=cond.allowable_mpa,
                                            tolerance_mpa_mm3=m5b_tolerance_mpa_mm3)
    m5b_target = sm.metric_exceedance_integral(vm, mask, spacing_mm=ctx.grid.spacing, allowable_mpa=target_allowable,
                                               tolerance_mpa_mm3=m5b_tolerance_mpa_mm3)
    zone_masks_for_m3 = dict(holding=zones["holding"], joint=zones["joint"], general=zones["general"])
    m3 = sm.metric_zone_percentile(vm, zone_masks_for_m3, percentile=m3_percentile, allowable_mpa=target_allowable)
    m7b = sm.metric_ks_aggregation(vm, mask, rho=ks_rho, allowable_mpa=target_allowable)

    return CandidateScore(
        material_mm3=float(mask.sum() * ctx.grid.element_volume),
        safety_factor_product=float(probe["safety_factor"]),
        m5b_raw_exceedance_mpa_mm3=m5b_raw.value, m5b_raw_passed=bool(m5b_raw.passed),
        m5b_target_exceedance_mpa_mm3=m5b_target.value, m5b_target_passed=bool(m5b_target.passed),
        m3_worst_zone=m3.detail.get("worst_zone"), m3_worst_value=m3.value, m3_passed=m3.passed,
        m7b_ks_mpa=m7b.value, compliance=float(c), max_displacement_mm=float(probe["max_displacement_mm"]),
        element_energy=element_energy, von_mises_mpa=vm, zones=zones,
        mask_hash=acceptance.material_sha256(mask),
    )


# ---------------------------------------------------------------------------
# 幾何不変条件(除去側は既存の binary_search 内部関数をそのまま呼ぶ。追加側は本モジュールが持つ)
# ---------------------------------------------------------------------------
def _removal_geometry_ok(candidate: np.ndarray, conditions) -> tuple[bool, list, np.ndarray]:
    """除去候補の幾何検査(単一成分・保持・アンカー到達)。`binary_search._reduce_volume` と同じ経路
    (`resolve_islands` → `_geometry_rejection`)を直接呼ぶ(再実装しない)。"""
    survivors, island_diag = binary_search.resolve_islands(candidate, conditions)
    rejection = binary_search._geometry_rejection(candidate, survivors, island_diag, conditions)
    if rejection is not None:
        return False, [rejection], candidate
    return True, [], survivors[0]


def core_group_count(mask: np.ndarray, min_width_cells: int) -> int:
    """`m` 立方の侵食(`min_width.core_cells`)後、芯が連結成分いくつに割れているか(「芯の連結」検査)。"""
    if min_width_cells <= 1:
        return 1 if mask.any() else 0
    core = min_width.core_cells(mask, min_width_cells)
    _, count = ndimage.label(core, structure=min_width.core_structure(min_width_cells))
    return int(count)


def _growth_geometry_ok(grown: np.ndarray, base_core_groups: int, conditions, min_width_cells: int
                        ) -> tuple[bool, list]:
    """追加候補の幾何検査。`acceptance.inspect` は「材料が `before` を超えない」ことを前提にしている
    (`if np.any(material & ~original): raise`)ので**呼べない**。代わりに個々のチェック
    (`boundary_check`/`connectivity_check`/`region_check`/`condition_check`)を直接呼び、
    保持・除外・アンカー、単一成分、正則境界、芯の連結、最小幅、閉空洞禁止を課す。
    安全率FEMは走らない(採点は呼び出し側が `score_mask` で行う)。
    """
    cond = conditions
    problems = []
    boundary = acceptance.boundary_check(grown)
    if boundary["status"] != "pass":
        problems.append(dict(check="boundary", **boundary))
    connectivity = acceptance.connectivity_check(grown)
    if connectivity["status"] != "pass":
        problems.append(dict(check="connectivity", **connectivity))
    preserve = acceptance.region_check(grown, cond.grid, cond.preserves, preserve=True,
                                       cache=cond.bbox_cache, overlaps=None)
    if preserve["status"] != "pass":
        problems.append(dict(check="preserve", **preserve))
    exclude = acceptance.region_check(grown, cond.grid, cond.excludes, preserve=False,
                                      cache=cond.bbox_cache, overlaps=None)
    if exclude["status"] != "pass":
        problems.append(dict(check="exclude", **exclude))
    condition_report, _fixed, _load = acceptance.condition_check(grown, cond.table, cond.fixed_faces,
                                                                  cond.load_faces, cond.forces)
    if condition_report["status"] != "pass":
        problems.append(dict(check="conditions", **condition_report))
    exempt = binary_search._min_width_exempt(cond, grown.shape, min_width_cells)
    if not binary_search._mask_satisfies_min_width(grown, min_width_cells, exempt=exempt):
        problems.append(dict(check="min_width", status="fail"))
    voids = acceptance.closed_voids(grown)
    if voids:
        problems.append(dict(check="closed_voids", status="fail", count=len(voids)))
    groups = core_group_count(grown, min_width_cells)
    if groups > max(1, base_core_groups):
        problems.append(dict(check="core_connectivity", status="fail", groups=groups, base_groups=base_core_groups))
    return len(problems) == 0, problems


# ---------------------------------------------------------------------------
# 記録形式(§後続への契約②・完了条件「記録形式が揃う」)
# ---------------------------------------------------------------------------
@dataclass
class StepRecord:
    index: int
    kind: str  # 'reduce' | 'grow' | 'reduce_reject' | 'grow_reject' | 'rollback'
    requested_cells: int
    actual_cells: int
    outcome: str  # 'accepted' | 'rejected'
    reasons: list = field(default_factory=list)
    material_mm3_before: Optional[float] = None
    material_mm3_after: Optional[float] = None
    safety_factor_product: Optional[float] = None
    #: 03bの凍結定義(生の許容応力)。`gate="m5b_raw"` の主実証はこちらがゲートと一致する。
    m5b_raw_exceedance_mpa_mm3: Optional[float] = None
    #: 目標調整版(`allowable/target_safety_factor`)。`gate="m5b_target"` のときこちらが一致する。
    m5b_exceedance_mpa_mm3: Optional[float] = None
    predicted_relative_jump: Optional[float] = None
    actual_relative_jump: Optional[float] = None
    compliance_relative_increase: Optional[float] = None
    displacement_relative_increase: Optional[float] = None
    note: str = ""


@dataclass
class MethodRecord:
    """4方式共通の戻り値。`mechanism_events` は小問題で各機構が発動した証拠の生ログ。"""

    method: str
    status: str  # 'feasible' | 'no_feasible_found'
    stop_reason: str
    evaluations: int
    fem_solves: int
    wall_seconds: float
    seed: Optional[int]
    best_ever_mask: Optional[np.ndarray]
    best_ever_score: Optional[dict]
    working_mask: Optional[np.ndarray]
    initial_material_mm3: Optional[float]
    steps: list
    mechanism_events: list
    config: dict
    checkpoint_path: Optional[str] = None

    def to_json(self, *, mask_dir: Optional[Path] = None, tag: str = "") -> dict:
        out = dict(method=self.method, status=self.status, stop_reason=self.stop_reason,
                   evaluations=self.evaluations, fem_solves=self.fem_solves, wall_seconds=self.wall_seconds,
                   seed=self.seed, initial_material_mm3=self.initial_material_mm3,
                   best_ever_score=self.best_ever_score, step_count=len(self.steps),
                   mechanism_events=self.mechanism_events, config=self.config,
                   checkpoint_path=self.checkpoint_path,
                   steps=[dataclasses.asdict(s) for s in self.steps])
        if mask_dir is not None and self.best_ever_mask is not None:
            mask_dir.mkdir(parents=True, exist_ok=True)
            path = mask_dir / f"{tag or self.method}_best_ever.npz"
            np.savez_compressed(path, mask=self.best_ever_mask)
            out["best_ever_mask_path"] = str(path.relative_to(REPO))
        return out


def _checkpoint(path: Optional[Path], *, working: np.ndarray, best_ever: np.ndarray, best_ever_mm3: float,
               poison: dict, rejected: set, evaluations: int, restart_count: int,
               steps: list, mechanism_events: list, config: dict) -> None:
    """`restart_count` は `order_for` の tie-break 乱数を `(config.seed, restart_count)` から再現する
    のに必要(層をまたぐ契約: `order_for` は永続RNGを持たず、この2値だけから決定的に組み立てる)。"""
    if path is None:
        return
    payload = dict(working_mask=working.tolist(), best_ever_mask=best_ever.tolist(),
                  best_ever_mm3=best_ever_mm3, poison={str(k): v for k, v in poison.items()},
                  rejected=sorted(rejected), evaluations=evaluations, restart_count=restart_count,
                  steps=[dataclasses.asdict(s) for s in steps], mechanism_events=mechanism_events,
                  config=config, saved_at=_common.now_iso())
    _common.write_atomic(path, json.dumps(payload, default=float))


def load_checkpoint(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["working_mask"] = np.asarray(payload["working_mask"], dtype=bool)
    payload["best_ever_mask"] = np.asarray(payload["best_ever_mask"], dtype=bool)
    payload["poison"] = {int(k): tuple(v) for k, v in payload["poison"].items()}
    payload["rejected"] = set(payload["rejected"])
    return payload


# ---------------------------------------------------------------------------
# 方式1・2: 旧20%/5%経路(製品コードをそのまま呼ぶ。再実装しない)
# ---------------------------------------------------------------------------
def run_fixed_step(ctx: SearchContext, *, max_step_ratio: float = binary_search.DEFAULT_MAX_STEP_RATIO,
                   max_evaluations: int = 2000, is_cancelled=None,
                   m5b_tolerance_mpa_mm3: float = 0.0) -> MethodRecord:
    """`method="step20"`(既定 `max_step_ratio=0.2`)/`"step05"`(`max_step_ratio=0.05`)。
    `binary_search.find_best_feasible` を無変更で呼ぶ(受理ゲートはパーセンタイル安全率のまま)。
    採用形状は探索後に一度だけ `score_mask` でM5b/M3/M7bを付ける(**比較の採点だけ**03bの評価器に揃える)。
    """
    method = "step20" if abs(max_step_ratio - 0.2) < 1e-9 else ("step05" if abs(max_step_ratio - 0.05) < 1e-9
                                                                 else f"step{max_step_ratio:g}")
    t0 = time.perf_counter()
    result = binary_search.find_best_feasible(
        ctx.space.mask, ctx.cond, max_evaluations=max_evaluations, max_step_ratio=max_step_ratio,
        min_width_cells=ctx.min_width_cells, is_cancelled=is_cancelled,
    )
    wall = time.perf_counter() - t0
    if result.status != "feasible" or result.best_feasible is None:
        return MethodRecord(method=method, status=result.status, stop_reason=result.termination,
                            evaluations=result.evaluations, fem_solves=result.evaluations, wall_seconds=wall,
                            seed=None, best_ever_mask=None, best_ever_score=None, working_mask=None,
                            initial_material_mm3=None, steps=[], mechanism_events=[],
                            config=dict(max_step_ratio=max_step_ratio, min_width_cells=ctx.min_width_cells))
    final_mask = result.best_feasible.mask
    score = score_mask(final_mask, ctx, m5b_tolerance_mpa_mm3=m5b_tolerance_mpa_mm3)
    return MethodRecord(
        method=method, status="feasible", stop_reason=result.termination, evaluations=result.evaluations,
        fem_solves=result.evaluations + 1, wall_seconds=wall, seed=None,
        best_ever_mask=final_mask, best_ever_score=score.to_json() if score else None,
        working_mask=final_mask, initial_material_mm3=None, steps=[], mechanism_events=[],
        config=dict(max_step_ratio=max_step_ratio, min_width_cells=ctx.min_width_cells,
                   max_evaluations=max_evaluations, m5b_tolerance_mpa_mm3=m5b_tolerance_mpa_mm3,
                   note="製品コード(binary_search.find_best_feasible)を無変更で呼んだ。受理ゲートは製品のパーセンタイル安全率。"),
    )


# ---------------------------------------------------------------------------
# 方式3: 適応歩幅＋復帰
# ---------------------------------------------------------------------------
@dataclass
class AdaptiveConfig:
    """`gate` は3択(理由は `score_mask` の docstring と `docs/topopt_idle_validation/10_SYNTHESIS.md` 付録C):
    `"m5b_raw"`(既定。03bが凍結した定義。生の許容応力に対する超過)/ `"m5b_target"`
    (`allowable_mpa/target_safety_factor` をしきい値にした版。方式1・2と同じ安全余裕で体積を比べる用。
    実測: dilution_smallの全材料形は `"m5b_raw"` では合格〈5.59〉だが `"m5b_target"` では
    不合格〈192.9 MPa·mm³超過〉なので既定にしない)/ `"percentile"`(製品ゲートで同じループを回す対照)。
    **3つは異なる安全の定義を測っており、跨いだ体積の大小だけで優劣を主張しない**。"""

    gate: str = "m5b_raw"
    m5b_tolerance_mpa_mm3: float = 0.0
    step_exponent: float = binary_search.DEFAULT_STEP_EXPONENT
    max_step_ratio: float = 0.2
    min_step_ratio: float = 0.005
    max_evaluations: int = 2000
    stagnation_patience: int = 6  # best_ever が改善しない連続手数
    prediction_error_multiplier: float = 2.5
    #: 実ジャンプ(M3最悪ゾーンの一手あたり相対変化)がこの値以下なら、予測との比が倍率を超えても
    #: `prediction_error` で拒否しない。0.0=比だけで拒否。予測値(分母)が小さいと実ジャンプが
    #: 微小でも比が倍率を超えて拒否が続くため(10_SYNTHESIS 付録D・付録F)。
    prediction_error_floor: float = 0.0
    sudden_jump_relative_drop: float = 0.15  # safety_factor_product の一手あたり許容低下率
    #: compliance C・最大変位の一手あたり許容増加率(sudden_jump の判定にも使う)。
    sudden_compliance_relative_increase: float = 0.5
    sudden_displacement_relative_increase: float = 0.5
    #: True なら、開始形が受理ゲートを満たさないとき、探索せず `stop_reason="start_fails_gate"` で戻る。
    #: False=全候補が `safety_gate` で拒否され続け開始形を返す(hanger 2000N太い幅で実測18.5〜19.1時間。
    #: 10_SYNTHESIS 付録E)。`resume_from` 付きの再開では見ない。
    stop_if_start_fails_gate: bool = False
    finish_window_tolerance: float = 0.02  # 目標 × (1+これ) 以下で仕上げ探索へ
    finish_max_evaluations: int = 200
    finish_patience: int = 8  # 仕上げ局面でbest_ever不変が何連続で続いたら打ち切るか
    stop_policy: str = "adaptive_finish"  # 'adaptive_finish' | 'legacy'(target_tolerance方式との対照)
    #: `stop_policy="legacy"` の許容。研究の対照が使った旧製品の既定 0.01 に固定する。製品の
    #: `binary_search.DEFAULT_TARGET_TOLERANCE` は `None`(見ない)なので、連動させると `1 + None` で落ちる。
    legacy_target_tolerance: float = 0.01
    seed: Optional[int] = 0


def _relative_zone_jump(before: Optional[CandidateScore], after: CandidateScore, *, allowable_mpa: float) -> float:
    """M3最悪ゾーン値の一手あたり相対変化(「部位別応力」信号)。`allowable_mpa` は探索が使うゲートのしきい値。"""
    if before is None or before.m3_worst_value is None or after.m3_worst_value is None:
        return 0.0
    return abs(after.m3_worst_value - before.m3_worst_value) / max(allowable_mpa, abs(before.m3_worst_value))


def _relative_increase(before: Optional[float], after: float) -> float:
    """compliance C・最大変位の一手あたり相対増加率(悪化のみ正値。改善は0扱いで急変検知に混ぜない)。"""
    if before is None or before <= 0:
        return 0.0
    return max(0.0, (after - before) / before)


def run_adaptive(ctx: SearchContext, config: AdaptiveConfig = AdaptiveConfig(), *,
                 checkpoint_path: Optional[Path] = None, is_cancelled: Optional[Callable[[], bool]] = None,
                 resume_from: Optional[dict] = None,
                 extra_protected: Optional[np.ndarray] = None,
                 on_accept: Optional[Callable[[int, np.ndarray, "CandidateScore"], None]] = None) -> MethodRecord:
    """安全率余裕・部位別応力・実削除体積の一次予測・compliance/変位の急変検知で歩幅を決め、
    予測誤差/急変でロールバック(小さい候補を同じ基点から再評価)、`stagnation_patience` 手
    best_ever が改善しなければ最軽量合格形へ復帰し、tie-break の乱数順序を変えて再開する
    (発動した機構は `mechanism_events` に記録)。

    `gate` は `AdaptiveConfig` の docstring 参照。`config.stop_policy="legacy"` は
    `binary_search` の `target_tolerance` 方式と同じ停止条件(対照用)。`resume_from` は
    `load_checkpoint` の戻り値をそのまま渡す。ループ本体は `_run_adaptive_impl`。

    `extra_protected`(既定`None`)は `conditions.keep_cells` と同じ `peel` の `protected` 集合へOR結合する
    セル集合。`gate="percentile"`・`stop_policy="legacy"`・`sudden_*`/`prediction_error_multiplier` を
    実質無効化・`stagnation_patience` を大きくすると、このループは製品の
    `binary_search._reduce_volume` と同じ受理条件・`peel`/`ranked_cells`/`step_ratio` を辿る対照になる。
    `on_accept(evaluations, working_mask, working_score)`(既定`None`)は受理した手ごとに1回、次の候補を
    作る前に呼ぶ(呼び出し側が例外を投げれば探索ごと落ちる)。
    """
    initial = binary_search.find_initial_feasible(ctx.space.mask, ctx.cond, max_evaluations=config.max_evaluations,
                                                   min_width_cells=ctx.min_width_cells, is_cancelled=is_cancelled)
    if initial.status != "feasible" or initial.best_feasible is None:
        return MethodRecord(method="adaptive", status=initial.status, stop_reason=initial.termination,
                            evaluations=initial.evaluations, fem_solves=initial.evaluations,
                            wall_seconds=0.0, seed=config.seed, best_ever_mask=None,
                            best_ever_score=None, working_mask=None, initial_material_mm3=None, steps=[],
                            mechanism_events=[], config=dataclasses.asdict(config))
    return _run_adaptive_impl(ctx, initial, config, checkpoint_path=checkpoint_path, is_cancelled=is_cancelled,
                              resume_from=resume_from, extra_protected=extra_protected, on_accept=on_accept)


# ---------------------------------------------------------------------------
# 方式4: 再配置(追加→削減)
# ---------------------------------------------------------------------------
@dataclass
class ReallocationConfig:
    """`grow_cycles` 回、削減(`run_adaptive`系)→追加(1連結成分)を繰り返す。削減の「停滞」は
    内側の削減フェーズ自身の停止条件(`no_removable_cell`/`within_target_tolerance`/評価予算切れ)が
    担い、追加専用の別カウンタは持たない。
    """

    adaptive: AdaptiveConfig = field(default_factory=AdaptiveConfig)
    grow_cycles: int = 3
    grow_fraction_of_removed: float = 0.5  # 直近の削減量に対する仮追加の割合(「同体積付近」の運用値)
    grow_min_cells: int = 4
    #: 追加1回で足してよい量の上限(=直近までの削除セル数に対する割合)。None=上限なし(最長の連結成分を
    #: まるごと足すため、単純な形状では設計空間の残り全体へ戻る)。値を入れると、上限を超える
    #: 連結成分は候補から外し、候補が尽きたときの局所表面追加も上限セル数に抑え、超過応力の目標換算
    #: しきい値(許容/目標安全率)を超えるセルの近くから足す。
    grow_max_fraction_of_removed: Optional[float] = None
    external_candidate_masks: tuple = ()  # 粗い解像度の探索結果・移植トラス等(呼び出し側が投影済みで渡す)
    external_candidate_labels: tuple = ()  # 上と同じ長さ。出所を記録に残す(「未認定」フラグなど)
    seed: Optional[int] = 0


def _connected_remnants(reference: np.ndarray, current: np.ndarray) -> list[tuple[np.ndarray, int]]:
    """`reference \\ current` を連結成分に分け、現在形へ隣接する成分だけ (mask, 長さ=セル数) で返す。"""
    remnant = reference & ~current
    if not remnant.any():
        return []
    labels, count = ndimage.label(remnant, structure=NEIGHBOURHOOD_STRUCTURE)
    dilated_current = ndimage.binary_dilation(current, structure=NEIGHBOURHOOD_STRUCTURE)
    out = []
    for i in range(1, count + 1):
        comp = labels == i
        if (comp & dilated_current).any():
            out.append((comp, int(comp.sum())))
    out.sort(key=lambda item: -item[1])
    return out


def _surface_growth_pool(current: np.ndarray, design_space: np.ndarray, hot_mask: Optional[np.ndarray]) -> np.ndarray:
    """連結成分の候補が尽きたときの局所表面フォールバック: 設計空間内で現在形に隣接する空セル。
    `hot_mask`(あれば超過セル)に近いものを優先する。"""
    empty_adjacent = ndimage.binary_dilation(current, structure=NEIGHBOURHOOD_STRUCTURE) & design_space & ~current
    idx = np.flatnonzero(empty_adjacent)
    if idx.size == 0 or hot_mask is None or not hot_mask.any():
        return idx
    dist = ndimage.distance_transform_edt(~hot_mask)
    flat_dist = dist.ravel()[idx]
    return idx[np.argsort(flat_dist)]


def run_reallocation(ctx: SearchContext, config: ReallocationConfig = ReallocationConfig(), *,
                     checkpoint_path: Optional[Path] = None, is_cancelled: Optional[Callable[[], bool]] = None
                     ) -> MethodRecord:
    """`run_adaptive` の削減ループを内側の部品として繰り返し呼び、削減フェーズが停止条件に
    達するたびに追加フェーズへ入る。追加候補は `initial_all_material ∪ history ∪ external` から
    現在形へ隣接する連結成分を長い順に提案し、`_growth_geometry_ok` を通ったものだけ採用する。
    **`_growth_geometry_ok` はFEMを呼ばない事前検査なので、幾何で却下された候補は `evaluations` に
    数えない**。`evaluations` は幾何検査に通った候補を `score_mask` で採点する箇所でのみ増える
    (採用側は幾何検査に通った最初の候補をそのまま採用する)。追加を受理した直後に
    `poison`/`rejected` を無効化する(`mechanism_events` に `poison_invalidated`)。
    """
    t0 = time.perf_counter()
    mechanism_events: list[dict] = []
    all_steps: list[StepRecord] = []
    fem_solves = 0
    evaluations = 0

    reduce_record = run_adaptive(ctx, config.adaptive, checkpoint_path=None, is_cancelled=is_cancelled)
    fem_solves += reduce_record.fem_solves
    evaluations += reduce_record.evaluations
    all_steps.extend(reduce_record.steps)
    mechanism_events.extend(reduce_record.mechanism_events)
    if reduce_record.status != "feasible" or reduce_record.best_ever_mask is None:
        reduce_record.method = "reallocation"
        return reduce_record

    working = reduce_record.best_ever_mask
    best_ever = working
    best_score = score_mask(best_ever, ctx, m5b_tolerance_mpa_mm3=config.adaptive.m5b_tolerance_mpa_mm3)
    fem_solves += 1
    best_ever_mm3 = best_score.material_mm3 if best_score else reduce_record.best_ever_score["material_mm3"]
    poison: dict = {}
    rejected: set = set()

    initial_all = binary_search.find_initial_feasible(ctx.space.mask, ctx.cond, max_evaluations=1,
                                                       min_width_cells=ctx.min_width_cells).best_feasible
    history_union = working.copy()
    base_core_groups = core_group_count(working, ctx.min_width_cells)
    design_space = ctx.space.mask

    reference_pools = [("initial_all_material", initial_all.mask)]
    for label, mask in zip(config.external_candidate_labels, config.external_candidate_masks):
        reference_pools.append((label, np.asarray(mask, dtype=bool) & design_space))

    for cycle in range(config.grow_cycles):
        if is_cancelled is not None and is_cancelled():
            break
        removed_cumulative = int(reduce_record.initial_material_mm3 / ctx.grid.element_volume) - int(working.sum()) \
            if reduce_record.initial_material_mm3 else int(working.sum())
        grow_target = max(config.grow_min_cells, int(config.grow_fraction_of_removed * max(removed_cumulative, 1)))
        grow_cap_cells = None
        if config.grow_max_fraction_of_removed is not None:
            grow_cap_cells = max(config.grow_min_cells,
                                 int(config.grow_max_fraction_of_removed * max(removed_cumulative, 1)))
            grow_target = min(grow_target, grow_cap_cells)

        remnants: list[tuple[str, np.ndarray, int]] = []
        for label, ref in reference_pools:
            for comp, length in _connected_remnants(ref, working):
                remnants.append((label, comp, length))
        history_remnants = _connected_remnants(history_union, working)
        remnants.extend(("run_history", comp, length) for comp, length in history_remnants)
        remnants.sort(key=lambda item: -item[2])

        grown = None
        grow_source = None
        added_cells = 0
        for label, comp, length in remnants:
            # 成分は「失った部材の単位」なのでまるごと足す(セル数上限で途中を切ると、ravel順の
            # 部分集合は部材自身と非連結になりやすく、幾何検査でほぼ必ず落ちる)。`grow_target` は
            # reporting用の目安で、部材を分割する上限ではない。
            if grow_cap_cells is not None and length > grow_cap_cells:
                continue
            take = comp
            candidate_grown = working | take
            key = acceptance.material_sha256(candidate_grown)
            if key in rejected:
                continue
            # 幾何検査は事前(FEM無し)なので `evaluations` を数えない。FEM(`score_mask`)を呼ぶ箇所で
            # のみ増やす(このサイクルで採用が決まった候補につき1回)。
            ok, problems = _growth_geometry_ok(candidate_grown, base_core_groups, ctx.cond, ctx.min_width_cells)
            step = StepRecord(index=len(all_steps), kind="grow", requested_cells=int(take.sum()),
                              actual_cells=int(take.sum()), outcome="rejected",
                              material_mm3_before=float(working.sum() * ctx.grid.element_volume),
                              note=f"source={label}")
            if not ok:
                step.reasons = [p["check"] for p in problems]
                all_steps.append(step)
                rejected.add(key)
                continue
            grown, grow_source, added_cells = candidate_grown, label, int(take.sum())
            step.outcome = "accepted"
            step.material_mm3_after = float(grown.sum() * ctx.grid.element_volume)
            all_steps.append(step)
            break

        if grown is None:
            hot_threshold = ctx.cond.allowable_mpa / (float(ctx.cond.target_safety_factor)
                                                      if grow_cap_cells is not None else 1.0)
            hot = best_score.von_mises_mpa > hot_threshold if best_score is not None else None
            pool_idx = _surface_growth_pool(working, design_space, hot)
            if pool_idx.size == 0:
                mechanism_events.append(dict(type="grow_no_candidate", cycle=cycle))
                break
            take_idx = pool_idx[:grow_target]
            take = np.zeros_like(working)
            take.flat[take_idx] = True
            candidate_grown = working | take
            ok, problems = _growth_geometry_ok(candidate_grown, base_core_groups, ctx.cond, ctx.min_width_cells)
            step = StepRecord(index=len(all_steps), kind="grow", requested_cells=int(take.sum()),
                              actual_cells=int(take.sum()), outcome="accepted" if ok else "rejected",
                              material_mm3_before=float(working.sum() * ctx.grid.element_volume),
                              note="source=local_surface")
            if ok:
                grown, grow_source, added_cells = candidate_grown, "local_surface", int(take.sum())
                step.material_mm3_after = float(grown.sum() * ctx.grid.element_volume)
            else:
                step.reasons = [p["check"] for p in problems]
                mechanism_events.append(dict(type="grow_no_candidate", cycle=cycle, reason="surface_geometry_fail"))
            all_steps.append(step)
            if not ok:
                break

        grown_score = score_mask(grown, ctx, m5b_tolerance_mpa_mm3=config.adaptive.m5b_tolerance_mpa_mm3)
        fem_solves += 1
        evaluations += 1
        mechanism_events.append(dict(
            type="grow_accepted", cycle=cycle, source=grow_source, added_cells=added_cells,
            material_mm3_before=float(working.sum() * ctx.grid.element_volume),
            material_mm3_after=grown_score.material_mm3 if grown_score else None,
            temporarily_heavier=bool(grown_score and grown_score.material_mm3 > best_ever_mm3),
        ))
        cleared_poison, cleared_rejected = len(poison), len(rejected)
        poison = {}
        rejected = set()
        mechanism_events.append(dict(type="poison_invalidated", cycle=cycle, cleared_poison=cleared_poison,
                                    cleared_rejected=cleared_rejected, trigger="growth_accept"))

        history_union = history_union | working
        inner_adaptive = dataclasses.replace(config.adaptive, seed=(config.seed or 0) + cycle + 1)
        inner_ctx_start = grown
        sub = _run_adaptive_from_mask(ctx, inner_ctx_start, inner_adaptive)
        fem_solves += sub.fem_solves
        evaluations += sub.evaluations
        all_steps.extend(sub.steps)
        mechanism_events.extend(sub.mechanism_events)
        working = sub.best_ever_mask if sub.best_ever_mask is not None else grown
        working_score = sub.best_ever_score

        if working_score and working_score["material_mm3"] < best_ever_mm3:
            best_ever, best_ever_mm3 = working, working_score["material_mm3"]
            best_score = score_mask(best_ever, ctx, m5b_tolerance_mpa_mm3=config.adaptive.m5b_tolerance_mpa_mm3)
            fem_solves += 1

        _checkpoint(checkpoint_path, working=working, best_ever=best_ever, best_ever_mm3=best_ever_mm3,
                   poison=poison, rejected=rejected, evaluations=evaluations, restart_count=0,
                   steps=all_steps, mechanism_events=mechanism_events, config=dataclasses.asdict(config))

    best_score = score_mask(best_ever, ctx, m5b_tolerance_mpa_mm3=config.adaptive.m5b_tolerance_mpa_mm3)
    fem_solves += 1
    return MethodRecord(
        method="reallocation", status="feasible", stop_reason="grow_cycles_exhausted", evaluations=evaluations,
        fem_solves=fem_solves, wall_seconds=time.perf_counter() - t0, seed=config.seed,
        best_ever_mask=best_ever, best_ever_score=best_score.to_json() if best_score else None,
        working_mask=working, initial_material_mm3=reduce_record.initial_material_mm3, steps=all_steps,
        mechanism_events=mechanism_events, config=dict(grow_cycles=config.grow_cycles,
                                                       grow_fraction_of_removed=config.grow_fraction_of_removed,
                                                       grow_min_cells=config.grow_min_cells,
                                                       grow_max_fraction_of_removed=config.grow_max_fraction_of_removed,
                                                       external_candidate_labels=list(config.external_candidate_labels),
                                                       adaptive=dataclasses.asdict(config.adaptive)),
    )


class _SeedResult:
    """`_run_adaptive_impl` が読むのは `initial.best_feasible.mask` だけ。
    `find_initial_feasible` は**設計空間全体**を1回FEMで解くので、大設計空間(実測: 2.0mm要素で
    64万要素)で既に合格が分かっている形から続きを回すときの無駄を、この軽量な代用オブジェクトで避ける。
    呼び出し側は `start_mask` が既に構造的に妥当(単一成分・水密・アンカー到達)であることを保証すること
    (`_run_adaptive_impl` は再検査しない)。"""

    def __init__(self, mask: np.ndarray):
        self.status = "feasible"
        self.best_feasible = types.SimpleNamespace(mask=np.array(mask, dtype=bool))


def run_adaptive_from_mask(ctx: SearchContext, start_mask: np.ndarray, config: AdaptiveConfig = AdaptiveConfig(), *,
                           checkpoint_path: Optional[Path] = None,
                           is_cancelled: Optional[Callable[[], bool]] = None,
                           extra_protected: Optional[np.ndarray] = None,
                           on_accept: Optional[Callable[[int, np.ndarray, "CandidateScore"], None]] = None
                           ) -> MethodRecord:
    """既存の合格形状から `run_adaptive` の削減ループを続ける公開入口。`start_mask` は `ctx.space.mask` と
    同じ形の bool 配列で、呼び出し側が構造的妥当性(単一成分・水密・保持/除外・アンカー到達・最小幅)を
    保証すること。`extra_protected`/`on_accept` は `run_adaptive` と同じ。"""
    return _run_adaptive_impl(ctx, _SeedResult(start_mask), config, checkpoint_path=checkpoint_path,
                              is_cancelled=is_cancelled, extra_protected=extra_protected, on_accept=on_accept)


def _run_adaptive_from_mask(ctx: SearchContext, start_mask: np.ndarray, config: AdaptiveConfig) -> MethodRecord:
    """再配置の内側ループ専用の薄いラッパー(仮追加形は `_growth_geometry_ok` で構造検査済み)。"""
    return run_adaptive_from_mask(ctx, start_mask, config)


def _run_adaptive_impl(ctx: SearchContext, initial, config: AdaptiveConfig, *,
                       checkpoint_path: Optional[Path], is_cancelled, resume_from: Optional[dict] = None,
                       extra_protected: Optional[np.ndarray] = None,
                       on_accept: Optional[Callable[[int, np.ndarray, "CandidateScore"], None]] = None
                       ) -> MethodRecord:
    """`run_adaptive`/`_run_adaptive_from_mask` が共有するループ本体。開始形状の違いは呼び出し側が
    `initial.best_feasible.mask` を差し替えることで表す。"""
    cond = ctx.cond
    t0 = time.perf_counter()
    fem_solves = 0
    steps: list[StepRecord] = []
    mechanism_events: list[dict] = []
    working = initial.best_feasible.mask
    initial_material_mm3 = float(working.sum() * ctx.grid.element_volume)
    if resume_from is not None:
        working = resume_from["working_mask"]
        best_ever = resume_from["best_ever_mask"]
        best_ever_mm3 = resume_from["best_ever_mm3"]
        poison: dict[int, tuple] = resume_from["poison"]
        rejected: set[str] = resume_from["rejected"]
        evaluations = resume_from["evaluations"]
        restart_count_start = int(resume_from.get("restart_count", 0))
        working_score = score_mask(working, ctx, m5b_tolerance_mpa_mm3=config.m5b_tolerance_mpa_mm3)
        fem_solves += 1
    else:
        poison = {}
        rejected = set()
        working_score = score_mask(working, ctx, m5b_tolerance_mpa_mm3=config.m5b_tolerance_mpa_mm3)
        fem_solves += 1
        best_ever = working
        best_ever_mm3 = working_score.material_mm3 if working_score else initial_material_mm3
        evaluations = 0
        restart_count_start = 0
    energy = working_score.element_energy if working_score else None
    anchor_ids = binary_search._anchor_node_ids(cond)
    exempt = binary_search._min_width_exempt(cond, working.shape, ctx.min_width_cells)
    protected = binary_search._keep_cells(cond, working.shape)
    if extra_protected is not None:
        protected = extra_protected.copy() if protected is None else (protected | extra_protected)
        # `peel` の不変条件(m>1では `protected` ⊆ `exempt`)を満たすため、追加保護セルも
        # 最小幅の規則対象外に含める(既に最小幅確認済みの既存構造の一部を想定)。
        if ctx.min_width_cells > 1:
            exempt = extra_protected.copy() if exempt is None else (exempt | extra_protected)
    multiplier = 1.0
    stagnation_counter = 0
    calibrated_k: Optional[float] = None
    in_finish_phase = False
    finish_counter = 0  # 仕上げ局面でbest_ever不変が連続した回数(finish_patienceと比較)
    finish_total_evals = 0  # 仕上げ局面に入って以降の評価回数の総数(finish_max_evaluationsと比較)
    target = float(cond.target_safety_factor)
    restart_count = restart_count_start

    def _gate_pass(score: CandidateScore) -> bool:
        if config.gate == "m5b_raw":
            return score.m5b_raw_passed
        if config.gate == "m5b_target":
            return score.m5b_target_passed
        if config.gate == "percentile":
            return score.safety_factor_product >= target
        raise ValueError(f"未知の gate: {config.gate!r}(m5b_raw/m5b_target/percentile のいずれか)")

    def order_for(mask: np.ndarray, en, seed_bump: int) -> np.ndarray:
        order = binary_search.ranked_cells(mask, en)
        if seed_bump:
            local = np.random.default_rng((config.seed or 0) + seed_bump)
            window = max(8, len(order) // 50)
            for start in range(0, len(order), window):
                chunk = order[start:start + window]
                local.shuffle(chunk)
                order[start:start + window] = chunk
        return order[~np.isin(order, list(poison))] if poison else order

    if (config.stop_if_start_fails_gate and resume_from is None and working_score is not None
            and not _gate_pass(working_score)):
        return MethodRecord(
            method="adaptive", status="no_feasible_found", stop_reason="start_fails_gate", evaluations=0,
            fem_solves=fem_solves, wall_seconds=time.perf_counter() - t0, seed=config.seed, best_ever_mask=None,
            best_ever_score=working_score.to_json(), working_mask=None, initial_material_mm3=initial_material_mm3,
            steps=[], mechanism_events=[dict(type="start_fails_gate", gate=config.gate)],
            config=dataclasses.asdict(config))

    order = order_for(working, energy, 0)
    stop_reason = "loop_exit"

    while True:
        if is_cancelled is not None and is_cancelled():
            stop_reason = "cancelled"
            break
        if evaluations >= config.max_evaluations:
            stop_reason = "evaluation_limit"
            break
        gate_ok_now = working_score is not None and _gate_pass(working_score)
        if gate_ok_now and not in_finish_phase:
            in_finish_phase = True
            mechanism_events.append(dict(type="finish_phase_entered", evaluation=evaluations,
                                        material_mm3=working_score.material_mm3))
        if config.stop_policy == "legacy":
            if working_score is not None and working_score.safety_factor_product <= target * (1 + config.legacy_target_tolerance):
                stop_reason = "within_target_tolerance"
                break
        elif in_finish_phase and finish_total_evals >= config.finish_max_evaluations:
            stop_reason = "finish_budget_exhausted"
            break
        elif in_finish_phase and finish_counter >= config.finish_patience:
            # `finish_max_evaluations` とは別に、best_ever を更新しない手が `finish_patience` 連続したら停滞とみなして打ち切る。
            stop_reason = "finish_stagnation"
            break

        gap_ratio = binary_search.step_ratio(max((working_score.safety_factor_product if working_score else 1.0), 1e-6),
                                             target, exponent=config.step_exponent, max_ratio=config.max_step_ratio)
        # `min_step_ratio` は基準歩幅(`gap_ratio`)だけへ床を敷く。後退倍率 `multiplier` には
        # 敷かない — 敷くと、同じ複数セル候補が却下され続けたとき `requested` が床で頭打ちのまま
        # `peel` が毎回同じ候補を返して `rejected` に当たり続ける無限ループになる(実測)。
        # `multiplier` は無制限に半減して初めて `requested→1` に収束し、`_reduce_volume` と同じ停止保証になる。
        ratio = max(config.min_step_ratio, gap_ratio) * multiplier
        requested = max(1, int(ratio * int(working.sum())))
        candidate, peeled = binary_search.peel(working, order, requested, anchor_ids,
                                               min_width_cells=ctx.min_width_cells, exempt=exempt, protected=protected)
        if peeled.picked == 0:
            stop_reason = "no_removable_cell" if (in_finish_phase or config.stop_policy == "legacy") \
                else "no_removable_cell_before_window"
            break
        candidate.setflags(write=False)
        key = acceptance.material_sha256(candidate)
        if key in rejected:
            if peeled.picked > 1:
                multiplier /= 2.0
            else:
                order = order[~np.isin(order, peeled.picked_cells)]
            continue

        removed_actual = int(working.sum() - candidate.sum())
        geometry_ok, reasons, survivor = _removal_geometry_ok(candidate, cond)
        step = StepRecord(index=len(steps), kind="reduce", requested_cells=requested, actual_cells=removed_actual,
                          outcome="rejected", material_mm3_before=working_score.material_mm3 if working_score else None)
        if not geometry_ok:
            step.reasons = ["geometry"]
            steps.append(step)
            rejected.add(key)
            if peeled.picked > 1:
                multiplier /= 2.0
            else:
                order = order[~np.isin(order, peeled.picked_cells)]
            continue

        evaluations += 1
        cand_score = score_mask(survivor, ctx, m5b_tolerance_mpa_mm3=config.m5b_tolerance_mpa_mm3)
        fem_solves += 1
        if cand_score is None:
            step.reasons = ["evaluation_error"]
            steps.append(step)
            rejected.add(key)
            if peeled.picked > 1:
                multiplier /= 2.0
            else:
                order = order[~np.isin(order, peeled.picked_cells)]
            continue

        removed_energy = float(energy[working & ~survivor].sum()) if energy is not None else 0.0
        total_energy = float(energy[working].sum()) if energy is not None and energy[working].size else 1.0
        predicted_jump = (calibrated_k or 1.0) * (removed_energy / max(total_energy, 1e-12))
        actual_jump = _relative_zone_jump(working_score, cand_score, allowable_mpa=cond.allowable_mpa)
        step.predicted_relative_jump = predicted_jump
        step.actual_relative_jump = actual_jump
        sudden_drop = ((working_score.safety_factor_product - cand_score.safety_factor_product)
                      / max(working_score.safety_factor_product, 1e-9)) if working_score else 0.0
        # compliance C・最大変位も急変検知に使う。安全率だけでは見えない構造性能の急変
        # (安全率に余裕があるのに剛性が急落する候補)を捕まえるため。
        compliance_jump = _relative_increase(working_score.compliance if working_score else None, cand_score.compliance)
        displacement_jump = _relative_increase(working_score.max_displacement_mm if working_score else None,
                                               cand_score.max_displacement_mm)
        step.compliance_relative_increase = compliance_jump
        step.displacement_relative_increase = displacement_jump
        prediction_error = (calibrated_k is not None and predicted_jump > 1e-12
                            and actual_jump > max(predicted_jump * config.prediction_error_multiplier,
                                                  config.prediction_error_floor))
        sudden_jump = (sudden_drop > config.sudden_jump_relative_drop
                      or compliance_jump > config.sudden_compliance_relative_increase
                      or displacement_jump > config.sudden_displacement_relative_increase)

        gate_ok = _gate_pass(cand_score)
        step.safety_factor_product = cand_score.safety_factor_product
        step.m5b_raw_exceedance_mpa_mm3 = cand_score.m5b_raw_exceedance_mpa_mm3
        step.m5b_exceedance_mpa_mm3 = cand_score.m5b_target_exceedance_mpa_mm3
        step.material_mm3_after = cand_score.material_mm3

        if gate_ok and not prediction_error and not sudden_jump:
            step.outcome = "accepted"
            steps.append(step)
            if calibrated_k is None and removed_energy > 0 and actual_jump > 0:
                calibrated_k = actual_jump / (removed_energy / max(total_energy, 1e-12))
            working, working_score = survivor, cand_score
            if on_accept is not None:
                on_accept(evaluations, working, working_score)
            energy = cand_score.element_energy
            multiplier = min(1.0, 2.0 * multiplier)
            best_ever_improved = working_score.material_mm3 < best_ever_mm3
            if best_ever_improved:
                best_ever, best_ever_mm3 = working, working_score.material_mm3
                stagnation_counter = 0
            else:
                stagnation_counter += 1
            order = order_for(working, energy, 0)
            _checkpoint(checkpoint_path, working=working, best_ever=best_ever, best_ever_mm3=best_ever_mm3,
                       poison=poison, rejected=rejected, evaluations=evaluations, restart_count=restart_count,
                       steps=steps, mechanism_events=mechanism_events, config=dataclasses.asdict(config))
            if in_finish_phase:
                # 受理でも best_ever が改善しなければ finish_counter は増やす
                # (「best_ever不変の連続回数」の定義を stagnation_counter と揃える)。
                finish_counter = 0 if best_ever_improved else finish_counter + 1
                finish_total_evals += 1
        else:
            step.reasons = (["prediction_error"] if prediction_error else []) + \
                           (["sudden_jump"] if sudden_jump else []) + (["safety_gate"] if not gate_ok else [])
            steps.append(step)
            rejected.add(key)
            if prediction_error or sudden_jump:
                sudden_jump_signal = ("safety_factor" if sudden_drop > config.sudden_jump_relative_drop else
                                      "compliance" if compliance_jump > config.sudden_compliance_relative_increase else
                                      "displacement" if displacement_jump > config.sudden_displacement_relative_increase
                                      else None)
                mechanism_events.append(dict(type="rollback", evaluation=evaluations,
                                            reason="prediction_error" if prediction_error else "sudden_jump",
                                            sudden_jump_signal=sudden_jump_signal,
                                            predicted_relative_jump=predicted_jump, actual_relative_jump=actual_jump,
                                            sudden_drop=sudden_drop, compliance_relative_increase=compliance_jump,
                                            displacement_relative_increase=displacement_jump))
            stagnation_counter += 1
            if in_finish_phase:
                finish_counter += 1
                finish_total_evals += 1
            if not gate_ok and config.gate == "percentile" and peeled.picked == 1:
                shortfall = target - cand_score.safety_factor_product
                poison[int(peeled.picked_cells[0])] = ((working_score.safety_factor_product if working_score else 0.0),
                                                       shortfall if shortfall > 0 else None)
            if peeled.picked > 1:
                multiplier /= 2.0
            else:
                order = order[~np.isin(order, peeled.picked_cells)]

        if stagnation_counter >= config.stagnation_patience:
            restart_count += 1
            mechanism_events.append(dict(type="revert_restart", evaluation=evaluations,
                                        from_material_mm3=(working_score.material_mm3 if working_score else None),
                                        to_material_mm3=best_ever_mm3, restart_count=restart_count))
            working = best_ever
            working_score = score_mask(working, ctx, m5b_tolerance_mpa_mm3=config.m5b_tolerance_mpa_mm3)
            fem_solves += 1
            energy = working_score.element_energy if working_score else energy
            order = order_for(working, energy, restart_count)
            multiplier = max(config.min_step_ratio, multiplier / 2.0) if restart_count % 2 else min(1.0, multiplier * 1.5)
            stagnation_counter = 0

    best_score = score_mask(best_ever, ctx, m5b_tolerance_mpa_mm3=config.m5b_tolerance_mpa_mm3)
    fem_solves += 1
    return MethodRecord(
        method="adaptive", status="feasible", stop_reason=stop_reason, evaluations=evaluations,
        fem_solves=fem_solves, wall_seconds=time.perf_counter() - t0, seed=config.seed,
        best_ever_mask=best_ever, best_ever_score=best_score.to_json() if best_score else None,
        working_mask=working, initial_material_mm3=initial_material_mm3, steps=steps,
        mechanism_events=mechanism_events, config=dataclasses.asdict(config),
    )
