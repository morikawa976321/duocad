"""`leftover_analysis.py` の結果を2枚の図にする(表面セルの散布図。横から・斜め上から)。

- `leftover_render.png`: 部材の太さ程度の近傍での応力の最大(探索の上限との比)で塗る。
- `leftover_verdict.png`: 低応力の塊の表面セルを、1セルずつ `peel` に通したときの判定(削れる / どの規則で止まるか)で塗る。

    python topopt_checks\\stress_gate\\leftover_render.py hanger_L90_h3.95_w7.9 ...
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import Normalize  # noqa: E402
from scipy import ndimage  # noqa: E402

#: `phase_a_fields.OUT / "leftover"`。matplotlib はリポジトリの .venv に無いので、duocad を読み込まずにシステムの Python で動かす。
DIR = Path(__file__).resolve().parents[2] / "output" / "stress_gate_20261008" / "leftover"
plt.rcParams["font.family"] = ["Yu Gothic", "Meiryo", "sans-serif"]
#: `leftover_analysis.VERDICTS` の添字 + 1 → (名前, 色)。
VERDICT_STYLE = {1: ("削れる", "#2ca02c"), 2: ("くびれ", "#d62728"), 3: ("連結", "#ff7f0e"), 4: ("多様体", "#9467bd"),
                 5: ("芯", "#8c564b"), 6: ("支え", "#e377c2"), 7: ("表面", "#7f7f7f"), 8: ("その他", "#17becf")}
VIEWS = ((0, 0, "横から(壁は右)"), (25, -55, "斜め上から"))


def _axes(fig, rows, index, tag, view):
    elev, azim, title = view
    ax = fig.add_subplot(rows, 2, index, projection="3d")
    ax.set_xlim(-60, 60)
    ax.set_ylim(-230, 10)
    ax.set_zlim(-75, 75)
    ax.set_box_aspect((120, 240, 150))
    ax.view_init(elev=elev, azim=azim)
    ax.set_xlabel("x")
    ax.set_ylabel("y")
    ax.set_zlabel("z")
    ax.set_title(f"{tag.replace('hanger_', '')}  {title}", fontsize=10)
    return ax


def _points(sel, origin, spacing):
    return (origin + (np.argwhere(sel) + 0.5) * spacing).T


def main() -> int:
    tags = sys.argv[1:]
    stress_fig = plt.figure(figsize=(6.0 * 2, 4.2 * len(tags)))
    verdict_fig = plt.figure(figsize=(6.2 * 2, 4.0 * len(tags)))
    for row, tag in enumerate(tags):
        z = np.load(DIR / f"{tag}_-1.npz")
        info = json.loads((DIR / f"{tag}_-1.json").read_text(encoding="utf-8"))
        mask, u, keep, idle, verdict = z["mask"], z["u"], z["keep"], z["idle"], z["verdict"]
        origin, spacing = z["origin"], float(z["spacing"])
        m = int(info["min_width_cells"])
        local = ndimage.maximum_filter(u, size=2 * m + 1)
        surface = mask & ~ndimage.binary_erosion(mask, border_value=0)
        for col, view in enumerate(VIEWS):
            ax = _axes(stress_fig, len(tags), row * 2 + col + 1, tag, view)
            ax.scatter(*_points(surface & keep, origin, spacing), s=3, c="#bbbbbb", alpha=0.35, depthshade=False)
            free = surface & ~keep
            sc = ax.scatter(*_points(free, origin, spacing), s=4, c=local[free], cmap="turbo", norm=Normalize(0, 1),
                            depthshade=False)
            ax = _axes(verdict_fig, len(tags), row * 2 + col + 1, tag, view)
            for sel, color, alpha, label in ((surface & keep, "#999999", 0.3, "保持"),
                                             (surface & ~keep & ~idle, "#dddddd", 0.5, "応力あり")):
                ax.scatter(*_points(sel, origin, spacing), s=2, c=color, alpha=alpha, depthshade=False,
                           label=label if col == 0 else None)
            for code, (name, color) in VERDICT_STYLE.items():
                sel = surface & idle & (verdict == code)
                if sel.any():
                    ax.scatter(*_points(sel, origin, spacing), s=6, c=color, depthshade=False,
                               label=f"低応力・{name}" if col == 0 else None)
            if col == 0:
                ax.legend(loc="upper left", fontsize=7, markerscale=2, bbox_to_anchor=(-0.25, 1.0))
        stress_fig.colorbar(sc, ax=stress_fig.axes[-2:], shrink=0.6, label="近傍の応力 ÷ 探索の上限")
    for fig, name in ((stress_fig, "leftover_render.png"), (verdict_fig, "leftover_verdict.png")):
        fig.savefig(DIR / name, dpi=90, bbox_inches="tight")
        print(DIR / name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
