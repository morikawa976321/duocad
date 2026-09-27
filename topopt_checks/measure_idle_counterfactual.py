"""(4)の原因検証: 代表応力の母集団から保持セルとその周り R セルを除いた版で探索を回し、材料の量と応力分布を比べる。

    python topopt_checks/measure_idle_counterfactual.py <荷重N> <要素サイズmm> <最小幅セル> <R(-1 なら差し替えない=製品どおり)> <出力名>

環境変数 MAX_STEP で1回に削る割合の上限(`binary_search.DEFAULT_MAX_STEP_RATIO`、既定 0.2)も差し替えられる。
製品コードは変えない(このプロセスの中だけで stress.representative_stress を包む)。探索の後で止める(平滑化・finalize は回さない)。
"""
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

REPO = Path(r"C:\Users\tm976\mf\duocad")
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: F401,E402
import numpy as np  # noqa: E402
from scipy import ndimage  # noqa: E402

from duocad import optimize_run  # noqa: E402
from duocad.topopt import binary_search, design_space as ds, mesh, stress, trace  # noqa: E402
from _documents import open_session  # noqa: E402
from sweep_smoothing_corpus import MODEL, _set_load  # noqa: E402

import os
MAX_STEP = float(os.environ["MAX_STEP"]) if os.environ.get("MAX_STEP") else None  # 1回に削る割合の上限を差し替える(既定 0.2)
load_n, size, cells, radius, name = int(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
out = REPO / "output" / "idle_study"
out.mkdir(parents=True, exist_ok=True)
captured = {}
real_derive, real_rep, real_search = ds.derive, stress.representative_stress, binary_search.find_best_feasible
history = []


class Stop(Exception):
    pass


def derive(*a, **k):
    space = real_derive(*a, **k)
    captured["space"] = space
    if radius >= 0:
        keep = np.asarray(space.model.keep, dtype=bool)
        captured["exclude"] = ndimage.binary_dilation(keep, iterations=radius) if radius > 0 else keep
    return space


def rep(field, rho, **k):
    full = real_rep(field, rho, **k)
    if "exclude" in captured:
        density = np.array(rho, dtype=float, copy=True)
        density[captured["exclude"]] = 0.0
        patched = real_rep(field, density, **k)
    else:
        patched = full
    history.append(dict(n=int((np.asarray(rho) >= 0.5).sum()), full=full.value, used=patched.value))
    return patched


def search(*a, **k):
    if MAX_STEP is not None:
        k["max_step_ratio"] = MAX_STEP
    t0 = time.perf_counter()
    r = real_search(*a, **k)
    captured["search"], captured["seconds"] = r, time.perf_counter() - t0
    raise Stop()


with patch.object(trace, "DEFAULT_ROOT", out / "_trace"), patch.object(ds, "derive", derive), \
        patch.object(stress, "representative_stress", rep), patch.object(binary_search, "find_best_feasible", search):
    doc = open_session(MODEL).document
    _set_load(doc, load_n)
    limits = optimize_run.resolution_limits(doc)
    spacing = optimize_run._snap_to_grid(limits["bbox_min"], limits["bbox_max"], optimize_run._resolve_element_size(limits, size))
    try:
        optimize_run.run(doc, optimize_run.OptimizeOptions(element_size_mm=size, min_width_mm=cells * spacing))
    except Stop:
        pass

r = captured["search"]
best = r.best_feasible
space = captured["space"]
rec = dict(max_step_ratio=MAX_STEP, name=name, load_n=load_n, size=size, cells=cells, radius=radius, status=r.status, termination=r.termination,
           evaluations=r.evaluations, seconds=captured["seconds"], spacing=float(space.grid.spacing),
           material_mm3=None if best is None else float(best.material_mm3),
           cells_final=None if best is None else int(best.mask.sum()),
           safety_factor_used=None if best is None else float(best.safety_factor), history=history)
if best is not None:
    np.savez_compressed(out / f"{name}.npz", mask=best.mask, keep=np.asarray(space.model.keep, bool),
                        origin=np.asarray(space.grid.origin, float), spacing=float(space.grid.spacing))
(out / f"{name}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps({k: v for k, v in rec.items() if k != "history"}, ensure_ascii=False))
