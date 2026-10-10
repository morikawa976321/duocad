"""形状(shape/load/level)を1つ渡すと、`refine_shape.py`(タスク2)と同じ境界条件パイプラインで
FEMを解き、`stress_metrics.py` の候補式が必要とする生の場(von_mises 全要素・ゾーンマスク)まで返す
タスク3の共有ヘルパー。**再実装しない**——`refine_shape.py` が定義済みのマスク読み込み・投影
(`_resolve_shape` / `_project_mask` / `HANGER_MODEL`)をそのまま import して使う(単一の真実源)。

`origin_shift_mm` は反例⑦(格子原点移動)専用。`design_space.grid_for_frame` をこのプロセス内だけ
差し替え、`derive` が組む格子の原点だけをずらす(box_lo/box_hi や `elements_long_axis` は変えない
ので、格子の大きさ・要素サイズは変わらず「どこにセル境界が来るか」だけが動く)。
`task01契約②`と同じ理由で `design_space.derive` は原点を直接のパラメータとして持たないため、
この形のパッチが必要(10_SYNTHESIS 付録A(旧 01_PROTOCOL 6-1節))。
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import numpy as np  # noqa: E402

from duocad import optimize_run  # noqa: E402
from duocad.topopt import binary_search, design_space as ds, stress  # noqa: E402

import _common  # noqa: E402
import _fem_probe  # noqa: E402
import refine_shape  # noqa: E402


def _shifted_grid_for_frame(shift_mm: tuple[float, float, float]):
    real = ds.grid_for_frame

    def wrapped(box_lo, box_hi, **kwargs):
        grid = real(box_lo, box_hi, **kwargs)
        new_origin = tuple(float(o) + float(s) for o, s in zip(grid.origin, shift_mm))
        return dataclasses.replace(grid, origin=new_origin)

    return wrapped


def solve_shape(
    shape_cfg: dict,
    load_n: float,
    level: int,
    *,
    origin_shift_mm: float | tuple[float, float, float] = 0.0,
    distance_mm: Optional[float] = None,
) -> dict:
    """`refine_shape._run_one` と同じ境界条件パイプライン。戻り値は生の場・ゾーンマスクまで持つ
    (`refine_shape` はJSONへ書けるサマリだけを残すが、こちらは `stress_metrics.py` が直接使う
    `von_mises_mpa` 全要素配列・ゾーンの bool マスクをそのまま返す)。
    """
    src_mask = np.asarray(shape_cfg["mask"], dtype=bool)
    src_origin = np.asarray(shape_cfg["origin"], dtype=float)
    src_spacing = float(shape_cfg["spacing"])
    fine_spacing = src_spacing / level

    doc = refine_shape.open_session(shape_cfg["model"]).document
    refine_shape._set_load_doc(doc, load_n)
    bundle = optimize_run.build_conditions(doc)

    shift = origin_shift_mm
    if isinstance(shift, (int, float)):
        shift = (float(shift), float(shift), float(shift))

    if any(shift):
        with patch.object(ds, "grid_for_frame", _shifted_grid_for_frame(shift)):
            space = ds.derive(bundle.conditions, spacing=fine_spacing)
    else:
        space = ds.derive(bundle.conditions, spacing=fine_spacing)

    table, mapping = optimize_run._map_boundary_conditions(space, bundle)
    cond = optimize_run._build_search_conditions(
        doc, space, bundle,
        optimize_run.OptimizeOptions(element_size_mm=fine_spacing),
        table=table, mapping=mapping,
    )
    cond = dataclasses.replace(cond, member_volume_mm3=None)  # この研究は材料全体の99.5%点(2026-10-09 より前の製品)を対象にする

    mask = refine_shape._project_mask(src_mask, src_origin, src_spacing, space) & space.mask
    keep = shape_cfg.get("keep")
    if keep is not None:
        keep_f = refine_shape._project_mask(np.asarray(keep, dtype=bool), src_origin, src_spacing, space) & space.mask
        mask = mask | keep_f
    elif getattr(cond, "keep_cells", None) is not None:
        keep_f = np.asarray(cond.keep_cells, dtype=bool)
        mask = mask | (keep_f & space.mask)

    survivors, diag = binary_search.resolve_islands(mask, cond)
    if not survivors:
        return dict(status="disconnected_at_this_level", shape=shape_cfg["name"], load_n=load_n,
                    level=level, element_mm=fine_spacing, error="resolve_islands で連結成分が残らなかった",
                    diagnostics=diag)
    mask = max(survivors, key=lambda c: c.sum())

    with _common.SingleFemRun() as run:
        probe = _fem_probe.evaluate_full(
            mask, space.grid, table, cond.fixed_faces, cond.load_faces, cond.forces,
            allowable_mpa=cond.allowable_mpa, youngs_modulus_mpa=cond.youngs_modulus_mpa,
            percentile=cond.percentile or stress.DEFAULT_PERCENTILE, solver="auto",
        )
    if probe["status"] != "ok":
        return dict(status=probe["status"], shape=shape_cfg["name"], load_n=load_n, level=level,
                    element_mm=fine_spacing, error=probe.get("error"), timing=dict(elapsed_seconds=run.elapsed_seconds))

    dist_default = 3.0 * src_spacing if distance_mm is None else distance_mm
    fixed_boxes = _fem_probe.face_world_boxes(table, cond.fixed_faces)
    load_boxes = _fem_probe.face_world_boxes(table, cond.load_faces)
    zones = _fem_probe.zone_masks(
        mask, space.grid, fixed_world_boxes=fixed_boxes, load_world_boxes=load_boxes, distance_mm=dist_default,
    )

    return dict(
        status="ok",
        shape=shape_cfg["name"],
        load_n=float(load_n),
        level=int(level),
        element_mm=float(fine_spacing),
        design_elements=int(space.mask.sum()),
        material_elements=int(mask.sum()),
        allowable_mpa=float(cond.allowable_mpa),
        percentile=float(cond.percentile or stress.DEFAULT_PERCENTILE),
        distance_mm=float(dist_default),
        mask=mask,
        design_mask=space.mask,
        grid=space.grid,
        von_mises_mpa=probe["von_mises_mpa"],
        tensor_mpa=probe["tensor_mpa"],
        principal_mpa=probe["principal_mpa"],
        zones=zones,
        safety_factor=float(probe["safety_factor"]),
        representative_mpa=float(probe["representative_mpa"]),
        raw_max_mpa=float(probe["raw_max_mpa"]),
        max_displacement_mm=float(probe["max_displacement_mm"]),
        origin_shift_mm=list(shift),
        achieved_origin_mm=list(space.grid.origin),
        table=table,
        cond=cond,
        timing=dict(elapsed_seconds=run.elapsed_seconds, peak_memory=run.peak, traced_peak_mb=run.traced_peak_mb),
    )
