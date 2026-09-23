"""最小幅 m(既定2=下限)で探索を1回回し、採用マスクを**ボクセルのまま**STLに書き出す(節I I2・節K の形)。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_min_width_floor.py --cells 3 --budget 60 --result C:\\tmp\\m3.json --mask-npz C:\\tmp\\m3.npz

hanger・要素サイズ要求(既定 2.4mm。実 spacing 2.387506mm)・目標(既定2.0)。最小幅は mm の固定値で渡さず、解決した実 spacing の
`--cells` 倍(段の値。ガードは `値 < 2×spacing` だけを断る)を渡し、`m == --cells` になったことを確かめてから測る。
**`--cells 1` は測れない**(結果は `error` 入りのJSONで、クラッシュはしない)。**判定はしない**(採用した形と数字を書き出すだけ)。`trace.DEFAULT_ROOT` を一時フォルダへ向け、
`output/optimization_runs` に何も残さない。STLは既定で `output/min_width/`(git 管理外)。
採用マスクの npz は `refine_adopted_mask.py` の入力になる。
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
REQUESTED_ELEMENT_SIZE_MM = 2.4
TARGET_SAFETY_FACTOR = 2.0


def voxel_surface(mask: np.ndarray, origin, spacing: float) -> tuple[np.ndarray, np.ndarray]:
    """材料セルのうち**空セル(格子の外を含む)と接する面だけ**の三角形メッシュ(頂点・面)。

    1面につき三角形2枚、巻き順は外向き(`mesh.write_stl` は巻き順から法線を計算する)。座標は
    `origin + spacing * 添字`(`origin` は要素 (0,0,0) の低い側の角)。頂点は面ごとに4つ持つ
    (共有しない。STLでは同じ)。marching cubes も平滑化も通さない。
    """
    material = np.asarray(mask, dtype=bool)
    padded = np.pad(material, 1, constant_values=False)
    origin = np.asarray(origin, dtype=float)
    quads: list[np.ndarray] = []
    for axis in range(3):
        u, v = [a for a in range(3) if a != axis]
        step_u, step_v = np.zeros(3), np.zeros(3)
        step_u[u] = step_v[v] = 1.0
        # 四角形を (角, +u, +u+v, +v) の順に回すと、法線は (u × v) の向き。
        along_axis_positive = np.cross(step_u, step_v)[axis] > 0
        for sign in (-1, +1):
            neighbour = np.roll(padded, -sign, axis=axis)[1:-1, 1:-1, 1:-1]
            cells = np.argwhere(material & ~neighbour)
            if not len(cells):
                continue
            corner = cells.astype(float)
            if sign > 0:
                corner[:, axis] += 1.0
            ring = [corner, corner + step_u, corner + step_u + step_v, corner + step_v]
            if (sign > 0) != along_axis_positive:
                ring = ring[::-1]
            quads.append(np.stack(ring, axis=1))
    if not quads:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)
    quad_array = np.concatenate(quads, axis=0)
    verts = origin + float(spacing) * quad_array.reshape(-1, 3)
    base = (np.arange(len(quad_array), dtype=np.int64) * 4)[:, None]
    faces = np.concatenate([base + [0, 1, 2], base + [0, 2, 3]], axis=0)
    return verts, faces


def _search_summary(result, best_mask: np.ndarray, keep: np.ndarray, m: int) -> dict:
    """探索の記録から数える数字: 評価の合否・`peel` の実績の合計・同じ候補の解き直しの回避・FEM前の大域検査の却下・
    規則の成否・侵食した芯の6連結成分数。"""
    from duocad.topopt import binary_search, min_width

    reduction = (result.diagnostics or {}).get("reduction") or {}
    peels = [step.get("peel", {}) for step in reduction.get("steps", [])]
    totals = {name: sum(int(p.get(name, 0)) for p in peels)
              for name in ("picked", "casualties", "islands", "skipped_support", "skipped_surface", "skipped_manifold",
                           "skipped_thickness", "skipped_connectivity", "skipped_core", "already_removed", "global_checks")}
    outcomes = [t.outcome for t in result.history]
    failed_checks: dict = {}
    for trial in result.history:
        if trial.outcome != "ok":
            for name, status in (trial.checks or {"no_checks": "fail"}).items():
                if status != "pass":
                    failed_checks[name] = failed_checks.get(name, 0) + 1
    core = ndimage.binary_erosion(np.pad(best_mask, m), structure=np.ones((m,) * 3, dtype=bool))
    return dict(
        evaluations_by_outcome={name: outcomes.count(name) for name in sorted(set(outcomes))},
        failed_checks_of_rejected_trials=failed_checks,
        keep_cells=int(keep.sum()), keep_cells_in_mask=int((best_mask & keep).sum()),
        mask_or_keep_cells=int((best_mask | keep).sum()),
        peel_totals=totals,
        peel_seconds=float(reduction.get("peel_seconds", 0.0)),
        repeated_candidates=int(reduction.get("repeated_candidates", 0)),
        global_min_width_rejections=sum(1 for t in result.history if t.checks == dict(min_width="fail")),
        rule_holds_with_keep_exempt=bool(binary_search._mask_satisfies_min_width(best_mask, m, keep)),
        rule_violating_cells=int((best_mask & ~keep & ~min_width.cube_opening(best_mask | keep, m)).sum()),
        eroded_core_6connected_parts=int(ndimage.label(core)[1]),
        material_6connected_parts=int(ndimage.label(best_mask)[1]),
    )


def _opening_removed(material: np.ndarray, size: int) -> dict:
    opened = ndimage.binary_opening(material, structure=np.ones((size,) * 3, dtype=bool))
    total, remaining = int(material.sum()), int(opened.sum())
    return {"structure": f"{size}x{size}x{size}", "material_before": total, "material_after": remaining,
            "removed": total - remaining, "removed_fraction": (total - remaining) / total if total else None}


def run(cells: int, budget: int, stl_dir: Path, mask_npz: Path | None,
        target: float = TARGET_SAFETY_FACTOR, element_size: float = REQUESTED_ELEMENT_SIZE_MM) -> dict:
    from duocad import optimize_run
    from duocad.server import Session
    from duocad.topopt import binary_search, design_space as ds, mesh, trace

    out: dict = {"model": str(MODEL), "element_size_requested_mm": element_size,
                 "target_safety_factor": target, "max_evaluations": int(budget)}
    with tempfile.TemporaryDirectory(prefix="topopt-trace-") as trace_root, \
            patch.object(trace, "DEFAULT_ROOT", Path(trace_root)):
        session = Session(None, 0.2, models_dir=MODEL.resolve().parent)
        session.open_document(MODEL.name)
        document = session.document
        bundle = optimize_run.build_conditions(document)
        limits = optimize_run.resolution_limits(document)
        spacing = optimize_run._snap_to_grid(
            limits["bbox_min"], limits["bbox_max"],
            optimize_run._resolve_element_size(limits, element_size))
        min_width_mm = None if cells == 1 else float(cells) * spacing
        min_width = optimize_run._resolve_min_width(spacing, min_width_mm)
        out.update(actual_spacing_mm=spacing, min_width_request_mm=min_width_mm, min_width=min_width)
        if min_width["cells"] != cells:
            out["error"] = f"m が {cells} にならなかった(m={min_width['cells']})。測定しない"
            return out

        space = ds.derive(bundle.conditions, spacing=spacing)
        table, mapping = optimize_run._map_boundary_conditions(space, bundle)
        out["grid_shape"] = list(space.grid.shape)
        out["design_space_elements"] = int(space.mask.sum())
        if mapping["status"] != "pass":
            out["error"] = f"確定表が不成立: {mapping['status']}"
            return out
        opts = optimize_run.OptimizeOptions(target_safety_factor=target, max_evaluations=budget,
                                            min_width_mm=min_width_mm, search_target_factor=1.0)
        conditions = optimize_run._build_search_conditions(document, space, bundle, opts, table=table, mapping=mapping)
        started = time.perf_counter()
        result = binary_search.find_best_feasible(space.mask, conditions, max_evaluations=budget,
                                                  min_width_cells=min_width["cells"])
        out["seconds"] = time.perf_counter() - started

    out.update(search_status=result.status, termination=result.termination, evaluations=int(result.evaluations))
    best = result.best_feasible
    if best is None:
        out["error"] = f"best_feasible が無い(status={result.status})"
        return out
    reduction = (result.diagnostics or {}).get("reduction") or {}
    out.update(
        accepted_elements=int(best.mask.sum()),
        material_fraction_of_design_space=float(best.mask.sum() / space.mask.sum()),
        coarse_safety_factor=float(best.safety_factor),
        reduction_stop_reason=reduction.get("stop_reason"),
        opening_2x2x2=_opening_removed(best.mask, 2),
        opening_3x3x3=_opening_removed(best.mask, 3),
        opening_4x4x4=_opening_removed(best.mask, 4),
        **_search_summary(result, best.mask, np.asarray(space.keep, dtype=bool), min_width["cells"]),
    )
    if mask_npz is not None:
        np.savez_compressed(mask_npz, mask=best.mask, origin=np.asarray(space.grid.origin, dtype=float),
                            spacing=float(space.grid.spacing))
    verts, faces = voxel_surface(best.mask, space.grid.origin, space.grid.spacing)
    stl_dir.mkdir(parents=True, exist_ok=True)
    stl_path = stl_dir / f"floor-m{cells}-{spacing:.6f}mm-voxel.stl"
    mesh.write_stl(stl_path, verts, faces)
    out.update(stl_path=str(stl_path), stl_triangles=int(len(faces)), stl_bytes=stl_path.stat().st_size)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cells", type=int, default=2, help="最小幅のセル数 m(既定2=下限)")
    parser.add_argument("--budget", type=int, default=20, help="評価予算(既定20)")
    parser.add_argument("--target", type=float, default=TARGET_SAFETY_FACTOR, help="探索にそのまま渡す目標安全率(製品の係数 2.4 は掛けない。既定2.0)")
    parser.add_argument("--element-size", type=float, default=REQUESTED_ELEMENT_SIZE_MM, help="要素サイズ要求 mm(既定2.4)")
    parser.add_argument("--stl-dir", type=Path, default=REPO_ROOT / "output" / "min_width")
    parser.add_argument("--result", type=Path, default=None, help="結果JSONの書き出し先(省略時は標準出力だけ)")
    parser.add_argument("--mask-npz", type=Path, default=None, help="採用マスクの保存先(省略時は保存しない)")
    args = parser.parse_args()
    started = time.perf_counter()
    result = run(args.cells, args.budget, args.stl_dir, args.mask_npz, args.target, args.element_size)
    result["total_seconds"] = time.perf_counter() - started
    text = _record.dumps(result, default=float)
    if args.result:
        args.result.write_text(text, encoding="utf-8")
    print(text, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
