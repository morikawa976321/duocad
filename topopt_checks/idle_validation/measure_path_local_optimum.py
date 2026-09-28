"""原因2(経路喪失)の認定: `topopt_idle_path_small.json` で、要素サイズ違いの局所解、
1回に削る割合の上限(`max_step_ratio`)の反実仮想、別格子の斜材入り候補の移植を行う。

    .\\.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\measure_path_local_optimum.py mesh --sizes 3.0 2.5 2.0 1.5
    .\\.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\measure_path_local_optimum.py step --size 2.0 --max-step-ratio 0.05
    .\\.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\measure_path_local_optimum.py transplant --src-tag mesh_2.5mm --dst-size 2.0

`mesh`/`step` は探索直後で止める(`binary_search.find_best_feasible` を直接呼ぶ。仕上げは回さない)。
採用マスクは `output/idle_validation_20260928/path_mask_<tag>.npz` に保存し、`transplant` はそれを
読んで別格子へ写す(製品と同じ `resolve_islands`/`acceptance.inspect` で再検査する)。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401
import numpy as np  # noqa: E402
from scipy import ndimage  # noqa: E402

import _record  # noqa: E402
from _documents import open_session  # noqa: E402

from duocad import optimize_run  # noqa: E402
from duocad.topopt import acceptance, binary_search, design_space as ds  # noqa: E402

MODELS_DIR = REPO / "models"
OUT = REPO / "output" / "idle_validation_20260928"
MODEL = MODELS_DIR / "topopt_idle_path_small.json"
#: 物理mmで固定する(セル数指定 `cells * size` だと解像度ごとに実際のmmが動き、
#: 解像度をまたぐ比較〈mesh/step/transplant〉の「同条件」が崩れる。docs 3-1節)。
#: **6.0mmは3.0mm要素での下限(`optimize_run._resolve_min_width`が課す2×要素サイズ)と一致する
#: 最小の値** —— 3.0/2.5/2.0/1.5mmの4解像度すべてで`_resolve_min_width`がエラーにならない
#: 唯一の整数mm値(4.0/5.0は3.0mm要素で下限6.0mm未満としてエラーになる。実測済み)。
MIN_WIDTH_MM = 6.0


def _search_conditions(doc, size: float):
    """`(space, cond, min_width_cells)` を返す。**`min_width_cells` は`find_best_feasible`へ
    明示で渡すこと** —— `SearchConditions`(`_build_search_conditions`の戻り値)は最小幅を持たず、
    製品入口(`optimize_run._run`)は`_resolve_min_width`の戻り値を`find_best_feasible`の
    キーワード引数として**別経路**で渡している(`duocad/optimize_run.py`: 1000行`_resolve_min_width`呼び出し・
    1119行`search_kwargs`への代入・1141行`find_best_feasible`呼び出し)。ここを省くと
    `find_best_feasible`の既定`min_width_cells=1`(制約なし)で走り、製品より細い部材を許してしまう。"""
    bundle = optimize_run.build_conditions(doc)
    space = ds.derive(bundle.conditions, spacing=size)
    opts = optimize_run.OptimizeOptions(element_size_mm=size, min_width_mm=MIN_WIDTH_MM)
    table, mapping = optimize_run._map_boundary_conditions(space, bundle)
    cond = optimize_run._build_search_conditions(doc, space, bundle, opts, table=table, mapping=mapping)
    resolved = optimize_run._resolve_min_width(size, MIN_WIDTH_MM, max_cells=min(space.grid.shape))
    return space, cond, resolved["cells"]


def _run_one(size: float, *, max_step_ratio: float, tag: str) -> dict:
    session = open_session(MODEL)
    doc = session.document
    space, cond, min_width_cells = _search_conditions(doc, size)
    t0 = time.perf_counter()
    r = binary_search.find_best_feasible(space.mask, cond, max_evaluations=1000, max_step_ratio=max_step_ratio,
                                         min_width_cells=min_width_cells)
    seconds = time.perf_counter() - t0
    best = r.best_feasible
    record = dict(tag=tag, size=size, min_width_mm=MIN_WIDTH_MM, min_width_cells=min_width_cells,
                  max_step_ratio=max_step_ratio, status=r.status,
                  termination=r.termination, evaluations=r.evaluations, seconds=seconds,
                  design_elements=int(space.element_count),
                  material_mm3=None if best is None else float(best.material_mm3),
                  cells_final=None if best is None else int(best.mask.sum()),
                  safety_factor=None if best is None else float(best.safety_factor))
    OUT.mkdir(parents=True, exist_ok=True)
    if best is not None:
        np.savez_compressed(OUT / f"path_mask_{tag}.npz", mask=best.mask, keep=np.asarray(space.model.keep, bool),
                            origin=np.asarray(space.grid.origin, float), spacing=float(space.grid.spacing))
    return record


def cmd_mesh(sizes: list[float]) -> None:
    rows = [_run_one(size, max_step_ratio=binary_search.DEFAULT_MAX_STEP_RATIO, tag=f"mesh_{size}mm") for size in sizes]
    _record.write(OUT / "path_mesh_sweep.json", dict(model=str(MODEL), rows=rows), default=str)
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))


def cmd_step(size: float, max_step_ratio: float) -> None:
    row = _run_one(size, max_step_ratio=max_step_ratio, tag=f"step_{size}mm_r{max_step_ratio}")
    _record.write(OUT / f"path_step_ratio_{size}mm_r{max_step_ratio}.json", row, default=str)
    print(json.dumps(row, ensure_ascii=False))


def cmd_transplant(src_tag: str, dst_size: float, closing_iterations: int) -> None:
    s = np.load(OUT / f"path_mask_{src_tag}.npz")
    session = open_session(MODEL)
    doc = session.document
    space, cond, min_width_cells = _search_conditions(doc, dst_size)

    idx = np.indices(space.grid.shape).reshape(3, -1).T
    w = np.asarray(space.grid.origin) + (idx + 0.5) * space.grid.spacing
    j = np.floor((w - s["origin"]) / float(s["spacing"])).astype(int)
    ok = np.all((j >= 0) & (j < np.array(s["mask"].shape)), axis=1)
    m = np.zeros(len(idx), bool)
    jj = j[ok]
    m[ok] = s["mask"][jj[:, 0], jj[:, 1], jj[:, 2]]
    mask = m.reshape(space.grid.shape) & space.mask
    if closing_iterations:
        mask = ndimage.binary_closing(mask, structure=np.ones((3, 3, 3)), iterations=closing_iterations) & space.mask
    keep = np.asarray(cond.keep_cells, bool) if getattr(cond, "keep_cells", None) is not None else np.zeros_like(mask)
    mask |= keep & space.mask
    survivors, diag = binary_search.resolve_islands(mask, cond)
    best = max(survivors, key=lambda c: c.sum()) if survivors else None
    record = dict(src=src_tag, dst_size=dst_size, closing_iterations=closing_iterations, min_width_cells=min_width_cells,
                  islands=diag.get("components"), surviving_components=diag.get("surviving_components"))
    if best is None:
        record["status"] = "no_surviving_component"
    else:
        # `acceptance.inspect`の6条件チェックに最小幅は無い(探索時のpeelでだけ強制される制約なので、
        # 移植した形は明示で確かめないと素通りする)。芯の連結(`min_width.CoreConnectivity`)も
        # `_mask_satisfies_min_width`が内部で使う判定と同じものを流用する。
        exempt = binary_search._min_width_exempt(cond, best.shape, min_width_cells)
        min_width_ok = bool(binary_search._mask_satisfies_min_width(best, min_width_cells, exempt))
        rep = acceptance.inspect(best, best, space.grid, **cond.inspect_kwargs(bbox_cache=None))
        cell_mm3 = space.grid.spacing ** 3
        record.update(cells=int(best.sum()), material_mm3=round(float(best.sum()) * cell_mm3, 1),
                      checks={k: v.get("status") for k, v in rep["checks"].items()}, min_width_ok=min_width_ok,
                      safety_factor=rep["checks"]["safety"].get("safety_factor"),
                      target_safety_factor=rep["checks"]["safety"].get("target_safety_factor"))
    OUT.mkdir(parents=True, exist_ok=True)
    tag = f"transplant_{src_tag}_to_{dst_size}mm_close{closing_iterations}"
    _record.write(OUT / f"path_{tag}.json", record, default=str)
    print(json.dumps(record, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_mesh = sub.add_parser("mesh")
    p_mesh.add_argument("--sizes", type=float, nargs="+", required=True)
    p_step = sub.add_parser("step")
    p_step.add_argument("--size", type=float, required=True)
    p_step.add_argument("--max-step-ratio", type=float, required=True)
    p_transplant = sub.add_parser("transplant")
    p_transplant.add_argument("--src-tag", required=True, help="path_mask_<tag>.npz の <tag>")
    p_transplant.add_argument("--dst-size", type=float, required=True)
    p_transplant.add_argument("--closing-iterations", type=int, default=0)
    args = parser.parse_args()
    if args.cmd == "mesh":
        cmd_mesh(args.sizes)
    elif args.cmd == "step":
        cmd_step(args.size, args.max_step_ratio)
    elif args.cmd == "transplant":
        cmd_transplant(args.src_tag, args.dst_size, args.closing_iterations)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
