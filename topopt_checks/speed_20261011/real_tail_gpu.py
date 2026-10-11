import sys, json, time
from pathlib import Path
sys.path.insert(0, r"C:\Users\tm976\mf\duocad"); sys.path.insert(0, str(Path(__file__).parent))
import numpy as np, scipy.sparse.linalg as spla
from duocad.topopt import fem, gpu_cg
import gpu_cg_fast
R = Path(r"C:\Users\tm976\mf\duocad\output\optimization_runs\20261011T093810-13bab2bbaf4d")
files = sorted(p for p in R.iterdir() if p.name.endswith("-stress-input.json"))
def arr(ref): a=np.load(R/ref["$array"]); return a[a.files[0]]
threads = gpu_cg.openblas_threads()
for idx in (int(v) for v in sys.argv[1:]):
    meta = json.loads(files[idx-1].read_text(encoding="utf-8")); md = meta["model"]
    shape=(md["nelx"],md["nely"],md["nelz"]); act=np.zeros(np.prod(shape),bool); act[arr(md["active"])]=True
    m = fem.Model(*shape, fixed_dofs=arr(md["fixed"]), load=arr(md["f"]), active_mask=act.reshape(shape))
    rho = arr(meta["rho"])
    kf = m.assemble(rho, 3.0)[m.free,:][:,m.free].tocsc(); ff = m.f[m.free]
    t=time.perf_counter(); u0,it0,_=gpu_cg._cg(gpu_cg._as_csr(kf), ff, threads, 1e-9); t0=time.perf_counter()-t
    t=time.perf_counter(); u1,it1,_=gpu_cg_fast.cg(kf, ff, threads, 1e-9, check_every=32, use_graph=True); t1=time.perf_counter()-t
    t=time.perf_counter(); u2=spla.spsolve(kf, ff); t2=time.perf_counter()-t
    print(idx, "dofs", kf.shape[0], "nnz/dof", round(kf.nnz/kf.shape[0],1), "iters", it0, "old", round(t0,2), "fast", round(t1,2), "equal", np.array_equal(u0,u1) and it0==it1, "spsolve", round(t2,2), flush=True)
    gpu_cg._release()
