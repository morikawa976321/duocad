"""タスク5(合成題材の比較・機構別検証)が共有する条件定義とヘルパー。

**スコープ**: タスクファイルの「小型題材への変更」が全合成題材(S-T/S-B/S-TB/S-L/S-MS)要求より
優先する(詳細は 10_SYNTHESIS 付録D)。主題材は task0 が認定した `topopt_idle_dilution_small.json`(原因1)・
`topopt_idle_path_small.json`(原因2、2.0mm/3000N/6.0mm・percentileシナリオに限り認定)。
S-T等は新規構築しない。

製品コード(`duocad.topopt.*`)は変更しない。実験コードは `search_methods.py`(タスク4の実装)を
再利用し、ここでは条件表・アブレーション用configの組み立て・原点シフト等のprocess内一時パッチだけを持つ。
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import numpy as np  # noqa: E402

from duocad.topopt import binary_search, design_space as ds  # noqa: E402

import search_methods as sm  # noqa: E402

MODELS_DIR = REPO / "models"
DILUTION_MODEL = MODELS_DIR / "topopt_idle_dilution_small.json"
PATH_MODEL = MODELS_DIR / "topopt_idle_path_small.json"
OUT = REPO / "output" / "idle_validation_20260928"
MASK_DIR = OUT / "05_masks"
LOCK = OUT / "_locks" / "fem_single_run.lock"

# ---------------------------------------------------------------------------
# baseline条件(04の主実証・04aの認定条件と一致させる——引用元は各スクリプトのdocstringに明記)
# ---------------------------------------------------------------------------
DILUTION_BASELINE = dict(model=DILUTION_MODEL, load_n=300.0, element_size_mm=2.0, min_width_mm=4.0,
                         tag="dilution_2.0mm_f300", cause="dilution", role="tuning")
# **04aが調整に使った条件として登録**。task5でこの条件を「未使用検証例」として扱わない。
# path側の未使用検証は2700N/3300N(04aが頑健性のみ確認、方式比較は未実施)。
PATH_BASELINE = dict(model=PATH_MODEL, load_n=3000.0, element_size_mm=2.0, min_width_mm=6.0,
                     tag="path_2.0mm_f3000", cause="path", role="tuning",
                     gate_scenario="percentile",  # 04a認定はpercentileシナリオに限る(m5b_targetは両形とも不合格・未決着)
                     )

# 04aの2.0mm認定作業で使った質量スケール参照(01_stage1_shapes等とは別系統で再利用しない)。
# 04aが見つけた優越マスクは warm-start 候補としてのみ再利用する。
PATH_QUALIFIED_MASK_NPZ = OUT / "04a_masks" / "qualified_path_2.0mm.npz"


def load_mask_npz(path: Path) -> np.ndarray:
    with np.load(path) as z:
        return np.asarray(z["mask"], dtype=bool)


# ---------------------------------------------------------------------------
# アブレーション用 AdaptiveConfig(検査5.4「適応歩幅のみ/復帰の有無/再配置の有無」)
# ---------------------------------------------------------------------------
def step_only_config(*, seed: int, max_evaluations: int, gate: str = "m5b_raw",
                     stop_policy: str = "adaptive_finish") -> sm.AdaptiveConfig:
    """rollback/revert_restart が実質発動しない閾値(=歩幅だけで削る「適応歩幅のみ」対照)。
    完全に0にはせず、安全率が0へ近づく異常時だけは rollback が効くよう極端だが有限の値を残す。"""
    return sm.AdaptiveConfig(gate=gate, stop_policy=stop_policy, seed=seed, max_evaluations=max_evaluations,
                             stagnation_patience=10_000_000, sudden_jump_relative_drop=0.999,
                             sudden_compliance_relative_increase=1e9, sudden_displacement_relative_increase=1e9,
                             prediction_error_multiplier=1e9)


def revert_config(*, seed: int, max_evaluations: int, gate: str = "m5b_raw",
                  stop_policy: str = "adaptive_finish", stagnation_patience: int = 6) -> sm.AdaptiveConfig:
    """既定値(「適応歩幅+復帰」)。既定の `AdaptiveConfig()` と同じ値を明示する。"""
    return sm.AdaptiveConfig(gate=gate, stop_policy=stop_policy, seed=seed, max_evaluations=max_evaluations,
                             stagnation_patience=stagnation_patience)


def reallocation_config(*, seed: int, max_evaluations: int, gate: str = "m5b_raw",
                        stop_policy: str = "adaptive_finish", grow_cycles: int = 3,
                        stagnation_patience: int = 6,
                        external_candidate_masks: tuple = (), external_candidate_labels: tuple = ()
                        ) -> sm.ReallocationConfig:
    return sm.ReallocationConfig(
        adaptive=revert_config(seed=seed, max_evaluations=max_evaluations, gate=gate, stop_policy=stop_policy,
                               stagnation_patience=stagnation_patience),
        grow_cycles=grow_cycles, seed=seed,
        external_candidate_masks=external_candidate_masks, external_candidate_labels=external_candidate_labels,
    )


# ---------------------------------------------------------------------------
# 原点シフト(10_SYNTHESIS 付録A 6-1節)。`metric_eval._shifted_grid_for_frame` と同じ手口を
# `search_methods.build_context` にも適用する(metric_eval側は既存マスクの再投影専用)。
# ---------------------------------------------------------------------------
def _shifted_grid_for_frame(shift_mm: tuple):
    real = ds.grid_for_frame

    def wrapped(box_lo, box_hi, **kwargs):
        grid = real(box_lo, box_hi, **kwargs)
        new_origin = tuple(float(o) + float(s) for o, s in zip(grid.origin, shift_mm))
        return dataclasses.replace(grid, origin=new_origin)

    return wrapped


def build_context_with_origin_shift(model_path: Path, *, load_n: float, element_size_mm: float,
                                    min_width_mm: float, shift_mm) -> sm.SearchContext:
    """`search_methods.build_context` と同じ手順だが、格子原点だけを `shift_mm`(スカラー or 3要素)
    ずらす。達成シフトがちょうど0.5セルにならない場合があるので、呼び出し側は `ctx.grid.origin` から
    `achieved_origin_mm` を確認して記録すること。"""
    shift = shift_mm if isinstance(shift_mm, tuple) else (float(shift_mm),) * 3
    if not any(shift):
        return sm.build_context(model_path, load_n=load_n, element_size_mm=element_size_mm, min_width_mm=min_width_mm)
    with patch.object(ds, "grid_for_frame", _shifted_grid_for_frame(shift)):
        return sm.build_context(model_path, load_n=load_n, element_size_mm=element_size_mm, min_width_mm=min_width_mm)


# ---------------------------------------------------------------------------
# フィルタ幅mm固定(10_SYNTHESIS 付録A 6-3節)。`binary_search.ENERGY_FILTER_SIZE` はセル数固定(既定3)。
# `ranked_cells` がモジュール属性を呼び出し時に参照する(実測)ので `patch.object` で一時的に差し替えられる。
# ---------------------------------------------------------------------------
def filter_size_cells_for_mm(target_mm: float, spacing_mm: float) -> int:
    """物理mm幅を最も近い整数セル数へ丸める(1セル未満にはしない)。丸め差は呼び出し側が記録する。"""
    return max(1, round(target_mm / spacing_mm))


class patched_filter_size:
    """`with patched_filter_size(n):` で `ENERGY_FILTER_SIZE` を一時的に差し替える。"""

    def __init__(self, cells: int):
        self._patcher = patch.object(binary_search, "ENERGY_FILTER_SIZE", int(cells))

    def __enter__(self):
        self._patcher.start()
        return self

    def __exit__(self, *exc):
        self._patcher.stop()
        return False


# ---------------------------------------------------------------------------
# StepRecord系列から best-so-far(累積合格最小体積)曲線を作る(§D「同時間/同FEM回数の最良合格体積曲線」)。
# ---------------------------------------------------------------------------
def best_so_far_curve(record: sm.MethodRecord, *, gate: str = "m5b_raw") -> list[dict]:
    """`steps`(StepRecordのlist)を評価回数順に読み、その時点までの最良合格体積を追う。
    受理された手(outcome=='accepted')だけを辿り、ゲート条件を満たす手のうち材料が単調非増加に
    なる点を「best-so-far」として記録する。
    """
    curve = []
    best = None
    idx = 0
    for step in record.steps:
        if step.outcome != "accepted" or step.kind not in ("reduce", "grow"):
            continue
        idx += 1
        exceedance = step.m5b_raw_exceedance_mpa_mm3 if gate == "m5b_raw" else step.m5b_exceedance_mpa_mm3
        passed = (exceedance is not None and exceedance <= 1e-9)
        material = step.material_mm3_after
        if passed and material is not None and (best is None or material < best):
            best = material
            curve.append(dict(evaluation_index=idx, material_mm3=best, step_index=step.index, kind=step.kind))
    return curve


def record_summary(record: sm.MethodRecord) -> dict:
    """`MethodRecord.to_json` の軽量版(マスクを保存しない、比較表に出す最小フィールドのみ)。"""
    score = record.best_ever_score or {}
    return dict(method=record.method, status=record.status, stop_reason=record.stop_reason,
               evaluations=record.evaluations, fem_solves=record.fem_solves, wall_seconds=record.wall_seconds,
               seed=record.seed, material_mm3=score.get("material_mm3"),
               safety_factor_product=score.get("safety_factor_product"),
               m5b_raw_exceedance_mpa_mm3=score.get("m5b_raw_exceedance_mpa_mm3"),
               m5b_raw_passed=score.get("m5b_raw_passed"),
               m5b_target_exceedance_mpa_mm3=score.get("m5b_target_exceedance_mpa_mm3"),
               m5b_target_passed=score.get("m5b_target_passed"),
               mask_hash=score.get("mask_hash"), mechanism_event_count=len(record.mechanism_events),
               step_count=len(record.steps))
