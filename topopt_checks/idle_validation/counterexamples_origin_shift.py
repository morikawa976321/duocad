"""必須反例(g): 格子原点移動。`design_space.derive` は原点を直接のパラメータとして持たないため
(task01契約②)、`metric_eval.solve_shape(..., origin_shift_mm=...)` がプロセス内だけで
`design_space.grid_for_frame` を差し替えて原点だけをずらす(01_PROTOCOL.md 6-1節の実行版)。

対象: `dilution_r-1`@300N(安全率に余裕あり)、`path_2.5mm`@6000N(安全率2.42、余裕が薄い)。
シフト量は±0.5セル幅(半セル、格子依存性の標準的な試験量)。

出力: `output/idle_validation_20260928/counterexamples_origin_shift.json`
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import _common  # noqa: E402
import metric_eval as me  # noqa: E402
import refine_shape as rs  # noqa: E402
import stress_metrics as sm  # noqa: E402

OUT = REPO / "output" / "idle_validation_20260928"


def _score(result: dict) -> dict:
    vm, mask, zones = result["von_mises_mpa"], result["mask"], result["zones"]
    allowable, spacing = result["allowable_mpa"], result["element_mm"]
    zone_dict = {"holding": zones["holding"], "joint": zones["joint"], "general": zones["general"]}
    return dict(
        full_population_percentile=sm.metric_full_population_percentile(vm, mask, allowable_mpa=allowable).__dict__,
        zone_percentile=sm.metric_zone_percentile(vm, zone_dict, allowable_mpa=allowable).__dict__,
        exceedance_integral=sm.metric_exceedance_integral(vm, mask, spacing_mm=spacing, allowable_mpa=allowable).__dict__,
        p_norm_unnormalized=sm.metric_p_norm_unnormalized(vm, mask, p=8.0, allowable_mpa=allowable).__dict__,
    )


def _sweep(shape: str, hanger_condition, load_n: float, spacing_mm: float) -> dict:
    shape_cfg = rs._resolve_shape(shape, hanger_condition)
    shifts = {"0": 0.0, "+0.5cell": 0.5 * spacing_mm, "-0.5cell": -0.5 * spacing_mm,
              "+1.0cell": 1.0 * spacing_mm}
    out = {}
    for tag, shift in shifts.items():
        result = me.solve_shape(shape_cfg, load_n, 1, origin_shift_mm=shift)
        if result["status"] != "ok":
            out[tag] = dict(status=result["status"], error=result.get("error"), shift_mm=shift)
            continue
        out[tag] = dict(
            status="ok", shift_mm=shift, achieved_origin_mm=result["achieved_origin_mm"],
            material_elements=result["material_elements"], safety_factor_product=result["safety_factor"],
            metrics=_score(result),
        )
    return out


def main() -> int:
    payload = dict(
        generated_at=_common.now_iso(),
        code_version=_common.git_state(),
        dilution_r_minus1_300N=_sweep("dilution_r-1", None, 300.0, 2.0),
        path_2_5mm_6000N=_sweep("path_2.5mm", None, 6000.0, 2.4705882352941178),
    )
    out_path = OUT / "counterexamples_origin_shift.json"
    _common.write_atomic(out_path, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
