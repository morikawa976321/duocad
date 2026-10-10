"""段階5d の前提: GPU の内積を `np.dot` とビット一致させられるかを、足す順序の候補を総当たりして確かめる。

実行(開発用の .venv。`uv sync --extra gpu` で CuPy を入れておく): .venv/Scripts/python.exe gpu_feasibility/scripts/s5_bitexact_dot_check.py
出力: gpu_feasibility/results/s5_bitexact_dot.json
"""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import _blasdot  # noqa: E402

RESULTS = ROOT / "results"
# 区間の境界(16 の倍数・端数あり)、単スレッドの上限の前後、計測用行列の実寸を含める。
LENGTHS = (17, 1000, 9999, 10000, 10001, 10016, 32574, 53496, 105033, 202359, 226758, 359751, 1200007, 2437998)


def main() -> None:
    threads = _blasdot.openblas_threads()
    out: dict = {"numpy": np.__version__, "openblas_threads": threads, "lengths": list(LENGTHS), "variants": []}
    for fma_main, fma_tail, by_register in itertools.product((True, False), repeat=3):
        variant = dict(fma_main=fma_main, fma_tail=fma_tail, reduce_by_register=by_register)
        failed = [n for n in LENGTHS if not _blasdot.self_test(n, threads, **variant)]
        out["variants"].append({**variant, "failed_lengths": failed, "all_match": not failed})
        print(variant, "all match" if not failed else f"failed at {failed}")
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "s5_bitexact_dot.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
