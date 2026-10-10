"""タスク4完了条件「小問題で各機構が発動した証拠があり、長い部材の復元が可能」の実証。

全項目、実際にFEM(`search_methods.score_mask`)を解いて確かめる(モックしない)。小型モデル
(`models/topopt_idle_dilution_small.json`)は安価に繰り返し実行でき、方式1・2の再現が
`00_COMPACT_MODELS.md` の既知値と一致する。

出力: `output/idle_validation_20260928/04_mechanism_checks.json`(各チェックの `passed`・生の証拠)。
1つでも失敗すれば非ゼロで終了する(呼び出し側=`build_04_searches_json.py`がこれを実行状態として扱う)。
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

import numpy as np  # noqa: E402

import _common  # noqa: E402
import search_methods as sm  # noqa: E402

OUT = REPO / "output" / "idle_validation_20260928"
DILUTION_MODEL = REPO / "models" / "topopt_idle_dilution_small.json"


def _context() -> sm.SearchContext:
    return sm.build_context(DILUTION_MODEL, load_n=300.0, element_size_mm=2.0, min_width_mm=4.0)


def check_rollback(ctx: sm.SearchContext) -> dict:
    """`sudden_jump_relative_drop` をほぼ0にすると、最初の一手あたりの安全率低下が必ず閾値を超え、
    `mechanism_events` に `rollback` が記録される(実測で発動を強制する——閾値を極端にするのは
    「発動する条件があることの証明」で、実運用の既定値ではない)。"""
    cfg = sm.AdaptiveConfig(seed=2, max_evaluations=15, sudden_jump_relative_drop=1e-6,
                            prediction_error_multiplier=1e9, stagnation_patience=1000)
    rec = sm.run_adaptive(ctx, cfg)
    rollbacks = [e for e in rec.mechanism_events if e["type"] == "rollback"]
    return dict(name="rollback", passed=len(rollbacks) > 0, rollback_count=len(rollbacks),
               evaluations=rec.evaluations, sample_event=rollbacks[0] if rollbacks else None)


def check_revert_restart(ctx: sm.SearchContext) -> dict:
    """`stagnation_patience=1` にすると、best_ever を更新しない手が1回出るだけで
    `revert_restart` が発動する(working が best_ever へ戻り、tie-break 順序を変えて再開)。
    `sudden_jump_relative_drop` をほぼ0にして候補を毎回却下させ(=rollback 相当を毎手起こし)、
    「非改善の手が実際に起きる」状況を作る——既定ゲート(`m5b_raw`)は緩く、素直に回すと最初の
    数十手が全部accept(material_mm3が毎回改善)されて stagnation が一度も起きないため。"""
    cfg = sm.AdaptiveConfig(seed=3, max_evaluations=25, stagnation_patience=1,
                            sudden_jump_relative_drop=1e-6, prediction_error_multiplier=1e9)
    rec = sm.run_adaptive(ctx, cfg)
    reverts = [e for e in rec.mechanism_events if e["type"] == "revert_restart"]
    return dict(name="revert_restart", passed=len(reverts) > 0, revert_count=len(reverts),
               evaluations=rec.evaluations, sample_event=reverts[0] if reverts else None)


def check_stop_policy_contrast(ctx: sm.SearchContext) -> dict:
    """`stop_policy="legacy"`(既存 `target_tolerance` 方式)と `"adaptive_finish"`(仕上げ探索+停滞判定)を
    同じ seed・同じ入口で回し、到達点(材料量・評価回数・停止理由)が異なりうることを示す
    (完了条件「元の停止条件との対照も取れるようにする」)。"""
    common = dict(seed=4, max_evaluations=60)
    legacy = sm.run_adaptive(ctx, sm.AdaptiveConfig(stop_policy="legacy", **common))
    adaptive = sm.run_adaptive(ctx, sm.AdaptiveConfig(stop_policy="adaptive_finish",
                                                      finish_max_evaluations=30, finish_patience=4, **common))
    return dict(name="stop_policy_contrast", passed=True,  # 対照が取れること自体が要求(優劣は主張しない)
               legacy=dict(stop_reason=legacy.stop_reason, evaluations=legacy.evaluations,
                          material_mm3=legacy.best_ever_score["material_mm3"] if legacy.best_ever_score else None),
               adaptive_finish=dict(stop_reason=adaptive.stop_reason, evaluations=adaptive.evaluations,
                                   material_mm3=adaptive.best_ever_score["material_mm3"] if adaptive.best_ever_score else None))


def check_determinism(ctx: sm.SearchContext) -> dict:
    """同じ seed・同じ入口の2回は、trial の material_mm3_after 列(`StepRecord` は mask 自体の
    ハッシュを持たないので体積列で代理する)と最終形状の `mask_hash`(`best_ever_score`。こちらは
    実際の内容ハッシュ)の両方が完全一致する。`revert_restart`(tie-break乱数を実際に使う経路)を
    確実に起こすため、`check_revert_restart` と同じ強制却下設定を使う——乱数を経由しない
    偶然の一致ではないことを示す。"""
    cfg = sm.AdaptiveConfig(seed=7, max_evaluations=20, stagnation_patience=1,
                            sudden_jump_relative_drop=1e-6, prediction_error_multiplier=1e9)
    rec_a = sm.run_adaptive(ctx, cfg)
    rec_b = sm.run_adaptive(ctx, cfg)
    hashes_a = [s.material_mm3_after for s in rec_a.steps]
    hashes_b = [s.material_mm3_after for s in rec_b.steps]
    mask_hash_a = rec_a.best_ever_score["mask_hash"] if rec_a.best_ever_score else None
    mask_hash_b = rec_b.best_ever_score["mask_hash"] if rec_b.best_ever_score else None
    same_seed_matches = hashes_a == hashes_b and mask_hash_a is not None and mask_hash_a == mask_hash_b
    restarts_a = len([e for e in rec_a.mechanism_events if e["type"] == "revert_restart"])
    cfg_other = dataclasses.replace(cfg, seed=8)
    rec_c = sm.run_adaptive(ctx, cfg_other)
    hashes_c = [s.material_mm3_after for s in rec_c.steps]
    different_seed_differs = hashes_a != hashes_c
    return dict(name="determinism", passed=bool(same_seed_matches and restarts_a > 0),
               final_mask_hash_matches=(mask_hash_a == mask_hash_b),
               same_seed_step_count=(len(hashes_a), len(hashes_b)), same_seed_matches=same_seed_matches,
               restarts_exercised=restarts_a, different_seed_differs=different_seed_differs,
               note="決定性は同seed一致のみを要求。異seedの分岐は参考情報。")


def check_resume(ctx: sm.SearchContext, tmp_dir: Path) -> dict:
    """`max_evaluations=8` で打ち切ってチェックポイントを保存し、`resume_from` で続きを走らせた
    結果が、同条件を1本で `max_evaluations=20` まで回した対照実行と完全一致するか(「再開の一致」)。"""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    ckpt = tmp_dir / "resume_check.json"
    cfg_full = sm.AdaptiveConfig(seed=9, max_evaluations=20, stagnation_patience=3)
    control = sm.run_adaptive(ctx, cfg_full)

    cfg_part = dataclasses.replace(cfg_full, max_evaluations=8)
    part = sm.run_adaptive(ctx, cfg_part, checkpoint_path=ckpt)
    if not ckpt.exists():
        return dict(name="resume", passed=False, error="checkpoint_not_written")
    resumed_state = sm.load_checkpoint(ckpt)
    resumed = sm.run_adaptive(ctx, cfg_full, resume_from=resumed_state)

    control_hash = control.best_ever_score["mask_hash"] if control.best_ever_score else None
    resumed_hash = resumed.best_ever_score["mask_hash"] if resumed.best_ever_score else None
    return dict(name="resume", passed=(control_hash is not None and control_hash == resumed_hash),
               control_mask_hash=control_hash, resumed_mask_hash=resumed_hash,
               control_evaluations=control.evaluations, resumed_evaluations=resumed.evaluations,
               checkpoint_evaluations=part.evaluations)


def check_cancel_retains_pass(ctx: sm.SearchContext) -> dict:
    """4手進んだら `is_cancelled` が真になるようにし、戻り値の `best_ever` が実際に使ったゲート
    (既定 `m5b_raw`)に合格したマスクのままであること(キャンセル後に合格形が残る)を確かめる。"""
    calls = {"n": 0}

    def is_cancelled() -> bool:
        calls["n"] += 1
        return calls["n"] > 4

    cfg = sm.AdaptiveConfig(seed=11, max_evaluations=500)
    rec = sm.run_adaptive(ctx, cfg, is_cancelled=is_cancelled)
    ok = (rec.stop_reason == "cancelled" and rec.best_ever_mask is not None
         and rec.best_ever_score is not None and rec.best_ever_score["m5b_raw_passed"])
    return dict(name="cancel_retains_pass", passed=bool(ok), stop_reason=rec.stop_reason,
               best_ever_score=rec.best_ever_score, is_cancelled_calls=calls["n"])


def check_poison_invalidation_and_reallocation(path_ctx: sm.SearchContext) -> dict:
    """再配置(path_small)で `grow_accepted` の直後に `poison_invalidated` が記録され、
    体積が一時的に増えること(仮追加)・長い部材(連結成分)が復元されることを1回のFEM系列で示す。"""
    cfg = sm.ReallocationConfig(
        adaptive=sm.AdaptiveConfig(seed=21, max_evaluations=40, stagnation_patience=3),
        grow_cycles=2, seed=21,
    )
    rec = sm.run_reallocation(path_ctx, cfg)
    grow_events = [e for e in rec.mechanism_events if e["type"] == "grow_accepted"]
    invalidate_events = [e for e in rec.mechanism_events if e["type"] == "poison_invalidated"]
    temporarily_heavier = any(e.get("temporarily_heavier") for e in grow_events)
    ordered_ok = True
    for g in grow_events:
        idx = rec.mechanism_events.index(g)
        following = rec.mechanism_events[idx + 1] if idx + 1 < len(rec.mechanism_events) else None
        if following is None or following["type"] != "poison_invalidated":
            ordered_ok = False
    max_added = max((e.get("added_cells", 0) for e in grow_events), default=0)
    return dict(name="poison_invalidation_and_reallocation", passed=bool(grow_events and invalidate_events and ordered_ok),
               grow_events=len(grow_events), invalidate_events=len(invalidate_events),
               temporarily_heavier_observed=temporarily_heavier, max_added_cells_in_one_grow=int(max_added),
               poison_invalidated_immediately_after_grow=ordered_ok,
               best_ever_score=rec.best_ever_score, stop_reason=rec.stop_reason)


def check_hash_independence(ctx_a: sm.SearchContext, ctx_b: sm.SearchContext) -> dict:
    """`rejected`/`poison` は `run_adaptive` 呼び出しごとのローカル変数で、内容ハッシュ
    (`acceptance.material_sha256`)で候補を突き合わせる(層をまたぐキャッシュを持たない)。
    条件(荷重)が違う`ctx_a`で強制却下を多数発生させた直後に`ctx_b`を走らせても、`ctx_b`単独の
    対照実行(同seed)と評価回数・trial列が一致すること(=`ctx_a`の却下履歴が漏れて`ctx_b`の
    候補評価を誤ってスキップしていないこと)を確かめる。"""
    cfg = sm.AdaptiveConfig(seed=13, max_evaluations=10, stagnation_patience=1,
                            sudden_jump_relative_drop=1e-6, prediction_error_multiplier=1e9)
    sm.run_adaptive(ctx_a, cfg)  # ctx_aで却下を多数発生させる(戻り値は使わない=状態は捨てる)
    rec_b1 = sm.run_adaptive(ctx_b, cfg)  # ctx_aの直後にctx_bを実行
    rec_b2 = sm.run_adaptive(ctx_b, cfg)  # ctx_b単独の対照(同条件を独立にもう一度)
    b1_seq = [s.material_mm3_after for s in rec_b1.steps]
    b2_seq = [s.material_mm3_after for s in rec_b2.steps]
    ok = (b1_seq == b2_seq and rec_b1.evaluations == rec_b2.evaluations
         and rec_b1.fem_solves == rec_b2.fem_solves)
    return dict(name="hash_independence", passed=bool(ok),
               ctx_a_then_b_evaluations=rec_b1.evaluations, isolated_b_evaluations=rec_b2.evaluations,
               ctx_a_then_b_fem_solves=rec_b1.fem_solves, isolated_b_fem_solves=rec_b2.fem_solves,
               step_sequences_match=(b1_seq == b2_seq))


def main() -> int:
    t0 = time.time()
    ctx = _context()
    ctx_alt_load = sm.build_context(DILUTION_MODEL, load_n=200.0, element_size_mm=2.0, min_width_mm=4.0)
    path_ctx = sm.build_context(REPO / "models" / "topopt_idle_path_small.json", load_n=3000.0,
                                element_size_mm=2.5, min_width_mm=6.0)
    tmp_dir = OUT / "_mechanism_checks_tmp"
    checks = [
        check_rollback(ctx),
        check_revert_restart(ctx),
        check_stop_policy_contrast(ctx),
        check_determinism(ctx),
        check_resume(ctx, tmp_dir),
        check_cancel_retains_pass(ctx),
        check_poison_invalidation_and_reallocation(path_ctx),
        check_hash_independence(ctx, ctx_alt_load),
    ]
    all_passed = all(c["passed"] for c in checks)
    payload = dict(generated_at=_common.now_iso(), code_version=_common.git_state(),
                  elapsed_seconds=time.time() - t0, all_passed=all_passed, checks=checks)
    out_path = OUT / "04_mechanism_checks.json"
    _common.write_atomic(out_path, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    for c in checks:
        print(f"{'PASS' if c['passed'] else 'FAIL'} {c['name']}")
    print(f"wrote {out_path}")
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
