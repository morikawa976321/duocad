"""`counterexamples_field_only.py` の (a)(e) を hanger_base@200N(H1) の実形状場へ拡張する。

出力: `output/idle_validation_20260928/03b_counterexamples_hanger.json`
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
from counterexamples_field_only import (  # noqa: E402
    counterexample_a_far_low_stress,
    counterexample_e_hotspot_transfer,
)

OUT = REPO / "output" / "idle_validation_20260928"


def main() -> int:
    shape_cfg = rs._resolve_shape("hanger_base", None)
    base = me.solve_shape(shape_cfg, 200.0, 1)
    assert base["status"] == "ok"
    vm3d = base["von_mises_mpa"]
    mask3d = base["mask"]
    base_values = vm3d[mask3d]
    allowable = base["allowable_mpa"]
    spacing = base["element_mm"]

    payload = dict(
        generated_at=_common.now_iso(),
        code_version=_common.git_state(),
        base_case=dict(
            shape="hanger_base",
            load_n=200.0,
            level=1,
            material_elements=int(base_values.size),
            allowable_mpa=allowable,
            spacing_mm=spacing,
            zone_counts=base["zones"]["counts"],
        ),
        a_far_low_stress_cells=counterexample_a_far_low_stress(base_values, allowable, spacing),
        e_hotspot_transfer=counterexample_e_hotspot_transfer(
            base_values,
            base["zones"]["holding"][mask3d],
            base["zones"]["joint"][mask3d],
            base["zones"]["general"][mask3d],
            allowable,
            spacing,
        ),
    )
    out_path = OUT / "03b_counterexamples_hanger.json"
    _common.write_atomic(out_path, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
