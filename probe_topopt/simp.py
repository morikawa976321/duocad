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
        fixed_dofs: np.ndarray | None = None,
        load: np.ndarray | None = None,
        extra_fixed_dofs: np.ndarray | None = None,
    ) -> None:
        """`fixed_dofs` / `load` を渡すと片持ち梁の既定の境界条件を置き換える。

        **既定(どちらも None)の挙動は 2026-08-30 以前と1ビットも変わらない** ——
        stage2「面アンカー」が CAD の面から起こした拘束・荷重を入れるための口で、
        既存の `check_simp.py` はこの口を使わない経路をそのまま通る。
        番号の規約は `_build_edof` と同じ(節点 `i*(nely+1)*(nelz+1)+j*(nelz+1)+k`、
        自由度 `3n+{0,1,2}`)。ずれると荷重は黙って別の場所に入る。

        `extra_fixed_dofs` は**置き換えではなく追加**。`optimize` が「空要素にしか接して
        いない浮いた節点」を系から外すのに使う。置き換えの口(`fixed_dofs`)に混ぜると、
        **`void_mask` だけを渡して拘束は既定のままにしたい呼び出しで、片持ち梁の拘束が
        黙って消える**(2026-08-31の自己レビューで見つけた欠陥)。
        """
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
        if fixed_dofs is None:
            fixed_nodes = np.arange(ny1 * nz1)
            self.fixed = np.concatenate(
                [3 * fixed_nodes, 3 * fixed_nodes + 1, 3 * fixed_nodes + 2]
            )
        else:
            given = np.unique(np.asarray(fixed_dofs, dtype=np.int64))
            if given.size == 0:
                raise ValueError("拘束が空(剛体運動が止まらず K が特異になる)")
            if given.min() < 0 or given.max() >= self.ndof:
                raise ValueError(
                    f"拘束の自由度番号が範囲外: [{given.min()}, {given.max()}] / ndof={self.ndof}"
                )
            self.fixed = given
        if extra_fixed_dofs is not None:
            extra = np.asarray(extra_fixed_dofs, dtype=np.int64)
            if extra.size and (extra.min() < 0 or extra.max() >= self.ndof):
                raise ValueError(
                    f"追加の拘束の自由度番号が範囲外: [{extra.min()}, {extra.max()}] / ndof={self.ndof}"
                )
            self.fixed = np.union1d(self.fixed, extra)
        self.free = np.setdiff1d(np.arange(self.ndof), self.fixed)
        if self.free.size == 0:
            raise ValueError("全自由度が拘束されている(解くものが無い)")
        if load is None:
            self.f = _build_load(nelx, nely, nelz, self.ndof)
        else:
            vec = np.asarray(load, dtype=float).ravel()
            if vec.size != self.ndof:
                raise ValueError(f"荷重ベクトルの長さが {vec.size}、期待は ndof={self.ndof}")
            if not np.all(np.isfinite(vec)):
                raise ValueError("荷重ベクトルに非有限値がある")
            self.f = vec.copy()
        # **拘束DOFに載った荷重は解に一切効かない。** 固定面と荷重面が節点を共有していると
        # ここに全部落ち、「変位ゼロ・コンプライアンスゼロ」で最適化がもっともらしく
        # 収束してしまう(荷重を掛けたつもりで何も掛かっていない)。先に落とす。
        if not np.any(self.f[self.free] != 0.0):
            raise ValueError(
                "荷重が自由な自由度に1つも載っていない(全部が拘束DOFに落ちている)。"
                "固定面と荷重面が同じ節点を共有していないか確認すること"
            )
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


def _as_mask(mask: np.ndarray | None, shape: tuple[int, int, int], label: str) -> np.ndarray:
    """パッシブ要素のマスクを検算して bool 配列にする。"""
    if mask is None:
        return np.zeros(shape, dtype=bool)
    arr = np.asarray(mask)
    if arr.shape != shape:
        raise ValueError(f"{label} の形が {arr.shape}、期待は {shape}")
    return arr.astype(bool)


def _floating_dofs(nelx: int, nely: int, nelz: int, void: np.ndarray) -> np.ndarray:
    """空要素にしか接していない節点の自由度番号(系から外すために拘束へ回す)。"""
    live = ~void
    active = np.zeros((nelx + 1, nely + 1, nelz + 1), dtype=bool)
    for a in (0, 1):
        for b in (0, 1):
            for c in (0, 1):
                active[a : a + nelx, b : b + nely, c : c + nelz] |= live
    idle = np.flatnonzero(~active.ravel())
    return np.concatenate([3 * idle, 3 * idle + 1, 3 * idle + 2])


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
    fixed_dofs: np.ndarray | None = None,
    load: np.ndarray | None = None,
    void_mask: np.ndarray | None = None,
    solid_mask: np.ndarray | None = None,
) -> Result:
    """SIMPでコンプライアンス最小化を行う。

    `fixed_dofs` / `load` は境界条件の差し替え(`Model` へそのまま渡る)。
    `void_mask` / `solid_mask` は**設計変数を動かさない要素**(パッシブ要素)で、
    それぞれ ρ=0 / ρ=1 に固定する。stage2 が「設計空間の外」「保持領域」「荷重直下」を
    入れるための口で、**4つとも None のときの挙動は追加前と完全に同じ**。

    体積制約は動かせる要素だけに掛ける(目標体積 = volfrac × 自由要素数 + 固定ρ=1 要素数)。
    そうしないと、設計空間の外の空要素を「もう十分軽い」と数えて、
    設計空間の中がほとんど埋まったまま止まる。
    """
    t_total = time.perf_counter()
    shape = (nelx, nely, nelz)
    void = _as_mask(void_mask, shape, "void_mask")
    solid = _as_mask(solid_mask, shape, "solid_mask")
    overlap = int(np.count_nonzero(void & solid))
    if overlap:
        raise ValueError(
            f"void_mask と solid_mask が {overlap} 要素で重なっている"
            "(同じ要素を空と材料の両方に固定できない)"
        )
    # 空要素にしか接していない節点は、剛性 emin でしか繋がっていない浮いた自由度。
    # 残すと K がほぼ特異になって CG が収束しないので、拘束側へ**足して**系から外す。
    # `fixed_dofs` へ代入してはいけない —— それだと `void_mask` だけを渡した呼び出しで
    # 既定の片持ち梁の拘束が消える。
    floating = _floating_dofs(nelx, nely, nelz, void) if void.any() else None
    model = Model(
        nelx,
        nely,
        nelz,
        E=E,
        nu=nu,
        emin=emin,
        fixed_dofs=fixed_dofs,
        load=load,
        extra_fixed_dofs=floating,
    )
    kernel, r = _filter_kernel(rmin)
    hs = _filter_conv(np.ones((nelx, nely, nelz)), kernel, r)
    movable = ~void & ~solid
    n_movable = int(movable.sum())
    if n_movable == 0:
        raise ValueError("動かせる要素が1つも無い(void と solid で埋まっている)")
    target_volume = volfrac * n_movable + float(np.count_nonzero(solid))
    live = movable.astype(float)

    def _physical(design: np.ndarray) -> np.ndarray:
        """設計変数 → 物理密度。**パッシブ要素は密度フィルタの後で上書きする。**

        設計変数だけを固定して物理密度を上書きしないと、フィルタが設計空間の外へ
        密度をにじませる(実測: 半径 rmin=1.5 で設計空間の外の要素が ρ=0.29 になった)。
        にじんだぶんは marching cubes がそのまま形にするので、**CADの設計空間から
        はみ出した形**が出てくる。上書きに合わせて感度も `live` で刈る(下の `dcn`/`dvn`)
        ので、連鎖律は上書き後の定義と厳密に一致する。
        """
        field = _filter_conv(design, kernel, r) / hs
        field[void] = 0.0
        field[solid] = 1.0
        return field

    x = np.full((nelx, nely, nelz), volfrac)
    x[void] = 0.0
    x[solid] = 1.0
    x_phys = _physical(x)
    u = np.zeros(model.ndof)
    history: list[Iteration] = []
    stopped_by = "max_iter"

    for it in range(1, max_iter + 1):
        t0 = time.perf_counter()
        u = model.solve(x_phys, penal, solver=solver, cg_rtol=cg_rtol, u0=u)
        c, ce = model.compliance(x_phys, penal, u)
        dc = -penal * x_phys ** (penal - 1.0) * (E - emin) * ce
        dv = np.ones_like(x)
        # `live` は「その要素の物理密度が設計変数で動くか」。パッシブ要素の物理密度は
        # 上書きで定数になっているので、そこへの感度は連鎖律上ちょうど 0 になる。
        dcn = _filter_conv(dc * live / hs, kernel, r)
        dvn = _filter_conv(dv * live / hs, kernel, r)
        # 近傍が全部パッシブな要素は dvn=0 になりうる。そのままだと 0 除算で NaN が
        # 全反復へ伝播する(その要素はどのみち下の上書きで固定されるので値は使わない)。
        dvn = np.maximum(dvn, 1e-300)
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
            xnew[void] = 0.0
            xnew[solid] = 1.0
            xp = _physical(xnew)
            if xp.sum() > target_volume:
                l1 = lm
            else:
                l2 = lm
        change = float(np.abs(xnew - x).max())
        x = xnew
        x_phys = _physical(x)
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
