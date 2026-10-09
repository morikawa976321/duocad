"""段階11b(#104): AMG の設定(平滑化・強さの閾値・延長の平滑化)を 1 行列で比べる。結果は標準出力だけで、数値は CONCLUSION.md に写す。

実行(s11 と同じ Python 3.13 の venv): <venv>/Scripts/python.exe gpu_feasibility/scripts/s11b_amg_variants.py F2000_thin5mm_h2_step20_d0
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pyamg  # noqa: E402
import scipy.sparse as sp  # noqa: E402
import scipy.sparse.linalg as spla  # noqa: E402

from _cpucg import THREADS, BlockedMatvec, load  # noqa: E402
from s11_amg_precond import near_nullspace  # noqa: E402

CHEB = ("chebyshev", {"degree": 2})
VARIANTS = {
    "default_bgs_sym": {},
    "block_jacobi": dict(presmoother=("block_jacobi", {"omega": 0.5, "iterations": 1}),
                         postsmoother=("block_jacobi", {"omega": 0.5, "iterations": 1})),
    "jacobi2": dict(presmoother=("jacobi", {"omega": 0.5, "iterations": 2}),
                    postsmoother=("jacobi", {"omega": 0.5, "iterations": 2})),
    "chebyshev": dict(presmoother=CHEB, postsmoother=CHEB),
    "bgs_sym_nosmoothP": dict(smooth=None),
    "bgs_sym_theta0.02": dict(strength=("symmetric", {"theta": 0.02})),
    "cheb_theta0.02": dict(strength=("symmetric", {"theta": 0.02}), presmoother=CHEB, postsmoother=CHEB),
    "bgs_theta0.05": dict(strength=("symmetric", {"theta": 0.05})),
    "bgs_theta0.1": dict(strength=("symmetric", {"theta": 0.1})),
}


def main() -> None:
    name = sys.argv[1]
    k, rhs = load(name)
    b = near_nullspace(name, True)
    a_op = spla.LinearOperator(k.shape, matvec=BlockedMatvec(k, THREADS), dtype=float)
    for label, kw in VARIANTS.items():
        t0 = time.perf_counter()
        ml = pyamg.smoothed_aggregation_solver(sp.bsr_matrix(k, blocksize=(3, 3)), B=b, symmetry="hermitian", **kw)
        m = ml.aspreconditioner(cycle="V")
        t1 = time.perf_counter()
        it = [0]
        _u, info = spla.cg(a_op, rhs, M=m, rtol=1e-9, callback=lambda _x: it.__setitem__(0, it[0] + 1))
        t2 = time.perf_counter()
        print(f"{label:20s} setup {t1-t0:5.2f} solve {t2-t1:6.2f} total {t2-t0:6.2f} it {it[0]:4d} info {info} "
              f"oc {ml.operator_complexity():.2f}", flush=True)


if __name__ == "__main__":
    main()
