"""保持セルを探索で削らせない版を入れる前に、保存済みの採用マスクで5項目を測る(`docs/TOPOPT_PRESERVE.md`)。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_keep_forced.py --mask m1=C:\\tmp\\m1.npz:1 m3=C:\\tmp\\m3.npz:3 --result C:\\tmp\\keep.json

`--mask` は `名前=npz:m`(npz は `mask`・`origin`・`spacing` を持つ採用マスク、m はその探索の最小幅セル数)。
格子は1つ目の npz の spacing で導出し、全マスクが同じ格子であることを確かめる。**判定はしない**。

1. `mask | keep`(納品形状のボクセル版)の幾何検査: 不正則頂点・成分数・閉じた空洞・最小幅。
2. 保持セル単体: ボディごとの6連結成分数・不正則頂点。
3. 除外CAD・保持CADとセルの部分的な重なり(finalize の cut / union が動かす薄片)。
4. `mask` と `mask | keep` を同じ確定表で解いた安全率(FEM。各1回)。
5. FEM なしの `peel` の空回し: 全材料マスクから、保持を抜かせない版と現行を同じ順位で削り切る。
   保持を抜かせない版は製品の `peel` をそのまま使い、順位から保持セルを外し、保持セルの角の節点を
   条件節点に足す(保持セルを孤立させる手が連結性で落ちる)。順位は全材料マスクの1回の求解から取った
   古いエネルギーなので、できる形に物理的な意味は無い(幾何の却下の内訳だけを見る)。
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401  (topopt_checks の import 経路を通す)
import _record
import numpy as np
from scipy import ndimage

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL = REPO_ROOT / "models" / "hanger_bar_attachment.json"
SIX = ndimage.generate_binary_structure(3, 1)
FULL = np.ones((3, 3, 3), dtype=bool)


def log(*parts) -> None:
    print(*parts, file=sys.stderr, flush=True)


def geometry(mask: np.ndarray) -> dict:
    from duocad.topopt import acceptance

    boundary = acceptance.boundary_check(mask)
    return dict(cells=int(mask.sum()), irregular_vertices=boundary["irregular_vertices"],
                void_components=boundary["void_components"], closed_voids=acceptance.closed_voids(mask),
                components_6=int(ndimage.label(mask, structure=SIX)[1]),
                components_26=int(ndimage.label(mask, structure=FULL)[1]))


def cell_nodes(cells: np.ndarray) -> np.ndarray:
    """セルの角にあたる節点(節点格子の形の bool)。"""
    from duocad.topopt import binary_search

    nodes = np.zeros(tuple(n + 1 for n in cells.shape), dtype=bool)
    for corner in binary_search._CORNERS:
        nodes[tuple(slice(c, c + n) for c, n in zip(corner, cells.shape))] |= cells
    return nodes


def union_geometry(mask: np.ndarray, keep: np.ndarray, m: int) -> dict:
    from duocad.topopt import acceptance, binary_search

    union = mask | keep
    bad, _ = acceptance.irregular_vertices(union)
    return dict(
        m=m, keep_in_mask=int((mask & keep).sum()), keep_removed=int((keep & ~mask).sum()),
        mask=geometry(mask), union=geometry(union),
        union_irregular_vertices_on_restored_keep=int((bad & cell_nodes(keep & ~mask)).sum()),
        min_width_mask_exempt_keep=bool(binary_search._mask_satisfies_min_width(mask, m, keep)),
        min_width_union_no_exemption=bool(binary_search._mask_satisfies_min_width(union, m)),
        min_width_union_exempt_keep=bool(binary_search._mask_satisfies_min_width(union, m, keep)),
    )


def _irregular(mask):
    from duocad.topopt import acceptance

    return acceptance.irregular_vertices(mask)


def keep_alone(space, document, bundle) -> dict:
    from duocad.topopt import voxelize as vx

    keep = np.asarray(space.keep, dtype=bool)
    design = np.asarray(space.mask, dtype=bool)
    rows = []
    for bid in bundle.keep_body_ids:
        soup = vx.triangles_from_shape(vx._unwrap(document.get_body(bid).shape),
                                       deviation=max(space.grid.spacing * vx.DEFAULT_DEVIATION_RATIO, 1e-6))
        try:
            raw, _ = vx.occupancy(soup, space.grid)
        except ValueError as error:
            rows.append(dict(body=bid, cells=0, reason=str(error)))
            continue
        cells = raw & design
        labels, count = ndimage.label(cells, structure=SIX)
        sizes = np.bincount(labels.ravel())[1:].tolist() if count else []
        rows.append(dict(body=bid, cells_raw=int(raw.sum()), cells_in_design=int(cells.sum()),
                         components_6=int(count), component_sizes=sorted(sizes, reverse=True)[:10],
                         components_26=int(ndimage.label(cells, structure=FULL)[1]),
                         irregular_vertices_alone=int(_irregular(cells)[0].sum())))
    return dict(keep_cells=int(keep.sum()), keep_outside_design=int((keep & ~design).sum()),
                combined=geometry(keep), bodies=rows)


def slivers(space, conditions, masks: dict) -> dict:
    """セル×CADの共通体積の分布。`partial` は 0 < 重なり < セル体積(許容 1e-6 セル体積)。"""
    from duocad.topopt import acceptance

    design = np.asarray(space.mask, dtype=bool)
    keep = np.asarray(space.keep, dtype=bool)
    cell = float(space.grid.spacing) ** 3
    out: dict = {}
    for role, bodies in (("exclude", conditions.excludes), ("preserve", conditions.preserves)):
        for name, shape in bodies:
            entry = acceptance._CellOverlaps(space.grid, shape)
            entry.overlap_of(design)
            window = tuple(slice(int(a), int(a) + n) for a, n in zip(entry.lo, entry.values.shape))
            frac = np.zeros(design.shape)
            frac[window] = np.nan_to_num(entry.values) / cell

            def dist(selection: np.ndarray) -> dict:
                values = frac[selection]
                partial = values[(values > 1e-6) & (values < 1 - 1e-6)]
                return dict(cells=int(selection.sum()), overlapping=int((values > 1e-6).sum()),
                            partial=int(partial.size), overlap_volume_mm3=float(values.sum() * cell),
                            partial_fraction_quartiles=[float(v) for v in np.percentile(partial, [25, 50, 75, 100])]
                            if partial.size else None)

            row = dict(solid_volume_mm3=entry.total, design_space=dist(design))
            if role == "preserve":
                row["keep_cells"] = dist(keep)
                row["design_not_keep"] = dist(design & ~keep)
            for label, mask in masks.items():
                row[f"mask_{label}"] = dist(mask)
                if role == "preserve":
                    row[f"mask_{label}_not_keep"] = dist(mask & ~keep)
            out[f"{role}:{name}"] = row
    return out


def solve(material: np.ndarray, conditions, keep: np.ndarray) -> tuple[dict, np.ndarray | None]:
    """`acceptance.safety_check` と同じ式で解き、上位の応力セルが保持セルにどれだけ入るかも返す。"""
    from duocad.topopt import acceptance, fem, stress

    started = time.perf_counter()
    check, fixed, load = acceptance.condition_check(material, conditions.table, conditions.fixed_faces,
                                                    conditions.load_faces, conditions.forces)
    out = dict(cells=int(material.sum()), conditions=check["status"])
    if check["status"] != "pass":
        return out, None
    live = acceptance.active_nodes(material).ravel()
    active_dofs = np.repeat(live, 3)
    model = fem.Model(*material.shape, fixed_dofs=fixed, load=load,
                      extra_fixed_dofs=np.flatnonzero(~active_dofs), active_mask=material)
    density = material.astype(float)
    u = stress.solve_displacement(model, density, solver="cg")
    residual = model.stiffness_apply(density, 3.0, u) - model.f
    relative = float(np.linalg.norm(residual[model.free]) / np.linalg.norm(model.f[model.free]))
    scale = stress.UnitScale(conditions.grid.spacing, 1.0, acceptance.DEFAULT_YOUNGS_MODULUS_MPA, solved_load=1.0)
    report = stress.evaluate(model, density, scale=scale, allowable_mpa=acceptance.DEFAULT_ALLOWABLE_MPA, u=u)
    vm = np.asarray(report.field.von_mises, dtype=float)
    threshold = report.representative.value
    top = material & (vm >= threshold)
    outside = material & ~keep
    compliance, energy = model.compliance(density, 3.0, u)
    out.update(
        safety_factor=float(report.safety_factor), representative_mpa=float(report.representative_mpa),
        raw_max_mpa=float(report.raw_max_mpa), max_displacement_mm=float(report.max_displacement_mm),
        compliance=float(compliance), relative_residual=relative, seconds=time.perf_counter() - started,
        top_cells=int(top.sum()), top_cells_in_keep=int((top & keep).sum()),
        safety_factor_excluding_keep_cells=float(report.safety_factor * threshold
                                                 / np.percentile(vm[outside], report.representative.percentile))
        if outside.any() else None,
    )
    return out, np.where(material, energy, 0.0)


def dry_peel(design: np.ndarray, energy: np.ndarray, conditions, keep: np.ndarray, m: int, *,
             forced: bool, step: float = 0.2, max_rounds: int = 400) -> dict:
    from duocad.topopt import binary_search

    anchors = binary_search._anchor_node_ids(conditions)
    if forced:
        anchors = np.union1d(anchors, np.flatnonzero(cell_nodes(keep).ravel()))
    exempt = keep if m > 1 else None
    mask = design.copy()
    totals: dict = {}
    trajectory = []
    started = time.perf_counter()
    stop = "max_rounds"
    for round_index in range(max_rounds):
        order = binary_search.ranked_cells(mask, energy)
        if forced:
            order = order[~keep.ravel()[order]]
        count = max(1, int(step * mask.sum()))
        mask, stats = binary_search.peel(mask, order, count, anchors, min_width_cells=m, exempt=exempt)
        removed = stats.picked + stats.casualties + stats.islands
        for key, value in stats.as_dict().items():
            totals[key] = totals.get(key, 0) + value
        trajectory.append(dict(round=round_index, cells=int(mask.sum()), keep_cells=int((mask & keep).sum()),
                               **stats.as_dict()))
        if removed == 0:
            stop = "nothing_removable"
            break
    tried = totals["picked"] + sum(v for k, v in totals.items() if k.startswith("skipped_"))
    return dict(forced=forced, m=m, stop=stop, rounds=len(trajectory), final_cells=int(mask.sum()),
                final_non_keep_cells=int((mask & ~keep).sum()), final_keep_cells=int((mask & keep).sum()),
                final=geometry(mask), totals=totals,
                skip_rate={k: totals[k] / tried for k in totals if k.startswith("skipped_")},
                seconds=time.perf_counter() - started, trajectory=trajectory)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mask", nargs="+", required=True, help="名前=npz:m")
    parser.add_argument("--steps", nargs="+", default=["1", "2", "3", "4", "5"], help="実施する項目(既定は全部)")
    parser.add_argument("--peel-m", type=int, nargs="+", default=[1, 3], help="項目5で回す最小幅セル数")
    parser.add_argument("--result", type=Path, default=None)
    args = parser.parse_args()

    from duocad import optimize_run
    from duocad.server import Session
    from duocad.topopt import design_space as ds, trace

    loaded = {}
    for item in args.mask:
        label, rest = item.split("=", 1)
        path, m = rest.rsplit(":", 1)
        data = np.load(path)
        loaded[label] = dict(mask=data["mask"].astype(bool), origin=data["origin"] if "origin" in data else None,
                             spacing=float(data["spacing"]) if "spacing" in data else None, m=int(m), path=path)
    first = next(iter(loaded.values()))
    out: dict = {"model": str(MODEL), "masks": {k: dict(path=v["path"], m=v["m"]) for k, v in loaded.items()}}

    with tempfile.TemporaryDirectory(prefix="topopt-trace-") as trace_root, \
            patch.object(trace, "DEFAULT_ROOT", Path(trace_root)):
        session = Session(None, 0.2, models_dir=MODEL.resolve().parent)
        session.open_document(MODEL.name)
        document = session.document
        bundle = optimize_run.build_conditions(document)
        space = ds.derive(bundle.conditions, spacing=first["spacing"])
        for label, item in loaded.items():
            if tuple(space.grid.shape) != item["mask"].shape or (
                    item["origin"] is not None and not np.allclose(space.grid.origin, item["origin"])):
                raise SystemExit(f"{label}: 格子が違う")
        table, mapping = optimize_run._map_boundary_conditions(space, bundle)
        opts = optimize_run.OptimizeOptions(target_safety_factor=2.0)
        conditions = optimize_run._build_search_conditions(document, space, bundle, opts, table=table, mapping=mapping)
        design = np.asarray(space.mask, dtype=bool)
        keep = np.asarray(space.keep, dtype=bool)
        out.update(grid_shape=list(space.grid.shape), spacing=float(space.grid.spacing),
                   design_cells=int(design.sum()), keep_cells=int(keep.sum()), mapping_status=mapping["status"])
        log("格子", out["grid_shape"], "設計空間", out["design_cells"], "保持", out["keep_cells"])

        if "1" in args.steps:
            out["1_union_geometry"] = {k: union_geometry(v["mask"], keep, v["m"]) for k, v in loaded.items()}
            log("1 done")
        if "2" in args.steps:
            out["2_keep_alone"] = keep_alone(space, document, bundle)
            log("2 done")
        if "3" in args.steps:
            out["3_slivers"] = slivers(space, conditions, {k: v["mask"] for k, v in loaded.items()})
            log("3 done")
        if "4" in args.steps:
            rows = {}
            for label, item in loaded.items():
                adopted, _ = solve(item["mask"], conditions, keep)
                union, _ = solve(item["mask"] | keep, conditions, keep)
                rows[label] = dict(mask=adopted, union=union)
                log("4", label, adopted.get("safety_factor"), "->", union.get("safety_factor"))
            out["4_fem"] = rows
        if "5" in args.steps:
            full, energy = solve(design, conditions, keep)
            out["5_full_design_solve"] = full
            log("5 full solve", full.get("safety_factor"), full.get("seconds"))
            rows = []
            for m in args.peel_m:
                for forced in (False, True):
                    row = dry_peel(design, energy, conditions, keep, m, forced=forced)
                    rows.append(row)
                    log("5", "m", m, "forced", forced, row["stop"], row["final_cells"], row["seconds"])
            out["5_dry_peel"] = rows

    text = _record.dumps(out, default=float)
    if args.result:
        args.result.write_text(text, encoding="utf-8")
    print(text, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
