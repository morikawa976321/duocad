"""面中心の応力(`gate_metrics.exposed_face_vm`)を直線の片持ち梁の理論値で確かめる。

m セル角の棒(長さ 16m)を x=0 の端の m セルで固定し、先端に横荷重。端から離れた区間(全長の 30〜70%)で、
要素中心の最大と表面の面中心の最大を、区間の最大モーメント(30% の位置)の表面理論値で割る。
docs/TOPOPT_TARGET_FACTOR.md §2.1 と同じ配置(要素中心の比は m=2 で 0.424 が出れば一致)。

    .venv\\Scripts\\python.exe topopt_checks\\stress_gate\\check_face_stress.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: E402,F401

import numpy as np  # noqa: E402

from duocad.topopt import fem, stress  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_metrics as gm  # noqa: E402


def run(m: int, k: int = 1):
    length = 16 * m
    mask = np.ones((length * k, m * k, m * k), bool)
    nx, ny, nz = mask.shape
    xs = np.arange(nx + 1) / k
    node = lambda i, j, l: (i * (ny + 1) + j) * (nz + 1) + l  # noqa: E731
    fixed_nodes = [node(i, j, l) for i in range(nx + 1) if xs[i] <= m for j in range(ny + 1) for l in range(nz + 1)]
    load_nodes = [node(i, j, l) for i in range(nx + 1) if xs[i] >= length - m for j in range(ny + 1) for l in range(nz + 1)]
    f = np.zeros(((nx + 1) * (ny + 1) * (nz + 1), 3))
    f[load_nodes, 1] = 1.0 / len(load_nodes)
    fixed = (np.asarray(fixed_nodes)[:, None] * 3 + np.arange(3)).ravel()
    model = fem.Model(nx, ny, nz, fixed_dofs=fixed, load=f.ravel(), active_mask=mask)
    u = model.solve(mask.astype(float), 3.0, solver="direct" if mask.sum() < 6000 else "cg")
    centre = stress.element_stress(model, u).von_mises * k ** 2
    face = gm.exposed_face_vm(model, u, mask, k ** 2)
    xc = (np.arange(nx) + 0.5) / k
    gauge = (xc >= 0.3 * length) & (xc <= 0.7 * length)
    arm = (length - m / 2) - 0.3 * length
    theory = arm * (m / 2) / (m ** 4 / 12)
    return float(centre[gauge].max() / theory), float(np.nanmax(face[gauge]) / theory)


if __name__ == "__main__":
    for m in (1, 2, 3, 4):
        c, s = run(m)
        print(f"m={m}: 要素中心の最大/理論 {c:.3f}  表面の面中心の最大/理論 {s:.3f}")
    c, s = run(2, 2)
    print(f"m=2,k=2: 要素中心 {c:.3f}  面中心 {s:.3f}")
