"""models/*.json の同じ条件で二分探索と Illinois 法を比較する。

--model hanger_test.json --spacing 2.4 --targets 2 3 4
荷重の大きさ・方向は文書から読み、材料量の探索範囲も製品と同じ規則で決める。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
from duocad.topopt import OUTPUT_DIR
from duocad.topopt import design_space as ds
from duocad.topopt import safety_loop as sl

OUT_PATH = OUTPUT_DIR / "safety_search.json"


def model_bounds(model: str, spacing: float):
    from duocad.session import Session
    from duocad.optimize_run import build_conditions, OptimizeOptions, _material_bounds

    session = Session(None, 0.2)
    session.open_document(model)
    bundle = build_conditions(session.document)
    space = ds.derive(bundle.conditions, spacing=spacing)
    capacity = space.grid.element_volume * space.element_count
    keep = float(space.model.keep.sum()) * space.grid.element_volume
    low, high = _material_bounds(OptimizeOptions(), keep_mm3=keep, capacity_mm3=capacity)
    return bundle, capacity, low, high


def run_case(
    cond: ds.Conditions,
    *,
    target: float,
    material_min: float,
    material_max: float,
    searcher: str,
    forces: list,
    spacing: float,
    max_iter: int,
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
        "max_iter": max_iter,
        "spacing_mm": spacing,
        "error": None,
    }
    try:
        result = sl.solve_for_safety_factor(
            cond,
            target_safety_factor=target,
            material_min_mm3=material_min,
            material_max_mm3=material_max,
            max_iter=max_iter,
            derivation_kwargs={"spacing": spacing},
            forces=forces,
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
    parser.add_argument("--model", required=True, help="models/*.json のファイル名")
    parser.add_argument("--spacing", type=float, default=2.4)
    parser.add_argument("--max-iter", type=int, default=sl.DEFAULT_MAX_ITER)
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    parser.add_argument("--targets", type=float, nargs="+", required=True)
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    bundle, capacity, material_min, material_max = model_bounds(args.model, args.spacing)
    print(
        f"{args.model} 塊容量 {capacity:.0f}mm^3 / 括り [{material_min:.0f}, {material_max:.0f}]"
        f" / max_iter={args.max_iter} / spacing={args.spacing}",
        flush=True,
    )

    cases: list[dict] = []
    for target in args.targets:
        for searcher in ("bisection", "illinois"):
            print(f"  目標 sf={target} / searcher={searcher}", flush=True)
            case = run_case(
                bundle.conditions,
                forces=bundle.forces,
                spacing=args.spacing,
                max_iter=args.max_iter,
                target=target,
                material_min=material_min,
                material_max=material_max,
                searcher=searcher,
            )
            cases.append(case)
            # **1ケース終わるたびに書き出す。** 途中で落ちてもそこまでが残る。
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(
                json.dumps(
                    {
                        "model": args.model,
                        "forces_n": bundle.forces,
                        "capacity_mm3": capacity,
                        "material_min_ratio": (material_min / capacity),
                        "material_max_ratio": (material_max / capacity),
                        "clip": [sl.INTERPOLATION_CLIP_LO, sl.INTERPOLATION_CLIP_HI],
                        "cases": cases,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

    print(f"\n書き出し: {args.out}", flush=True)
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
