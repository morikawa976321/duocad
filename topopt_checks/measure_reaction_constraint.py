"""反力の下限制約(`simp.optimize(reaction_min=...)`)を実物モデルで振る実測。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_reaction_constraint.py ^
        --model hanger_test.json --spacing 2.4 --ratio 0.30 --eps none,0.10,0.20 --merge

測るのは2つ:

1. **各拘束面が受け持つ反力の内訳**(`run_topopt` の戻り値の `"reactions"`)。
   構造最適化は荷重を運ばない材料を置かないので、制約が無いと拘束面の一部が
   `fraction ≈ 0` になり、そこの保持ソリッドが浮き島になって `mesh.remove_islands` に消える。
2. **保持ソリッドが結果に残ったか**。判定は2本立てで、
   (a) 保持ソリッドの各連結成分の要素が、平滑化・島除去のあとの密度場
   (`MeshResult.rho_clean`)で生きている割合、(b) 保持ソリッドのワールド bbox が
   結果メッシュの bbox に収まっているか。**(a) が本命**で、(b) はタスクが名指しした
   粗い判定(bbox は片方の成分が消えても他方が広ければ通ってしまう)。

**hanger では ε の下限を測れない(2026-09-11 実測)。** 粗い格子でも本番の 2.412mm でも、
制約なしで3枚とも 0.97 以上を受け持つ —— 制約が1度も効かないので、どの ε でも同じ形が出る。
ε を振るための場は `--far-wall`(合成題材)のほうにある。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_reaction_constraint.py ^
        --far-wall --eps none,0.02,0.05,0.10,0.20,0.35,0.50,0.70 --merge

生データは `duocad/topopt/output/reaction_constraint.json`(`--merge` で段ごとに足す)。
"""

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


def run_far_wall(
    values: list[float | None], *, max_iter: int, threads: int | None
) -> dict:
    """合成題材(遠い壁)で ε を振る。**制約が実際に効く唯一の場**なので代償はここで測る。

    題材は `check_simp.two_wall_case`(回帰検査と同じもの)。**別に組み直さない** ——
    組み直すと docs/TOPOPT_REACTION.md の表を検査が固定できなくなる。
    """
    from check_simp import FAR_WALL_GRID, two_wall_case

    nelx, nely, nelz = FAR_WALL_GRID
    fixed, load, groups = two_wall_case()
    common: dict[str, Any] = dict(
        volfrac=0.15,
        max_iter=max_iter,
        change_tol=0.0,
        fixed_dofs=fixed,
        load=load,
        reactions=groups,
        threads=threads,
        update="mma",
    )
    rows: list[dict[str, Any]] = []
    baseline = None
    for eps in values:
        label = "none" if eps is None else f"{eps:g}"
        print(f"--- eps={label} 開始", flush=True)
        started = time.perf_counter()
        failure = None
        try:
            result = simp.optimize(nelx, nely, nelz, reaction_min=eps, **common)
        except simp.ReactionTargetError as error:
            # **満たせなかったことも1行として残す。** 例外に付いてくる `Result` から
            # そこまでの反力とコンプライアンスは読める。
            failure = {"kind": "ReactionTargetError", "message": str(error)}
            result = error.result
        compliance = float(result.history[-1].compliance)
        if baseline is None:
            baseline = compliance
        row = {
            "eps": eps,
            "compliance": compliance,
            "compliance_penalty": compliance / baseline - 1.0,
            "fractions": {item.name: item.fraction for item in result.reactions},
            "seconds": float(time.perf_counter() - started),
            "iterations": len(result.history),
            "stopped_by": result.stopped_by,
            "failure": failure,
        }
        rows.append(row)
        share = {name: round(value, 6) for name, value in row["fractions"].items()}
        print(
            f"    {row['seconds']:.0f}秒 C={compliance:.6g} "
            f"({row['compliance_penalty']:+.1%}) 反力={share}",
            flush=True,
        )
    return {
        "case": "far-wall",
        "grid": list(FAR_WALL_GRID),
        "volfrac": 0.15,
        "threads": threads if threads is not None else simp.PARALLEL_THREADS,
        "requested_update": "mma",
        "cases": rows,
    }


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
    parser.add_argument(
        "--far-wall",
        action="store_true",
        help="実物モデルではなく合成題材(遠い壁)で振る。**制約が効く唯一の場**",
    )
    args = parser.parse_args()

    values: list[float | None] = [
        None if item.strip() == "none" else float(item) for item in args.eps.split(",")
    ]
    if args.far_wall:
        # **既定は1スレッド。** 表の数字(docs/TOPOPT_REACTION.md)を取ったときの条件で、
        # 40x8x8 は並列にしても速くならない。
        payload = run_far_wall(
            values,
            max_iter=args.max_iter,
            threads=1 if args.threads is None else args.threads,
        )
        return _write(args, args.tag or "far-wall-40x8x8", payload)

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
