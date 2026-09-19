"""`verify_hanger_fix.py` が書いた段階4の記録(JSON)から、計画書 §12 の比較表の値を取り出す。

    .\\.venv\\Scripts\\python.exe topopt_checks\\summarize_stage4.py output\\stage4_peel\\A-*.json output\\stage4_peel\\B-*.json

基準(peel 導入前の記録)にも使える(`peel` / `geometry_rejections` が無い記録は該当欄を空にする)。
「最初の10%」は段階4の試行(steps)の先頭 `int(0.1 × 試行数)` 件で、採用1回あたりの削減セル数は
その区間で採用された試行の削減セル数の合計 ÷ 採用回数。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def summarize(record: dict) -> dict:
    search = record["search"]
    reduction = search["diagnostics"]["reduction"]
    steps = reduction["steps"]
    best = search.get("best") or {}
    cut = int(0.1 * len(steps))

    def removed_per_acceptance(part: list) -> dict:
        accepted = [s for s in part if s["outcome"] == "accepted"]
        removed = sum(s["removed_cells"] for s in accepted)
        return dict(steps=len(part), accepted=len(accepted), removed_cells=removed,
                    per_acceptance=(removed / len(accepted)) if accepted else None)

    rejected = [s for s in steps if s["outcome"] == "rejected"]
    peel_seconds = [s["peel"]["seconds"] for s in steps if "peel" in s]
    return dict(
        surface_only=record.get("surface_only"), git_head=record.get("git_head"),
        termination=search["termination"], stop_reason=reduction["stop_reason"],
        stage4_fem=reduction["fem_solves"], evaluations=search["evaluations"], stage4_steps=len(steps),
        seconds=search["seconds"], final_mm3=best.get("material_mm3"), final_cells=best.get("cells"),
        safety_factor=best.get("safety_factor"),
        rejected=dict(
            total=len(rejected),
            safety=sum("safety" in s["failed"] and "no_component" not in s["failed"] for s in rejected),
            geometry=sum("geometry" in s["failed"] for s in rejected),
            no_component=sum("no_component" in s["failed"] for s in rejected),
            other=sum(not {"safety", "geometry", "no_component"} & set(s["failed"]) for s in rejected)),
        geometry_rejections=len(reduction.get("geometry_rejections", [])) if "geometry_rejections" in reduction else None,
        peel_seconds_total=sum(peel_seconds) if peel_seconds else None,
        peel_seconds_max=max(peel_seconds) if peel_seconds else None,
        peel_calls=reduction.get("peel_calls"), repeated_candidates=reduction.get("repeated_candidates"),
        first_10_percent=removed_per_acceptance(steps[:cut]), rest=removed_per_acceptance(steps[cut:]),
        final_region_recheck=reduction.get("final_region_recheck"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", nargs="+", type=Path)
    args = parser.parse_args()
    for path in args.records:
        print(path)
        print(json.dumps(summarize(json.loads(path.read_text(encoding="utf-8"))), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
