"""タスク5・アブレーション: 「適応歩幅のみ/復帰の有無/再配置の有無」と「停止条件(legacy/adaptive_finish)
×評価ゲート(percentile/m5b_raw)の2x2」を、dilution_small・path_small(04a条件)それぞれの baseline
1条件で対照する(タスク指示「適応歩幅のみ、復帰の有無、再配置の有無、停止条件の新旧、エネルギー順位と
巻き添え単位評価の差を分ける対照実験」「旧評価/新評価と削除/可逆の組合せも測る」)。

同一seed・同一評価予算で揃え、機構ログ(`mechanism_events`)・材料量・M5b・停止理由の違いだけを
比較材料にする(新方式の性能優位は主張しない——10_SYNTHESIS 付録C(旧 04_SEARCHES 3節)と同じ立場)。

出力: `output/idle_validation_20260928/05_ablation.json`
"""
from __future__ import annotations

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
EVALS = 100


def _run(ctx, cfg, *, tag, reallocation=False, grow_cycles=3):
    if reallocation:
        realloc_cfg = sm.ReallocationConfig(adaptive=cfg, grow_cycles=grow_cycles, seed=cfg.seed)
        rec = sm.run_reallocation(ctx, realloc_cfg)
    else:
        rec = sm.run_adaptive(ctx, cfg)
    return dict(summary=sup.record_summary(rec),
               mechanism_event_types=sorted({e["type"] for e in rec.mechanism_events}),
               mechanism_event_counts={t: sum(1 for e in rec.mechanism_events if e["type"] == t)
                                       for t in sorted({e["type"] for e in rec.mechanism_events})},
               best_so_far_curve=sup.best_so_far_curve(rec),
               full=rec.to_json(mask_dir=sup.MASK_DIR, tag=tag))


def run_model(name: str, model_kwargs: dict) -> dict:
    t0 = time.time()
    ctx = sm.build_context(**model_kwargs)
    out = dict(model=str(model_kwargs["model_path"].relative_to(REPO)), load_n=model_kwargs["load_n"],
              element_size_mm=model_kwargs["element_size_mm"], min_width_mm=model_kwargs["min_width_mm"],
              design_elements=int(ctx.space.mask.sum()))

    print(f"[{name}] step_only ...", flush=True)
    out["step_only"] = _run(ctx, sup.step_only_config(seed=100, max_evaluations=EVALS), tag=f"{name}_ab_step_only")

    print(f"[{name}] revert(既定) ...", flush=True)
    revert_cfg = sup.revert_config(seed=100, max_evaluations=EVALS)
    out["revert"] = _run(ctx, revert_cfg, tag=f"{name}_ab_revert")

    print(f"[{name}] reallocation ...", flush=True)
    out["reallocation"] = _run(ctx, revert_cfg, tag=f"{name}_ab_reallocation", reallocation=True, grow_cycles=3)

    print(f"[{name}] stop=legacy x gate=percentile ...", flush=True)
    out["legacy_percentile"] = _run(ctx, sup.revert_config(seed=100, max_evaluations=EVALS, gate="percentile",
                                                          stop_policy="legacy"), tag=f"{name}_ab_legacy_pct")

    print(f"[{name}] stop=legacy x gate=m5b_raw ...", flush=True)
    out["legacy_m5b_raw"] = _run(ctx, sup.revert_config(seed=100, max_evaluations=EVALS, gate="m5b_raw",
                                                        stop_policy="legacy"), tag=f"{name}_ab_legacy_m5b")

    print(f"[{name}] stop=adaptive_finish x gate=percentile ...", flush=True)
    out["adaptive_finish_percentile"] = _run(ctx, sup.revert_config(seed=100, max_evaluations=EVALS, gate="percentile",
                                                                    stop_policy="adaptive_finish"),
                                             tag=f"{name}_ab_af_pct")
    # adaptive_finish x m5b_raw は `revert`(既定)と同一設定なのでそのまま再掲する(重複実行しない)。
    out["adaptive_finish_m5b_raw"] = out["revert"]

    # 巻き添え単位評価の差(§D「エネルギー順位と巻き添え単位評価の差」の一次集計): revert実行の
    # reduce手について、要求セル数(energy順ランキングの申請)と実際削除セル数(resolve_islandsの
    # 巻き添え込み)の差を集計する。
    steps = out["revert"]["full"]["steps"]
    reduce_steps = [s for s in steps if s["kind"] == "reduce"]
    collateral = [s["actual_cells"] - s["requested_cells"] for s in reduce_steps if s["outcome"] == "accepted"]
    out["collateral_removal_stats"] = dict(
        n_accepted_reduce=len(collateral),
        collateral_positive_count=sum(1 for c in collateral if c > 0),
        collateral_mean=sum(collateral) / len(collateral) if collateral else None,
        collateral_max=max(collateral) if collateral else None,
    )

    out["wall_seconds_total"] = time.time() - t0
    return out


def main() -> int:
    sup.MASK_DIR.mkdir(parents=True, exist_ok=True)
    with _common.ExecutionLock(sup.LOCK, tag="run_05_ablation"):
        payload = dict(generated_at=_common.now_iso(), code_version=_common.git_state())
        print("=== dilution baseline ===", flush=True)
        payload["dilution"] = run_model("dilution", dict(model_path=sup.DILUTION_MODEL,
                                                          load_n=sup.DILUTION_BASELINE["load_n"],
                                                          element_size_mm=sup.DILUTION_BASELINE["element_size_mm"],
                                                          min_width_mm=sup.DILUTION_BASELINE["min_width_mm"]))
        print("=== path baseline(04a条件) ===", flush=True)
        payload["path"] = run_model("path", dict(model_path=sup.PATH_MODEL,
                                                  load_n=sup.PATH_BASELINE["load_n"],
                                                  element_size_mm=sup.PATH_BASELINE["element_size_mm"],
                                                  min_width_mm=sup.PATH_BASELINE["min_width_mm"]))
    out_path = OUT / "05_ablation.json"
    _common.write_atomic(out_path, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
