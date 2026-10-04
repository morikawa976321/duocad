"""タスク4の主実証: 4方式(20%削除・5%削除・適応歩幅＋復帰・再配置)を小型モデルで走らせ、
条件・記録・成果物を `output/idle_validation_20260928/` へ保存する(§完了条件「小問題で各機構が
発動した証拠」の主データ)。

- dilution_small(300N・2.0mm・min_width 4.0mm): 方式1・2・3(母数希釈=原因1の代表題材)。
  方式1・2は `00_COMPACT_MODELS.md` §2-2の既知値(88,368 mm³・18評価)と一致することを確認済み
  (§後続への契約④「踏んだ罠」参照)。
- path_small(3000N・2.5mm・min_width 6.0mm): 方式1・2・3・4。経路喪失=原因2の代表題材
  ——04a未認定のため、長い部材の復元評価は「未認定pathモデルでの機構確認」と明記する。

**単一実行ロック**を全モデル・全方式で共有する(`docs/TOPOPT_IDLE_VALIDATION_TASKS_2026-09-28.md`
「計算の継続と資源」)。`hanger_external_check.py` と同じ `_locks/fem_single_run.lock` を使うので、
どちらか一方が実行中はもう一方を起動しない(同時に起動すると `RuntimeError` で即座に落ちる——
仕様どおり)。

再現: `.\\.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\run_04_searches_compact.py`
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

OUT = REPO / "output" / "idle_validation_20260928"
MASK_DIR = OUT / "04_masks"
LOCK = OUT / "_locks" / "fem_single_run.lock"

DILUTION = dict(model=REPO / "models" / "topopt_idle_dilution_small.json", load_n=300.0,
                element_size_mm=2.0, min_width_mm=4.0, tag="dilution_2.0mm_f300")
PATH = dict(model=REPO / "models" / "topopt_idle_path_small.json", load_n=3000.0,
           element_size_mm=2.5, min_width_mm=6.0, tag="path_2.5mm_f3000")


def _record(rec: sm.MethodRecord, *, tag: str) -> dict:
    return rec.to_json(mask_dir=MASK_DIR, tag=tag)


def run_case(case: dict) -> dict:
    t0 = time.time()
    ctx = sm.build_context(case["model"], load_n=case["load_n"], element_size_mm=case["element_size_mm"],
                           min_width_mm=case["min_width_mm"])
    out = dict(model=str(case["model"].relative_to(REPO)), model_sha256=_common.sha256_file(case["model"]),
              load_n=case["load_n"], element_size_mm=case["element_size_mm"], min_width_mm=case["min_width_mm"],
              design_elements=int(ctx.space.mask.sum()), min_width_cells=ctx.min_width_cells)

    print(f"[{case['tag']}] step20 ...", flush=True)
    step20 = sm.run_fixed_step(ctx, max_step_ratio=0.2)
    out["step20"] = _record(step20, tag=f"{case['tag']}_step20")

    print(f"[{case['tag']}] step05 ...", flush=True)
    step05 = sm.run_fixed_step(ctx, max_step_ratio=0.05)
    out["step05"] = _record(step05, tag=f"{case['tag']}_step05")

    print(f"[{case['tag']}] adaptive (m5b_raw) ...", flush=True)
    adaptive_cfg = sm.AdaptiveConfig(seed=42, max_evaluations=120, stagnation_patience=8)
    adaptive = sm.run_adaptive(ctx, adaptive_cfg,
                               checkpoint_path=OUT / f"04_{case['tag']}_adaptive.checkpoint.json")
    out["adaptive"] = _record(adaptive, tag=f"{case['tag']}_adaptive")

    print(f"[{case['tag']}] adaptive (percentile対照) ...", flush=True)
    adaptive_pct_cfg = sm.AdaptiveConfig(seed=42, max_evaluations=120, stagnation_patience=8, gate="percentile")
    adaptive_pct = sm.run_adaptive(ctx, adaptive_pct_cfg)
    out["adaptive_percentile_gate"] = _record(adaptive_pct, tag=f"{case['tag']}_adaptive_percentile")

    print(f"[{case['tag']}] reallocation ...", flush=True)
    realloc_cfg = sm.ReallocationConfig(
        adaptive=sm.AdaptiveConfig(seed=43, max_evaluations=80, stagnation_patience=6),
        grow_cycles=3, seed=43,
    )
    realloc = sm.run_reallocation(ctx, realloc_cfg,
                                  checkpoint_path=OUT / f"04_{case['tag']}_reallocation.checkpoint.json")
    out["reallocation"] = _record(realloc, tag=f"{case['tag']}_reallocation")

    out["wall_seconds_total"] = time.time() - t0
    return out


def main() -> int:
    MASK_DIR.mkdir(parents=True, exist_ok=True)
    with _common.ExecutionLock(LOCK, tag="run_04_searches_compact"):
        results = {}
        print("=== dilution_small ===", flush=True)
        results["dilution"] = run_case(DILUTION)
        print("=== path_small ===", flush=True)
        results["path"] = run_case(PATH)

    payload = dict(generated_at=_common.now_iso(), code_version=_common.git_state(), cases=results)
    out_path = OUT / "04_searches_compact.json"
    _common.write_atomic(out_path, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
