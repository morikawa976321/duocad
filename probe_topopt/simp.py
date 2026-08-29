"""3D SIMP トポロジー最適化 (片持ち梁)。"""

from __future__ import annotations

import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

PRESET: dict = {
    "nelx": 40,
    "nely": 16,
    "nelz": 16,
    "volfrac": 0.3,
    "penal": 3.0,
    "rmin": 1.5,
    "max_iter": 60,
    "change_tol": 0.01,
    "E": 1.0,
    "nu": 0.3,
    "emin": 1e-9,
    "move": 0.2,
    "solver": "cg",
    "cg_rtol": 1e-9,
}


def element_stiffness(E: float = 1.0, nu: float = 0.3) -> np.ndarray:
    """一辺1の8節点六面体(trilinear hex)の要素剛性行列 (24,24) を返す。"""
    lam_nodes = np.array(
        [[2 * a - 1, 2 * b - 1, 2 * c - 1] for a in (0, 1) for b in (0, 1) for c in (0, 1)],
        float,
    )
    d = np.zeros((6, 6))
    c1 = E / ((1.0 + nu) * (1.0 - 2.0 * nu))
    d[:3, :3] = c1 * (nu * np.ones((3, 3)) + (1.0 - 2.0 * nu) * np.eye(3))
    d[3:, 3:] = c1 * (1.0 - 2.0 * nu) / 2.0 * np.eye(3)
    g = 1.0 / np.sqrt(3.0)
    ke = np.zeros((24, 24))
    for xi in (-g, g):
        for eta in (-g, g):
            for zeta in (-g, g):
                dn = np.empty((8, 3))
                for l in range(8):
                    s = lam_nodes[l]
                    dn[l, 0] = 0.125 * s[0] * (1.0 + s[1] * eta) * (1.0 + s[2] * zeta)
                    dn[l, 1] = 0.125 * (1.0 + s[0] * xi) * s[1] * (1.0 + s[2] * zeta)
                    dn[l, 2] = 0.125 * (1.0 + s[0] * xi) * (1.0 + s[1] * eta) * s[2]
                dndx = dn * 2.0
                det_j = 0.125
                b = np.zeros((6, 24))
                for l in range(8):
                    bx, by, bz = dndx[l]
                    b[0, 3 * l + 0] = bx
                    b[1, 3 * l + 1] = by
                    b[2, 3 * l + 2] = bz
                    b[3, 3 * l + 0] = by
                    b[3, 3 * l + 1] = bx
                    b[4, 3 * l + 1] = bz
                    b[4, 3 * l + 2] = by
                    b[5, 3 * l + 0] = bz
                    b[5, 3 * l + 2] = bx
                ke += b.T @ d @ b * det_j
    return ke


def _build_edof(nelx: int, nely: int, nelz: int) -> np.ndarray:
    """要素DOF写像 (nele, 24) を返す。"""
    nz1 = nelz + 1
    ny1 = nely + 1
    ex, ey, ez = np.meshgrid(
        np.arange(nelx), np.arange(nely), np.arange(nelz), indexing="ij"
    )
    ex = ex.ravel()
    ey = ey.ravel()
    ez = ez.ravel()
    ed = np.empty((ex.size, 24), dtype=np.int32)
    l = 0
    for a in (0, 1):
        for b in (0, 1):
            for c in (0, 1):
                n = (ex + a) * ny1 * nz1 + (ey + b) * nz1 + (ez + c)
                ed[:, 3 * l + 0] = 3 * n
                ed[:, 3 * l + 1] = 3 * n + 1
                ed[:, 3 * l + 2] = 3 * n + 2
                l += 1
    return ed


def _build_load(nelx: int, nely: int, nelz: int, ndof: int) -> np.ndarray:
    """片持ち梁の荷重ベクトルを組み立てる(z方向対称)。"""
    f = np.zeros(ndof)
    ny1 = nely + 1
    nz1 = nelz + 1
    if nelz % 2 == 0:
        k_nodes = [nelz // 2]
        magnitudes = [1.0]
    else:
        k_nodes = [nelz // 2, nelz // 2 + 1]
        magnitudes = [0.5, 0.5]
    for k, mag in zip(k_nodes, magnitudes, strict=True):
        n = nelx * ny1 * nz1 + nely * nz1 + k
        f[3 * n + 1] = -mag
    return f


def _filter_kernel(rmin: float) -> tuple[np.ndarray, int]:
    """密度フィルタのカーネルと半径(整数)を返す。"""
    r = int(np.ceil(rmin)) - 1
    rng = np.arange(-r, r + 1)
    dx, dy, dz = np.meshgrid(rng, rng, rng, indexing="ij")
    dist = np.sqrt(dx**2 + dy**2 + dz**2)
    return np.maximum(0.0, rmin - dist), r


def _filter_conv(field: np.ndarray, kernel: np.ndarray, r: int) -> np.ndarray:
    """3次元シフト加算による線形重み畳み込み。"""
    out = np.zeros_like(field)
    n = 2 * r + 1
    for a in range(n):
        for b in range(n):
            for c in range(n):
                w = kernel[a, b, c]
                if w == 0.0:
                    continue
                da, db, dc = a - r, b - r, c - r
                s_src = tuple(
                    slice(max(0, -d), field.shape[i] + min(0, -d))
                    for i, d in enumerate((da, db, dc))
                )
                s_dst = tuple(
                    slice(max(0, d), field.shape[i] + min(0, d))
                    for i, d in enumerate((da, db, dc))
                )
                out[s_dst] += w * field[s_src]
    return out


class Model:
    """片持ち梁の格子・DOF・境界条件・荷重。密度に依らない部分をここに持つ。"""

    nelx: int
    nely: int
    nelz: int
    nele: int
    ndof: int
    ke: np.ndarray
    edof: np.ndarray
    fixed: np.ndarray
    free: np.ndarray
    f: np.ndarray

    def __init__(
        self,
        nelx: int,
        nely: int,
        nelz: int,
        E: float = 1.0,
        nu: float = 0.3,
        emin: float = 1e-9,
    ) -> None:
        self.nelx = nelx
        self.nely = nely
        self.nelz = nelz
        self.nele = nelx * nely * nelz
        self.ndof = 3 * (nelx + 1) * (nely + 1) * (nelz + 1)
        self.E = E
        self.nu = nu
        self.emin = emin
        self.ke = element_stiffness(E=E, nu=nu)
        self.edof = _build_edof(nelx, nely, nelz)
        ny1 = nely + 1
        nz1 = nelz + 1
        fixed_nodes = np.arange(ny1 * nz1)
        self.fixed = np.concatenate([3 * fixed_nodes, 3 * fixed_nodes + 1, 3 * fixed_nodes + 2])
        self.free = np.setdiff1d(np.arange(self.ndof), self.fixed)
        self.f = _build_load(nelx, nely, nelz, self.ndof)
        self._iK = np.kron(self.edof, np.ones((24, 1), dtype=np.int32)).ravel()
        self._jK = np.kron(self.edof, np.ones((1, 24), dtype=np.int32)).ravel()

    def assemble(self, rho: np.ndarray, penal: float) -> sp.spmatrix:
        """全体剛性行列 K (ndof,ndof) を返す。SIMP補間 E_e = emin + rho_e**penal * (E - emin)。"""
        ee = self.emin + rho.ravel() ** penal * (self.E - self.emin)
        sk = (self.ke.ravel()[None, :] * ee[:, None]).ravel()
        return sp.coo_matrix((sk, (self._iK, self._jK)), shape=(self.ndof, self.ndof)).tocsc()

    def solve(
        self,
        rho: np.ndarray,
        penal: float,
        *,
        solver: str = "cg",
        cg_rtol: float = 1e-9,
        u0: np.ndarray | None = None,
    ) -> np.ndarray:
        """K U = F を解いて U (ndof,) を返す。拘束DOFの成分はきっかり 0.0。"""
        if solver not in ("cg", "direct"):
            raise ValueError(f"未知の解法: {solver!r}")
        kg = self.assemble(rho, penal)
        kf = kg[self.free, :][:, self.free].tocsc()
        ff = self.f[self.free]
        x0 = np.zeros(self.free.size) if u0 is None else u0[self.free].copy()
        if solver == "direct":
            u_free = spla.spsolve(kf, ff)
        else:
            diag = kf.diagonal()
            precond = spla.LinearOperator(kf.shape, matvec=lambda v, d=diag: v / d)
            u_free, info = spla.cg(kf, ff, x0=x0, M=precond, rtol=cg_rtol)
            if info != 0:
                warnings.warn(
                    f"CG が収束しませんでした (info={info})。直接解法へフォールバックします。",
                    RuntimeWarning,
                    stacklevel=2,
                )
                u_free = spla.spsolve(kf, ff)
        u = np.zeros(self.ndof)
        u[self.free] = u_free
        return u

    def compliance(
        self, rho: np.ndarray, penal: float, u: np.ndarray
    ) -> tuple[float, np.ndarray]:
        """(コンプライアンス c, 要素ひずみエネルギー ce) を返す。"""
        ue = u[self.edof]
        ce = np.einsum("ij,jk,ik->i", ue, self.ke, ue).reshape(self.nelx, self.nely, self.nelz)
        ee = self.emin + rho**penal * (self.E - self.emin)
        c = float((ee * ce).sum())
        return c, ce


@dataclass(frozen=True)
class Iteration:
    it: int
    compliance: float
    volfrac: float
    change: float
    seconds: float


@dataclass
class Result:
    rho: np.ndarray
    history: list[Iteration]
    nelx: int
    nely: int
    nelz: int
    seconds: float
    stopped_by: str


def optimize(
    nelx: int,
    nely: int,
    nelz: int,
    *,
    volfrac: float = 0.3,
    penal: float = 3.0,
    rmin: float = 1.5,
    max_iter: int = 60,
    change_tol: float = 0.01,
    E: float = 1.0,
    nu: float = 0.3,
    emin: float = 1e-9,
    move: float = 0.2,
    solver: str = "cg",
    cg_rtol: float = 1e-9,
    callback: Callable[[Iteration, np.ndarray], None] | None = None,
) -> Result:
    """SIMPでコンプライアンス最小化を行う。"""
    t_total = time.perf_counter()
    model = Model(nelx, nely, nelz, E=E, nu=nu, emin=emin)
    kernel, r = _filter_kernel(rmin)
    hs = _filter_conv(np.ones((nelx, nely, nelz)), kernel, r)
    x = np.full((nelx, nely, nelz), volfrac)
    x_phys = _filter_conv(x, kernel, r) / hs
    u = np.zeros(model.ndof)
    history: list[Iteration] = []
    stopped_by = "max_iter"

    for it in range(1, max_iter + 1):
        t0 = time.perf_counter()
        u = model.solve(x_phys, penal, solver=solver, cg_rtol=cg_rtol, u0=u)
        c, ce = model.compliance(x_phys, penal, u)
        dc = -penal * x_phys ** (penal - 1.0) * (E - emin) * ce
        dv = np.ones_like(x)
        dcn = _filter_conv(dc / hs, kernel, r)
        dvn = _filter_conv(dv / hs, kernel, r)
        l1, l2 = 0.0, 1e9
        while (l2 - l1) / (l1 + l2) > 1e-9:
            lm = 0.5 * (l1 + l2)
            # `-dcn` は理屈上つねに正だが、`ce` がきっかり0の要素(荷重の届かない孤島)や
            # 丸めで符号が反転した要素があると sqrt が NaN を返し、**以後の反復が全部
            # NaN で埋まる**。0で潰しておく(その要素は下限へ寄るだけで済む)。
            xnew = np.clip(
                x * np.sqrt(np.maximum(0.0, -dcn) / (dvn * lm)),
                x - move,
                x + move,
            )
            xnew = np.clip(xnew, 0.0, 1.0)
            xp = _filter_conv(xnew, kernel, r) / hs
            if xp.sum() > volfrac * model.nele:
                l1 = lm
            else:
                l2 = lm
        change = float(np.abs(xnew - x).max())
        x = xnew
        x_phys = _filter_conv(x, kernel, r) / hs
        seconds = time.perf_counter() - t0
        iteration = Iteration(
            it=it,
            compliance=c,
            volfrac=float(x_phys.mean()),
            change=change,
            seconds=seconds,
        )
        history.append(iteration)
        if callback is not None:
            callback(iteration, x_phys.copy())
        if change < change_tol:
            stopped_by = "change_tol"
            break

    return Result(
        rho=x_phys,
        history=history,
        nelx=nelx,
        nely=nely,
        nelz=nelz,
        seconds=time.perf_counter() - t_total,
        stopped_by=stopped_by,
    )


def run_preset(
    callback: Callable[[Iteration, np.ndarray], None] | None = None,
    **overrides: object,
) -> Result:
    """PRESET の値で optimize を呼ぶ。overrides で個別に上書きできる
    (**格子 `nelx`/`nely`/`nelz` も上書きできる** —— 分けて扱うと `optimize` へ
    同じ引数を2度渡して TypeError になる)。"""
    opts: dict = dict(PRESET)
    opts.update(overrides)
    return optimize(
        int(opts.pop("nelx")),
        int(opts.pop("nely")),
        int(opts.pop("nelz")),
        callback=callback,
        **opts,  # type: ignore[arg-type]
    )


if __name__ == "__main__":
    result = run_preset()
    for item in result.history:
        print(
            f"反復 {item.it:3d}  C={item.compliance:10.4f}  "
            f"体積={item.volfrac:.4f}  change={item.change:.4f}  {item.seconds:.2f}秒"
        )
    print(f"完了: {result.stopped_by}  合計 {result.seconds:.1f}秒")
