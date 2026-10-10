"""二値FEM(`duocad/topopt/fem.py`)の受入検査。

    .\\.venv\\Scripts\\python.exe topopt_checks\\check_fem.py

検査の構成(上から順に、狭いものから広いものへ):

1. **要素剛性行列 `Ke`** — 対称性 / 半正定値 / 剛体モードがちょうど6本 / **パッチ試験**。
   パッチ試験は「一様ひずみを与えたときのひずみエネルギーが弾性論の値と一致するか」を見る。
   B行列・D行列・ヤコビアン・局所節点順のどれが間違っていても落ちる。
2. **全体系** — 組み立てた K と解 U が `K U = F` を満たすか / 拘束DOFが0か /
   コンプライアンスが `F・U` と一致するか。要素→全体のDOF写像(`edof`)の誤りを捕まえる。
3. **移設前一致(4題材)** — 二値の棒・梁・空セル領域を含む4題材について、**移設前の実装
   (`simp.py`)で採った基準値**(変位・コンプライアンス・反力・von Mises 応力のSHA-256と
   スカラー値)と、いまの実装の出力を突き合わせる。要素剛性・DOF番号・組立・線形解法のどこかが
   変わっていれば、SHAかスカラー値のどちらかが必ず動く。
4. **void 要素の除去** — `Model(active_mask=...)` の不変条件。**自由自由度の集合が全要素で
   組んだときと完全に一致する**ことを固定する(性能の話ではなく、除去が「未知数を減らす」
   以上のことをしていない根拠)。併せて COO の添字の並び・K の分解・自由自由度が孤立して
   いないことを見る。**目的値が解いた系のものであること**(`c = F・U`)もここで見る。
5. **反力と多重右辺** — `R = (K u - f)|_fixed` が全体の釣り合い(Σ R = -F)を満たすか /
   `stiffness_apply` が組み立てた K の積と一致するか / `solve_many` が右辺1本のとき
   `solve` と**1ビット同じ**か・複数本でも個別に解いたのと一致するか。

`fem.Model` は SIMP のペナルティ引数 `penal` を持つが、ここでは全題材を ρ∈{0,1} の
二値(`active_mask` で空セルを組み立てから除外)にしてあるので `rho**penal` は
0/1のどちらでも `penal` に依らない —— 二値FEMとしての振る舞いだけを見ている。
"""

from __future__ import annotations

import hashlib

import numpy as np

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
from _harness import Checker, run
from duocad.topopt import anchor, fem, stress

CHECK = Checker()
check = CHECK.check
FAILURES = CHECK.failures

# --- 弾性論から出る参照値(E=1, nu=0.3) --------------------------------------
E_REF, NU_REF = 1.0, 0.3
_C1 = E_REF / ((1.0 + NU_REF) * (1.0 - 2.0 * NU_REF))
D00_REF = _C1 * (1.0 - NU_REF)  # 一軸ひずみ eps_xx=1 のときの eps^T D eps
G_REF = E_REF / (2.0 * (1.0 + NU_REF))  # せん断 gamma_xy=1 のときの eps^T D eps
D01_REF = _C1 * NU_REF  # ポアソン結合項。二軸ひずみでしか姿を現さない

# 移設前の `simp.py` で採った基準値。題材の定義は `_make_case` / `_run_case` と1対1。
# `beam_void_interior_cg` と `beam_multi_void_cg_threads` は cg で解き直した値
# (コンプライアンスは旧値と 1e-13 台で一致する)。
BASELINE = {
    "rod_solid_cg": {
        "grid": (16, 4, 4),
        "void_blocks": [],
        "solver": "cg",
        "threads": 1,
        "void_cells": 0,
        "u_sha": "f7182a1cec94fb99ce3d23ffb536c76e331dd2e5bf877cbcb57c65b59c1e8d7c",
        "compliance": 66.34665868500139,
        "cg_iterations": 108,
        "reaction_sha": "8100a5826c8b9ac900726f3e0619f23daf4c21a27179c934fe5d683b321bcbca",
        "von_mises_sha": "aef999b072ffc64889bd1372a9ec789313ce89692e8271f96c813655c75f0acc",
        "representative_mpa": 0.9810916712161777,
        "raw_max_mpa": 0.9810917083269399,
        "free_dofs": 1200,
        "fixed_dofs": 75,
    },
    "beam_void_interior_cg": {
        "grid": (16, 6, 6),
        "void_blocks": [(6, 10, 2, 4, 2, 4)],
        "solver": "cg",
        "threads": 1,
        "void_cells": 16,
        "u_sha": "60cdacef940645a4ec9cfab41283bfcba6efe969897f3c828b102c8c20611b4e",
        "compliance": 16.86291299395033,
        "cg_iterations": 127,
        "reaction_sha": "a8ae4119ca6058693fb8bad9861b185cfc5bd138afa7439a1d99e35231e7fe59",
        "von_mises_sha": "804cfb4543d3f9712efb37702930f4fd540998c4718ace56bbc5928f445e4fe3",
        "representative_mpa": 2.5222538558620244,
        "raw_max_mpa": 2.524771543521795,
        "free_dofs": 2343,
        "fixed_dofs": 156,
    },
    "beam_void_surface_direct": {
        "grid": (12, 6, 6),
        "void_blocks": [(4, 8, 0, 2, 0, 6)],
        "solver": "direct",
        "threads": 1,
        "void_cells": 48,
        "u_sha": "42125d0c12c9b033b90e63ff4a717f09fecd8f4819d27ed7b735b935de654a4b",
        "compliance": 14.569613168075428,
        "cg_iterations": 0,
        "reaction_sha": "27d9d94f87dfdc4e6c0921e91e2cb828126009f8fe78ad1e5e19d430fbd90458",
        "von_mises_sha": "b18ece3f902afc10b8c59acf404bf66aac2a446a678f3fb4a4ca90b8e9ac96a4",
        "representative_mpa": 4.696393561325111,
        "raw_max_mpa": 4.701231762848346,
        "free_dofs": 1638,
        "fixed_dofs": 273,
    },
    "beam_multi_void_cg_threads": {
        "grid": (20, 8, 8),
        "void_blocks": [(2, 5, 1, 3, 1, 3), (12, 16, 4, 7, 4, 7)],
        "solver": "cg",
        "threads": 4,
        "void_cells": 48,
        "u_sha": "8935e887403de950224b0ca5fe1b370fcd74b0c67b8e969dd1f0225b3ee47201",
        "compliance": 12.242055466859828,
        "cg_iterations": 271,
        "reaction_sha": "1e6cdf2ea14eee547cda2eb330f796d2a959ee15b32395bce61bd52c454819da",
        "von_mises_sha": "32e9f3314678ac96bf159999107e0e109be2b5f084f5b6d1ea9446dbae5ebcc0",
        "representative_mpa": 3.455232473824888,
        "raw_max_mpa": 4.243306641522755,
        "free_dofs": 4818,
        "fixed_dofs": 285,
    },
}


def _sha(arr: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(arr).tobytes()).hexdigest()


# ---------------------------------------------------------------------------
# 1. 要素剛性行列
# ---------------------------------------------------------------------------
def check_element_stiffness() -> None:
    print("\n[1] 要素剛性行列 Ke (24x24)")
    ke = fem.element_stiffness(E=E_REF, nu=NU_REF)

    check("形状", ke.shape == (24, 24), f"shape={ke.shape}")

    asym = float(np.abs(ke - ke.T).max())
    check("対称", asym < 1e-12, f"max|Ke-Ke^T|={asym:.3e}")

    w = np.linalg.eigvalsh(0.5 * (ke + ke.T))
    scale = float(np.abs(w).max())
    n_zero = int((np.abs(w) < 1e-9 * scale).sum())
    check("剛体モードがちょうど6本", n_zero == 6, f"|λ|<1e-9・λmax の個数={n_zero}")
    check("半正定値", float(w.min()) > -1e-9 * scale, f"λmin={w.min():.3e}")

    # 局所節点 l = 4a+2b+c が単位立方体の頂点 (a,b,c) に対応する(契約)。
    coords = np.array([(a, b, c) for a in (0, 1) for b in (0, 1) for c in (0, 1)], float)

    def energy(field) -> float:
        """線形変位場を24次元ベクトルへ並べ、u^T Ke u を返す(=2×ひずみエネルギー)。"""
        u = np.zeros(24)
        for l, (x, y, z) in enumerate(coords):
            ux, uy, uz = field(x, y, z)
            u[3 * l : 3 * l + 3] = (ux, uy, uz)
        return float(u @ ke @ u)

    # 一様ひずみ eps_xx = 1 → u^T Ke u = eps^T D eps × 体積(=1) = D[0,0]
    e_axial = energy(lambda x, y, z: (x, 0.0, 0.0))
    check(
        "パッチ試験(一軸ひずみ)",
        abs(e_axial - D00_REF) < 1e-10 * D00_REF,
        f"u^T Ke u={e_axial:.12f} (理論値 {D00_REF:.12f})",
    )

    # せん断 gamma_xy = 1 → u^T Ke u = G
    e_shear = energy(lambda x, y, z: (y, 0.0, 0.0))
    check(
        "パッチ試験(せん断)",
        abs(e_shear - G_REF) < 1e-10 * G_REF,
        f"u^T Ke u={e_shear:.12f} (理論値 {G_REF:.12f})",
    )

    # 二軸ひずみ eps_xx = eps_yy = 1 → eps^T D eps = 2*D[0,0] + 2*D[0,1]
    # 一軸とせん断だけでは D[0,1](ポアソン結合項)が式に現れず、そこを壊した実装が
    # 素通りする。結合項を単独で炙り出すためにこの1件を足してある。
    e_biaxial = energy(lambda x, y, z: (x, y, 0.0))
    biaxial_ref = 2.0 * (D00_REF + D01_REF)
    check(
        "パッチ試験(二軸ひずみ・ポアソン結合項)",
        abs(e_biaxial - biaxial_ref) < 1e-10 * biaxial_ref,
        f"u^T Ke u={e_biaxial:.12f} (理論値 {biaxial_ref:.12f}, D[0,1]={D01_REF:.12f})",
    )

    # 剛体移動・剛体回転はエネルギーを持たない
    e_trans = energy(lambda x, y, z: (1.0, 2.0, -3.0))
    e_rot = energy(lambda x, y, z: (-y, x, 0.0))
    check("剛体並進のエネルギー0", abs(e_trans) < 1e-12, f"u^T Ke u={e_trans:.3e}")
    check("剛体回転のエネルギー0", abs(e_rot) < 1e-12, f"u^T Ke u={e_rot:.3e}")


# ---------------------------------------------------------------------------
# 2. 全体系(組み立て・拘束・荷重・コンプライアンス)
# ---------------------------------------------------------------------------
def check_global_system() -> None:
    print("\n[2] 全体系 (8x4x4)")
    m = fem.Model(8, 4, 4, E=E_REF, nu=NU_REF)
    rho = np.ones((m.nelx, m.nely, m.nelz))
    penal = 3.0

    k = m.assemble(rho, penal).tocsr()
    u = m.solve(rho, penal, solver="direct")

    check("Uの長さ", u.shape == (m.ndof,), f"U.shape={u.shape}")

    fixed_max = float(np.abs(u[m.fixed]).max())
    check("拘束DOFの変位が0", fixed_max == 0.0, f"max|U[fixed]|={fixed_max:.3e}")

    r = (k @ u - m.f)[m.free]
    rel = float(np.linalg.norm(r) / np.linalg.norm(m.f))
    check("釣り合い K・U = F", rel < 1e-9, f"||(K U - F)_free|| / ||F||={rel:.3e}")

    c, ce = m.compliance(rho, penal, u)
    fu = float(m.f @ u)
    check(
        "コンプライアンス = F・U",
        abs(c - fu) < 1e-8 * abs(fu),
        f"c={c:.10f} F・U={fu:.10f}",
    )
    check("要素エネルギーが非負", float(ce.min()) >= 0.0, f"min(ce)={ce.min():.3e}")


# ---------------------------------------------------------------------------
# 3. 移設前一致(4題材)
# ---------------------------------------------------------------------------
def _floating_dofs(void: np.ndarray) -> np.ndarray:
    """空要素にしか接していない節点の自由度番号(`acceptance.safety_check` と同じ `anchor.floating_dofs`)。"""
    return anchor.floating_dofs(~void)


def _make_model(nelx: int, nely: int, nelz: int, void_blocks: list[tuple[int, ...]]):
    """棒(全材料)・梁(void_blocksで空洞を開けた形)を二値の `active_mask` で作る。"""
    void = np.zeros((nelx, nely, nelz), dtype=bool)
    for (x0, x1, y0, y1, z0, z1) in void_blocks:
        void[x0:x1, y0:y1, z0:z1] = True
    extra_fixed = _floating_dofs(void) if void.any() else None
    model = fem.Model(
        nelx, nely, nelz,
        active_mask=(~void) if void.any() else None,
        extra_fixed_dofs=extra_fixed,
    )
    return model, void


def run_case(name: str, spec: dict) -> None:
    nelx, nely, nelz = spec["grid"]
    model, void = _make_model(nelx, nely, nelz, spec["void_blocks"])
    check(
        f"{name}: void セル数が期待どおり",
        int(void.sum()) == spec["void_cells"],
        f"{int(void.sum())} / 期待 {spec['void_cells']}",
    )
    check(
        f"{name}: 自由/固定自由度数が期待どおり",
        int(model.free.size) == spec["free_dofs"] and int(model.fixed.size) == spec["fixed_dofs"],
        f"free={model.free.size} fixed={model.fixed.size} / 期待 free={spec['free_dofs']} fixed={spec['fixed_dofs']}",
    )

    rho = np.ones((nelx, nely, nelz), dtype=float)
    penal = 3.0  # 二値(active_maskで空を除外済み)なので rho**penal は penal に依らない

    with fem.collect_solve_stats() as stats:
        u = model.solve(rho, penal, solver=spec["solver"], threads=spec["threads"])

    check(
        f"{name}: 変位 U が移設前とビット一致",
        _sha(u) == spec["u_sha"],
        f"solver={spec['solver']} threads={spec['threads']}",
    )
    check(
        f"{name}: CG反復数が移設前と一致(解法経路が変わっていない)",
        stats["cg_iterations"] == spec["cg_iterations"],
        f"{stats['cg_iterations']} / 期待 {spec['cg_iterations']}",
    )

    c, _ce = model.compliance(rho, penal, u)
    check(
        f"{name}: コンプライアンス c=F・U が移設前と一致",
        c == spec["compliance"],
        f"{c!r} / 期待 {spec['compliance']!r}",
    )

    reactions = fem.reaction_forces(model, rho, penal, u)
    check(
        f"{name}: 反力 R=(Ku-f)|_fixed が移設前とビット一致",
        _sha(reactions) == spec["reaction_sha"],
        f"合力 {reactions.tolist()}",
    )

    scale = stress.UnitScale.for_model(model, element_mm=1.0, load_n=1.0, youngs_modulus_mpa=2000.0)
    field = stress.element_stress(model, u).scaled(scale)
    check(
        f"{name}: von Mises 応力場が移設前とビット一致",
        _sha(field.von_mises) == spec["von_mises_sha"],
        "element_stress().scaled()",
    )
    rep = stress.representative_stress(field, rho, rho_min=0.5, percentile=99.5)
    check(
        f"{name}: 代表応力(99.5%点)・素の max が移設前と一致",
        rep.value == spec["representative_mpa"] and rep.raw_max == spec["raw_max_mpa"],
        f"代表={rep.value!r} max={rep.raw_max!r}",
    )


# ---------------------------------------------------------------------------
# 4. void 要素の除去
# ---------------------------------------------------------------------------
VOID_GRID = (12, 6, 6)


def _void_case() -> tuple[np.ndarray, np.ndarray]:
    """空要素のある題材。**面ごと落として「浮いた節点」を必ず作る**(そこが焦点)。"""
    nelx, nely, nelz = VOID_GRID
    void = np.zeros((nelx, nely, nelz), dtype=bool)
    void[3:8, 1:5, 1:5] = True  # 内部のくり抜き
    void[:, :, nelz - 1] = True  # 面ごと落とす
    floating = _floating_dofs(void)
    return void, floating


def _orphan_free_dofs(model: "fem.Model") -> int:
    """組み立てに載る要素のどれにも接していない自由自由度の数(K_ff の全零行)。"""
    active = model.edof if model.active is None else model.edof[model.active]
    touched = np.zeros(model.ndof, dtype=bool)
    touched[active.ravel()] = True
    return int(np.count_nonzero(~touched[model.free]))


def check_void_assembly() -> None:
    nelx, nely, nelz = VOID_GRID
    print(f"\n[4] void 要素の除去 ({nelx}x{nely}x{nelz})")
    void, floating = _void_case()
    full = fem.Model(nelx, nely, nelz, extra_fixed_dofs=floating)
    lean = fem.Model(nelx, nely, nelz, extra_fixed_dofs=floating, active_mask=~void)

    rows = np.kron(full.edof, np.ones((24, 1), dtype=np.int32)).ravel()
    cols = np.kron(full.edof, np.ones((1, 24), dtype=np.int32)).ravel()
    mine_rows, mine_cols = fem._coo_index_arrays(full.edof)
    check(
        "COO の添字が ke.ravel() の並びと対応している",
        np.array_equal(rows, mine_rows) and np.array_equal(cols, mine_cols),
        f"{mine_rows.size} 項 / dtype={mine_rows.dtype}",
    )

    # **これが本題。** 空要素を組み立てから外しても、解く未知数は1つも減らない。
    check(
        "自由自由度の集合が全要素で組んだときと完全に一致する",
        np.array_equal(full.free, lean.free) and full.ndof == lean.ndof,
        f"free {full.free.size} 個が一致 / ndof {full.ndof}",
    )
    check(
        "組み立てる要素だけが減っている",
        lean._iK.size == 576 * int((~void).sum()) < full._iK.size,
        f"COO 項 {full._iK.size} -> {lean._iK.size} "
        f"(空 {int(void.sum())}/{void.size} 要素)",
    )
    check(
        "自由自由度が1つも孤立していない(K_ff に全零行が無い)",
        _orphan_free_dofs(lean) == 0,
        f"孤立 {_orphan_free_dofs(lean)} 個",
    )
    # **検出力**: `extra_fixed_dofs` と `active_mask` は同じ void から作るという契約
    # (`Model` の docstring)。片方だけ渡すと上の検査が実際に落ちることを見せる。
    naive = fem.Model(nelx, nely, nelz, active_mask=~void)
    check(
        "契約を破ると上の検査が落ちる(extra_fixed_dofs を渡さない)",
        _orphan_free_dofs(naive) > 0,
        f"孤立 {_orphan_free_dofs(naive)} 個",
    )

    rng = np.random.default_rng(0)
    rho = rng.random((nelx, nely, nelz))
    rho[void] = 0.0
    k_full = full.assemble(rho, 3.0)
    k_lean = lean.assemble(rho, 3.0)
    k_void = fem.Model(
        nelx, nely, nelz, extra_fixed_dofs=floating, active_mask=void
    ).assemble(rho, 3.0)
    scale = float(abs(k_full).max())
    split = float(abs(k_full - (k_lean + k_void)).max())
    check(
        "K = (active ぶん) + (void ぶん) が機械精度で成り立つ",
        split < 1e-12 * scale,
        f"max|差|={split:.3e} / max|K|={scale:.3e}",
    )
    gap = float(abs(k_full - k_lean).max()) / scale
    check(
        "残る差は emin 媒質の寄与ぶんだけ",
        gap < 100.0 * full.emin,
        f"相対 {gap:.3e} (emin={full.emin:.0e})",
    )

    u_full = full.solve(rho, 3.0, solver="direct")
    u_lean = lean.solve(rho, 3.0, solver="direct")
    c_full, _ = full.compliance(rho, 3.0, u_full)
    c_lean, ce_lean = lean.compliance(rho, 3.0, u_lean)
    rel = abs(c_lean - c_full) / abs(c_full)
    check(
        "コンプライアンスの相対差が 1e-3 以内",
        rel < 1e-3,
        f"{c_full:.6f} -> {c_lean:.6f} (相対 {rel:.3e})",
    )
    # **目的値は解いた系のものでなければならない。** [2] の同じ等式を void を外した系でも
    # 見る —— [2] は `void_mask` を渡さない片持ち梁なので、この経路には届かない。
    fu = float(lean.f @ u_lean)
    check(
        "void を外した系でも コンプライアンス = F・U",
        abs(c_lean - fu) < 1e-8 * abs(fu),
        f"c={c_lean:.12f} F・U={fu:.12f} (相対 {abs(c_lean - fu) / abs(fu):.3e})",
    )
    # **検出力**: 全要素で足すと、解いた系に無い emin 媒質のぶんだけ F・U から外れる。
    ee_all = lean.emin + rho**3.0 * (lean.E - lean.emin)
    c_all = float((ee_all * ce_lean).sum())
    check(
        "全要素で足すと F・U から外れる(上の検査の検出力)",
        abs(c_all - fu) > 1e-8 * abs(fu),
        f"全要素で足すと {c_all:.12f} (相対 {abs(c_all - fu) / abs(fu):.3e})",
    )


# ---------------------------------------------------------------------------
# 5. 反力と多重右辺
# ---------------------------------------------------------------------------
REACTION_GRID = (10, 5, 5)


def check_reactions_and_multi_rhs() -> None:
    nelx, nely, nelz = REACTION_GRID
    print(f"\n[5] 反力と多重右辺 ({nelx}x{nely}x{nelz})")
    model = fem.Model(nelx, nely, nelz)
    rng = np.random.default_rng(7)
    rho = 0.5 + 0.5 * rng.random((nelx, nely, nelz))

    # (a) K を組み立てずに掛けても同じ行列を掛けている
    vec = rng.standard_normal(model.ndof)
    exact = model.assemble(rho, 3.0) @ vec
    lean = model.stiffness_apply(rho, 3.0, vec)
    rel = float(np.linalg.norm(lean - exact) / np.linalg.norm(exact))
    check(
        "stiffness_apply が組み立てた K の積と一致する",
        rel < 1e-12,
        f"相対 {rel:.3e}",
    )

    # (b) 右辺1本の solve_many は solve と1ビット同じ(既定経路が動いていない根拠)
    u = model.solve(rho, 3.0, solver="direct")
    many = model.solve_many(rho, 3.0, model.f[None, :], solver="direct")
    check(
        "solve_many(右辺1本) が solve と1ビット同じ",
        np.array_equal(many[0], u),
        f"max|差| = {float(np.abs(many[0] - u).max()):.3e}",
    )

    # (c) 多重右辺は K の組み立てを1回だけして解く。答えは個別に解いたのと一致する
    extra = rng.standard_normal(model.ndof)
    extra[model.fixed] = 0.0
    pair = model.solve_many(rho, 3.0, np.stack([model.f, extra]), cg_rtol=1e-12)
    alone = model.solve_many(rho, 3.0, extra[None, :], cg_rtol=1e-12)[0]
    rel = float(np.linalg.norm(pair[1] - alone) / np.linalg.norm(alone))
    with fem.collect_solve_stats() as stats:
        model.solve_many(rho, 3.0, np.stack([model.f, extra]))
    check(
        "多重右辺の解が個別に解いたのと一致する",
        rel < 1e-8,
        f"相対 {rel:.3e}",
    )
    check(
        "多重右辺は solve 2本として数えられる",
        stats["solves"] == 2,
        f"solve {stats['solves']} 本",
    )

    # (d) 全体の釣り合い。反力の総和は荷重の合力の符号を反転したもの
    total = fem.reaction_forces(model, rho, 3.0, u)
    load = fem.load_resultant(model.f)
    err = float(np.linalg.norm(total + load) / np.linalg.norm(load))
    check(
        "反力の総和 = -(荷重の合力)",
        err < 1e-8,
        f"Σ R = {np.array2string(total, precision=6)} / F = "
        f"{np.array2string(load, precision=6)} (相対 {err:.3e})",
    )


def check_baseline_cases() -> None:
    for name, spec in BASELINE.items():
        print(f"\n[3] {name}: grid={spec['grid']} void_blocks={spec['void_blocks']} "
              f"solver={spec['solver']} threads={spec['threads']}")
        run_case(name, spec)


def main() -> int:
    # RESULTS(MEASURED 行)は持たない。NG/OK 行に経過秒数を出す。
    return run(
        CHECK,
        "check_fem.py : 二値FEM(fem.py)の受入検査",
        [check_element_stiffness, check_global_system, check_baseline_cases, check_void_assembly, check_reactions_and_multi_rhs],
        timed=True,
        measured=False,
    )


if __name__ == "__main__":
    raise SystemExit(main())
