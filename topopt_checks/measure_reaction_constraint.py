"""models/*.json の条件で反力下限制約の効果と保持領域の生存を測る。

--model hanger_test.json --spacing 2.4 --ratio 0.30 --eps none,0.10,0.20
制約あり/なしの比較では --update mma を指定して更新則を揃える。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401
import numpy as np
from scipy import ndimage

from duocad.topopt import OUTPUT_DIR
from duocad.topopt import design_space as ds
from duocad.topopt import mesh as ms
from duocad.topopt import simp

OUTPUT_JSON = OUTPUT_DIR / "reaction_constraint.json"
STL_DIR = OUTPUT_DIR / "reaction_constraint"


def build_space(model: str, spacing: float):
    """製品の条件から塊を起こす。**自分で境界条件を組み直さない**(別の問題を測ることになる)。"""
    from duocad.optimize_run import build_conditions
    from duocad.session import Session

    session = Session(None, 0.2)
    session.open_document(model)
    bundle = build_conditions(session.document)
    return ds.derive(bundle.conditions, spacing=spacing), bundle.forces


def keep_survival(space, surface) -> list[dict]:
    """保持ソリッドの連結成分ごとに「結果に残ったか」を2本立てで測る。"""
    labels, count = ndimage.label(np.asarray(space.model.keep))
    grid = space.grid
    origin = np.asarray(grid.origin, dtype=float)
    alive = surface.rho_clean > ms.DEFAULT_LEVEL
    verts = origin + np.asarray(surface.verts_smooth, dtype=float) * float(grid.spacing)
    mesh_lo, mesh_hi = verts.min(axis=0), verts.max(axis=0)
    slack = float(grid.spacing)
    rows = []
    for index in range(1, count + 1):
        block = labels == index
        cells = np.argwhere(block)
        lo = origin + cells.min(axis=0) * grid.spacing
        hi = origin + (cells.max(axis=0) + 1) * grid.spacing
        rows.append(
            {
                "component": index,
                "elements": int(block.sum()),
                "alive_ratio": float(alive[block].mean()),
                "world_box": [lo.tolist(), hi.tolist()],
                "inside_mesh_box": bool(
                    np.all(lo >= mesh_lo - slack) and np.all(hi <= mesh_hi + slack)
                ),
            }
        )
    return rows


def run_case(
    space,
    forces,
    *,
    ratio: float,
    eps: float | None,
    max_iter: int,
    threads: int | None,
    update: str | None,
    stl_path: Path,
) -> dict:
    capacity = space.element_count * space.grid.element_volume
    started = time.perf_counter()
    failure = None
    with simp.collect_solve_stats() as stats:
        try:
            out = ds.run_topopt(
                space,
                material_mm3=capacity * ratio,
                max_iter=max_iter,
                forces=forces,
                threads=threads,
                update=update,
                reaction_min=eps,
                stl_path=stl_path,
            )
        except simp.ReactionTargetError as error:
            # **満たせなかったことも観測**。`Result` は例外に付いてくるので、そこまでの
            # 反力の内訳は読める(メッシュは作られていないので生存率は測れない)。
            failure = {
                "kind": "ReactionTargetError",
                "message": str(error),
                "reactions": [
                    {"name": item.name, "fraction": item.fraction}
                    for item in error.reactions
                ],
            }
            out = None
    seconds = time.perf_counter() - started
    row: dict[str, Any] = {
        "ratio": ratio,
        "eps": eps,
        "requested_update": update,
        "max_iter": max_iter,
        "seconds": seconds,
        "solves": int(stats["solves"]),
        "cg_iterations": int(stats["cg_iterations"]),
        "mg_setup_seconds": float(stats["mg_setup_seconds"]),
        "failure": failure,
    }
    if out is None:
        return row
    history = out["result"].history
    row.update(
        {
            "update": out["update"],
            "iterations": out["iterations"],
            "seconds_per_iteration": float(
                np.median([item.seconds for item in history])
            ),
            "compliance_first": out["compliance_first"],
            "compliance_last": out["compliance_last"],
            "achieved_material_mm3": out["achieved_material_mm3"],
            "target_material_mm3": out["target_material_mm3"],
            "reactions": out["reactions"],
            "keep": keep_survival(space, out["surface"]),
            "triangles": int(len(out["surface"].faces)),
            "stl": str(stl_path),
        }
    )
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="hanger_test.json")
    parser.add_argument("--spacing", type=float, default=2.4)
    parser.add_argument("--ratio", type=float, default=0.30)
    parser.add_argument(
        "--eps",
        default="none",
        help='カンマ区切り。"none" は制約なし(既定の更新則)。例: none,0.10,0.20',
    )
    parser.add_argument("--max-iter", type=int, default=40)
    parser.add_argument("--threads", type=int, default=None)
    parser.add_argument(
        "--update",
        default=None,
        help='更新則の上書き。**制約あり/なしを比べるときは "mma" を明示すること** —— '
        "省くと制約なしの段だけ OC になり、更新則の差が制約の差に化ける",
    )
    parser.add_argument("--tag", default=None)
    parser.add_argument("--out", default=str(OUTPUT_JSON))
    parser.add_argument("--merge", action="store_true")
    args = parser.parse_args()

    values: list[float | None] = [
        None if item.strip() == "none" else float(item) for item in args.eps.split(",")
    ]
    space, forces = build_space(args.model, args.spacing)
    tag = args.tag or f"{Path(args.model).stem}-{args.spacing:g}mm-{args.ratio:g}"
    STL_DIR.mkdir(parents=True, exist_ok=True)
    print(
        f"{args.model} spacing={space.grid.spacing:.4f} 格子={space.grid.shape} "
        f"塊={space.element_count} 拘束面={space.fixed_faces} 荷重面={space.load_faces}",
        flush=True,
    )
    rows = []
    for eps in values:
        label = "none" if eps is None else f"{eps:g}"
        print(f"--- eps={label} 開始", flush=True)
        row = run_case(
            space,
            forces,
            ratio=args.ratio,
            eps=eps,
            max_iter=args.max_iter,
            threads=args.threads,
            update=args.update,
            stl_path=STL_DIR / f"{tag}-eps{label}.stl",
        )
        rows.append(row)
        if row.get("reactions") is not None:
            share = [f"{item['fraction']:.4f}" for item in row["reactions"]]
            keep = [f"{item['alive_ratio']:.3f}" for item in row["keep"]]
            print(
                f"    {row['seconds']:.0f}秒 C={row['compliance_last']:.6g} "
                f"反力={share} 保持の生存={keep}",
                flush=True,
            )
        else:
            print(f"    {row['seconds']:.0f}秒 失敗: {row['failure']['kind']}", flush=True)

    payload = {
        "model": args.model,
        "spacing": float(space.grid.spacing),
        "grid": list(space.grid.shape),
        "elements": int(space.element_count),
        "fixed_faces": list(space.fixed_faces),
        "load_faces": list(space.load_faces),
        "threads": args.threads if args.threads is not None else simp.PARALLEL_THREADS,
        "requested_update": args.update,
        "cases": rows,
    }
    return _write(args, tag, payload)


def _write(args, tag: str, payload: dict) -> int:
    out_path = Path(args.out)
    document = {"stages": {}}
    if args.merge and out_path.exists():
        document = json.loads(out_path.read_text(encoding="utf-8"))
    document.setdefault("stages", {})[tag] = payload
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=1, default=float),
        encoding="utf-8",
    )
    print(f"MEASURED -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
