"""検証材料の採用マスクを製品の6条件でもう一度評価し、応力場から「目標応力を超えるセル」の数と場所を数える。

    python topopt_checks/measure_idle_hot_cells.py [条件名 ...]   (省略時は全64条件。結果は corpus_hot.jsonl に1条件1行で追記)
"""
import dataclasses
import json
import sys
from pathlib import Path
from unittest.mock import patch

REPO = Path(r"C:\Users\tm976\mf\duocad")
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: F401,E402
import numpy as np  # noqa: E402
from scipy import ndimage  # noqa: E402

from duocad import optimize_run  # noqa: E402
from duocad.topopt import acceptance, design_space as ds, stress  # noqa: E402
from _documents import open_session  # noqa: E402
from sweep_smoothing_corpus import MODEL, _set_load  # noqa: E402

CORPUS = REPO / "output" / "smoothing_corpus"
out = REPO / "output" / "idle_study" / "corpus_hot.jsonl"
out.parent.mkdir(parents=True, exist_ok=True)
done = {json.loads(l)["name"] for l in out.read_text(encoding="utf-8").splitlines()} if out.exists() else set()
rows = {r["name"]: r for r in json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))["rows"]}
names = sys.argv[1:] or sorted(rows, key=lambda n: -rows[n]["element_size_requested_mm"])
real_eval = stress.evaluate
captured = {}


def evaluate(*a, **k):
    rep = real_eval(*a, **k)
    captured["report"] = rep
    return rep


for name in names:
    if name in done:
        continue
    row = rows[name]
    data = np.load(CORPUS / "voxel" / f"{name}.npz")
    mask = data["mask"]
    doc = open_session(MODEL).document
    _set_load(doc, row["load_n"])
    spacing = float(data["spacing"])
    bundle = optimize_run.build_conditions(doc)
    space = ds.derive(bundle.conditions, spacing=spacing)
    assert tuple(space.grid.shape) == tuple(mask.shape), (space.grid.shape, mask.shape)
    opts = optimize_run.OptimizeOptions(element_size_mm=row["element_size_requested_mm"], min_width_mm=row["min_width"]["requested_mm"])
    table, mapping = optimize_run._map_boundary_conditions(space, bundle)
    conditions = optimize_run._build_search_conditions(doc, space, bundle, opts, table=table, mapping=mapping)
    conditions = dataclasses.replace(conditions, member_volume_mm3=None)  # この研究は材料全体の99.5%点(2026-10-09 より前の製品)を対象にする
    with patch.object(stress, "evaluate", evaluate):
        report = acceptance.inspect(mask, mask, space.grid, **conditions.inspect_kwargs(bbox_cache=None))
    safety = report["checks"]["safety"]
    rep = captured["report"]
    vm = np.asarray(rep.field.von_mises)
    keep = np.asarray(space.model.keep, bool)
    near = ndimage.binary_dilation(keep, iterations=2)
    target = safety["target_safety_factor"]
    thr = safety["allowable_mpa"] / target
    hot = mask & (vm > thr)
    n = int(mask.sum())
    def sf(sel):
        return float(safety["allowable_mpa"] / np.percentile(vm[sel], 99.5))
    rec = dict(name=name, load_n=row["load_n"], size=row["element_size_requested_mm"], cells=row["cells_requested"], n=n,
               sf=safety["safety_factor"], target=target, hot=int(hot.sum()), hot_keep=int((hot & keep).sum()),
               hot_near=int((hot & near & ~keep).sum()), hot_far=int((hot & ~near).sum()), half_pct_n=0.005 * n,
               keep_in_material=int((mask & keep).sum()), sf_excl_keep_r2=sf(mask & ~near),
               design_vm_median_over_rep=float(np.median(vm[mask & ~near]) / safety["representative_mpa"]))
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps(rec, ensure_ascii=False), flush=True)
