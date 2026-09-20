"""最小幅の下限(m=2)で探索を1回回し、採用マスクを**ボクセルのまま**STLに書き出す(節I I2 の形)。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_min_width_floor.py --budget 20 --result C:\\tmp\\floor.json

hanger・要素サイズ要求 2.4mm・目標2.0。最小幅は mm の固定値で渡さず、解決した実 spacing の
2倍(下限そのもの。ガードは `値 < 2×spacing` だけを断る)を渡し、`m == 2` になったことを確かめてから測る。
**判定はしない**(採用した形と数字を書き出すだけ)。`trace.DEFAULT_ROOT` を一時フォルダへ向け、
`output/optimization_runs` に何も残さない。STLは既定で `output/min_width/`(git 管理外)。
"""
from __future__ import annotations

import argparse
import json
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


def _opening_removed(material: np.ndarray, size: int) -> dict:
    opened = ndimage.binary_opening(material, structure=np.ones((size,) * 3, dtype=bool))
    total, remaining = int(material.sum()), int(opened.sum())
    return {"structure": f"{size}x{size}x{size}", "material_before": total, "material_after": remaining,
            "removed": total - remaining, "removed_fraction": (total - remaining) / total if total else None}


def run(budget: int, stl_dir: Path, mask_npz: Path | None) -> dict:
    from duocad import optimize_run
    from duocad.server import Session
    from duocad.topopt import binary_search, design_space as ds, mesh, trace

    out: dict = {"model": str(MODEL), "element_size_requested_mm": REQUESTED_ELEMENT_SIZE_MM,
                 "target_safety_factor": TARGET_SAFETY_FACTOR, "max_evaluations": int(budget)}
    with tempfile.TemporaryDirectory(prefix="topopt-trace-") as trace_root, \
            patch.object(trace, "DEFAULT_ROOT", Path(trace_root)):
        session = Session(None, 0.2, models_dir=MODEL.resolve().parent)
        session.open_document(MODEL.name)
        document = session.document
        bundle = optimize_run.build_conditions(document)
        limits = optimize_run.resolution_limits(document)
        spacing = optimize_run._snap_to_grid(
            limits["bbox_min"], limits["bbox_max"],
            optimize_run._resolve_element_size(limits, REQUESTED_ELEMENT_SIZE_MM))
        min_width_mm = 2.0 * spacing
        min_width = optimize_run._resolve_min_width(spacing, min_width_mm)
        out.update(actual_spacing_mm=spacing, min_width_request_mm=min_width_mm, min_width=min_width)
        if min_width["cells"] != 2:
            out["error"] = f"m が 2 にならなかった(m={min_width['cells']})。測定しない"
            return out

        space = ds.derive(bundle.conditions, spacing=spacing)
        table, mapping = optimize_run._map_boundary_conditions(space, bundle)
        out["grid_shape"] = list(space.grid.shape)
        out["design_space_elements"] = int(space.mask.sum())
        if mapping["status"] != "pass":
            out["error"] = f"確定表が不成立: {mapping['status']}"
            return out
        opts = optimize_run.OptimizeOptions(target_safety_factor=TARGET_SAFETY_FACTOR, max_evaluations=budget,
                                            min_width_mm=min_width_mm)
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
        skipped_thickness_total=sum(int(s.get("peel", {}).get("skipped_thickness", 0))
                                    for s in reduction.get("steps", [])),
        opening_2x2x2=_opening_removed(best.mask, 2),
        opening_3x3x3=_opening_removed(best.mask, 3),
    )
    if mask_npz is not None:
        np.savez_compressed(mask_npz, mask=best.mask, origin=np.asarray(space.grid.origin, dtype=float),
                            spacing=float(space.grid.spacing))
    verts, faces = voxel_surface(best.mask, space.grid.origin, space.grid.spacing)
    stl_dir.mkdir(parents=True, exist_ok=True)
    stl_path = stl_dir / f"floor-m2-{spacing:.6f}mm-voxel.stl"
    mesh.write_stl(stl_path, verts, faces)
    out.update(stl_path=str(stl_path), stl_triangles=int(len(faces)), stl_bytes=stl_path.stat().st_size)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--budget", type=int, default=20, help="評価予算(既定20)")
    parser.add_argument("--stl-dir", type=Path, default=REPO_ROOT / "output" / "min_width")
    parser.add_argument("--result", type=Path, default=None, help="結果JSONの書き出し先(省略時は標準出力だけ)")
    parser.add_argument("--mask-npz", type=Path, default=None, help="採用マスクの保存先(省略時は保存しない)")
    args = parser.parse_args()
    started = time.perf_counter()
    result = run(args.budget, args.stl_dir, args.mask_npz)
    result["total_seconds"] = time.perf_counter() - started
    text = json.dumps(result, ensure_ascii=False, indent=2, default=float)
    if args.result:
        args.result.write_text(text, encoding="utf-8")
    print(text, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
