"""path_2.5mm(level=2)で keep 合成と resolve_islands が体積(セル数)に与える寄与を段階記録する。

出力: `output/idle_validation_20260928/03b_keep_islands_volume_path25.json`
"""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

from duocad import optimize_run  # noqa: E402
from duocad.topopt import binary_search, design_space as ds  # noqa: E402

import _common  # noqa: E402
import refine_shape as rs  # noqa: E402

OUT = REPO / "output" / "idle_validation_20260928"


def _final_after_islands(mask: np.ndarray, cond) -> np.ndarray:
    survivors, _diag = binary_search.resolve_islands(mask, cond)
    if not survivors:
        raise RuntimeError("resolve_islands で連結成分が残らなかった")
    return max(survivors, key=lambda c: c.sum())


def main() -> int:
    shape_cfg = rs._resolve_shape("path_2.5mm", None)
    level = 2
    src_mask = np.asarray(shape_cfg["mask"], dtype=bool)
    src_origin = np.asarray(shape_cfg["origin"], dtype=float)
    src_spacing = float(shape_cfg["spacing"])
    keep = shape_cfg.get("keep")
    fine_spacing = src_spacing / level

    doc = rs.open_session(shape_cfg["model"]).document
    rs._set_load_doc(doc, shape_cfg["default_load"])
    bundle = optimize_run.build_conditions(doc)
    space = ds.derive(bundle.conditions, spacing=fine_spacing)
    table, mapping = optimize_run._map_boundary_conditions(space, bundle)
    cond = optimize_run._build_search_conditions(
        doc,
        space,
        bundle,
        optimize_run.OptimizeOptions(element_size_mm=fine_spacing),
        table=table,
        mapping=mapping,
    )
    cond = dataclasses.replace(cond, member_volume_mm3=None)  # この研究は材料全体の99.5%点(2026-10-09 より前の製品)を対象にする

    mask_regrid_no_keep = rs._project_mask(src_mask, src_origin, src_spacing, space) & space.mask
    mask_regrid_with_keep = mask_regrid_no_keep
    if keep is not None:
        keep_f = rs._project_mask(np.asarray(keep, dtype=bool), src_origin, src_spacing, space) & space.mask
        mask_regrid_with_keep = mask_regrid_no_keep | keep_f
    elif getattr(cond, "keep_cells", None) is not None:
        keep_f = np.asarray(cond.keep_cells, dtype=bool)
        mask_regrid_with_keep = mask_regrid_no_keep | (keep_f & space.mask)

    mask_exact_no_keep, _diag = rs._project_mask_exact_subdivide(
        src_mask, level, space, src_origin, src_spacing
    )
    mask_exact_with_keep = mask_exact_no_keep
    if keep is not None:
        keep_f = rs._project_mask(np.asarray(keep, dtype=bool), src_origin, src_spacing, space) & space.mask
        mask_exact_with_keep = mask_exact_no_keep | keep_f
    elif getattr(cond, "keep_cells", None) is not None:
        keep_f = np.asarray(cond.keep_cells, dtype=bool)
        mask_exact_with_keep = mask_exact_no_keep | (keep_f & space.mask)

    final_mask_regrid = _final_after_islands(mask_regrid_with_keep, cond)
    final_mask_exact = _final_after_islands(mask_exact_with_keep, cond)

    regrid_no_keep_cells = int(mask_regrid_no_keep.sum())
    regrid_with_keep_cells = int(mask_regrid_with_keep.sum())
    regrid_after_islands_cells = int(final_mask_regrid.sum())
    exact_no_keep_cells = int(mask_exact_no_keep.sum())
    exact_with_keep_cells = int(mask_exact_with_keep.sum())
    exact_after_islands_cells = int(final_mask_exact.sum())

    keep_contribution_regrid_cells = regrid_with_keep_cells - regrid_no_keep_cells
    keep_contribution_exact_cells = exact_with_keep_cells - exact_no_keep_cells
    islands_removed_regrid_cells = regrid_with_keep_cells - regrid_after_islands_cells
    islands_removed_exact_cells = exact_with_keep_cells - exact_after_islands_cells

    for name, val in (
        ("keep_contribution_regrid_cells", keep_contribution_regrid_cells),
        ("keep_contribution_exact_cells", keep_contribution_exact_cells),
        ("islands_removed_regrid_cells", islands_removed_regrid_cells),
        ("islands_removed_exact_cells", islands_removed_exact_cells),
    ):
        assert val >= 0, f"{name}={val} (negative implies implementation bug)"

    spacing_mm = float(space.grid.spacing)
    cell_volume_mm3 = float(space.grid.spacing) ** 3

    payload = dict(
        generated_at=_common.now_iso(),
        code_version=_common.git_state(),
        shape="path_2.5mm",
        level=level,
        spacing_mm=spacing_mm,
        cell_volume_mm3=cell_volume_mm3,
        regrid_no_keep_cells=regrid_no_keep_cells,
        regrid_with_keep_cells=regrid_with_keep_cells,
        regrid_after_islands_cells=regrid_after_islands_cells,
        exact_no_keep_cells=exact_no_keep_cells,
        exact_with_keep_cells=exact_with_keep_cells,
        exact_after_islands_cells=exact_after_islands_cells,
        keep_contribution_regrid_cells=keep_contribution_regrid_cells,
        keep_contribution_exact_cells=keep_contribution_exact_cells,
        islands_removed_regrid_cells=islands_removed_regrid_cells,
        islands_removed_exact_cells=islands_removed_exact_cells,
    )
    out_path = OUT / "03b_keep_islands_volume_path25.json"
    _common.write_atomic(out_path, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
