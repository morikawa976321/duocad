"""保存済みマスクを細分格子へ投影し、同形状のままFEMを解いて参照データを作るCLI。"""
from __future__ import annotations

import dataclasses
import argparse
import json
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import numpy as np  # noqa: E402

from _documents import open_session  # noqa: E402
from duocad import optimize_run  # noqa: E402
from duocad.topopt import binary_search, design_space as ds, stress  # noqa: E402
from sweep_smoothing_corpus import MODEL as HANGER_MODEL, _set_load  # noqa: E402

import _common  # noqa: E402
import _fem_probe  # noqa: E402

OUT = REPO / "output" / "idle_validation_20260928"
LEDGER = OUT / "02_reference_ledger.json"
STAGE1 = OUT / "01_stage1_shapes.json"

HANGER_DEFAULTS = {
    "hanger_base": ("stage1_base", "F0200_h2.0_w5"),
    "hanger_r2": ("stage1_R2", "F0200_h2.4_w2"),
    "hanger_step05": ("stage1_step05", "F0200_h2.0_w5"),
    "hanger_transplant": ("stage1_transplant_truss", "F0200_h2.0_w5"),
}


def _set_load_doc(doc, newtons: float) -> None:
    for load in doc.optimize.loads:
        load["magnitude"] = float(newtons)
        vec = load.get("vector")
        if isinstance(vec, (list, tuple)) and len(vec) == 3 and all(
            isinstance(c, (int, float)) for c in vec
        ):
            norm = float(sum(c * c for c in vec)) ** 0.5 or 1.0
            load["vector"] = [c / norm * float(newtons) for c in vec]
        else:
            load["vector"] = [c * float(newtons) for c in load["direction"]]


def _project_mask_exact_subdivide(
    src_mask: np.ndarray, level: int, fine_space, src_origin, src_spacing: float
) -> tuple[np.ndarray, dict]:
    """元マスクの各セルを level^3 個の同一の子セルへ厳密に分割し(体積保存)、
    fine_space.grid の実際の外枠(origin・shape)に合わせて配置する。
    """
    if level < 1:
        raise ValueError(f"level must be >= 1, got {level}")
    src_mask = np.asarray(src_mask, dtype=bool)
    fine_spacing = np.asarray(fine_space.grid.spacing, dtype=float)
    fine_origin = np.asarray(fine_space.grid.origin, dtype=float)
    src_origin_arr = np.asarray(src_origin, dtype=float)
    fine_shape = tuple(int(x) for x in fine_space.grid.shape)

    delta = fine_origin - src_origin_arr
    offset_float = delta / fine_spacing
    offset_cells = np.rint(offset_float).astype(int)
    residual = delta - offset_cells.astype(float) * fine_spacing
    if not np.all(np.abs(residual) <= 1e-6):
        raise ValueError(
            "fine_space.grid.origin が src_origin から細格子 spacing の整数倍だけずれていない: "
            f"delta_mm={delta.tolist()} spacing_mm={fine_spacing.tolist()}"
        )

    full_mask = src_mask.repeat(level, axis=0).repeat(level, axis=1).repeat(level, axis=2)
    full_shape = full_mask.shape
    cell_vol = float(np.prod(fine_spacing))
    volume_before_crop_mm3 = float(full_mask.sum()) * cell_vol

    fine_mask = np.zeros(fine_shape, dtype=bool)
    full_start = []
    full_end = []
    fine_start = []
    fine_end = []
    for d in range(3):
        fs = max(0, int(offset_cells[d]))
        fe = min(full_shape[d], int(offset_cells[d]) + fine_shape[d])
        gs = max(0, -int(offset_cells[d]))
        ge = gs + (fe - fs)
        full_start.append(fs)
        full_end.append(fe)
        fine_start.append(gs)
        fine_end.append(ge)

    sl_full = tuple(slice(full_start[d], full_end[d]) for d in range(3))
    sl_fine = tuple(slice(fine_start[d], fine_end[d]) for d in range(3))
    fine_mask[sl_fine] = full_mask[sl_full]

    cropped = full_mask.copy()
    cropped[sl_full] = False
    cropped_material_cells = int(cropped.sum())
    overlap_cells = int(np.prod([full_end[d] - full_start[d] for d in range(3)]))
    padded_cells = int(np.prod(fine_shape) - overlap_cells)
    volume_after_crop_mm3 = float(fine_mask.sum()) * cell_vol

    diagnostics = dict(
        volume_before_crop_mm3=volume_before_crop_mm3,
        volume_after_crop_mm3=volume_after_crop_mm3,
        cropped_material_cells=cropped_material_cells,
        padded_cells=padded_cells,
        origin_offset_fine_cells=offset_cells.astype(float).tolist(),
    )
    return fine_mask, diagnostics


def _project_mask(src_mask: np.ndarray, src_origin, src_spacing: float, space) -> np.ndarray:
    idx = np.indices(space.grid.shape).reshape(3, -1).T
    world = np.asarray(space.grid.origin) + (idx + 0.5) * space.grid.spacing
    src_idx = np.floor((world - np.asarray(src_origin)) / float(src_spacing)).astype(int)
    inside = np.all((src_idx >= 0) & (src_idx < np.array(src_mask.shape)), axis=1)
    transplanted = np.zeros(len(idx), bool)
    clipped = src_idx[inside]
    transplanted[inside] = src_mask[clipped[:, 0], clipped[:, 1], clipped[:, 2]]
    return transplanted.reshape(space.grid.shape)


def _load_dilution_mask() -> tuple[np.ndarray, np.ndarray, float]:
    path = OUT / "dilution_mask_r-1.npz"
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} がありません。先に measure_dilution_counterfactual.py --radius -1 を実行してください"
        )
    data = np.load(path)
    return data["mask"], data["origin"], float(data["spacing"])


def _load_path_mask() -> tuple[np.ndarray, np.ndarray, float, np.ndarray | None]:
    path = OUT / "path_mask_mesh_2.5mm.npz"
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} がありません。先に measure_path_local_optimum.py mesh --sizes 2.5 を実行してください"
        )
    data = np.load(path)
    keep = data["keep"] if "keep" in data.files else None
    return data["mask"], data["origin"], float(data["spacing"]), keep


def _load_hanger_entry(shape_cli: str, hanger_condition: str) -> dict:
    kind, default_cond = HANGER_DEFAULTS[shape_cli]
    condition = hanger_condition or default_cond
    entries = json.loads(STAGE1.read_text(encoding="utf-8"))["entries"]
    for entry in entries:
        if entry.get("shape_kind") == kind and entry.get("condition") == condition:
            return entry
    raise KeyError(f"01_stage1_shapes.json に shape_kind={kind} condition={condition} が無い")


def _resolve_shape(shape: str, hanger_condition: str) -> dict:
    if shape == "dilution_r-1":
        mask, origin, spacing = _load_dilution_mask()
        return dict(
            name=shape,
            model=REPO / "models" / "topopt_idle_dilution_small.json",
            mask=mask,
            origin=origin,
            spacing=spacing,
            keep=None,
            default_load=300.0,
        )
    if shape == "path_2.5mm":
        mask, origin, spacing, keep = _load_path_mask()
        return dict(
            name=shape,
            model=REPO / "models" / "topopt_idle_path_small.json",
            mask=mask,
            origin=origin,
            spacing=spacing,
            keep=keep,
            default_load=1500.0,
        )
    if shape in HANGER_DEFAULTS:
        entry = _load_hanger_entry(shape, hanger_condition)
        npz_path = REPO / entry["files"]["npz"]
        data = np.load(npz_path)
        mask = data["mask"]
        keep = data["keep"] if "keep" in data.files else None
        return dict(
            name=shape,
            model=HANGER_MODEL,
            mask=mask,
            origin=entry["origin_mm"],
            spacing=float(entry["spacing_mm"]),
            keep=keep,
            default_load=float(entry["load_n"]),
            entry=entry,
        )
    raise ValueError(f"未知の shape: {shape}")


def _section_indices(zones: dict) -> tuple[float, float]:
    """ゾーンのx範囲の境界(保持|接続|一般)から断面位置を取る。"""
    grid_x = None

    def xmax(mask):
        xs = np.where(mask)[0]
        return int(xs.max()) if xs.size else 0

    def xmin(mask):
        xs = np.where(mask)[0]
        return int(xs.min()) if xs.size else 0

    hold, joint, general = zones["holding"], zones["joint"], zones["general"]
    # 保持部の右端と接続部左端の間、接続部右端と一般部左端の間(セル中心のxは後で grid から)
    i_hold_max = xmax(hold)
    i_joint_min, i_joint_max = xmin(joint), xmax(joint)
    i_gen_min = xmin(general)
    return (i_hold_max, i_joint_min), (i_joint_max, i_gen_min)


def _record_strip_internal(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if not k.startswith("_")}


def _run_one(
    shape_cfg: dict,
    load_n: float,
    level: int,
    distance_base: float | None,
    out_dir: Path,
    *,
    exact_subdivide: bool = False,
) -> dict:
    src_mask = np.asarray(shape_cfg["mask"], dtype=bool)
    src_origin = np.asarray(shape_cfg["origin"], dtype=float)
    src_spacing = float(shape_cfg["spacing"])
    fine_spacing = src_spacing / level

    doc = open_session(shape_cfg["model"]).document
    _set_load_doc(doc, load_n)
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

    exact_subdivide_diagnostics = None
    if exact_subdivide:
        projected, exact_subdivide_diagnostics = _project_mask_exact_subdivide(
            src_mask, level, space, src_origin, src_spacing
        )
        mask = projected & space.mask
        # 整数分割マスクは体積保存(diagnostics参照)でも、`space.mask`(design_space.deriveが
        # spacingごとに独立再計算する設計空間の外枠)との積で材料セルが失われることがある
        # (10_SYNTHESIS 付録A(旧 03A_REFERENCE_REPAIR 3節): dilution_r-1 level2で355セル)。投影方式に関わらず起きる
        # 外枠の後退なので、exact_subdivideのときだけ生じる問題と誤解されないよう明示的に記録する。
        lost_outside_space = projected & ~space.mask
        exact_subdivide_diagnostics["cells_lost_to_design_space_mask"] = int(lost_outside_space.sum())
        exact_subdivide_diagnostics["volume_lost_to_design_space_mask_mm3"] = (
            int(lost_outside_space.sum()) * float(np.prod(space.grid.spacing))
        )
    else:
        mask = _project_mask(src_mask, src_origin, src_spacing, space) & space.mask
    keep = shape_cfg.get("keep")
    if keep is not None:
        keep_f = _project_mask(np.asarray(keep, dtype=bool), src_origin, src_spacing, space) & space.mask
        mask = mask | keep_f
    elif getattr(cond, "keep_cells", None) is not None:
        keep_f = np.asarray(cond.keep_cells, dtype=bool)
        mask = mask | (keep_f & space.mask)

    survivors, _diag = binary_search.resolve_islands(mask, cond)
    if not survivors:
        return dict(
            status="disconnected_at_this_level",
            shape=shape_cfg["name"],
            load_n=load_n,
            level=level,
            element_mm=fine_spacing,
            error="resolve_islands で連結成分が残らなかった",
        )
    mask = max(survivors, key=lambda c: c.sum())

    case_id = _common.condition_id(
        shape=shape_cfg["name"], load_n=load_n, level=level, spacing_mm=fine_spacing
    )
    mem_before = _common.available_memory_mb()
    with _common.SingleFemRun() as run:
        probe = _fem_probe.evaluate_full(
            mask,
            space.grid,
            table,
            cond.fixed_faces,
            cond.load_faces,
            cond.forces,
            allowable_mpa=cond.allowable_mpa,
            youngs_modulus_mpa=cond.youngs_modulus_mpa,
            percentile=cond.percentile or stress.DEFAULT_PERCENTILE,
            solver="auto",
        )
    timing = dict(elapsed_seconds=run.elapsed_seconds, peak_memory=run.peak, traced_peak_mb=run.traced_peak_mb,
                  available_memory_mb_before=mem_before)

    if probe["status"] != "ok":
        return dict(
            status="failed",
            case_id=case_id,
            shape=shape_cfg["name"],
            load_n=load_n,
            level=level,
            element_mm=fine_spacing,
            error=probe.get("error"),
            probe_status=probe["status"],
            **timing,
        )

    dist_default = 3.0 * src_spacing if distance_base is None else distance_base
    dist_triplets = [
        ("base", dist_default),
        ("wider", dist_default * 1.5),
        ("narrower", dist_default * 0.67),
    ]
    fixed_boxes = _fem_probe.face_world_boxes(table, cond.fixed_faces)
    load_boxes = _fem_probe.face_world_boxes(table, cond.load_faces)

    zone_sensitivity = {}
    zones_base = None
    for tag, dist in dist_triplets:
        zones = _fem_probe.zone_masks(
            mask, space.grid, fixed_world_boxes=fixed_boxes, load_world_boxes=load_boxes, distance_mm=dist
        )
        if tag == "base":
            zones_base = zones
        zone_sensitivity[tag] = dict(
            distance_mm=dist,
            counts=zones["counts"],
            holding=_fem_probe.zone_stats(probe["von_mises_mpa"], probe["principal_mpa"], zones["holding"]),
            joint=_fem_probe.zone_stats(probe["von_mises_mpa"], probe["principal_mpa"], zones["joint"]),
            general=_fem_probe.zone_stats(probe["von_mises_mpa"], probe["principal_mpa"], zones["general"]),
        )

    assert zones_base is not None
    (i_h, i_j), (i_j2, i_g) = _section_indices(zones_base)
    gx = space.grid.centers_1d(0)
    idx_a = min(i_h, len(gx) - 1)
    idx_b = min(max(i_j2, i_j), len(gx) - 1)
    sections = dict(
        holding_joint_boundary=_fem_probe.section_force_moment(
            probe["tensor_mpa"], mask, space.grid, axis=0, index_mm=float(gx[idx_a])
        ),
        joint_general_boundary=_fem_probe.section_force_moment(
            probe["tensor_mpa"], mask, space.grid, axis=0, index_mm=float(gx[idx_b])
        ),
    )

    per_face = _fem_probe.per_face_reactions(probe, table, cond.fixed_faces)
    json_name = f"refine_{shape_cfg['name']}_load{int(load_n) if load_n == int(load_n) else load_n}_level{level}.json"
    json_path = out_dir / json_name

    record = dict(
        case_id=case_id,
        status="ok",
        shape=shape_cfg["name"],
        load_n=load_n,
        level=level,
        element_mm=fine_spacing,
        design_elements=int(space.mask.sum()),
        material_elements=int(mask.sum()),
        safety_factor=probe["safety_factor"],
        representative_mpa=probe["representative_mpa"],
        raw_max_mpa=probe["raw_max_mpa"],
        max_displacement_mm=probe["max_displacement_mm"],
        relative_residual=probe["relative_residual"],
        solver_used=probe["solver_used"],
        reactions_n=probe["reactions_n"].tolist(),
        per_face_reactions_n=per_face,
        zone_stats=dict(
            holding=zone_sensitivity["base"]["holding"],
            joint=zone_sensitivity["base"]["joint"],
            general=zone_sensitivity["base"]["general"],
        ),
        zone_sensitivity=zone_sensitivity,
        section_forces=sections,
        achieved_shift_note=None,
        model_input=dict(model=str(shape_cfg["model"].relative_to(REPO)), model_sha256=_common.sha256_file(shape_cfg["model"])),
        code_version=_common.git_state(),
        generated_at=_common.now_iso(),
        timing=timing,
    )
    if exact_subdivide:
        record["exact_subdivide"] = True
        record["exact_subdivide_diagnostics"] = exact_subdivide_diagnostics
    rel_json = str(json_path.relative_to(REPO))
    record["files"] = {json_name: rel_json}
    _common.write_atomic(json_path, json.dumps(_record_strip_internal(record), ensure_ascii=False, indent=1, default=float) + "\n")
    record["files"][f"{json_name}_sha256"] = _common.sha256_file(json_path)
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--shape",
        required=True,
        choices=["dilution_r-1", "path_2.5mm", "hanger_base", "hanger_r2", "hanger_step05", "hanger_transplant"],
    )
    parser.add_argument("--levels", nargs="+", type=int, default=[1, 2, 3])
    parser.add_argument("--loads", nargs="+", type=float, default=None)
    parser.add_argument("--distance-mm", type=float, default=None)
    parser.add_argument("--hanger-condition", default=None)
    parser.add_argument("--out-dir", type=Path, default=OUT)
    parser.add_argument(
        "--exact-subdivide",
        action="store_true",
        help="材料マスクを整数分割(体積保存)で細分格子へ配置する",
    )
    args = parser.parse_args()

    shape_cfg = _resolve_shape(args.shape, args.hanger_condition)
    loads = args.loads if args.loads is not None else [shape_cfg["default_load"]]
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger = _common.load_ledger(LEDGER)

    for load_n in loads:
        for level in args.levels:
            try:
                record = _run_one(
                    shape_cfg,
                    load_n,
                    level,
                    args.distance_mm,
                    out_dir,
                    exact_subdivide=args.exact_subdivide,
                )
            except Exception as exc:
                record = dict(
                    status="failed",
                    shape=args.shape,
                    load_n=load_n,
                    level=level,
                    error=f"{type(exc).__name__}: {exc}",
                    traceback=traceback.format_exc(),
                )
            case_id = record.get("case_id") or _common.condition_id(
                shape=args.shape, load_n=load_n, level=level
            )
            record.setdefault("case_id", case_id)
            ledger = _common.upsert_case(ledger, case_id, record)
            ledger["generated_at"] = _common.now_iso()
            _common.write_atomic(LEDGER, json.dumps(ledger, ensure_ascii=False, indent=1, default=float) + "\n")
            print(json.dumps({k: v for k, v in record.items() if k != "traceback"}, ensure_ascii=False, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
