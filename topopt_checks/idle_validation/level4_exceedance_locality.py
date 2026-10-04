"""path_2.5mm@6000N level=4 の限度超過8セルが設計境界近傍か内部かを判定する。

出力: `output/idle_validation_20260928/03b_level4_exceedance_locality.json`
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from scipy import ndimage

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import _common  # noqa: E402
import metric_eval as me  # noqa: E402
import refine_shape as rs  # noqa: E402

OUT = REPO / "output" / "idle_validation_20260928"

EXPECTED_MATERIAL_ELEMENTS = 243696
EXPECTED_OVER_COUNT = 8


def _zone_at(zones: dict, idx: tuple[int, int, int]) -> str:
    i, j, k = idx
    if zones["holding"][i, j, k]:
        return "holding"
    if zones["joint"][i, j, k]:
        return "joint"
    return "general"


def main() -> int:
    shape_cfg = rs._resolve_shape("path_2.5mm", None)
    result = me.solve_shape(shape_cfg, 6000.0, 4)
    assert result["status"] == "ok"
    assert result["material_elements"] == EXPECTED_MATERIAL_ELEMENTS, (
        f"material_elements {result['material_elements']} != metric_comparison {EXPECTED_MATERIAL_ELEMENTS}"
    )

    vm = result["von_mises_mpa"]
    mask = result["mask"]
    design_mask = result["design_mask"]
    allowable = result["allowable_mpa"]
    grid = result["grid"]
    zones = result["zones"]

    over = mask & (vm > allowable)
    over_count = int(over.sum())
    over_count_mismatch = None
    if over_count != EXPECTED_OVER_COUNT:
        print(
            f"warning: over_count={over_count} (expected {EXPECTED_OVER_COUNT} from metric_comparison)"
        )
        over_count_mismatch = over_count

    eroded = ndimage.binary_erosion(design_mask, iterations=1, border_value=0)
    boundary_shell = design_mask & ~eroded
    dist_to_outside = ndimage.distance_transform_cdt(design_mask, metric="chessboard")

    exceedance_cells = []
    for idx in zip(*np.nonzero(over)):
        i, j, k = (int(idx[0]), int(idx[1]), int(idx[2]))
        world_mm = grid.origin + (np.array([i, j, k], dtype=float) + 0.5) * grid.spacing
        exceedance_cells.append(
            dict(
                index=[i, j, k],
                von_mises_mpa=float(vm[i, j, k]),
                world_mm=[float(x) for x in world_mm],
                zone=_zone_at(zones, (i, j, k)),
                design_boundary_adjacent=bool(boundary_shell[i, j, k]),
                distance_to_design_edge_cells=int(dist_to_outside[i, j, k]),
            )
        )

    mat_boundary = boundary_shell & mask
    material_wide_boundary_adjacent_fraction = float(mat_boundary.sum() / mask.sum())
    if over_count:
        exceedance_boundary_adjacent_fraction = float((boundary_shell & over).sum() / over_count)
    else:
        exceedance_boundary_adjacent_fraction = 0.0

    payload = dict(
        generated_at=_common.now_iso(),
        code_version=_common.git_state(),
        shape="path_2.5mm",
        load_n=6000.0,
        level=4,
        material_elements=result["material_elements"],
        over_count=over_count,
        exceedance_cells=exceedance_cells,
        material_wide_boundary_adjacent_fraction=material_wide_boundary_adjacent_fraction,
        exceedance_boundary_adjacent_fraction=exceedance_boundary_adjacent_fraction,
    )
    if over_count_mismatch is not None:
        payload["over_count_mismatch"] = over_count_mismatch

    out_path = OUT / "03b_level4_exceedance_locality.json"
    _common.write_atomic(out_path, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out_path} (over_count={over_count})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
