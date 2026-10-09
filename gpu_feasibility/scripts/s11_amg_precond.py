"""段階11(#104): 前処理をヤコビから剛体モード付きの平滑化集約 AMG(pyamg)に替えると、CPU の CG がどれだけ速くなるか。

実行(Python 3.13・numpy 2.4.6・scipy 1.17.1・pyamg 5.3.0 の venv。pyamg 5.3.0 には cp314 の Windows wheel が無い):
    <venv>/Scripts/python.exe gpu_feasibility/scripts/s11_amg_precond.py [行列名 ...]
出力: gpu_feasibility/results/s11_amg_precond.json

同じ行列・右辺・停止条件(scipy の cg、rtol 1e-9)で、次の 3 つを比べる。CG の行列ベクトル積は製品と同じく
`PARALLEL_THREADS` 本の行ブロックで並列化し、AMG の V サイクル(pyamg の C++)は 1 スレッドで動く。
- jacobi: 製品の現行(対角の逆数)
- sa_rigid: 平滑化集約 AMG、近零空間に剛体モード 6 本(並進 3・回転 3)、3×3 ブロック
- sa_trans: 同じ AMG で近零空間を並進 3 本だけにしたもの(剛体モードの寄与を見るため)
AMG は解のビットが現行と一致しないので、解の差(相対 2 ノルム)とコンプライアンス(右辺・解)の相対差も残す。
"""
from __future__ import annotations

import datetime as dt
import json
import platform
import sys
import time
from pathlib import Path

import numpy as np
import pyamg
import scipy
import scipy.sparse as sp
import scipy.sparse.linalg as spla

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import _winmem  # noqa: E402
from _cpucg import RTOL, THREADS, BlockedMatvec, cpu_cg, load  # noqa: E402

DATA = ROOT / "data"
RESULTS = ROOT / "results"


def near_nullspace(name: str, rotations: bool) -> np.ndarray:
    """自由な自由度ごとの剛体モード。節点番号は fem.py `_build_edof` の `ix*ny1*nz1 + iy*nz1 + iz`。"""
    z = np.load(DATA / f"{name}.npz")
    free, (_nelx, nely, nelz) = z["free"], z["mask"].shape
    ny1, nz1 = nely + 1, nelz + 1
    node = free[0::3] // 3
    xyz = np.stack([node // (ny1 * nz1), (node // nz1) % ny1, node % nz1], axis=1).astype(float)
    xyz -= xyz.mean(axis=0)
    m = node.size
    b = np.zeros((3 * m, 6 if rotations else 3))
    for axis in range(3):
        b[axis::3, axis] = 1.0
    if rotations:
        x, y, w = xyz[:, 0], xyz[:, 1], xyz[:, 2]
        b[1::3, 3], b[2::3, 3] = -w, y   # x 軸まわり
        b[0::3, 4], b[2::3, 4] = w, -x   # y 軸まわり
        b[0::3, 5], b[1::3, 5] = -y, x   # z 軸まわり
    return b


def amg_cg(k: sp.csr_matrix, rhs: np.ndarray, b: np.ndarray, threads: int) -> tuple[dict, np.ndarray]:
    t0 = time.perf_counter()
    ml = pyamg.smoothed_aggregation_solver(sp.bsr_matrix(k, blocksize=(3, 3)), B=b, symmetry="hermitian")
    precond = ml.aspreconditioner(cycle="V")
    a_op = spla.LinearOperator(k.shape, matvec=BlockedMatvec(k, threads), dtype=float)
    setup = time.perf_counter() - t0
    iterations = 0

    def counter(_x: np.ndarray) -> None:
        nonlocal iterations
        iterations += 1

    t1 = time.perf_counter()
    u, info = spla.cg(a_op, rhs, M=precond, rtol=RTOL, callback=counter)
    seconds = time.perf_counter() - t1
    return {"setup_seconds": setup, "seconds": seconds, "total_seconds": setup + seconds,
            "iterations": iterations, "info": int(info), "levels": len(ml.levels),
            "operator_complexity": float(ml.operator_complexity()),
            "relative_residual": float(np.linalg.norm(k @ u - rhs) / np.linalg.norm(rhs))}, u


def main() -> None:
    names = sys.argv[1:] or sorted(p.stem for p in DATA.glob("*.npz"))
    out_path = RESULTS / "s11_amg_precond.json"
    out = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    out["_env"] = {"python": platform.python_version(), "numpy": np.__version__, "scipy": scipy.__version__,
                   "pyamg": pyamg.__version__, "spmv_threads": THREADS}
    for name in names:
        k, rhs = load(name)
        jac, u_ref = cpu_cg(k, rhs, THREADS)
        jac["total_seconds"] = jac["setup_seconds"] + jac["seconds"]
        rec = {"free_dofs": int(k.shape[0]), "nnz": int(k.nnz), "jacobi": jac}
        c_ref = float(rhs @ u_ref)
        for label, rot in (("sa_rigid", True), ("sa_trans", False)):
            r, u = amg_cg(k, rhs, near_nullspace(name, rot), THREADS)
            r["solution_rel_diff_vs_jacobi"] = float(np.linalg.norm(u - u_ref) / np.linalg.norm(u_ref))
            r["compliance_rel_diff_vs_jacobi"] = float(abs(rhs @ u - c_ref) / abs(c_ref))
            r["speedup_vs_jacobi"] = jac["total_seconds"] / r["total_seconds"]
            rec[label] = r
        rec["peak_working_set_gb"] = _winmem.peak_working_set_gb()
        rec["measured_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        out[name] = rec
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{name}: n={rec['free_dofs']} jacobi {jac['total_seconds']:.2f}s/{jac['iterations']}it | "
              + " | ".join(f"{lb} {rec[lb]['setup_seconds']:.2f}+{rec[lb]['seconds']:.2f}s/{rec[lb]['iterations']}it "
                           f"x{rec[lb]['speedup_vs_jacobi']:.1f} du={rec[lb]['solution_rel_diff_vs_jacobi']:.1e}"
                           for lb in ("sa_rigid", "sa_trans")), flush=True)


if __name__ == "__main__":
    main()
