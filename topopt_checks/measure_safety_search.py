"""安全率探索の探索器(補間 対 二分)の実測。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_safety_search.py

題材は `bench.SAFETY_LOOP`(bracket = `demo_conditions`)で、`check_safety_loop.py` の [3] と
同じ括り(塊の容量の 15%〜70%)。**目標安全率だけを振る** —— 案Aの動機は「上端の安全率が目標を
大きく上回るとき二分が無駄を踏む」なので、目標を動かすとその比が動く。

**1つのプロセスで両方の探索器を回す。** 参照 ρ は解ごとに作り直されるが `optimize` は決定的
なので、比べているのは探索器の差だけになる。

結果は `output/safety_search.json`。読み方は docs/TOPOPT_SPEED.md「実装済み A」。
"""

from __future__ import annotations

import argparse
import json
import sys
import time

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
from duocad.topopt import OUTPUT_DIR
from duocad.topopt import bench
from duocad.topopt import design_space as ds
from duocad.topopt import safety_loop as sl

CFG = bench.SAFETY_LOOP
DERIVATION_KW = dict(elements_long_axis=CFG["elements_long_axis"])
OUT_PATH = OUTPUT_DIR / "safety_search.json"

#: 目標安全率。**この題材の上端の安全率は約6026**(容量の70%)なので、下から順に
#: 「目標が上端よりかなり低い」から「ほぼ張り付く」までを並べてある。
TARGETS = (3500.0, 4500.0, 5500.0, 5900.0)

MATERIAL_MIN_RATIO = 0.15
MATERIAL_MAX_RATIO = 0.70


def _bracket_bounds() -> tuple[ds.Conditions, float, float, float]:
    cond = ds.demo_conditions()
    fine = ds.derive(cond, elements_long_axis=CFG["elements_long_axis"])
    capacity = fine.grid.element_volume * fine.element_count
    return cond, capacity, capacity * MATERIAL_MIN_RATIO, capacity * MATERIAL_MAX_RATIO


def run_case(
    cond: ds.Conditions,
    *,
    target: float,
    material_min: float,
    material_max: float,
    searcher: str,
) -> dict:
    marks: list[float] = []
    started = time.perf_counter()

    def progress(step: sl.SafetyLoopStep) -> None:
        marks.append(time.perf_counter() - started)
        print(
            f"    [{searcher}] {len(marks)}: material={step.material_mm3:.0f} "
            f"{step.outcome} sf={step.safety_factor if step.safety_factor is None else round(step.safety_factor, 2)} "
            f"by={step.proposed_by} fallback={step.fallback_reason} clipped={step.clipped}",
            flush=True,
        )

    payload: dict = {
        "searcher": searcher,
        "target_safety_factor": target,
        "material_min_mm3": material_min,
        "material_max_mm3": material_max,
        "rel_tol": sl.DEFAULT_REL_TOL,
        "max_evaluations": sl.DEFAULT_MAX_EVALUATIONS,
        "max_iter": CFG["max_iter"],
        "elements_long_axis": CFG["elements_long_axis"],
        "error": None,
    }
    try:
        result = sl.solve_for_safety_factor(
            cond,
            target_safety_factor=target,
            material_min_mm3=material_min,
            material_max_mm3=material_max,
            max_iter=CFG["max_iter"],
            derivation_kwargs=dict(DERIVATION_KW),
            searcher=searcher,
            progress=progress,
        )
    except sl.SafetyTargetError as exc:
        payload["error"] = f"{type(exc).__name__}({exc.side}): {exc}"
        payload["seconds"] = time.perf_counter() - started
        return payload

    history = [
        {
            "material_mm3": float(step.material_mm3),
            "outcome": step.outcome,
            "safety_factor": None if step.safety_factor is None else float(step.safety_factor),
            "elements": step.elements,
            "proposed_by": step.proposed_by,
            "fallback_reason": step.fallback_reason,
            "clipped": bool(step.clipped),
            "seconds_from_start": mark,
        }
        for step, mark in zip(result.history, marks)
    ]
    upper = history[0]["safety_factor"]
    payload.update(
        {
            "evaluations": int(result.evaluations),
            "material_mm3": float(result.material_mm3),
            "achieved_material_mm3": float(result.achieved_material_mm3),
            "safety_factor": float(result.safety_factor),
            "converged": bool(result.converged),
            "upper_bound_safety_factor": upper,
            "upper_bound_over_target": None if upper is None else upper / target,
            "interpolated": sum(1 for s in history if s["proposed_by"] == "illinois"),
            "bisected": sum(1 for s in history if s["proposed_by"] == "bisection"),
            "clipped": sum(1 for s in history if s["clipped"]),
            "fallbacks": [s["fallback_reason"] for s in history if s["fallback_reason"]],
            "history": history,
            "seconds": time.perf_counter() - started,
        }
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="安全率探索の探索器の実測")
    parser.add_argument(
        "--targets", type=float, nargs="*", default=list(TARGETS), help="目標安全率(複数可)"
    )
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cond, capacity, material_min, material_max = _bracket_bounds()
    print(
        f"bracket 塊容量 {capacity:.0f}mm^3 / 括り [{material_min:.0f}, {material_max:.0f}]"
        f" / max_iter={CFG['max_iter']} / elements_long_axis={CFG['elements_long_axis']}",
        flush=True,
    )

    cases: list[dict] = []
    for target in args.targets:
        for searcher in ("bisection", "illinois"):
            print(f"  目標 sf={target} / searcher={searcher}", flush=True)
            case = run_case(
                cond,
                target=target,
                material_min=material_min,
                material_max=material_max,
                searcher=searcher,
            )
            cases.append(case)
            # **1ケース終わるたびに書き出す。** 途中で落ちてもそこまでが残る。
            OUT_PATH.write_text(
                json.dumps(
                    {
                        "model": "demo_conditions (bracket)",
                        "capacity_mm3": capacity,
                        "material_min_ratio": MATERIAL_MIN_RATIO,
                        "material_max_ratio": MATERIAL_MAX_RATIO,
                        "clip": [sl.INTERPOLATION_CLIP_LO, sl.INTERPOLATION_CLIP_HI],
                        "cases": cases,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

    print(f"\n書き出し: {OUT_PATH}", flush=True)
    print(f"{'目標':>8} {'探索器':>10} {'評価':>4} {'材料[mm^3]':>12} {'安全率':>9} {'上端/目標':>9}")
    for case in cases:
        if case["error"]:
            print(f"{case['target_safety_factor']:>8.0f} {case['searcher']:>10} {case['error']}")
            continue
        print(
            f"{case['target_safety_factor']:>8.0f} {case['searcher']:>10} {case['evaluations']:>4} "
            f"{case['material_mm3']:>12.0f} {case['safety_factor']:>9.2f} "
            f"{case['upper_bound_over_target']:>9.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
