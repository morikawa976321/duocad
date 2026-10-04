"""段階1入力棚卸しの「移植トラス」を再生成し、フル記録(mask/keep/origin/spacing/ハッシュ)を保存する。

`docs/TOPOPT_IDLE_MATERIAL.md` 2-1節の「2.2 mm のトラスを 2.0 mm の格子に写し…198.4 cm³」は
`output/idle_study/transplant.py`(gitignore対象・製品コード扱いされないその場スクリプト)の
stdout印字だけが根拠で、マスク・原点・ハッシュ付きの記録として保存されていなかった
(`01_PROTOCOL.md`棚卸しの「欠損は既存スクリプトから再生成する」に該当)。本スクリプトは同じ手口
(製品の`resolve_islands`/`acceptance.inspect`をそのまま使う)を、`topopt_checks/idle_validation/`配下の
追跡対象コードとして再実装し、`output/idle_validation_20260928/`へハッシュ付きで保存する。

    .\\.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\reproduce_stage1_transplant.py

既定は `docs/TOPOPT_IDLE_MATERIAL.md` と同じ条件(src=F0200_h2.2_w5 → dst=F0200_h2.0_w5, dilate=1)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401
import numpy as np  # noqa: E402
from scipy import ndimage  # noqa: E402

from _documents import open_session  # noqa: E402
from duocad import optimize_run  # noqa: E402
from duocad.topopt import acceptance, binary_search, design_space as ds  # noqa: E402
from sweep_smoothing_corpus import MODEL as HANGER_MODEL, _set_load  # noqa: E402

import _common  # noqa: E402

CORPUS = REPO / "output" / "smoothing_corpus"
OUT = REPO / "output" / "idle_validation_20260928"


def _load_voxel(name: str) -> dict:
    data = np.load(CORPUS / "voxel" / f"{name}.npz")
    return {k: data[k] for k in data.files}


def run(src: str, dst: str, dilate: int) -> dict:
    manifest = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
    rows = {r["name"]: r for r in manifest["rows"]}
    dst_row = rows[dst]
    src_voxel, dst_voxel = _load_voxel(src), _load_voxel(dst)

    doc = open_session(HANGER_MODEL).document
    _set_load(doc, dst_row["load_n"])
    bundle = optimize_run.build_conditions(doc)
    space = ds.derive(bundle.conditions, spacing=float(dst_voxel["spacing"]))
    opts = optimize_run.OptimizeOptions(element_size_mm=dst_row["element_size_requested_mm"],
                                        min_width_mm=dst_row["min_width"]["requested_mm"])
    table, mapping = optimize_run._map_boundary_conditions(space, bundle)
    cond = optimize_run._build_search_conditions(doc, space, bundle, opts, table=table, mapping=mapping)
    resolved = optimize_run._resolve_min_width(float(dst_voxel["spacing"]), dst_row["min_width"]["requested_mm"],
                                               max_cells=min(space.grid.shape))
    min_width_cells = resolved["cells"]

    # 先(dst)の格子のセル中心 → 元(src)の格子のセル添字(層をまたぐ契約: build_compact_models.pyの
    # コメントと同じ規則。原点は各格子が別々に持つので、世界座標を経由してから戻す)。
    idx = np.indices(space.grid.shape).reshape(3, -1).T
    world = np.asarray(space.grid.origin) + (idx + 0.5) * space.grid.spacing
    src_idx = np.floor((world - src_voxel["origin"]) / float(src_voxel["spacing"])).astype(int)
    inside = np.all((src_idx >= 0) & (src_idx < np.array(src_voxel["mask"].shape)), axis=1)
    transplanted = np.zeros(len(idx), bool)
    clipped = src_idx[inside]
    transplanted[inside] = src_voxel["mask"][clipped[:, 0], clipped[:, 1], clipped[:, 2]]
    mask = transplanted.reshape(space.grid.shape) & space.mask
    if dilate:
        mask = ndimage.binary_dilation(mask, iterations=dilate) & space.mask

    keep = np.asarray(cond.keep_cells, bool) if getattr(cond, "keep_cells", None) is not None else np.zeros_like(mask)
    mask = mask | (keep & space.mask)

    survivors, island_diag = binary_search.resolve_islands(mask, cond)
    if survivors:
        mask = max(survivors, key=lambda c: c.sum())

    report = acceptance.inspect(mask, mask, space.grid, **cond.inspect_kwargs(bbox_cache=None))
    checks = {k: v.get("status") for k, v in report["checks"].items()}
    # acceptance.inspectの6条件(watertight/single_solid/preserve/exclude/conditions/safety)は最小幅を
    # 含まない(00_COMPACT_MODELS.md 3-4節・task00契約②と同じ罠)。ここで別途検査する。
    exempt = binary_search._min_width_exempt(cond, mask.shape, min_width_cells)
    min_width_ok = bool(binary_search._mask_satisfies_min_width(mask, min_width_cells, exempt))

    cell_mm3 = space.grid.spacing ** 3
    stem = f"stage1_transplant_{src}_to_{dst}_dilate{dilate}"
    npz_path = OUT / f"{stem}.npz"
    OUT.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(npz_path, mask=mask, keep=keep, origin=np.asarray(space.grid.origin, float),
                        spacing=float(space.grid.spacing))

    record = dict(
        shape_kind="stage1_transplant_truss",
        src_condition=src, dst_condition=dst, dilate_iterations=dilate,
        src_input=dict(npz=str((CORPUS / "voxel" / f"{src}.npz").relative_to(REPO)),
                       sha256=_common.sha256_file(CORPUS / "voxel" / f"{src}.npz"),
                       spacing_mm=float(src_voxel["spacing"]), origin_mm=[float(v) for v in src_voxel["origin"]],
                       source_material_mm3=float(next(r for r in manifest["rows"] if r["name"] == src)["material_mm3"])),
        dst_condition_spec=dict(load_n=dst_row["load_n"], element_size_requested_mm=dst_row["element_size_requested_mm"],
                                min_width_requested_mm=dst_row["min_width"]["requested_mm"],
                                min_width_cells_resolved=min_width_cells,
                                min_width_guaranteed_mm=resolved.get("guaranteed_mm"),
                                native_material_mm3=float(dst_row["material_mm3"]), native_safety_factor=float(dst_row["safety_factor"])),
        grid=dict(origin_mm=[float(v) for v in space.grid.origin], spacing_mm=float(space.grid.spacing), shape=list(space.grid.shape)),
        material_mm3=float(mask.sum() * cell_mm3), material_cm3=round(float(mask.sum() * cell_mm3) / 1000, 1),
        cells=int(mask.sum()),
        island_resolution=dict(kept={k: v for k, v in island_diag.items() if k != "removed_islands"},
                                removed_island_cell_counts=[int(i["cells"]) for i in island_diag["removed_islands"]][:20]),
        checks=checks, min_width_ok=min_width_ok,
        safety_factor=report["checks"]["safety"].get("safety_factor"),
        target_safety_factor=report["checks"]["safety"].get("target_safety_factor"),
        model=str(HANGER_MODEL.relative_to(REPO)),
        code_version=dict(product=_common.git_state(),
                          idle_study_transplant_py_sha256=_common.sha256_file(REPO / "output" / "idle_study" / "transplant.py")
                          if (REPO / "output" / "idle_study" / "transplant.py").is_file() else None,
                          this_script_sha256=_common.sha256_file(Path(__file__))),
        generated_at=_common.now_iso(),
        files=dict(npz=str(npz_path.relative_to(REPO))),
    )
    record["files"]["npz_sha256"] = _common.sha256_file(npz_path)
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default="F0200_h2.2_w5")
    parser.add_argument("--dst", default="F0200_h2.0_w5")
    parser.add_argument("--dilate", type=int, default=1)
    args = parser.parse_args()
    record = run(args.src, args.dst, args.dilate)
    out_json = OUT / f"stage1_transplant_{args.src}_to_{args.dst}_dilate{args.dilate}.json"
    _common.write_atomic(out_json, json.dumps(record, ensure_ascii=False, indent=1, default=float) + "\n")
    print(json.dumps({k: v for k, v in record.items() if k not in ("island_resolution",)}, ensure_ascii=False, indent=1, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
