"""段階8・監査1: 05/06/07/04の保存台帳から、探索不具合の3機構を新規FEMなしで数える。

1. `prediction_error` 拒否: 予測ジャンプ(分母側)が小さいだけで、実ジャンプが微小でも比率で拒否されていないか。
2. 再配置の追加(grow): 追加候補の出所と、追加直後の材料が初期全材料のどれだけに戻るか。
3. ゲート不一致: 受理ゲート(m5b_raw/m5b_target/percentile)と、製品SFゲート・目標換算M5bの合否のずれ。

入力は保存台帳だけ(新規FEMなし)。同じ実行が複数の台帳に重複して載っているので、実行の指紋で重複を除く。
出力: `output/idle_validation_20260928/08_audit.json`
実行: `.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\analyze_08_audit.py`
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "output" / "idle_validation_20260928"
SOURCES = ["04_searches_compact", "05_main_matrix", "05_ablation", "05_initshape", "05_sensitivity",
           "06_hanger_low", "06_main_matrix", "06_adaptive_variants", "06_long_budget", "06_initshape",
           "07_methods"]
TARGET_SF = 4.8
SF_TOLERANCE = 0.005


def _walk(obj, path, found):
    if isinstance(obj, dict):
        steps = obj.get("steps")
        if isinstance(steps, list) and steps and isinstance(steps[0], dict) and "kind" in steps[0] \
                and "outcome" in steps[0]:
            found.append((path, obj))
        for key, value in obj.items():
            if key != "steps":
                _walk(value, f"{path}/{key}", found)
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            _walk(value, f"{path}[{i}]", found)


def _fingerprint(run) -> str:
    steps = run["steps"]
    payload = json.dumps([len(steps), steps[0], steps[-1], run.get("method")], sort_keys=True, default=str)
    return hashlib.sha1(payload.encode()).hexdigest()


def _cfg(run, key, default=None):
    cfg = run.get("config") or {}
    if key in cfg:
        return cfg[key]
    return (cfg.get("adaptive") or {}).get(key, default)


def _quantiles(values):
    if not values:
        return None
    s = sorted(values)
    pick = lambda q: s[min(len(s) - 1, int(q * (len(s) - 1)))]
    return dict(n=len(s), min=s[0], p25=pick(0.25), median=pick(0.5), p75=pick(0.75), p90=pick(0.9), max=s[-1])


def collect_runs():
    runs, seen = [], set()
    for name in SOURCES:
        path = OUT / f"{name}.json"
        if not path.exists():
            continue
        found = []
        _walk(json.loads(path.read_text(encoding="utf-8")), "", found)
        for sub, run in found:
            fp = _fingerprint(run)
            if fp in seen:
                continue
            seen.add(fp)
            runs.append(dict(source=name, path=sub, run=run))
    return runs


def audit_prediction_error(runs):
    per_run, sole_actual, sole_pred, accepted_actual, thresholds = [], [], [], [], defaultdict(int)
    for item in runs:
        run = item["run"]
        mult = _cfg(run, "prediction_error_multiplier")
        reduce_steps = [s for s in run["steps"] if s["kind"] == "reduce"]
        evaluated = [s for s in reduce_steps if s.get("actual_relative_jump") is not None]
        pe = [s for s in evaluated if "prediction_error" in (s.get("reasons") or [])]
        sole = [s for s in pe if (s.get("reasons") or []) == ["prediction_error"]]
        acc = [s for s in evaluated if s["outcome"] == "accepted"]
        per_run.append(dict(source=item["source"], path=item["path"], method=run.get("method"),
                            multiplier=mult, evaluated=len(evaluated), accepted=len(acc), prediction_error=len(pe),
                            prediction_error_sole=len(sole),
                            stop_reason=run.get("stop_reason"), status=run.get("status")))
        if mult is not None and mult < 1e5:
            for s in sole:
                sole_actual.append(s["actual_relative_jump"])
                sole_pred.append(s["predicted_relative_jump"])
                for floor in (1e-4, 1e-3, 3e-3, 1e-2, 3e-2, 5e-2, 1e-1):
                    if s["actual_relative_jump"] <= floor:
                        thresholds[floor] += 1
            for s in acc:
                accepted_actual.append(s["actual_relative_jump"])
    frozen = [r for r in per_run if r["multiplier"] is not None and r["multiplier"] < 1e5 and r["evaluated"]]
    return dict(
        runs_with_default_multiplier=len(frozen),
        runs_with_any_prediction_error=sum(1 for r in frozen if r["prediction_error"] > 0),
        evaluated_steps=sum(r["evaluated"] for r in frozen),
        prediction_error_steps=sum(r["prediction_error"] for r in frozen),
        prediction_error_sole_steps=sum(r["prediction_error_sole"] for r in frozen),
        sole_actual_jump=_quantiles(sole_actual),
        sole_predicted_jump=_quantiles(sole_pred),
        accepted_actual_jump=_quantiles(accepted_actual),
        sole_with_actual_at_or_below=dict((str(k), v) for k, v in sorted(thresholds.items())),
        per_run=per_run)


def audit_reallocation(runs):
    events, per_run = [], []
    for item in runs:
        run = item["run"]
        if run.get("method") != "reallocation":
            continue
        full = run.get("initial_material_mm3")
        grows = [e for e in run.get("mechanism_events", []) if e.get("type") == "grow_accepted"]
        for e in grows:
            after, before = e.get("material_mm3_after"), e.get("material_mm3_before")
            events.append(dict(source=item["source"], path=item["path"], grow_source=e.get("source"),
                               before=before, after=after, full=full,
                               after_over_full=(after / full) if after and full else None,
                               before_over_full=(before / full) if before and full else None))
        per_run.append(dict(source=item["source"], path=item["path"], grows=len(grows),
                            grow_sources=dict(Counter(e.get("source") for e in grows)), full_mm3=full,
                            best_ever_over_full=((run.get("best_ever_score") or {}).get("material_mm3", 0) / full)
                            if full else None))
    ratios = [e["after_over_full"] for e in events if e["after_over_full"] is not None]
    return dict(runs=len(per_run), grow_accepted_events=len(events),
                grow_source_counts=dict(Counter(e["grow_source"] for e in events)),
                after_over_full=_quantiles(ratios),
                events_with_after_ge_90pct_of_full=sum(1 for r in ratios if r >= 0.9),
                per_run=per_run)


def audit_gates(runs):
    rows = []
    for item in runs:
        run = item["run"]
        score = run.get("best_ever_score")
        if not score or run.get("method") in (None,):
            continue
        gate = _cfg(run, "gate")
        sf = score.get("safety_factor_product")
        rows.append(dict(source=item["source"], path=item["path"], method=run["method"], gate=gate or "product_percentile",
                         sf=sf, sf_ok=(sf is not None and sf >= TARGET_SF * (1 - SF_TOLERANCE)),
                         raw_ok=score.get("m5b_raw_passed"), target_ok=score.get("m5b_target_passed"),
                         material=score.get("material_mm3")))
    by_gate = defaultdict(lambda: Counter())
    for r in rows:
        key = r["gate"]
        by_gate[key]["n"] += 1
        by_gate[key]["sf_ok"] += int(bool(r["sf_ok"]))
        by_gate[key]["raw_ok"] += int(bool(r["raw_ok"]))
        by_gate[key]["target_ok"] += int(bool(r["target_ok"]))
    raw_pass_sf_fail = [r for r in rows if r["gate"] == "m5b_raw" and r["raw_ok"] and not r["sf_ok"]]
    return dict(shapes=len(rows), by_gate={k: dict(v) for k, v in by_gate.items()},
                m5b_raw_gate_shapes_below_product_target=[
                    dict(path=r["path"], source=r["source"], sf=r["sf"], material=r["material"]) for r in raw_pass_sf_fail],
                sf_tolerance=SF_TOLERANCE, target_sf=TARGET_SF)


def main():
    runs = collect_runs()
    result = dict(schema_version=1, runs_after_dedup=len(runs), sources=SOURCES,
                  prediction_error=audit_prediction_error(runs), reallocation=audit_reallocation(runs),
                  gates=audit_gates(runs))
    (OUT / "08_audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    pe, ra, gt = result["prediction_error"], result["reallocation"], result["gates"]
    print("runs", len(runs))
    print("pred_err", {k: v for k, v in pe.items() if k != "per_run"})
    print("realloc", {k: v for k, v in ra.items() if k != "per_run"})
    print("gates", gt["by_gate"], len(gt["m5b_raw_gate_shapes_below_product_target"]))


if __name__ == "__main__":
    sys.exit(main())
