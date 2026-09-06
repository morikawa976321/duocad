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


def main() -> int:
    print("=" * 72)
    print("check_simp.py : SIMPトポロジー最適化の受入検査")
    print("=" * 72)
    check_element_stiffness()
    check_global_system()
    check_preset()
    check_symmetry_direct()

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
