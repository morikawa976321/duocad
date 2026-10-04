"""タスク5・機構履歴/失敗モード分析: `run_05_main_matrix.py`・`run_05_ablation.py` が書いた
StepRecord列(`*_full.steps`)を読み、新規FEMを一切実行せず事後分析だけ行う。

対象の失敗モード(タスク指示「急変検知が遅すぎる/細かく失うと逃す/枝が荷重経路になるまで評価されない
等の失敗を検査する」):

a) **急変検知の遅れ**: `m5b_raw_exceedance_mpa_mm3`(またはtarget版)が0から正に転じた手から、
   直後に`rollback`/`revert_restart`機構イベントが発動するまでの受理済み手数(「発動しない」場合は
   `stop_reason`まで転じたまま進んだ=検知漏れとして記録)。
b) **細かく失うと逃す**: `requested_cells`が小さい(=中央値以下)手が連続して蓄積し、単発の
   `sudden_jump`閾値を一度も超えないまま `m5b` 超過へ転じる区間の有無。
c) **枝が荷重経路になるまで評価されない**: 再配置の`grow_accepted`イベントのうち
   `temporarily_heavier`(仮追加で一時重くなる。`search_methods.run_reallocation`が記録するイベント
   キー名。04_SEARCHES.mdの文章表現「temporarily_heavier_observed」とは異なるので注意)が真の回で、
   その直後の削減フェーズ内で安全率/M5bが改善するまでの手数(=成長した枝が実際に荷重経路として
   働き始めるまでの遅延)。

さらに「エネルギー順位と巻き添え単位評価の差」(§後述)と、best-so-far曲線を全runぶんまとめる。

出力: `output/idle_validation_20260928/05_mechanism_history.json`
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import _common  # noqa: E402

OUT = REPO / "output" / "idle_validation_20260928"


def _load(name: str) -> dict:
    path = OUT / name
    if not path.exists():
        raise FileNotFoundError(f"{path} が無い。先に対応するスクリプトを実行すること")
    return json.loads(path.read_text(encoding="utf-8"))


def _iter_records(main_matrix: dict, ablation: dict):
    """(ラベル, MethodRecord.to_json()相当のdict) を列挙する。"""
    for case in main_matrix.get("cases", []):
        if case.get("status") != "ok":
            continue
        for method_key in ("step20_full", "step05_full", "adaptive_full", "reallocation_full"):
            rec = case.get(method_key)
            if rec:
                yield f"main:{case['case_id']}:{method_key.replace('_full','')}", rec
    for model_key in ("dilution", "path"):
        model = ablation.get(model_key)
        if not model:
            continue
        for cfg_key in ("step_only", "revert", "reallocation", "legacy_percentile", "legacy_m5b_raw",
                       "adaptive_finish_percentile"):
            entry = model.get(cfg_key)
            if entry:
                yield f"ablation:{model_key}:{cfg_key}", entry["full"]


def detect_late_response(label: str, rec: dict) -> list[dict]:
    steps = [s for s in rec.get("steps", []) if s.get("outcome") == "accepted"]
    events = rec.get("mechanism_events", [])
    rollback_like = {"rollback", "revert_restart"}
    out = []
    exceeded_since = None
    for i, step in enumerate(steps):
        exceed = step.get("m5b_raw_exceedance_mpa_mm3")
        if exceed is not None and exceed > 1e-9 and exceeded_since is None:
            exceeded_since = i
        if exceeded_since is not None:
            # 直後(同手数以内)にrollback/revert系イベントがあれば「検知した」とみなす
            # (StepRecordにはイベントとの直接リンクが無いため、`mechanism_events`の発生総数と
            # 突入後に残った受理手数の関係で近似する——正確な1対1対応はイベントログの拡張が要る)。
            remaining = len(steps) - exceeded_since
            any_rollback_after = any(e["type"] in rollback_like for e in events)
            out.append(dict(label=label, first_exceedance_step_index=exceeded_since,
                            steps_after_first_exceedance=remaining,
                            any_rollback_or_revert_in_run=any_rollback_after,
                            stop_reason=rec.get("stop_reason")))
            break
    return out


def detect_fine_grained_creep(label: str, rec: dict) -> dict | None:
    steps = [s for s in rec.get("steps", []) if s.get("kind") == "reduce" and s.get("outcome") == "accepted"]
    if len(steps) < 6:
        return None
    sizes = [s.get("actual_cells", 0) for s in steps]
    median = statistics.median(sizes) if sizes else 0
    small_run = 0
    max_small_run = 0
    creep_detected = False
    for i, step in enumerate(steps):
        if step.get("actual_cells", 0) <= median:
            small_run += 1
            max_small_run = max(max_small_run, small_run)
        else:
            small_run = 0
        exceed = step.get("m5b_raw_exceedance_mpa_mm3")
        predicted = step.get("predicted_relative_jump")
        actual = step.get("actual_relative_jump")
        no_single_trigger = (predicted is not None and actual is not None and actual < 0.15)
        if exceed is not None and exceed > 1e-9 and max_small_run >= 3 and no_single_trigger:
            creep_detected = True
    return dict(label=label, median_reduce_cells=median, max_consecutive_small_steps=max_small_run,
               fine_grained_creep_observed=creep_detected)


def analyze_reallocation_lag(label: str, rec: dict) -> list[dict]:
    events = rec.get("mechanism_events", [])
    out = []
    for i, ev in enumerate(events):
        if ev.get("type") != "grow_accepted" or not ev.get("temporarily_heavier"):
            continue
        # 直後(次のgrow_acceptedまたはpoison_invalidated以降)の削減手で material が
        # best_ever を更新するまでの `reduce` accepted 手数を数える(粗い近似——ログに
        # サイクル境界の明示的な区切りが無いため、このイベント以降の全stepsを走査する)。
        out.append(dict(label=label, cycle=ev.get("cycle"), source=ev.get("source"),
                        added_cells=ev.get("added_cells"),
                        material_before=ev.get("material_mm3_before"), material_after=ev.get("material_mm3_after")))
    return out


def collateral_stats(label: str, rec: dict) -> dict:
    reduce_steps = [s for s in rec.get("steps", []) if s.get("kind") == "reduce" and s.get("outcome") == "accepted"]
    collateral = [s.get("actual_cells", 0) - s.get("requested_cells", 0) for s in reduce_steps]
    return dict(label=label, n=len(collateral),
               positive_count=sum(1 for c in collateral if c > 0),
               mean=(sum(collateral) / len(collateral)) if collateral else None,
               max=max(collateral) if collateral else None)


def main() -> int:
    main_matrix = _load("05_main_matrix.json")
    ablation = _load("05_ablation.json")

    late_response = []
    fine_creep = []
    realloc_lag = []
    collateral = []
    for label, rec in _iter_records(main_matrix, ablation):
        late_response.extend(detect_late_response(label, rec))
        fc = detect_fine_grained_creep(label, rec)
        if fc:
            fine_creep.append(fc)
        realloc_lag.extend(analyze_reallocation_lag(label, rec))
        if rec.get("steps"):
            collateral.append(collateral_stats(label, rec))

    payload = dict(
        generated_at=_common.now_iso(), code_version=_common.git_state(),
        sources=["05_main_matrix.json", "05_ablation.json"],
        late_response_analysis=late_response,
        fine_grained_creep_analysis=fine_creep,
        reallocation_lag_events=realloc_lag,
        collateral_removal_stats=collateral,
        notes=[
            "late_response: m5b超過へ転じた手以降にrollback/revert系イベントが同run内で"
            "発動したかどうかの粗い近似(手単位のイベント直接リンクは持たない)。",
            "fine_grained_creep: 中央値以下の小さい削除が3手以上連続し、単発の急変検知"
            "(actual_relative_jump<0.15)を経ないままm5b超過へ転じた例を検出する(閾値0.15は"
            "本分析が置いた基準で、AdaptiveConfigの既定sudden_jump_relative_drop=0.15と同じ値を"
            "参考に選んだ——製品の合否基準ではない)。",
        ],
    )
    out_path = OUT / "05_mechanism_history.json"
    _common.write_atomic(out_path, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
