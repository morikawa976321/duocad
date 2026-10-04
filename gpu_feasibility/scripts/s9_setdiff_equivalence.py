"""`acceptance.safety_check` の `np.setdiff1d(fixed, floating)` と、ブール配列で引く書き方が同じ配列を返すかを確かめ、秒を比べる。

**製品の `.venv` の python で、リポジトリ直下から実行する。**

    .venv/Scripts/python.exe gpu_feasibility/scripts/s9_setdiff_equivalence.py <行列名> ...

FEM は解かない(マスクと拘束の自由度だけを使う)。製品のファイルは変更しない。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("TOPOPT_THREADS", "1")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPO = ROOT.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "topopt_checks"))
sys.path.insert(0, str(REPO / "topopt_checks" / "idle_validation"))

import numpy as np  # noqa: E402

import search_methods as sm  # noqa: E402
from duocad.topopt import acceptance, anchor  # noqa: E402

HANGER_MODEL = REPO / "models" / "hanger_bar_attachment.json"
DATA = ROOT / "data"
RESULTS = ROOT / "results"
REPEATS = 3


def best_of(call) -> tuple[float, np.ndarray]:
    seconds, value = [], None
    for _ in range(REPEATS):
        t0 = time.perf_counter()
        value = call()
        seconds.append(time.perf_counter() - t0)
    return min(seconds), value


def main() -> None:
    names = sys.argv[1:]
    matrices = json.loads((RESULTS / "s2_matrices.json").read_text(encoding="utf-8"))
    out = {}
    for name in names:
        meta = matrices[name]
        mask = np.asarray(np.load(DATA / f"{name}.npz")["mask"], dtype=bool)
        ctx = sm.build_context(HANGER_MODEL, load_n=meta["load_n"], element_size_mm=meta["size_mm"],
                               min_width_mm=5.0 if "thin5" in name else 10.0)
        cond = ctx.cond
        _checks, fixed, _load = acceptance.condition_check(mask, ctx.table, cond.fixed_faces, cond.load_faces, cond.forces)
        floating = anchor.floating_dofs(mask)

        t_old, old = best_of(lambda: np.setdiff1d(fixed, floating))

        def by_mask() -> np.ndarray:
            live = np.repeat(anchor.active_nodes(mask).ravel(), 3)
            return np.unique(fixed[live[fixed]])

        t_new, new = best_of(by_mask)
        # 入力が未整列・重複ありでも同じ結果になるか(setdiff1d は昇順・重複なしを返す)。
        rng = np.random.default_rng(0)
        shuffled = rng.permutation(np.concatenate([fixed, fixed[: max(1, fixed.size // 3)]]))
        old_shuffled = np.setdiff1d(shuffled, floating)
        live = np.repeat(anchor.active_nodes(mask).ravel(), 3)
        new_shuffled = np.unique(shuffled[live[shuffled]])
        out[name] = {
            "grid_shape": list(mask.shape), "total_dofs": int(live.size), "fixed_size": int(fixed.size),
            "floating_size": int(floating.size), "physical_fixed_size": int(old.size),
            "fixed_sorted_unique": bool(np.array_equal(fixed, np.unique(fixed))),
            "setdiff1d_seconds": t_old, "mask_lookup_seconds": t_new,
            "identical": bool(np.array_equal(old, new) and old.dtype == new.dtype),
            "identical_on_shuffled_duplicated_input": bool(
                np.array_equal(old_shuffled, new_shuffled) and old_shuffled.dtype == new_shuffled.dtype),
            "dtype": str(old.dtype),
        }
        print(name, json.dumps(out[name], ensure_ascii=False))
    (RESULTS / "s9_setdiff_equivalence.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
