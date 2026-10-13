"""最後の合格形から、指定した箱の中の材料(部材の一区間)をまとめて抜いたら合格するかを、製品と同じ条件で確かめる(研究用)。

輪をなす低応力の部材は、1セルずつの手(巻き添え込み)ではくびれの判定に止められて切れない(`leftover_analysis.py`)。
部材を断面ごと切った形が6条件に通るなら、切れないのは探索の手の作り方のせいで、形の性能のせいではない。
抜いた後、m 立方に収まらなくなった材料と、本体から離れた島も消す(保持セルは残す)。

    .venv\\Scripts\\python.exe topopt_checks\\stress_gate\\cut_member.py --load 240 --h 2.73 --w 13.64 --box -40 40 -70 -50 -15 25
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import phase_a_fields as pa  # noqa: E402

import numpy as np  # noqa: E402
from scipy import ndimage  # noqa: E402

import refine_shape  # noqa: E402
from duocad import optimize_run as orun  # noqa: E402
from duocad.topopt import acceptance, binary_search as bs, design_space as ds, min_width  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--load", type=float, required=True)
    ap.add_argument("--h", type=float, required=True)
    ap.add_argument("--w", type=float, required=True)
    ap.add_argument("--box", type=float, nargs=6, action="append", required=True,
                    metavar=("X0", "X1", "Y0", "Y1", "Z0", "Z1"), help="抜く箱 [mm](何個でも)")
    a = ap.parse_args()
    tag = f"hanger_L{a.load:g}_h{a.h:g}_w{a.w:g}"
    doc = refine_shape.open_session(pa.MODELS["hanger"]["model"]).document
    refine_shape._set_load_doc(doc, a.load)
    opts = orun.OptimizeOptions(element_size_mm=a.h, min_width_mm=a.w)
    bundle = orun.build_conditions(doc)
    limits = orun.resolution_limits(doc)
    spacing = orun._snap_to_grid(limits["bbox_min"], limits["bbox_max"], orun._resolve_element_size(limits, a.h))
    m = int(orun._resolve_min_width(
        spacing, a.w, max_cells=min(orun._grid_shape(limits["bbox_min"], limits["bbox_max"], spacing)))["cells"])
    space = ds.derive(bundle.conditions, spacing=spacing)
    table, mapping = orun._map_boundary_conditions(space, bundle)
    cond = orun._build_search_conditions(doc, space, bundle, opts, table=table, mapping=mapping)
    keep = np.asarray(cond.keep_cells, bool)

    z = np.load(pa.OUT / "leftover" / f"{tag}_-1.npz")
    mask = z["mask"]
    centres = np.asarray(space.grid.origin) + (np.indices(mask.shape).transpose(1, 2, 3, 0) + 0.5) * spacing
    cut = np.zeros(mask.shape, bool)
    for x0, x1, y0, y1, z0, z1 in a.box:
        inside = np.ones(mask.shape, bool)
        for axis, (lo, hi) in enumerate(((x0, x1), (y0, y1), (z0, z1))):
            inside &= (centres[..., axis] >= lo) & (centres[..., axis] <= hi)
        cut |= inside
    cut &= mask & ~keep
    candidate = mask & ~cut
    # m 立方に収まらなくなった材料を消す(保持は被覆を問わない)。消すと別の材料が外れることがあるので止まるまで。
    while True:
        lost = candidate & ~keep & ~min_width.cube_opening(candidate | keep, m)
        if not lost.any():
            break
        candidate &= ~lost
    survivors, island_diag = bs.resolve_islands(candidate, cond)
    out = dict(tag=tag, removed_box_cells=int(cut.sum()), removed_total=int(mask.sum() - candidate.sum()),
               survivors=len(survivors), island=island_diag)
    if len(survivors) == 1:
        shape = survivors[0]
        out["removed_with_islands"] = int(mask.sum() - shape.sum())
        report = acceptance.inspect(shape, candidate, cond.grid, **cond.inspect_kwargs(bbox_cache=None))
        out["status"] = report["status"]
        out["failed"] = [k for k, v in report["checks"].items() if v.get("status") != "pass"]
        out["safety_factor"] = report["checks"]["safety"].get("safety_factor")
        out["target"] = cond.target_safety_factor
        out["material_mm3_before"] = round(float(mask.sum()) * spacing ** 3)
        out["material_mm3_after"] = round(float(shape.sum()) * spacing ** 3)
        out["min_width_ok"] = bs._mask_satisfies_min_width(shape, m, keep)
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
