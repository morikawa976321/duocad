"""ビット一致版 CG の 1 反復の内訳を測る(さらに速くできる余地を見積もるための材料)。

実行(検証用 venv): gpu_feasibility/.venv/Scripts/python.exe gpu_feasibility/scripts/s7_iteration_breakdown.py [行列名 ...]
出力: gpu_feasibility/results/s7_iteration_breakdown.json

各演算を REPEATS 回続けて投げ、最後に 1 回だけ同期して割った秒(= GPU が実際に計算している時間に近い)と、
1 回ごとに同期した秒(= 起動と同期の待ちを含む)を並べる。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import cupy as cp  # noqa: E402

import _blasdot  # noqa: E402
import _gpucg  # noqa: E402

DATA = ROOT / "data"
RESULTS = ROOT / "results"
REPEATS = 300


def sync() -> None:
    cp.cuda.Stream.null.synchronize()


def timed(call, each_sync: bool) -> float:
    call()
    sync()
    t0 = time.perf_counter()
    for _ in range(REPEATS):
        call()
        if each_sync:
            sync()
    sync()
    return (time.perf_counter() - t0) / REPEATS * 1e3


def run(name: str, s5: dict) -> dict:
    z = np.load(DATA / f"{name}.npz")
    system = _gpucg.GpuSystem(z["data"], z["indices"], z["indptr"], z["shape"])
    n, nnz = system.n, int(z["data"].size)
    dot = _blasdot.BlasDot(n, _blasdot.openblas_threads())
    rng = np.random.default_rng(0)
    p, q, x, r, zv = (cp.asarray(rng.standard_normal(n)) for _ in range(5))
    ell = system.matvec("ell_nofma")
    raw = system.matvec("raw_nofma")
    cus = system.matvec("cusparse")
    noop = cp.ElementwiseKernel("T a", "T b", "b = a", "s7_noop")
    one = cp.zeros(1, dtype=cp.float64)
    one_out = cp.zeros(1, dtype=cp.float64)
    ops = {
        "spmv_ell_nofma": lambda: ell(p, out=q),
        "spmv_raw_nofma": lambda: raw(p, out=q),
        "spmv_cusparse": lambda: cus(p),
        "divide": lambda: _gpucg._exact_divide(r, system.diag, zv),
        "update_p": lambda: _gpucg._exact_update_p(zv, 0.5, p),
        "update_xr": lambda: _gpucg._exact_update_xr(1e-3, p, q, x, r),
        "dot_kernel_only": lambda: dot.kernel((dot.chunks,), (16,), (r, zv, dot.starts, dot.widths, dot.out)),
        "empty_kernel_launch": lambda: noop(one, one_out),
    }
    rec: dict = {"free_dofs": n, "nnz": nnz, "ms": {}}
    for label, call in ops.items():
        rec["ms"][label] = {"batched": timed(call, False), "each_synced": timed(call, True)}
    # 内積は結果をホストへ読むので、呼ぶたびに同期が入る(現行のループと同じ使い方)。
    dot(r, zv)
    t0 = time.perf_counter()
    for _ in range(REPEATS):
        dot(r, zv)
    rec["ms"]["dot_with_readback"] = {"each_synced": (time.perf_counter() - t0) / REPEATS * 1e3}
    measured = s5.get(name, {}).get("variants", {}).get("ell_nofma", {}).get("ms_per_iteration")
    m = rec["ms"]
    rec["iteration_ms_measured_in_cg"] = measured
    rec["iteration_ms_sum_of_parts"] = (m["spmv_ell_nofma"]["batched"] + m["divide"]["batched"] + m["update_p"]["batched"]
                                        + m["update_xr"]["batched"] + 3 * m["dot_with_readback"]["each_synced"])
    rec["iteration_ms_compute_only"] = (m["spmv_ell_nofma"]["batched"] + m["divide"]["batched"] + m["update_p"]["batched"]
                                        + m["update_xr"]["batched"] + 3 * m["dot_kernel_only"]["batched"])
    rec["spmv_ell_bytes_per_second_gb"] = nnz * 12 / (m["spmv_ell_nofma"]["batched"] * 1e-3) / 1e9
    del system
    cp.get_default_memory_pool().free_all_blocks()
    return rec


def main() -> None:
    names = sys.argv[1:] or ["F0500_thin5mm_h2_step20_d0", "F2000_thin5mm_h2_step20_d0", "F2000_thin5mm_h1.8_step20_d0"]
    s5 = json.loads((RESULTS / "s5_bitexact_cg.json").read_text(encoding="utf-8"))
    out = {}
    for name in names:
        out[name] = run(name, s5)
        r = out[name]
        print(f"== {name} dofs={r['free_dofs']} nnz={r['nnz']}")
        for label, v in r["ms"].items():
            print(f"   {label:22s} " + "  ".join(f"{k}={ms:.4f}ms" for k, ms in v.items()))
        print(f"   1反復: CG 内の実測 {r['iteration_ms_measured_in_cg']:.3f} ms / 部品の和 {r['iteration_ms_sum_of_parts']:.3f} ms / "
              f"計算だけ {r['iteration_ms_compute_only']:.3f} ms / ELL の積の転送量 {r['spmv_ell_bytes_per_second_gb']:.0f} GB/s")
    (RESULTS / "s7_iteration_breakdown.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
