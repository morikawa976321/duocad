"""採用マスクを、形は変えず各要素を k×k×k に割って解き直す(`docs/TOPOPT_THICKNESS.md` 節H のやり方)。

    .\\.venv\\Scripts\\python.exe topopt_checks\\refine_adopted_mask.py --mask-npz C:\\tmp\\m3.npz --k 1 2 3 --result C:\\tmp\\m3-refine.json

`--mask-npz` は `measure_min_width_floor.py --mask-npz` が書く採用マスク(`mask`・`origin`・`spacing`)。確定表は
細分した形に接する節点で作り、`acceptance.inspect` へ直接渡す(節H「再現」)。k=1 は探索の格子そのもので、探索の
安全率と一致する。**判定はしない**(k ごとの要素数・安全率・最大変位などを書き出すだけ)。
`trace.DEFAULT_ROOT` を一時フォルダへ向け、`output/optimization_runs` に何も残さない。
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
import _record
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL = REPO_ROOT / "models" / "hanger_bar_attachment.json"


def refine(mask: np.ndarray, origin: np.ndarray, spacing: float, ks: list[int], target: float, model: Path) -> list[dict]:
    from duocad import optimize_run
    from duocad.server import Session
    from duocad.topopt import acceptance, anchor, binary_search, design_space as ds, trace
    from duocad.topopt.voxelize import Grid

    rows: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="topopt-trace-") as trace_root, \
            patch.object(trace, "DEFAULT_ROOT", Path(trace_root)):
        session = Session(None, 0.2, models_dir=model.resolve().parent)
        session.open_document(model.name)
        document = session.document
        bundle = optimize_run.build_conditions(document)
        space = ds.derive(bundle.conditions, spacing=spacing)
        if tuple(space.grid.shape) != mask.shape or not np.allclose(space.grid.origin, origin):
            raise SystemExit(f"格子が違う: モデルから 形 {tuple(space.grid.shape)}・原点 {tuple(space.grid.origin)}、"
                             f"マスク 形 {mask.shape}・原点 {tuple(origin)}")
        preserves = tuple((bid, document.get_body(bid).shape) for bid in bundle.keep_body_ids)
        excludes = tuple((bid, document.get_body(bid).shape) for bid in bundle.exclude_body_ids)
        for k in ks:
            fine = mask
            for axis in range(3):
                fine = np.repeat(fine, k, axis=axis)
            grid = Grid(origin=tuple(space.grid.origin), spacing=float(space.grid.spacing) / k,
                        shape=tuple(int(n) * k for n in space.grid.shape))
            table = anchor.build_anchors(space.soup, grid, allowed_nodes=acceptance.active_nodes(fine))
            conditions = binary_search.SearchConditions(
                grid=grid, table=table, fixed_faces=space.fixed_faces, load_faces=space.load_faces,
                forces=bundle.forces, preserves=preserves, excludes=excludes, target_safety_factor=target)
            started = time.perf_counter()
            report = acceptance.inspect(fine, fine, grid, **conditions.inspect_kwargs(bbox_cache=None))
            safety = report.get("checks", {}).get("safety") or {}
            row = dict(k=k, spacing=grid.spacing, elements=int(fine.sum()), status=report.get("status"),
                       seconds=time.perf_counter() - started,
                       failed=[n for n, c in report.get("checks", {}).items() if c.get("status") != "pass"],
                       safety={key: safety.get(key) for key in safety if not isinstance(safety.get(key), (dict, list))})
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False, default=float), flush=True)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mask-npz", type=Path, required=True, help="採用マスク(mask・origin・spacing を持つ npz)")
    parser.add_argument("--k", type=int, nargs="+", default=[1, 2, 3], help="各要素の分割数 k(既定 1 2 3)")
    parser.add_argument("--target", type=float, default=2.0, help="目標安全率(既定2.0。判定には使わず条件に渡すだけ)")
    parser.add_argument("--model", type=Path, default=MODEL)
    parser.add_argument("--result", type=Path, default=None, help="結果JSONの書き出し先(省略時は標準出力だけ)")
    args = parser.parse_args()
    data = np.load(args.mask_npz)
    rows = refine(data["mask"].astype(bool), data["origin"], float(data["spacing"]), args.k, args.target, args.model)
    if args.result:
        _record.write(args.result, rows, default=float)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
