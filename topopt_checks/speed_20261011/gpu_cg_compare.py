r"""実行記録の K_ff で、旧版(引数のフォルダのソース)と今の `gpu_cg._cg` の秒とビット一致を比べる。今の版は1回に流す反復数も変えて測る。
usage: .venv\Scripts\python.exe topopt_checks\speed_20261011\gpu_cg_compare.py <旧版の duocad ルート> 30 100 184
"""
import importlib.util
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, r"C:\Users\tm976\mf\duocad")
import numpy as np

from duocad.topopt import fem, gpu_cg

RUN = Path(r"C:\Users\tm976\mf\duocad\output\optimization_runs\20261011T093810-13bab2bbaf4d")
spec = importlib.util.spec_from_file_location("old_gpu_cg", Path(sys.argv[1]) / "duocad" / "topopt" / "gpu_cg.py")
old = importlib.util.module_from_spec(spec)
spec.loader.exec_module(old)
files = sorted(p for p in RUN.iterdir() if p.name.endswith("-stress-input.json"))


def arr(ref):
    a = np.load(RUN / ref["$array"])
    return a[a.files[0]]


threads = gpu_cg.openblas_threads()
for idx in (int(v) for v in sys.argv[2:]):
    meta = json.loads(files[idx - 1].read_text(encoding="utf-8"))
    md = meta["model"]
    shape = (md["nelx"], md["nely"], md["nelz"])
    act = np.zeros(int(np.prod(shape)), bool)
    act[arr(md["active"])] = True
    m = fem.Model(*shape, fixed_dofs=arr(md["fixed"]), load=arr(md["f"]), active_mask=act.reshape(shape))
    kf = gpu_cg._as_csr(m.assemble(arr(meta["rho"]), 3.0)[m.free, :][:, m.free])
    ff = m.f[m.free]
    row = [idx, kf.shape[0]]
    for rep in range(2):
        t = time.perf_counter()
        u0, it0, _ = old._cg(kf, ff, threads, 1e-9)
        old._release()
        row.append(("old", round(time.perf_counter() - t, 2), it0))
        for per_sync in (8, 32, 128):
            gpu_cg._ITERATIONS_PER_SYNC = per_sync
            t = time.perf_counter()
            u1, it1, _ = gpu_cg._cg(kf, ff, threads, 1e-9)
            gpu_cg._release()
            row.append((per_sync, round(time.perf_counter() - t, 2), np.array_equal(u0, u1) and it0 == it1))
    print(row, flush=True)
