"""別の格子で得た採用マスクを写して、この格子の6条件で評価する。 python topopt_checks/measure_idle_transplant.py <元条件> <先条件> [太らせるセル数]"""
import dataclasses
import sys, json, numpy as np
from pathlib import Path
REPO = Path(r"C:\Users\tm976\mf\duocad"); sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa
from duocad import optimize_run
from duocad.topopt import acceptance, design_space as ds, binary_search
from _documents import open_session
from sweep_smoothing_corpus import MODEL, _set_load
C = REPO / "output/smoothing_corpus"
rows = {r["name"]: r for r in json.loads((C / "manifest.json").read_text(encoding="utf-8"))["rows"]}
src, dst = sys.argv[1], sys.argv[2]
s, t = np.load(C / "voxel" / f"{src}.npz"), np.load(C / "voxel" / f"{dst}.npz")
row = rows[dst]
doc = open_session(MODEL).document; _set_load(doc, row["load_n"])
bundle = optimize_run.build_conditions(doc)
space = ds.derive(bundle.conditions, spacing=float(t["spacing"]))
opts = optimize_run.OptimizeOptions(element_size_mm=row["element_size_requested_mm"], min_width_mm=row["min_width"]["requested_mm"])
table, mapping = optimize_run._map_boundary_conditions(space, bundle)
cond = optimize_run._build_search_conditions(doc, space, bundle, opts, table=table, mapping=mapping)
cond = dataclasses.replace(cond, member_volume_mm3=None)  # この研究は材料全体の99.5%点(2026-10-09 より前の製品)を対象にする
# 先の格子のセル中心 → 元の格子のセル
idx = np.indices(space.grid.shape).reshape(3, -1).T
w = np.asarray(space.grid.origin) + (idx + 0.5) * space.grid.spacing
j = np.floor((w - s["origin"]) / float(s["spacing"])).astype(int)
ok = np.all((j >= 0) & (j < np.array(s["mask"].shape)), axis=1)
m = np.zeros(len(idx), bool); jj = j[ok]; m[ok] = s["mask"][jj[:, 0], jj[:, 1], jj[:, 2]]
mask = m.reshape(space.grid.shape) & space.mask
from scipy import ndimage
DILATE = int(sys.argv[3]) if len(sys.argv) > 3 else 0
if DILATE:
    mask = ndimage.binary_dilation(mask, iterations=DILATE) & space.mask
keep = np.asarray(cond.keep_cells, bool) if getattr(cond, "keep_cells", None) is not None else np.zeros_like(mask)
mask |= keep & space.mask
survivors, diag = binary_search.resolve_islands(mask, cond)
print("islands", {k: v for k, v in diag.items() if k != "removed_islands"}, "removed", [i["cells"] for i in diag["removed_islands"]][:10])
if survivors:
    mask = max(survivors, key=lambda c: c.sum())
rep = acceptance.inspect(mask, mask, space.grid, **cond.inspect_kwargs(bbox_cache=None))
checks = {k: v.get("status") for k, v in rep["checks"].items()}
cell_mm3 = space.grid.spacing ** 3
print(json.dumps(dict(src=src, dst=dst, cells=int(mask.sum()), material_cm3=round(mask.sum() * cell_mm3 / 1000, 1),
      dst_material_cm3=round(row["material_mm3"] / 1000, 1), checks=checks,
      sf=rep["checks"]["safety"].get("safety_factor"), target=rep["checks"]["safety"].get("target_safety_factor")), ensure_ascii=False))
