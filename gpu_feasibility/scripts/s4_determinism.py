"""段階4a: GPU だけで完結する CG が、同じ入力で毎回同じビットを返すかを確かめる。

実行(開発用の .venv。`uv sync --extra gpu` で CuPy を入れておく): .venv/Scripts/python.exe gpu_feasibility/scripts/s4_determinism.py <run_id> [行列名 ...]
出力: gpu_feasibility/results/s4_determinism.json の `<run_id>` の下

同一プロセス内で REPEATS 回解いて解のハッシュを並べる。プロセスをまたぐ一致は、`run_id` を変えて
もう一度実行し、ハッシュを突き合わせて見る。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import cupy as cp  # noqa: E402
import cupyx.scipy.sparse.linalg as cspla  # noqa: E402

import _gpucg  # noqa: E402

DATA = ROOT / "data"
RESULTS = ROOT / "results"
RTOL = 1e-9
REPEATS = int(os.environ.get("S4_REPEATS", "10"))


def digest(u) -> str:
    return hashlib.sha256(np.ascontiguousarray(cp.asnumpy(u)).tobytes()).hexdigest()[:16]


def run(name: str) -> dict:
    z = np.load(DATA / f"{name}.npz")
    system = _gpucg.GpuSystem(z["data"], z["indices"], z["indptr"], z["shape"])
    b = cp.asarray(z["rhs"])
    n = system.n
    rec: dict = {"free_dofs": n}

    probe = cp.asarray(np.random.default_rng(2).standard_normal(n))
    rec["spmv"] = {}
    for kind in ("cusparse", "raw", "raw_nofma"):
        apply = system.matvec(kind)
        hashes = {digest(apply(probe)) for _ in range(REPEATS)}
        rec["spmv"][kind] = sorted(hashes)

    precond = cspla.LinearOperator((n, n), matvec=lambda v: v * system.inv_diag, dtype=cp.float64)
    rec["cg"] = {"cupyx": [digest(cspla.cg(system.csr, b, M=precond, rtol=RTOL, maxiter=n * 10)[0])
                           for _ in range(REPEATS)]}
    for kind in ("cusparse", "raw", "raw_nofma"):
        rec["cg"][f"own_{kind}"] = [digest(_gpucg.cg_jacobi(system, b, rtol=RTOL, matvec_kind=kind)[0])
                                    for _ in range(REPEATS)]
    rec["cg_distinct"] = {k: len(set(v)) for k, v in rec["cg"].items()}
    rec["spmv_distinct"] = {k: len(v) for k, v in rec["spmv"].items()}
    del system, b
    cp.get_default_memory_pool().free_all_blocks()
    return rec


def main() -> None:
    run_id = sys.argv[1]
    names = sys.argv[2:] or sorted(p.stem for p in DATA.glob("*.npz"))
    RESULTS.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS / "s4_determinism.json"
    out = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    block = out.setdefault(run_id, {"measured_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                                    "repeats": REPEATS, "matrices": {}})
    for name in names:
        rec = run(name)
        block["matrices"][name] = rec
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{name}: spmv distinct={rec['spmv_distinct']} cg distinct={rec['cg_distinct']}")


if __name__ == "__main__":
    main()
