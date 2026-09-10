"""SIMPトポロジー最適化(`simp.py`)の受入検査。

    .\\.venv\\Scripts\\python.exe topopt_checks\\check_simp.py

**このファイルが合否そのものである。** タスクの完了条件1の (a)(b)(c) を、
それだけでは通らない下支えの検査(要素剛性行列・全体系の釣り合い)と一緒に assert する。
どれか1つでも落ちればプロセスは非ゼロで終わる。

検査の構成(上から順に、狭いものから広いものへ):

1. **要素剛性行列 `Ke`** — 対称性 / 半正定値 / 剛体モードがちょうど6本 / **パッチ試験**。
   パッチ試験は「一様ひずみを与えたときのひずみエネルギーが弾性論の値と一致するか」を見る。
   B行列・D行列・ヤコビアン・局所節点順のどれが間違っていても落ちる。
2. **全体系** — 組み立てた K と解 U が `K U = F` を満たすか / 拘束DOFが0か /
   コンプライアンスが `F・U` と一致するか。要素→全体のDOF写像(`edof`)の誤りを捕まえる。
3. **完了条件1-(a)** 体積分率が目標の±1%以内。
4. **完了条件1-(b)** コンプライアンスが減少し、終盤10反復の変動が1%未満。
5. **完了条件1-(c)** z方向に対称な荷重・拘束なら密度場もz対称(相対誤差1e-6以内)。
   プリセット(既定のCG解法)と、小格子の直接解法(こちらは1e-9)の両方で見る。
6. **void 要素を組み立てから外しても系が変わらない** — `Model(active_mask=...)` の不変条件。
   **自由自由度の集合が全要素で組んだときと完全に一致する**ことを固定する
   (性能の話ではなく、除去が「未知数を減らす」以上のことをしていない根拠)。
   併せて COO の添字の並び・K の分解・自由自由度が孤立していないことを見る。
   **目的値が解いた系のものであること**(`c = F・U`)もここで見る —— [2] の同じ等式は
   `void_mask` を渡さない片持ち梁なので、この経路には届かない。
   **検出力**: `extra_fixed_dofs` を渡さずに void だけ外すと孤立した自由自由度が出る /
   コンプライアンスを全要素で足すと F・U から外れる。
7. **反力と多重右辺** — `R = (K u - f)|_fixed` が全体の釣り合い(Σ R = -F)を満たすか /
   `stiffness_apply` が組み立てた K の積と一致するか / `solve_many` が右辺1本のとき
   `solve` と**1ビット同じ**か・複数本でも個別に解いたのと一致するか /
   `ReactionGroup` の契約(拘束の部分集合・重ならない)を破ると落ちるか。
"""

from __future__ import annotations

import json
import sys
import time

import numpy as np

# **Windowsの既定コンソール(cp932)で落ちないようにする。** 検査の本文は日本語なので
# cp932でも出せるが、記号(em dash等)が1つ混ざるだけで `UnicodeEncodeError` になり、
# **検査が1件も走らないまま異常終了する**。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
from duocad.topopt import bench
from duocad.topopt import simp

# --- 弾性論から出る参照値(E=1, nu=0.3) --------------------------------------
E_REF, NU_REF = 1.0, 0.3
_C1 = E_REF / ((1.0 + NU_REF) * (1.0 - 2.0 * NU_REF))
D00_REF = _C1 * (1.0 - NU_REF)  # 一軸ひずみ eps_xx=1 のときの eps^T D eps
G_REF = E_REF / (2.0 * (1.0 + NU_REF))  # せん断 gamma_xy=1 のときの eps^T D eps
D01_REF = _C1 * NU_REF  # ポアソン結合項。二軸ひずみでしか姿を現さない

FAILURES: list[str] = []
RESULTS: dict[str, object] = {}


def check(name: str, ok: bool, detail: str) -> None:
    """1件の検査結果を出力する。**落ちても即死せず全件を出してから終わる**
    (1つ直すたびに次が出てくるより、全部見えたほうが直す順を決められる)。"""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    if not ok:
        FAILURES.append(f"{name}: {detail}")


# ---------------------------------------------------------------------------
# 1. 要素剛性行列
# ---------------------------------------------------------------------------
def check_element_stiffness() -> None:
    print("\n[1] 要素剛性行列 Ke (24x24)")
    ke = simp.element_stiffness(E=E_REF, nu=NU_REF)

    check("形状", ke.shape == (24, 24), f"shape={ke.shape}")

    asym = float(np.abs(ke - ke.T).max())
    check("対称", asym < 1e-12, f"max|Ke-Ke^T|={asym:.3e}")

    w = np.linalg.eigvalsh(0.5 * (ke + ke.T))
    scale = float(np.abs(w).max())
    n_zero = int((np.abs(w) < 1e-9 * scale).sum())
    check("剛体モードがちょうど6本", n_zero == 6, f"|λ|<1e-9·λmax の個数={n_zero}")
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

    RESULTS["ke_axial"] = e_axial
    RESULTS["ke_shear"] = e_shear
    RESULTS["ke_biaxial"] = e_biaxial


# ---------------------------------------------------------------------------
# 2. 全体系(組み立て・拘束・荷重・コンプライアンス)
# ---------------------------------------------------------------------------
def check_global_system() -> None:
    print("\n[2] 全体系 (8x4x4)")
    m = simp.Model(8, 4, 4, E=E_REF, nu=NU_REF)
    rho = np.ones((m.nelx, m.nely, m.nelz))
    penal = 3.0

    k = m.assemble(rho, penal).tocsr()
    u = m.solve(rho, penal, solver="direct")

    check("Uの長さ", u.shape == (m.ndof,), f"U.shape={u.shape}")

    fixed_max = float(np.abs(u[m.fixed]).max())
    check("拘束DOFの変位が0", fixed_max == 0.0, f"max|U[fixed]|={fixed_max:.3e}")

    r = (k @ u - m.f)[m.free]
    rel = float(np.linalg.norm(r) / np.linalg.norm(m.f))
    check("釣り合い K·U = F", rel < 1e-9, f"||(K U - F)_free|| / ||F||={rel:.3e}")

    c, ce = m.compliance(rho, penal, u)
    fu = float(m.f @ u)
    check(
        "コンプライアンス = F·U",
        abs(c - fu) < 1e-8 * abs(fu),
        f"c={c:.10f} F·U={fu:.10f}",
    )
    check("要素エネルギーが非負", float(ce.min()) >= 0.0, f"min(ce)={ce.min():.3e}")

    # 荷重と拘束がz対称であること自体を確かめる(1-(c)の前提)
    fz = m.f.reshape(m.nelx + 1, m.nely + 1, m.nelz + 1, 3)
    f_asym = float(np.abs(fz - fz[:, :, ::-1]).max())
    check("荷重ベクトルがz対称", f_asym == 0.0, f"max|F - flip_z(F)|={f_asym:.3e}")

    fixed_mask = np.zeros(m.ndof, bool)
    fixed_mask[m.fixed] = True
    fm = fixed_mask.reshape(m.nelx + 1, m.nely + 1, m.nelz + 1, 3)
    check("拘束集合がz対称", bool((fm == fm[:, :, ::-1]).all()), "flip_zで一致")


def z_symmetry_error(rho: np.ndarray) -> float:
    """密度場のz方向の非対称さ(最大値で正規化した相対誤差)。"""
    return float(np.abs(rho - rho[:, :, ::-1]).max() / max(1e-30, np.abs(rho).max()))


# ---------------------------------------------------------------------------
# 3〜5. 完了条件1 (a)(b)(c)
# ---------------------------------------------------------------------------
def check_preset() -> simp.Result:
    p = bench.PRESET
    print(
        f"\n[3-5] プリセットの片持ち梁 "
        f"({p['nelx']}x{p['nely']}x{p['nelz']} = {p['nelx'] * p['nely'] * p['nelz']}要素, "
        f"最大{p['max_iter']}反復)"
    )
    t0 = time.perf_counter()
    res = simp.run_preset()
    elapsed = time.perf_counter() - t0
    it = res.history
    print(
        f"  完了: {len(it)}反復 / {elapsed:.1f}秒 ({elapsed / len(it):.2f}秒per反復) "
        f"/ 停止理由={res.stopped_by}"
    )
    print(f"  C: 初期 {it[0].compliance:.4f} → 最終 {it[-1].compliance:.4f}")

    # --- (a) 体積分率 ---
    print("\n  (a) 体積分率")
    target = float(p["volfrac"])
    v = float(res.rho.mean())
    check(
        "目標の±1%以内",
        abs(v - target) <= 0.01 * target,
        f"最終={v:.6f} 目標={target} 相対誤差={(v - target) / target:+.4%}",
    )

    # --- (b) コンプライアンス ---
    print("\n  (b) コンプライアンス")
    c_hist = np.array([i.compliance for i in it], float)
    check(
        "最終 < 初期",
        c_hist[-1] < c_hist[0],
        f"{c_hist[-1]:.4f} < {c_hist[0]:.4f} (減少率 {1 - c_hist[-1] / c_hist[0]:.2%})",
    )
    check("有限値のみ", bool(np.isfinite(c_hist).all()), "NaN/infなし")
    check("すべて正", bool((c_hist > 0).all()), f"min={c_hist.min():.4f}")
    tail = c_hist[-bench.TAIL_WINDOW :]
    check(
        f"終盤{bench.TAIL_WINDOW}反復あるか",
        tail.size == bench.TAIL_WINDOW,
        f"len={tail.size}",
    )
    swing = float((tail.max() - tail.min()) / tail.mean())
    check("終盤10反復の変動が1%未満", swing < 0.01, f"(max-min)/mean={swing:.4%}")

    # --- (c) z対称性(プリセット=既定のCG解法) ---
    print("\n  (c) z対称性")
    err = z_symmetry_error(res.rho)
    check(
        f"プリセット({p['solver']}解法)の密度場がz対称",
        err < 1e-6,
        f"相対誤差={err:.3e} (< 1e-6)",
    )

    RESULTS["preset"] = {
        "grid": [p["nelx"], p["nely"], p["nelz"]],
        "elements": p["nelx"] * p["nely"] * p["nelz"],
        "ndof": 3 * (p["nelx"] + 1) * (p["nely"] + 1) * (p["nelz"] + 1),
        "iterations": len(it),
        "seconds": round(elapsed, 1),
        "seconds_per_iteration": round(elapsed / len(it), 2),
        "compliance_initial": round(c_hist[0], 4),
        "compliance_final": round(c_hist[-1], 4),
        "tail10_swing": swing,
        "volfrac_final": v,
        "z_symmetry_rel_err": err,
        "stopped_by": res.stopped_by,
    }
    return res


def check_symmetry_direct() -> None:
    """直接解法での対称性。**反復解法の丸めを排して定式化そのものを見る**ので、
    許容は1e-6ではなく1e-9に取る(ここが緩いと(c)は反復回数を増やすだけで崩れうる)。"""
    g = bench.SYMMETRY_DIRECT
    print(
        f"\n  (c-2) z対称性(直接解法 "
        f"{g['nelx']}x{g['nely']}x{g['nelz']} / {g['max_iter']}反復)"
    )
    t0 = time.perf_counter()
    res = simp.optimize(
        g["nelx"], g["nely"], g["nelz"], max_iter=g["max_iter"], solver="direct"
    )
    err = z_symmetry_error(res.rho)
    check(
        "直接解法の密度場がz対称",
        err < 1e-9,
        f"相対誤差={err:.3e} (< 1e-9, {time.perf_counter() - t0:.1f}秒)",
    )
    RESULTS["symmetry_direct_rel_err"] = err


# ---------------------------------------------------------------------------
# 6. void 要素の除去
# ---------------------------------------------------------------------------
VOID_GRID = (12, 6, 6)


def _void_case() -> tuple[np.ndarray, np.ndarray]:
    """空要素のある題材。**面ごと落として「浮いた節点」を必ず作る**(そこが焦点)。"""
    nelx, nely, nelz = VOID_GRID
    void = np.zeros((nelx, nely, nelz), dtype=bool)
    void[3:8, 1:5, 1:5] = True  # 内部のくり抜き
    void[:, :, nelz - 1] = True  # 面ごと落とす
    floating = simp._floating_dofs(nelx, nely, nelz, void)
    return void, floating


def _orphan_free_dofs(model: simp.Model) -> int:
    """組み立てに載る要素のどれにも接していない自由自由度の数(K_ff の全零行)。"""
    active = model.edof if model.active is None else model.edof[model.active]
    touched = np.zeros(model.ndof, dtype=bool)
    touched[active.ravel()] = True
    return int(np.count_nonzero(~touched[model.free]))


def check_void_assembly() -> None:
    nelx, nely, nelz = VOID_GRID
    print(f"\n[6] void 要素の除去 ({nelx}x{nely}x{nelz})")
    void, floating = _void_case()
    full = simp.Model(nelx, nely, nelz, extra_fixed_dofs=floating)
    lean = simp.Model(nelx, nely, nelz, extra_fixed_dofs=floating, active_mask=~void)

    rows = np.kron(full.edof, np.ones((24, 1), dtype=np.int32)).ravel()
    cols = np.kron(full.edof, np.ones((1, 24), dtype=np.int32)).ravel()
    mine_rows, mine_cols = simp._coo_index_arrays(full.edof)
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
    naive = simp.Model(nelx, nely, nelz, active_mask=~void)
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
    k_void = simp.Model(
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
    RESULTS["void_free_dofs"] = int(lean.free.size)
    RESULTS["void_coo_terms"] = [int(full._iK.size), int(lean._iK.size)]
    RESULTS["void_nnz"] = [int(k_full.nnz), int(k_lean.nnz)]
    RESULTS["void_compliance_rel_diff"] = rel


# ---------------------------------------------------------------------------
# 7. 反力と多重右辺
# ---------------------------------------------------------------------------
REACTION_GRID = (10, 5, 5)


def check_reactions() -> None:
    nelx, nely, nelz = REACTION_GRID
    print(f"\n[7] 反力と多重右辺 ({nelx}x{nely}x{nelz})")
    model = simp.Model(nelx, nely, nelz)
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

    # (c) 多重右辺は前処理を1回だけ作って解く。答えは個別に解いたのと一致する
    extra = rng.standard_normal(model.ndof)
    extra[model.fixed] = 0.0
    pair = model.solve_many(rho, 3.0, np.stack([model.f, extra]), cg_rtol=1e-12)
    alone = model.solve_many(rho, 3.0, extra[None, :], cg_rtol=1e-12)[0]
    rel = float(np.linalg.norm(pair[1] - alone) / np.linalg.norm(alone))
    with simp.collect_solve_stats() as stats:
        model.solve_many(rho, 3.0, np.stack([model.f, extra]))
    check(
        "多重右辺の解が個別に解いたのと一致する",
        rel < 1e-8,
        f"相対 {rel:.3e}",
    )
    check(
        "多重右辺でも多重格子の setup は1回だけ",
        stats["solves"] == 2 and stats["mg_setup_seconds"] > 0.0,
        f"solve {stats['solves']} 本 / setup {stats['mg_setup_seconds']:.3f}秒",
    )

    # (d) 全体の釣り合い。反力の総和は荷重の合力の符号を反転したもの
    residual = model.stiffness_apply(rho, 3.0, u) - model.f
    total = np.bincount(model.fixed % 3, weights=residual[model.fixed], minlength=3)
    load = simp.load_resultant(model.f)
    err = float(np.linalg.norm(total + load) / np.linalg.norm(load))
    check(
        "反力の総和 = -(荷重の合力)",
        err < 1e-8,
        f"Σ R = {np.array2string(total, precision=6)} / F = "
        f"{np.array2string(load, precision=6)} (相対 {err:.3e})",
    )

    # (e) グループへ割っても総和は変わらない
    half = model.fixed.size // 2
    groups = [
        simp.ReactionGroup("lower", model.fixed[:half]),
        simp.ReactionGroup("upper", model.fixed[half:]),
    ]
    vectors = simp.reaction_forces(model, rho, 3.0, u, groups)
    err = float(np.linalg.norm(vectors.sum(axis=0) - total) / np.linalg.norm(total))
    check(
        "グループごとの反力を足すと全体の反力になる",
        err < 1e-12,
        f"2グループの和の相対差 {err:.3e}",
    )

    # (f) 契約の検出力。拘束されていない自由度・重なるグループは落ちる
    free_one = simp.ReactionGroup("bad", model.free[:3])
    caught_free = _raises(lambda: simp._check_reaction_groups(model, [free_one]))
    overlap = [
        simp.ReactionGroup("a", model.fixed[:half]),
        simp.ReactionGroup("b", model.fixed[half - 1 :]),
    ]
    caught_overlap = _raises(lambda: simp._check_reaction_groups(model, overlap))
    check(
        "契約を破った ReactionGroup は例外になる",
        caught_free and caught_overlap,
        f"拘束外={caught_free} / 重なり={caught_overlap}",
    )

    # (g) optimize から取り出せる(反復ごとと最終)
    result = simp.optimize(
        nelx, nely, nelz, max_iter=3, change_tol=0.0, reactions=groups
    )
    fractions = [r.fraction for r in result.reactions]
    check(
        "optimize が反力を Result と履歴の両方に載せる",
        len(result.reactions) == 2
        and all(len(item.reactions) == 2 for item in result.history)
        and abs(sum(fractions) - 1.0) < 1e-6,
        f"割合 {['%.4f' % v for v in fractions]}",
    )
    check(
        "reactions を渡さなければ1つも計算しない",
        simp.optimize(nelx, nely, nelz, max_iter=1).reactions == (),
        "既定は空",
    )
    RESULTS["reaction_fractions"] = [float(v) for v in fractions]


def _raises(call) -> bool:
    """呼んで ValueError が出れば True。**検出力の検査でしか使わない。**"""
    try:
        call()
    except ValueError:
        return True
    return False


def main() -> int:
    print("=" * 72)
    print("check_simp.py : SIMPトポロジー最適化の受入検査")
    print("=" * 72)
    check_element_stiffness()
    check_global_system()
    check_preset()
    check_symmetry_direct()
    check_void_assembly()
    check_reactions()

    print("\n" + "=" * 72)
    print("MEASURED " + json.dumps(RESULTS, ensure_ascii=False, default=float))
    print("=" * 72)
    if FAILURES:
        print(f"NG: {len(FAILURES)}件が不合格")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("OK: 全検査に合格")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
