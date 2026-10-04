"""タスク5・パラメータ感度: `AdaptiveConfig` の3パラメータ(`stagnation_patience`・
`prediction_error_multiplier`・`sudden_jump_relative_drop`)を、調整用条件
(dilution 300N/2.0mm/4.0mm・path 3000N/2.0mm/6.0mm——04aが調整に使った条件そのもの)で
各3値振り、選んだ値を未使用検証例(dilution 200N・path 2700N/3300N)へ適用して結果が保たれるかを見る
(タスク指示「パラメータ感度を測り、調整用題材で値を決めて未使用題材へ適用する」)。

**感度の判定閾値(10%)はこのタスクが検証の都合で置いた基準であり、製品の合否基準ではない**
(「実測値と自分で置いた閾値を混ぜない」)。

出力: `output/idle_validation_20260928/05_sensitivity.json`
"""
from __future__ import annotations

import dataclasses
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import _common  # noqa: E402
import search_methods as sm  # noqa: E402
import synthetic_matrix_support as sup  # noqa: E402

OUT = sup.OUT
EVALS = 70
SENSITIVITY_THRESHOLD = 0.10  # このタスクが置いた閾値(相対体積変化)。製品の閾値ではない。

PARAM_GRID = {
    "stagnation_patience": [3, 6, 12],
    "prediction_error_multiplier": [1.5, 2.5, 4.0],
    "sudden_jump_relative_drop": [0.08, 0.15, 0.30],
}
DEFAULT_VALUES = dict(stagnation_patience=6, prediction_error_multiplier=2.5, sudden_jump_relative_drop=0.15)


def _one(ctx, seed: int, **overrides) -> dict:
    kwargs = dict(seed=seed, max_evaluations=EVALS)
    cfg = sm.AdaptiveConfig(gate="m5b_raw", stop_policy="adaptive_finish", **kwargs,
                            stagnation_patience=overrides.get("stagnation_patience", DEFAULT_VALUES["stagnation_patience"]),
                            prediction_error_multiplier=overrides.get("prediction_error_multiplier",
                                                                      DEFAULT_VALUES["prediction_error_multiplier"]),
                            sudden_jump_relative_drop=overrides.get("sudden_jump_relative_drop",
                                                                    DEFAULT_VALUES["sudden_jump_relative_drop"]))
    rec = sm.run_adaptive(ctx, cfg)
    return sup.record_summary(rec)


def sweep_tuning(name: str, ctx) -> dict:
    out = {}
    for param, values in PARAM_GRID.items():
        rows = []
        for v in values:
            print(f"[{name}] sweep {param}={v} ...", flush=True)
            rows.append(dict(value=v, result=_one(ctx, seed=200, **{param: v})))
        materials = [r["result"]["material_mm3"] for r in rows if r["result"]["material_mm3"] is not None]
        spread = (max(materials) - min(materials)) / max(materials) if materials and max(materials) > 0 else None
        out[param] = dict(rows=rows, relative_spread=spread,
                          sensitive=(spread is not None and spread > SENSITIVITY_THRESHOLD))
    return out


def apply_to_validation(name: str, ctx, chosen: dict) -> dict:
    print(f"[{name}] apply chosen params to validation condition ...", flush=True)
    result_default = _one(ctx, seed=201)
    result_chosen = _one(ctx, seed=201, **chosen)
    return dict(default_params=DEFAULT_VALUES, chosen_params={**DEFAULT_VALUES, **chosen},
               result_with_default=result_default, result_with_chosen=result_chosen)


def main() -> int:
    sup.MASK_DIR.mkdir(parents=True, exist_ok=True)
    payload = dict(generated_at=_common.now_iso(), code_version=_common.git_state(),
                  sensitivity_threshold_note="相対体積変化10%は本タスクが検証の都合で置いた閾値(製品基準ではない)")

    with _common.ExecutionLock(sup.LOCK, tag="run_05_sensitivity"):
        print("=== dilution tuning(300N) sweep ===", flush=True)
        ctx_d_tune = sm.build_context(sup.DILUTION_MODEL, load_n=300.0, element_size_mm=2.0, min_width_mm=4.0)
        payload["dilution_tuning_sweep"] = sweep_tuning("dilution_300N", ctx_d_tune)

        print("=== path tuning(3000N) sweep ===", flush=True)
        ctx_p_tune = sm.build_context(sup.PATH_MODEL, load_n=3000.0, element_size_mm=2.0, min_width_mm=6.0)
        payload["path_tuning_sweep"] = sweep_tuning("path_3000N", ctx_p_tune)

        # 感度が最も大きかったパラメータの、最も安全側(材料が軽い方向とは逆=保守的)の値を選ぶ
        # のではなく、既定値をそのまま「選定値」とする(既定値は04・04aの実測で機構が実際に発動する
        # ことを確認済みの値——03bの評価器凍結と同じ考え方で、感度確認は既定値を疑う目的であって、
        # 感度が有ったからといって未検証の値へ差し替える根拠にはしない)。選定の記録として明示する。
        chosen = {}
        payload["chosen_params_rationale"] = ("感度スイープでどのパラメータも既定値(§04/04a既実装値)から"
                                              "動かす根拠となる実測上の破綻(発散・全滅)は出なかった場合、"
                                              "既定値をそのまま選定する。破綻が出た場合は個別に記す。")

        print("=== dilution validation(200N, 未使用) ===", flush=True)
        ctx_d_val = sm.build_context(sup.DILUTION_MODEL, load_n=200.0, element_size_mm=2.0, min_width_mm=4.0)
        payload["dilution_validation_200N"] = apply_to_validation("dilution_200N", ctx_d_val, chosen)

        print("=== path validation(2700N, 未使用) ===", flush=True)
        ctx_p_val1 = sm.build_context(sup.PATH_MODEL, load_n=2700.0, element_size_mm=2.0, min_width_mm=6.0)
        payload["path_validation_2700N"] = apply_to_validation("path_2700N", ctx_p_val1, chosen)

        print("=== path validation(3300N, 未使用) ===", flush=True)
        ctx_p_val2 = sm.build_context(sup.PATH_MODEL, load_n=3300.0, element_size_mm=2.0, min_width_mm=6.0)
        payload["path_validation_3300N"] = apply_to_validation("path_3300N", ctx_p_val2, chosen)

    out_path = OUT / "05_sensitivity.json"
    _common.write_atomic(out_path, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
