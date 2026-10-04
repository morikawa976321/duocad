"""200 N・w5 の 2.0 mm の探索で、2.2 mm のトラスにしか無い部分(斜めの部材)がどの評価で消えたかを追う。

    python topopt_checks/measure_idle_divergence.py <記録付きで回した実行の trace フォルダ>
"""
import json, os, sys
import numpy as np
from pathlib import Path

C = Path(r"C:\Users\tm976\mf\duocad\output\smoothing_corpus\voxel")
run = Path(sys.argv[1])
s, t = np.load(C / "F0200_h2.2_w5.npz"), np.load(C / "F0200_h2.0_w5.npz")
shape = t["mask"].shape
idx = np.indices(shape).reshape(3, -1).T
w = t["origin"] + (idx + 0.5) * float(t["spacing"])
j = np.floor((w - s["origin"]) / float(s["spacing"])).astype(int)
ok = np.all((j >= 0) & (j < np.array(s["mask"].shape)), axis=1)
m = np.zeros(len(idx), bool); jj = j[ok]; m[ok] = s["mask"][jj[:, 0], jj[:, 1], jj[:, 2]]
truss = m.reshape(shape)
final = t["mask"]
D = truss & ~final          # トラスにだけある部分
V = final & ~truss          # V字にだけある部分
print("truss cells", truss.sum(), "V-final cells", final.sum(), "D(truss only)", D.sum(), "V only", V.sum())
files = sorted(os.listdir(run))
def load(ref):
    z = np.load(run / ref["$array"]); return z[z.files[0]]
ins = [f for f in files if f.endswith("stress-input.json")]
outs = [f for f in files if f.endswith("stress-output.json")]
prev = None
for i, (fi, fo) in enumerate(zip(ins, outs)):
    rho = load(json.load(open(run / fi))["rho"]) > 0.5
    so = json.load(open(run / fo))
    n = int(rho.sum())
    step = "" if prev is None else f"-{(prev - n) / prev:.1%}"
    print(f"{i:>3} N {n:>7} {step:>7} SF {so['safety_factor']:7.2f}  D残 {rho[D].mean():6.1%}  V残 {rho[V].mean():6.1%}  truss残 {rho[truss].mean():6.1%}")
    prev = n
