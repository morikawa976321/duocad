"""段階4: 「行列ベクトル積だけ GPU、他は CPU の scipy のまま」の CG が、CPU の CG とビット一致するかを確かめる。

実行(検証用 venv): gpu_feasibility/.venv/Scripts/python.exe gpu_feasibility/scripts/s4_hybrid_bitmatch.py [行列名 ...]
出力: gpu_feasibility/results/s4_hybrid_bitmatch.json

CPU の参照解は scipy の cg を 1 スレッドの行列ベクトル積で解く(製品の行ブロック並列とビット一致する経路)。
ここで出る CPU の秒は 07 と競合した 1 スレッドの参考値で、ベースラインではない。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import cupy as cp  # noqa: E402

import _gpucg  # noqa: E402
import _winmem  # noqa: E402

DATA = ROOT / "data"
RESULTS = ROOT / "results"
RTOL = 1e-9


def digest(u: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(u).tobytes()).hexdigest()[:16]


def solve(a_op, rhs: np.ndarray, diag: np.ndarray) -> tuple[np.ndarray, int, int, float]:
    """製品の `PreparedSystem.solve` と同じ呼び方(ヤコビは対角で割る、初期値なし)。"""
    iterations = 0

    def counter(_xk) -> None:
        nonlocal iterations
        iterations += 1

    precond = spla.LinearOperator(a_op.shape, matvec=lambda v, d=diag: v / d)
    t0 = time.perf_counter()
    u, info = spla.cg(a_op, rhs, M=precond, rtol=RTOL, callback=counter)
    return u, iterations, int(info), time.perf_counter() - t0


def run(name: str) -> dict:
    z = np.load(DATA / f"{name}.npz")
    data, indices, indptr, shape, rhs = z["data"], z["indices"], z["indptr"], z["shape"], z["rhs"]
    k_host = sp.csr_matrix((data, indices, indptr), shape=tuple(shape))
    diag = k_host.diagonal()
    n = int(shape[0])
    rec: dict = {"name": name, "free_dofs": n, "nnz": int(data.size),
                 "measured_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}

    system = _gpucg.GpuSystem(data, indices, indptr, shape)
    hybrid = _gpucg.HybridMatvec(system, "raw_nofma")
    probe = np.random.default_rng(1).standard_normal(n)
    rec["spmv_bit_identical"] = bool(np.array_equal(hybrid(probe), k_host @ probe))
    t0 = time.perf_counter()
    for _ in range(200):
        hybrid(probe)
    rec["hybrid_matvec_ms"] = (time.perf_counter() - t0) / 200 * 1e3

    a_gpu = spla.LinearOperator((n, n), matvec=hybrid, dtype=float)
    u_h, it_h, info_h, sec_h = solve(a_gpu, rhs, diag)
    u_h = u_h.copy()
    u_h2, _, _, sec_h2 = solve(a_gpu, rhs, diag)
    rec["hybrid"] = {"iterations": it_h, "info": info_h, "seconds": [sec_h, sec_h2],
                     "ms_per_iteration": min(sec_h, sec_h2) / it_h * 1e3, "sha": digest(u_h),
                     "repeat_bit_identical": bool(np.array_equal(u_h, u_h2)),
                     "relative_residual": float(np.linalg.norm(k_host @ u_h - rhs) / np.linalg.norm(rhs))}

    u_c, it_c, info_c, sec_c = solve(k_host, rhs, diag)
    rec["cpu_1thread"] = {"iterations": it_c, "info": info_c, "seconds_contended": sec_c,
                          "ms_per_iteration_contended": sec_c / it_c * 1e3, "sha": digest(u_c)}
    rec["solution_bit_identical"] = bool(np.array_equal(u_h, u_c))
    rec["max_abs_diff"] = float(np.max(np.abs(u_h - u_c)))
    del system, hybrid
    cp.get_default_memory_pool().free_all_blocks()
    return rec


def main() -> None:
    names = sys.argv[1:] or sorted(p.stem for p in DATA.glob("*.npz"))
    start = dt.datetime.now().astimezone()
    RESULTS.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS / "s4_hybrid_bitmatch.json"
    out = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    for name in names:
        rec = run(name)
        out[name] = rec
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{name}: dofs={rec['free_dofs']} spmvBit={rec['spmv_bit_identical']} "
              f"hybrid={min(rec['hybrid']['seconds']):.2f}s/{rec['hybrid']['iterations']}it "
              f"cpu1={rec['cpu_1thread']['seconds_contended']:.2f}s/{rec['cpu_1thread']['iterations']}it "
              f"bitIdentical={rec['solution_bit_identical']} maxAbsDiff={rec['max_abs_diff']:.3e}")
    _winmem.log_load_window(RESULTS, start, "s4_hybrid_bitmatch " + " ".join(names), cpu_threads=1)


if __name__ == "__main__":
    main()
