"""段階5d: 演算の順序と丸めを CPU に合わせた GPU の CG が、CPU の CG とビット一致するかを確かめ、秒を測る。

実行(開発用の .venv。`uv sync --extra gpu` で CuPy を入れておく): .venv/Scripts/python.exe gpu_feasibility/scripts/s5_bitexact_cg.py [行列名 ...]
出力: gpu_feasibility/results/s5_bitexact_cg.json

CPU の解は解き直さない。`s4_hybrid_bitmatch.json` に残した CPU の解のハッシュと突き合わせる
(CPU の解が無い行列は、CPU とビット一致を確認済みの方式〈行列ベクトル積だけ GPU〉の解を `data/<名前>_u.npy` から読む)。
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

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import cupy as cp  # noqa: E402

import _blasdot  # noqa: E402
import _gpucg  # noqa: E402

DATA = ROOT / "data"
RESULTS = ROOT / "results"
RTOL = 1e-9
CPU_NS_PER_NNZ_ITER = (0.41, 0.48)


def digest(u: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(u).tobytes()).hexdigest()[:16]


def run(name: str, reference: dict, threads: int) -> dict:
    z = np.load(DATA / f"{name}.npz")
    data, indices, indptr, shape, rhs = z["data"], z["indices"], z["indptr"], z["shape"], z["rhs"]
    n, nnz = int(shape[0]), int(data.size)
    k_host = sp.csr_matrix((data, indices, indptr), shape=tuple(shape))
    rec: dict = {"name": name, "free_dofs": n, "nnz": nnz, "openblas_threads": threads,
                 "measured_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    ref = reference.get(name, {})
    if "cpu_1thread" in ref:
        rec["reference"] = "cpu_1thread"
        ref_sha, ref_iterations = ref["cpu_1thread"]["sha"], ref["cpu_1thread"]["iterations"]
    else:
        rec["reference"] = "hybrid_solution_file"
        ref_sha, ref_iterations = digest(np.load(DATA / f"{name}_u.npy")), None

    rec["dot_self_test"] = _blasdot.self_test(n, threads)
    # 準備の秒: ホストの CSR と右辺を送り、列優先の並びと内積の区間表を作るまで(探索では solve のたびにかかる)。
    cp.cuda.Stream.null.synchronize()
    t0 = time.perf_counter()
    dot = _blasdot.BlasDot(n, threads)
    system = _gpucg.GpuSystem(data, indices, indptr, shape)
    b = cp.asarray(rhs)
    system.matvec("ell_nofma")
    cp.cuda.Stream.null.synchronize()
    rec["setup_seconds_ell"] = time.perf_counter() - t0
    probe = np.random.default_rng(3).standard_normal(n)
    y_host = k_host @ probe
    rec["variants"] = {}
    for kind in ("raw_nofma", "ell_nofma"):
        apply = system.matvec(kind)
        y = cp.asnumpy(apply(cp.asarray(probe)))
        _gpucg.cg_bitexact(system, b, dot, rtol=RTOL, matvec_kind=kind, maxiter=3)
        cp.cuda.Stream.null.synchronize()
        t0 = time.perf_counter()
        u_gpu, iterations, info = _gpucg.cg_bitexact(system, b, dot, rtol=RTOL, matvec_kind=kind)
        u = cp.asnumpy(u_gpu)
        seconds = time.perf_counter() - t0
        lo, hi = (c * 1e-9 * nnz * iterations for c in CPU_NS_PER_NNZ_ITER)
        rec["variants"][kind] = {
            "spmv_bit_identical_to_cpu": bool(np.array_equal(y, y_host)),
            "iterations": int(iterations), "info": int(info), "seconds": seconds,
            "ms_per_iteration": seconds / max(iterations, 1) * 1e3,
            "sha": digest(u), "solution_bit_identical_to_cpu": digest(u) == ref_sha,
            "iterations_match_cpu": None if ref_iterations is None else iterations == ref_iterations,
            "relative_residual_host": float(np.linalg.norm(k_host @ u - rhs) / np.linalg.norm(rhs)),
            "cpu_seconds_converted": [lo, hi], "speedup_converted": [lo / seconds, hi / seconds],
        }
    rec["ell_width"] = getattr(system, "ell_width", None)
    rec["vram_pool_total_mib"] = cp.get_default_memory_pool().total_bytes() / 2**20
    del system, b, u_gpu
    cp.get_default_memory_pool().free_all_blocks()
    return rec


def main() -> None:
    names = sys.argv[1:] or sorted(p.stem for p in DATA.glob("*.npz"))
    threads = _blasdot.openblas_threads()
    reference = json.loads((RESULTS / "s4_hybrid_bitmatch.json").read_text(encoding="utf-8"))
    out_path = RESULTS / "s5_bitexact_cg.json"
    out = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    for name in names:
        rec = run(name, reference, threads)
        out[name] = rec
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{name}: dofs={rec['free_dofs']} dotSelfTest={rec['dot_self_test']} ref={rec['reference']} "
              + " ".join(f"{k}: {v['seconds']:.2f}s/{v['iterations']}it bit={v['solution_bit_identical_to_cpu']} "
                         f"x{v['speedup_converted'][0]:.1f}-{v['speedup_converted'][1]:.1f}"
                         for k, v in rec["variants"].items()))


if __name__ == "__main__":
    main()
