"""探索の途中で採用された形が、安全率が高いうちは太いのかを、FEM なしで数える。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_thickness_along_search.py --run output\\min_width\\projected-peel-20260920\\runs\\project_m2

各採用マスク(`accepted_*.npz`)について、2・3・4 立方の opening で落ちる材料の割合(= その太さ未満の所にある材料)を出し、
`progress.log` の安全率(`inspect #N cells=C status=pass sf=S`)と要素数 C で突き合わせる。読むだけ。60,000 要素を超える形は飛ばす。
**判定はしない**。結果は `docs/TOPOPT_TARGET_FACTOR.md` に転記してある。

`--run` の入力(実行の記録フォルダ)は git 管理外。無ければ落ちる。
出力は既定で標準出力だけ(`--out` を渡したときだけファイルへ書く)。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import numpy as np
from scipy import ndimage

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
import _record

MAX_CELLS = 60_000


def opened_away(mask, size):
    cube = np.ones((size,) * 3, bool)
    return 1.0 - ndimage.binary_opening(mask, structure=cube).sum() / mask.sum()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", type=Path, required=True, help="`accepted_*.npz` と `progress.log` があるフォルダ")
    parser.add_argument("--out", type=Path, default=None, help="結果JSONの書き出し先(省略時は標準出力だけ)")
    args = parser.parse_args()
    run = args.run

    sf_by_cells = {}
    for line in (run / "progress.log").read_text(encoding="utf-8", errors="replace").splitlines():
        hit = re.search(r"inspect #(\d+) cells=(\d+) status=pass sf=([\d.]+)", line)
        if hit:
            sf_by_cells[int(hit.group(2))] = float(hit.group(3))

    rows = []
    for path in sorted(run.glob("accepted_*.npz")):
        data = np.load(path)
        mask = data[data.files[0] if "mask" not in data.files else "mask"].astype(bool)
        cells = int(mask.sum())
        if cells > MAX_CELLS:
            continue
        box = tuple(slice(max(0, idx.min() - 4), idx.max() + 5) for idx in np.nonzero(mask))
        small = mask[box]
        rows.append(dict(file=path.name, cells=cells, sf=sf_by_cells.get(cells),
                         below2=opened_away(small, 2), below3=opened_away(small, 3), below4=opened_away(small, 4)))

    print(f"{'file':<18} {'cells':>7} {'sf(k=1)':>8} {'<2厚':>7} {'<3厚':>7} {'<4厚':>7}")
    for row in rows:
        sf = f"{row['sf']:.3f}" if row["sf"] else "?"
        print(f"{row['file']:<18} {row['cells']:>7} {sf:>8} {row['below2']:>7.1%} {row['below3']:>7.1%} {row['below4']:>7.1%}")
    if args.out:
        _record.write(args.out, rows, default=float)
    return 0


if __name__ == "__main__":
    sys.exit(main())
