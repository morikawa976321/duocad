"""探索の最後の合格形で、応力の低い(仕事をしていない)材料がどこに残り、なぜ削られなかったかを調べる(研究用)。

`run_product.py --trace` が残した `_accepted.npz` の最後の形を、製品と同じ格子・条件で解き直し、
セルごとの von Mises ÷ 探索の上限(許容 ÷ 探索の目標)を出す。部材の太さ程度の近傍で最大を取って 0.3 未満の材料を
「低応力の塊」とし、その表面セルを1つずつ `peel` に通して、幾何の規則でどれだけ抜けないかを数える。

    .venv\\Scripts\\python.exe topopt_checks\\stress_gate\\leftover_analysis.py --load 150 --h 3.29 --w 9.86
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import phase_a_fields as pa  # noqa: E402

import numpy as np  # noqa: E402
from scipy import ndimage  # noqa: E402

import _fem_probe  # noqa: E402
import refine_shape  # noqa: E402
from duocad import optimize_run as orun  # noqa: E402
from duocad.topopt import acceptance, binary_search as bs, design_space as ds  # noqa: E402

IDLE_RATIO = 0.3
VERDICTS = ("removable", "skipped_neck", "skipped_connectivity", "skipped_manifold", "skipped_core", "skipped_support",
            "skipped_surface", "none")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--load", type=float, required=True)
    ap.add_argument("--h", type=float, required=True)
    ap.add_argument("--w", type=float, required=True)
    ap.add_argument("--index", type=int, default=-1, help="合格形の列の何番目を調べるか(既定は最後)")
    ap.add_argument("--max-probe", type=int, default=4000, help="peel に通す表面セルの上限(多ければ等間隔に間引く)")
    a = ap.parse_args()
    spec = dict(pa.MODELS["hanger"], load_n=a.load, h=a.h, w=a.w)
    tag = f"hanger_L{a.load:g}_h{a.h:g}_w{a.w:g}"
    doc = refine_shape.open_session(spec["model"]).document
    refine_shape._set_load_doc(doc, a.load)
    opts = orun.OptimizeOptions(element_size_mm=a.h, min_width_mm=a.w)
    bundle = orun.build_conditions(doc)
    limits = orun.resolution_limits(doc)
    spacing = orun._snap_to_grid(limits["bbox_min"], limits["bbox_max"], orun._resolve_element_size(limits, a.h))
    min_width = orun._resolve_min_width(
        spacing, a.w, max_cells=min(orun._grid_shape(limits["bbox_min"], limits["bbox_max"], spacing)))
    m = int(min_width["cells"])
    space = ds.derive(bundle.conditions, spacing=spacing)
    table, mapping = orun._map_boundary_conditions(space, bundle)
    cond = orun._build_search_conditions(doc, space, bundle, opts, table=table, mapping=mapping)

    with np.load(pa.OUT / "product" / f"{tag}_accepted.npz") as z:
        shape = tuple(int(v) for v in z["shape"])
        packed = z["packed"][a.index]
        sf_recorded = float(z["safety_factor"][a.index])
        count = len(z["packed"])
    mask = np.unpackbits(packed, count=int(np.prod(shape))).astype(bool).reshape(shape)
    assert shape == tuple(space.grid.shape), (shape, space.grid.shape)

    probe = _fem_probe.evaluate_full(mask, space.grid, cond.table, cond.fixed_faces, cond.load_faces, cond.forces,
                                     allowable_mpa=cond.allowable_mpa, youngs_modulus_mpa=cond.youngs_modulus_mpa,
                                     solver=cond.solver)
    vm = np.nan_to_num(np.asarray(probe["von_mises_mpa"], float), nan=0.0)
    limit = cond.allowable_mpa / cond.target_safety_factor
    u = np.where(mask, vm / limit, 0.0)
    keep = np.asarray(cond.keep_cells if cond.keep_cells is not None else np.zeros(shape, bool))
    region = acceptance.member_region(mask, cond.table, cond.fixed_faces, cond.load_faces, keep)
    ordered = np.sort(vm[region])[::-1]
    sf_member = cond.allowable_mpa / ordered[1]
    hot = np.argwhere(region & (vm >= ordered[1]))
    origin = np.asarray(space.grid.origin, float)
    to_mm = lambda idx: (origin + (np.asarray(idx, float) + 0.5) * spacing).round(1).tolist()

    free = mask & ~keep
    reach = max(1, m)  # 部材の断面程度
    local = ndimage.maximum_filter(u, size=2 * reach + 1)
    idle = free & (local < IDLE_RATIO)
    labels, n = ndimage.label(idle, structure=np.ones((3, 3, 3), bool))
    blobs = []
    for k in range(1, n + 1):
        cells = labels == k
        size = int(cells.sum())
        if size < m ** 3:
            continue
        blobs.append(dict(cells=size, mm3=round(size * spacing ** 3), centroid_mm=to_mm(np.argwhere(cells).mean(0)),
                          bbox_mm=[to_mm(np.argwhere(cells).min(0)), to_mm(np.argwhere(cells).max(0))],
                          touches_keep=bool((ndimage.binary_dilation(cells) & keep).any())))
    blobs.sort(key=lambda b: -b["cells"])

    hist = {f"<{t}": round(float((u[free] < t).mean()), 3) for t in (0.1, 0.2, 0.3, 0.5, 0.8)}

    # 低応力の塊の表面セルを、1セルずつ peel に通す(最後の形が親。毒の記録は無いので、安全率で外したセルは分からない)。
    anchor_ids = bs._anchor_node_ids(cond)
    exempt = bs._min_width_exempt(cond, shape, m)
    protected = bs._keep_cells(cond, shape)
    padded = np.pad(mask, 1)
    empty_neighbour = np.zeros(shape, bool)
    for axis in range(3):
        for d in (-1, 1):
            empty_neighbour |= ~np.roll(padded, d, axis=axis)[1:-1, 1:-1, 1:-1]
    surface = idle & empty_neighbour
    cells = np.flatnonzero(surface)
    if len(cells) > a.max_probe:
        cells = cells[np.linspace(0, len(cells) - 1, a.max_probe).astype(int)]
    walk = bs.PeelWalk(mask, anchor_ids, min_width_cells=m, exempt=exempt, protected=protected)
    reasons = Counter()
    bites = []
    verdict = np.zeros(shape, np.int8)  # VERDICTS の添字 + 1(0 は調べていない)
    for cell in cells:
        _, stats = walk.take(np.array([cell]), 1)
        if stats.picked:
            name = "removable"
            bites.append(stats.casualties + 1 + stats.islands)
        else:
            hit = [k for k, v in stats.as_dict().items() if k.startswith("skipped_") and v]
            name = hit[0] if hit else "none"
        reasons[name] += 1
        verdict.ravel()[cell] = VERDICTS.index(name) + 1
    out = dict(tag=tag, index=a.index, accepted_shapes=count, spacing_mm=spacing, min_width_cells=m,
               material_cells=int(mask.sum()), keep_cells=int((mask & keep).sum()),
               sf_recorded=sf_recorded, sf_member_q1=float(sf_member), limit_mpa=limit,
               hot_cells_mm=[to_mm(c) for c in hot[:4]],
               u_fraction_below=hist,
               idle_cells=int(idle.sum()), idle_fraction_of_free=round(float(idle.sum() / max(1, free.sum())), 3),
               idle_blobs=blobs[:8],
               idle_surface_cells=int(surface.sum()), probed=len(cells), peel_verdicts=dict(reasons),
               removable_bite_cells=dict(p50=float(np.median(bites)) if bites else None,
                                         max=int(max(bites)) if bites else None))
    print(json.dumps(out, ensure_ascii=False, indent=1))
    (pa.OUT / "leftover").mkdir(parents=True, exist_ok=True)
    np.savez_compressed(pa.OUT / "leftover" / f"{tag}_{a.index}.npz", mask=mask, u=u.astype(np.float32), keep=keep,
                        idle=idle, verdict=verdict, origin=origin, spacing=spacing)
    (pa.OUT / "leftover" / f"{tag}_{a.index}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                                             encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
