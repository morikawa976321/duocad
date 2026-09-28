"""hanger 4形状を同一条件 F0200_h2.0_w5 で採点し、最小幅(5セル)合格も記録する。

出力: `output/idle_validation_20260928/03b_hanger_common_condition.json`
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import numpy as np  # noqa: E402
from duocad.topopt import binary_search  # noqa: E402

import _common  # noqa: E402
import metric_eval as me  # noqa: E402
import refine_shape as rs  # noqa: E402
import run_metric_comparison as rmc  # noqa: E402

OUT = REPO / "output" / "idle_validation_20260928"

HANGER_CONDITION = "F0200_h2.0_w5"
LOAD_N = 200.0
LEVEL = 1
MIN_WIDTH_CELLS = 5
CASE_SHAPES = ("hanger_base", "hanger_r2", "hanger_step05", "hanger_transplant")


def _shape_cfg(shape: str) -> dict:
    if shape != "hanger_transplant":
        return rs._resolve_shape(shape, HANGER_CONDITION)
    kind, _default = rs.HANGER_DEFAULTS[shape]
    entries = json.loads(rs.STAGE1.read_text(encoding="utf-8"))["entries"]
    entry = next(
        e for e in entries
        if e.get("shape_kind") == kind and e.get("dst_condition") == HANGER_CONDITION
    )
    npz_path = REPO / entry["files"]["npz"]
    data = np.load(npz_path)
    mask = data["mask"]
    keep = data["keep"] if "keep" in data.files else None
    origin = data["origin"] if "origin" in data.files else entry["grid"]["origin_mm"]
    spacing = float(data["spacing"]) if "spacing" in data.files else float(entry["grid"]["spacing_mm"])
    return dict(
        name=shape,
        model=rs.HANGER_MODEL,
        mask=mask,
        origin=origin,
        spacing=spacing,
        keep=keep,
        default_load=float(entry["dst_condition_spec"]["load_n"]),
        entry=entry,
    )


def _run_case(shape: str) -> dict:
    shape_cfg = _shape_cfg(shape)
    t0 = time.time()
    result = me.solve_shape(shape_cfg, LOAD_N, LEVEL)
    elapsed = time.time() - t0
    if result["status"] != "ok":
        return dict(status=result["status"], error=result.get("error"), elapsed_seconds=elapsed)
    mask = result["mask"]
    cond = result["cond"]
    # `_mask_satisfies_min_width`単独(exempt無し)は、保持セル(`cond.keep_cells`)も5立方の
    # 材料ブロックへ属することを要求する厳格版。製品の実際の受理基準は`_min_width_exempt`で
    # 保持セルを規則の対象外にしてから判定する(`reproduce_stage1_transplant.py`と同じ経路、
    # 01_stage1_shapes.jsonのtransplant min_width_ok=falseもこの経路で得た値)。exempt無しの
    # 厳格版だと現行形(hanger_base)自身も不合格になり(保持セルが必ずしも5立方の塊にならない
    # ため)、製品の受理基準と矛盾する見かけの結果になる——4形状とも`_min_width_exempt`を通す。
    exempt = binary_search._min_width_exempt(cond, mask.shape, MIN_WIDTH_CELLS)
    min_width_ok = bool(binary_search._mask_satisfies_min_width(mask, min_width_cells=MIN_WIDTH_CELLS, exempt=exempt))
    min_width_ok_strict_no_exempt = bool(binary_search._mask_satisfies_min_width(mask, min_width_cells=MIN_WIDTH_CELLS))
    return dict(
        material_elements=result["material_elements"],
        design_elements=result["design_elements"],
        safety_factor_product=result["safety_factor"],
        representative_mpa_product=result["representative_mpa"],
        raw_max_mpa=result["raw_max_mpa"],
        min_width_ok=min_width_ok,
        min_width_ok_strict_no_exempt=min_width_ok_strict_no_exempt,
        elapsed_seconds=elapsed,
        metrics=rmc._score_all_metrics(result),
    )


def main() -> int:
    results = {}
    for shape in CASE_SHAPES:
        print(f"solving {shape} @ {LOAD_N}N level={LEVEL} condition={HANGER_CONDITION} ...")
        record = _run_case(shape)
        if record.get("status"):
            print(f"  failed: {record}")
            return 1
        results[shape] = record
        print(f"  material_elements={record['material_elements']} min_width_ok={record['min_width_ok']}")

    assert results["hanger_transplant"]["min_width_ok"] is False, "既知の不合格結果と食い違う"

    payload = dict(
        generated_at=_common.now_iso(),
        code_version=_common.git_state(),
        condition=HANGER_CONDITION,
        load_n=LOAD_N,
        cases=results,
    )
    out_path = OUT / "03b_hanger_common_condition.json"
    _common.write_atomic(out_path, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
