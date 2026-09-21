"""片持ち梁(節Aと同じ作り)で、要素の定式化と応力を拾う位置だけを替えて比べる(1〜8 層)。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_element_locking.py
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_element_locking.py --out C:\\tmp\\locking_probe.json

`fem.Model` の `ke` を非適合モード要素(静的縮約)へ差し替えるだけで、リポジトリのコードは変更しない。
`std` は現行の完全積分 trilinear hex、`im` は非適合モード要素。列の意味:

    tip/EulerBernoulli  先端変位 ÷ 梁理論(1 に近いほど硬さの過大評価が小さい)
    centre/surface      要素中心の応力 ÷ 表面の理論値(現行の評価。1層は 0)
    face/surface        面中心での応力 ÷ 表面の理論値(`im` はモードの寄与を含む)

**判定はしない**。結果は `docs/TOPOPT_TARGET_FACTOR.md` の節「要素の定式化だけを替えた測定」に転記してある。
出力は既定で標準出力だけ(`--out` を渡したときだけファイルへ書く)。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
import _record
from duocad.topopt import fem, stress

NU = 0.3
SIGNS = np.array([[2*a-1, 2*b-1, 2*c-1] for a in (0, 1) for b in (0, 1) for c in (0, 1)], float)
D = stress.elasticity_matrix(1.0, NU)


def bmat(p):
    xi, eta, zeta = p
    b = np.zeros((6, 24))
    for l, s in enumerate(SIGNS):
        bx = 0.25 * s[0] * (1 + s[1]*eta) * (1 + s[2]*zeta)
        by = 0.25 * (1 + s[0]*xi) * s[1] * (1 + s[2]*zeta)
        bz = 0.25 * (1 + s[0]*xi) * (1 + s[1]*eta) * s[2]
        b[0, 3*l] = bx; b[1, 3*l+1] = by; b[2, 3*l+2] = bz
        b[3, 3*l] = by; b[3, 3*l+1] = bx
        b[4, 3*l+1] = bz; b[4, 3*l+2] = by
        b[5, 3*l] = bz; b[5, 3*l+2] = bx
    return b


def gmat(p):
    """非適合モード u_c += a_{jc} (1 - xi_j^2)。一辺1の立方体なので d/dx_j = -4 xi_j。"""
    g = np.zeros((6, 9))
    shear_row = {(0, 1): 3, (1, 0): 3, (1, 2): 4, (2, 1): 4, (0, 2): 5, (2, 0): 5}
    for j in range(3):
        for c in range(3):
            col = 3*j + c
            d = -4.0 * p[j]
            if j == c:
                g[c, col] = d
            else:
                g[shear_row[(c, j)], col] = d
    return g


def incompatible():
    gp = 1/np.sqrt(3)
    kuu = np.zeros((24, 24)); kua = np.zeros((24, 9)); kaa = np.zeros((9, 9))
    for xi in (-gp, gp):
        for eta in (-gp, gp):
            for zeta in (-gp, gp):
                b = bmat((xi, eta, zeta)); g = gmat((xi, eta, zeta))
                kuu += b.T @ D @ b * 0.125
                kua += b.T @ D @ g * 0.125
                kaa += g.T @ D @ g * 0.125
    alpha_of_u = -np.linalg.solve(kaa, kua.T)  # (9,24)
    return kuu + kua @ alpha_of_u, alpha_of_u, kuu


KE_IM, ALPHA, KUU = incompatible()
assert np.allclose(KUU, fem.element_stiffness(1.0, NU)), "KE の再現が fem と一致しない"


def node_id(i, j, k, nely, nelz):
    return i*(nely+1)*(nelz+1) + j*(nelz+1) + k


def cantilever(nelx, nely, nelz):
    ndof = 3*(nelx+1)*(nely+1)*(nelz+1)
    fixed = []
    for j in range(nely+1):
        for k in range(nelz+1):
            n = node_id(0, j, k, nely, nelz)
            fixed += [3*n, 3*n+1, 3*n+2]
    wy = np.where((np.arange(nely+1) == 0) | (np.arange(nely+1) == nely), 0.5, 1.0)
    wz = np.where((np.arange(nelz+1) == 0) | (np.arange(nelz+1) == nelz), 0.5, 1.0)
    w = np.outer(wy, wz)
    f = np.zeros(ndof)
    for j in range(nely+1):
        for k in range(nelz+1):
            f[3*node_id(nelx, j, k, nely, nelz)+1] = -w[j, k]
    f /= -f.sum()
    return fem.Model(nelx, nely, nelz, fixed_dofs=np.unique(np.array(fixed)), load=f)


def run(n, kind):
    nelx, nely, nelz = 16*n, n, 2*n
    model = cantilever(nelx, nely, nelz)
    if kind == "im":
        model.ke = KE_IM
    rho = np.ones((nelx, nely, nelz))
    t0 = time.perf_counter()
    u = model.solve(rho, 1.0, solver="direct")
    sec = time.perf_counter() - t0
    tip = np.mean([u[3*node_id(nelx, j, k, nely, nelz)+1] for j in range(nely+1) for k in range(nelz+1)])
    # 断面: 根元から梁せい1つぶん先の要素列 i=n、最外層 j=n-1、z 方向は平均
    i, j = n, n-1
    centre, face = [], []
    for k in range(nelz):
        e = i*nely*nelz + j*nelz + k
        ue = u[model.edof[e]]
        a = ALPHA @ ue if kind == "im" else np.zeros(9)
        centre.append((D @ (bmat((0, 0, 0)) @ ue))[0])
        p = (0.0, 1.0, 0.0)
        face.append((D @ (bmat(p) @ ue + gmat(p) @ a))[0])
    L, x = 16.0*n, n + 0.5
    surface_theory = (L - x) * (n/2) / (2*n * n**3 / 12)
    return dict(n=n, kind=kind, sec=round(sec, 2), tip=float(-tip),
                centre_over_surface=float(abs(np.mean(centre))/surface_theory),
                face_over_surface=float(abs(np.mean(face))/surface_theory))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=None, help="結果JSONの書き出し先(省略時は標準出力だけ)")
    args = parser.parse_args()
    rows = [run(n, kind) for n in (1, 2, 3, 4, 8) for kind in ("std", "im")]
    # 参照の変位は n ごとに寸法が違うので、無次元化: 梁理論 PL^3/3EI に対する比で並べる(E=1,P=1)
    for r in rows:
        n = r["n"]; L = 16.0*n; I = 2*n * n**3 / 12
        r["tip_over_EB"] = r["tip"] / (L**3 / (3*I))
    print(f"{'n':>2} {'kind':>4} {'tip/EulerBernoulli':>19} {'centre/surface':>15} {'face/surface':>13} {'sec':>6}")
    for r in rows:
        print(f"{r['n']:>2} {r['kind']:>4} {r['tip_over_EB']:>19.4f} {r['centre_over_surface']:>15.4f} "
              f"{r['face_over_surface']:>13.4f} {r['sec']:>6}")
    if args.out:
        _record.write(args.out, rows, default=float)
    return 0


if __name__ == "__main__":
    sys.exit(main())
