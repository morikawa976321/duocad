"""段階1: 1評価のうち CG 以外にかかる秒を測る(CG は回さない)。

**製品の `.venv` の python で、リポジトリ直下から実行する。**

    .venv/Scripts/python.exe gpu_feasibility/scripts/s1_eval_breakdown.py <行列名> ...

このプロセスの中でだけ `scipy.sparse.linalg.cg` を「保存済みの解を返す関数」に差し替え、製品の評価
(`acceptance.inspect`=探索の1評価、`search_methods.score_mask`=07 の採点)をそのまま呼ぶ。
保存済みの解は `s4_hybrid_bitmatch.py` が作った、CPU の CG とビット一致する解(`data/<名前>_u.npy`)。
組立・切り出し・残差検算・応力・判定は本物が走るので、得られる秒は「CG を 0 秒にしたときの1評価」になる。
FEM ロックは取らない。製品のファイルは変更しない。
"""
from __future__ import annotations

import cProfile
import datetime as dt
import json
import os
import pstats
import sys
import time
from pathlib import Path

os.environ.setdefault("TOPOPT_THREADS", "1")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPO = ROOT.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "topopt_checks"))
sys.path.insert(0, str(REPO / "topopt_checks" / "idle_validation"))

import numpy as np  # noqa: E402

import _winmem  # noqa: E402
import search_methods as sm  # noqa: E402
from duocad.topopt import acceptance, fem  # noqa: E402

HANGER_MODEL = REPO / "models" / "hanger_bar_attachment.json"
DATA = ROOT / "data"
RESULTS = ROOT / "results"
RAM_NEED_FACTOR = 576 * 48
RAM_MARGIN_GB = 2.0


def run(name: str, matrices: dict) -> dict:
    meta = matrices[name]
    rec: dict = {"name": name, "free_dofs": meta["free_dofs"], "cells": meta["cells"],
                 "measured_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                 "ram_available_gb_before": _winmem.available_gb()}
    if meta["cells"] * RAM_NEED_FACTOR / 2**30 + RAM_MARGIN_GB > rec["ram_available_gb_before"]:
        rec["status"] = "skipped_ram"
        return rec
    stored = np.load(DATA / f"{name}.npz")
    mask, rhs = np.asarray(stored["mask"], dtype=bool), stored["rhs"]
    solution = np.load(DATA / f"{name}_u.npy")
    width_mm = 5.0 if "thin5" in name else 10.0
    ctx = sm.build_context(HANGER_MODEL, load_n=meta["load_n"], element_size_mm=meta["size_mm"],
                           min_width_mm=width_mm)
    cond = ctx.cond
    calls = {"cg": 0}
    real_cg = fem.spla.cg

    def stored_cg(a_op, b, *args, **kwargs):
        if not np.array_equal(b, rhs):
            raise RuntimeError("右辺が保存した系と違う(別の系に保存済みの解を返すところだった)")
        calls["cg"] += 1
        return solution.copy(), 0

    fem.spla.cg = stored_cg
    try:
        # inspect は 2 回呼ぶ。1 回目だけにかかる準備(前計算・キャッシュ)と、探索中に毎回かかる分を分けるため。
        for label, call in (
            ("inspect_first", lambda: acceptance.inspect(mask, mask, cond.grid, **cond.inspect_kwargs(), fields={})),
            ("inspect_repeat", lambda: acceptance.inspect(mask, mask, cond.grid, **cond.inspect_kwargs(), fields={})),
            ("score_mask", lambda: sm.score_mask(mask, ctx)),
        ):
            calls["cg"] = 0
            profiler = cProfile.Profile() if label == "inspect_repeat" else None
            with fem.collect_solve_stats() as stats:
                t0 = time.perf_counter()
                result = profiler.runcall(call) if profiler else call()
                wall = time.perf_counter() - t0
            entry = {"wall_seconds_without_cg": wall, "assemble_seconds": stats["assemble_seconds"],
                     "slice_seconds": stats["slice_seconds"], "solve_seconds_incl_verify": stats["solve_seconds"],
                     "cg_calls": calls["cg"], "direct_solves": stats["direct_solves"]}
            if profiler:
                rows = pstats.Stats(profiler).sort_stats("cumulative").stats.items()
                top = sorted(rows, key=lambda kv: kv[1][3], reverse=True)[:25]
                entry["profile_top_cumulative"] = [
                    {"function": f"{Path(key[0]).name}:{key[1]}:{key[2]}", "calls": val[1],
                     "cumulative_seconds": round(val[3], 3), "own_seconds": round(val[2], 3)}
                    for key, val in top
                ]
            if label.startswith("inspect"):
                safety = result["checks"]["safety"]
                entry.update(status=result["status"], solver=safety.get("solver"),
                             safety_factor=safety.get("safety_factor"))
            else:
                entry.update(scored=result is not None,
                             safety_factor_product=None if result is None else result.safety_factor_product)
            rec[label] = entry
    finally:
        fem.spla.cg = real_cg
    rec["status"] = "ok"
    rec["peak_working_set_gb"] = _winmem.peak_working_set_gb()
    return rec


def main() -> None:
    names = sys.argv[1:]
    if not names:
        raise SystemExit(__doc__)
    start = dt.datetime.now().astimezone()
    matrices = json.loads((RESULTS / "s2_matrices.json").read_text(encoding="utf-8"))
    out_path = RESULTS / "s1_eval_breakdown.json"
    out = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    for name in names:
        rec = run(name, matrices)
        out[name] = rec
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(rec, ensure_ascii=False))
    _winmem.log_load_window(RESULTS, start, "s1_eval_breakdown " + " ".join(names), cpu_threads=1)


if __name__ == "__main__":
    main()
