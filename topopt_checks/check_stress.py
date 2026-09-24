"""von Mises応力・実単位換算・代表応力・最小安全率(`stress.py`)の受入検査。

    .\\.venv\\Scripts\\python.exe topopt_checks\\check_stress.py

**このファイルが合否そのものである。** タスク `20260904-0143` の完了条件1〜5を、
それだけでは通らない下支えの検査(B行列と `fem` の局所節点順が一致しているか)と
一緒に assert する。どれか1つでも落ちればプロセスは非ゼロで終わる。

検査の構成(上から順に、狭いものから広いものへ):

0. **B行列の下支え** — 一様ひずみ場を入れたら D·eps がそのまま出るか /
   剛体移動・剛体回転で応力が 0 か / **格子全体で `edof` と局所節点順が一致しているか**。
   ここがずれても上の検査は「もっともらしい別の値」で通りうるので、単独で押さえる。
1. **一様な棒の単軸引張** — von Mises が sigma = F/A と一致する(解析解。実単位で比較)。
2. **片持ち梁の曲げ** — 根元付近の断面の曲げ応力が sigma = M·c/I と一致し、
   解像度を3段上げると理論値へ収束する。
3. **無次元と実単位が一貫する** — E・荷重・要素サイズを振ったときに換算則どおりに動く
   (**応力は E に依存しない**ことも含む)。
4. **代表値の規則が効いている** — 階段状境界を持つ形(斜めに切った板)で、素の max と
   採用した代表値を両方出す。max は細分するほど跳ね、代表値は動かない。
   中間密度(灰色)を含む合成の密度場でも同じ規則が効くことを [4b] で見る。
5. **安全率が単調** — 許容応力を2倍にすると安全率がちょうど2倍になる。
"""

from __future__ import annotations

import json
import sys
import time

import numpy as np
from scipy import ndimage

# **Windowsの既定コンソール(cp932)で落ちないようにする。** 記号が1つ混ざるだけで
# `UnicodeEncodeError` になり、検査が1件も走らないまま異常終了する。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
from duocad.topopt import anchor
from duocad.topopt import fem
from duocad.topopt import stress

# [4] 斜めに切った板。3段とも必要(素の max が細分で単調に増えないことを見る検査なので、
# 段を減らせない)。12x8x2 までは落とせない(代表値の振れ幅が5%を超える。実測)。
STRESS_TAPERED_BASE = (16, 8, 2)
STRESS_TAPERED_MULTS = (1, 2, 3)

# [4b] 剛性差のある密度場を合成する格子。片持ち梁で十分な要素数を確保する。
STRESS_MIXED_DENSITY_GRID = (20, 10, 10)

FAILURES: list[str] = []
RESULTS: dict[str, object] = {}

# 実材料相当の換算パラメータ(アルミ 6061-T6 のあたり)。値そのものに意味は無く、
# 「1でない数を通しても式が合う」ことを見るために 1 から離した値を使う。
E_MPA = 68900.0
ALLOWABLE_MPA = 275.0 / 1.5
ELEMENT_MM = 2.5
LOAD_N = 1234.0


def check(name: str, ok: bool, detail: str) -> None:
    """1件の検査結果を出力する。**落ちても即死せず全件を出してから終わる。**"""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def node_id(i: int, j: int, k: int, nely: int, nelz: int) -> int:
    """`fem._build_edof` と同じ節点番号の規約。"""
    return i * (nely + 1) * (nelz + 1) + j * (nelz + 1) + k


def face_weights(n1: int, n2: int) -> np.ndarray:
    """(n1+1)x(n2+1) の節点へ一様な面圧を配る担当面積の重み(合計 = n1*n2)。

    双線形四辺形に一様トラクションを載せたときの整合節点荷重と一致する
    (面1つあたり各節点へ 1/4)。角 1/4・辺 1/2・内部 1。
    """
    wy = np.where((np.arange(n1 + 1) == 0) | (np.arange(n1 + 1) == n1), 0.5, 1.0)
    wz = np.where((np.arange(n2 + 1) == 0) | (np.arange(n2 + 1) == n2), 0.5, 1.0)
    return np.outer(wy, wz)


# ---------------------------------------------------------------------------
# 0. B行列の下支え
# ---------------------------------------------------------------------------
def check_b_matrix() -> None:
    print("\n[0] B行列と局所節点順の下支え")
    d = fem.elasticity_matrix(1.0, 0.3)
    b = stress.strain_displacement_center()

    coords = np.array(
        [(a, b_, c) for a in (0, 1) for b_ in (0, 1) for c in (0, 1)], dtype=float
    )

    def stress_of(field) -> np.ndarray:
        u = np.zeros(24)
        for l, (x, y, z) in enumerate(coords):
            u[3 * l : 3 * l + 3] = field(x, y, z)
        return d @ (b @ u)

    # 一様ひずみ eps_xx = 1 → sigma = D[:,0]
    got = stress_of(lambda x, y, z: (x, 0.0, 0.0))
    err = float(np.abs(got - d[:, 0]).max())
    check("一様ひずみ eps_xx=1 で sigma = D[:,0]", err < 1e-12, f"max|差|={err:.3e}")

    # 工学せん断ひずみ gamma_xy = 1 → sigma_xy = G
    got = stress_of(lambda x, y, z: (y, 0.0, 0.0))
    g_ref = 1.0 / (2.0 * (1.0 + 0.3))
    err = float(abs(got[3] - g_ref)) + float(np.abs(np.delete(got, 3)).max())
    check("せん断 gamma_xy=1 で sigma_xy=G、他成分0", err < 1e-12, f"誤差合計={err:.3e}")

    # von Mises の定義そのもの。単軸の検査(1・2)は純せん断・静水圧の項を通らないので、
    # せん断の係数3や偏差成分の取り違えがそこでは姿を現さない。
    tau = 3.7
    got = float(stress.von_mises(np.array([0.0, 0.0, 0.0, tau, 0.0, 0.0])))
    check(
        "純せん断の von Mises = sqrt(3)·tau",
        abs(got - np.sqrt(3.0) * tau) < 1e-12,
        f"{got:.12f} (理論値 {np.sqrt(3.0) * tau:.12f})",
    )
    got = float(stress.von_mises(np.array([tau, tau, tau, 0.0, 0.0, 0.0])))
    check("静水圧の von Mises = 0", abs(got) < 1e-12, f"{got:.3e}")

    for label, field in (
        ("剛体並進", lambda x, y, z: (1.0, 2.0, -3.0)),
        ("剛体回転", lambda x, y, z: (-y, x, 0.0)),
    ):
        got = stress_of(field)
        m = float(np.abs(got).max())
        check(f"{label}の応力が0", m < 1e-12, f"max|sigma|={m:.3e}")

    # **格子全体での整合。** `fem.NODE_SIGNS` の並びが `fem._build_edof` の l のまわし方と
    # ずれていると、要素1つの検査は通っても格子では要素ごとに違う値が出る。
    nelx, nely, nelz = 3, 4, 2
    fixed = np.array([0, 1, 2, 3, 4, 5, 6, 7, 8])
    load = np.zeros(3 * (nelx + 1) * (nely + 1) * (nelz + 1))
    load[-3] = 1.0
    model = fem.Model(nelx, nely, nelz, fixed_dofs=fixed, load=load)
    u = np.zeros(model.ndof)
    eps = np.array([0.011, -0.007, 0.003, 0.005, -0.002, 0.009])  # 6成分すべて非零
    for i in range(nelx + 1):
        for j in range(nely + 1):
            for k in range(nelz + 1):
                n = node_id(i, j, k, nely, nelz)
                x, y, z = float(i), float(j), float(k)
                u[3 * n + 0] = eps[0] * x + eps[3] * y + eps[5] * z
                u[3 * n + 1] = eps[1] * y + eps[4] * z
                u[3 * n + 2] = eps[2] * z
    field = stress.element_stress(model, u)
    want = d @ eps
    err = float(np.abs(field.tensor - want).max())
    check(
        "格子全体で一様ひずみ場が全要素同じ sigma になる",
        err < 1e-12,
        f"max|差|={err:.3e} (6成分すべて非零のひずみ、{nelx}x{nely}x{nelz})",
    )
    RESULTS["b_matrix_uniform_strain_err"] = err


# ---------------------------------------------------------------------------
# 1. 一様な棒の単軸引張
# ---------------------------------------------------------------------------
def build_bar(nelx: int, nely: int, nelz: int) -> fem.Model:
    """密度1で埋めた直方体を x 方向に引く系。

    x=0 / y=0 / z=0 の3面を対称面(その面に垂直な成分だけ拘束)にすると、横方向の
    収縮が自由になり**厳密に一様な単軸応力**になる。全自由度を固定する片持ち梁式の
    拘束にすると、拘束面の近傍でポアソン収縮が止められて解析解と合わなくなる。
    """
    ndof = 3 * (nelx + 1) * (nely + 1) * (nelz + 1)
    fixed = []
    for j in range(nely + 1):
        for k in range(nelz + 1):
            fixed.append(3 * node_id(0, j, k, nely, nelz) + 0)
    for i in range(nelx + 1):
        for k in range(nelz + 1):
            fixed.append(3 * node_id(i, 0, k, nely, nelz) + 1)
    for i in range(nelx + 1):
        for j in range(nely + 1):
            fixed.append(3 * node_id(i, j, 0, nely, nelz) + 2)
    weights = face_weights(nely, nelz)
    f = np.zeros(ndof)
    for j in range(nely + 1):
        for k in range(nelz + 1):
            f[3 * node_id(nelx, j, k, nely, nelz) + 0] = weights[j, k]
    f /= f.sum()
    return fem.Model(
        nelx, nely, nelz, fixed_dofs=np.unique(np.array(fixed)), load=f
    )


def check_uniaxial_bar() -> tuple[fem.Model, np.ndarray, np.ndarray]:
    print("\n[1] 一様な棒の単軸引張 (完了条件1・2・5)")
    nelx, nely, nelz = 8, 4, 3
    model = build_bar(nelx, nely, nelz)
    rho = np.ones((nelx, nely, nelz))
    u = model.solve(rho, 1.0, solver="direct")
    scale = stress.UnitScale.for_model(
        model, element_mm=ELEMENT_MM, load_n=LOAD_N, youngs_modulus_mpa=E_MPA
    )
    report = stress.evaluate(
        model, rho, scale=scale, allowable_mpa=ALLOWABLE_MPA, u=u
    )
    field = report.field

    area_mm2 = (nely * ELEMENT_MM) * (nelz * ELEMENT_MM)
    theory_mpa = LOAD_N / area_mm2
    vm = field.von_mises
    rel_max = float(np.abs(vm - theory_mpa).max() / theory_mpa)
    check(
        "全要素の von Mises が sigma = F/A と一致(相対1%以内)",
        rel_max < 0.01,
        f"最大相対誤差={rel_max:.3e} / 理論値={theory_mpa:.6f} MPa "
        f"(F={LOAD_N} N, A={area_mm2:.4g} mm^2)",
    )
    # 単軸であること(横応力とせん断が 0)まで見ないと、von Mises が偶然合う経路が残る。
    lateral = float(np.abs(field.tensor[..., 1:3]).max() / theory_mpa)
    shear = float(np.abs(field.tensor[..., 3:]).max() / theory_mpa)
    check(
        "横応力とせん断が0(純粋な単軸状態)",
        lateral < 1e-9 and shear < 1e-9,
        f"max|sigma_yy,zz|/sigma={lateral:.3e} / max|せん断|/sigma={shear:.3e}",
    )
    rep = report.representative
    check(
        "代表応力も F/A に一致(一様な場では規則が値を変えない)",
        abs(rep.value - theory_mpa) / theory_mpa < 1e-9,
        f"代表={rep.value:.6f} MPa / 素のmax={rep.raw_max:.6f} MPa / 理論={theory_mpa:.6f} MPa",
    )
    RESULTS["bar_rel_err"] = rel_max
    RESULTS["bar_theory_mpa"] = theory_mpa
    return model, rho, u


# ---------------------------------------------------------------------------
# 2. 片持ち梁の曲げ
# ---------------------------------------------------------------------------
def build_cantilever(nelx: int, nely: int, nelz: int) -> fem.Model:
    """x=0 面を全自由度固定し、x=nelx 面へ -y 方向の合力 1 を一様に配る。"""
    ndof = 3 * (nelx + 1) * (nely + 1) * (nelz + 1)
    fixed = []
    for j in range(nely + 1):
        for k in range(nelz + 1):
            n = node_id(0, j, k, nely, nelz)
            fixed += [3 * n, 3 * n + 1, 3 * n + 2]
    weights = face_weights(nely, nelz)
    f = np.zeros(ndof)
    for j in range(nely + 1):
        for k in range(nelz + 1):
            f[3 * node_id(nelx, j, k, nely, nelz) + 1] = -weights[j, k]
    f /= -f.sum()
    return fem.Model(
        nelx, nely, nelz, fixed_dofs=np.unique(np.array(fixed)), load=f
    )


def bending_ratio(nelx: int, nely: int, nelz: int) -> dict:
    """1解像度ぶんの曲げ応力を測って理論値との比を返す。

    測る断面を根元そのものではなく**梁せい1つぶん先**に取る。x=0 は全自由度を固定した
    面で、そこでは梁理論の前提(断面が自由に反る)が成り立たず、サンブナンの局所効果が
    残る(実測: 根元の要素は理論値を 8.5% 上回る)。**この offset は理論と合わせるための
    細工ではなく、比較してよい場所を選んでいる** —— offset を 1〜2 倍に振っても比は
    同じ値に落ち着く。
    """
    model = build_cantilever(nelx, nely, nelz)
    rho = np.ones((nelx, nely, nelz))
    t0 = time.perf_counter()
    u = model.solve(rho, 1.0, solver="cg", cg_rtol=1e-11)
    field = stress.element_stress(model, u)
    seconds = time.perf_counter() - t0

    h = float(nely)
    inertia = float(nelz) * h**3 / 12.0
    c = h / 2.0
    isec = nely  # 断面の中心は x = nely + 0.5(= 梁せい1つぶん先)
    x_sec = isec + 0.5
    moment = 1.0 * (nelx - x_sec)
    ys = (np.arange(nely) + 0.5) - c
    column = field.tensor[isec, :, nelz // 2, 0]
    slope = float(np.polyfit(ys, column, 1)[0])
    residual = float(np.abs(column - slope * ys).max() / np.abs(column).max())
    fiber = slope * c
    theory_fiber = moment * c / inertia
    # 外側1層の要素は中心が y = c - 0.5 にあるので、そこでの理論値とも比べる
    # (外挿を経由しない、生の値どうしの比較)。
    cell_ratio = float(column[-1] / (moment * ys[-1] / inertia))
    vm_outer = float(field.von_mises[isec, nely - 1, nelz // 2])
    return {
        "grid": f"{nelx}x{nely}x{nelz}",
        "seconds": seconds,
        "fiber_ratio": float(fiber / theory_fiber),
        "cell_ratio": cell_ratio,
        "linearity_residual": residual,
        "vm_vs_sxx": vm_outer / abs(float(column[-1])),
        "theory_fiber": theory_fiber,
        "fiber": float(fiber),
    }


def check_cantilever_bending() -> None:
    print("\n[2] 片持ち梁の曲げ / 解析解 sigma = M·c/I への収束 (完了条件1・5)")
    rows = [bending_ratio(*g) for g in ((32, 8, 4), (64, 16, 8), (96, 24, 12))]
    for row in rows:
        print(
            f"    {row['grid']:>12s}  外挿fiber/理論={row['fiber_ratio']:.4f}  "
            f"外側要素/理論={row['cell_ratio']:.4f}  "
            f"直線残差={row['linearity_residual']:.2e}  {row['seconds']:.1f}秒"
        )
    errors = [abs(r["fiber_ratio"] - 1.0) for r in rows]
    check(
        "曲げ応力が M·c/I と数%以内で一致",
        max(errors) < 0.03,
        "誤差=" + " / ".join(f"{e * 100:.2f}%" for e in errors),
    )
    check(
        "解像度を上げると理論値へ収束(3段で単調に誤差が減る)",
        errors[0] > errors[1] > errors[2],
        f"{errors[0] * 100:.2f}% > {errors[1] * 100:.2f}% > {errors[2] * 100:.2f}%",
    )
    check(
        "外挿を経由しない生の値(外側1層の要素中心)も理論値と一致",
        max(abs(r["cell_ratio"] - 1.0) for r in rows) < 0.03,
        " / ".join(f"{r['cell_ratio']:.4f}" for r in rows),
    )
    check(
        "断面内の sigma_xx が y の直線分布",
        max(r["linearity_residual"] for r in rows) < 0.02,
        " / ".join(f"{r['linearity_residual']:.2e}" for r in rows),
    )
    check(
        "外側1層では von Mises = |sigma_xx|(曲げは単軸)",
        max(abs(r["vm_vs_sxx"] - 1.0) for r in rows) < 0.01,
        " / ".join(f"{r['vm_vs_sxx']:.5f}" for r in rows),
    )
    RESULTS["cantilever"] = rows


# ---------------------------------------------------------------------------
# 3. 無次元と実単位が一貫する
# ---------------------------------------------------------------------------
def check_unit_scaling(model: fem.Model, rho: np.ndarray, u: np.ndarray) -> None:
    print("\n[3] 無次元 → 実単位の換算則 (完了条件2)")

    def report(*, e_mpa=E_MPA, load_n=LOAD_N, element_mm=ELEMENT_MM):
        scale = stress.UnitScale.for_model(
            model, element_mm=element_mm, load_n=load_n, youngs_modulus_mpa=e_mpa
        )
        return stress.evaluate(
            model, rho, scale=scale, allowable_mpa=ALLOWABLE_MPA, u=u
        )

    base = report()
    for label, got, want in (
        ("E を10倍 → 応力は変わらない", report(e_mpa=E_MPA * 10).representative_mpa, base.representative_mpa),
        ("E を10倍 → 変位は1/10", report(e_mpa=E_MPA * 10).max_displacement_mm, base.max_displacement_mm / 10.0),
        ("荷重を10倍 → 応力も10倍", report(load_n=LOAD_N * 10).representative_mpa, base.representative_mpa * 10.0),
        ("荷重を10倍 → 変位も10倍", report(load_n=LOAD_N * 10).max_displacement_mm, base.max_displacement_mm * 10.0),
        ("要素サイズを2倍 → 応力は1/4", report(element_mm=ELEMENT_MM * 2).representative_mpa, base.representative_mpa / 4.0),
        ("要素サイズを2倍 → 変位は1/2", report(element_mm=ELEMENT_MM * 2).max_displacement_mm, base.max_displacement_mm / 2.0),
    ):
        rel = abs(got - want) / abs(want)
        check(label, rel < 1e-12, f"{got:.10g} (期待 {want:.10g}, 相対差 {rel:.2e})")

    # 「E と荷重を10倍」の合わせ技(タスク本文の検査3の文言そのもの)。
    both = report(e_mpa=E_MPA * 10, load_n=LOAD_N * 10)
    rel = abs(both.representative_mpa - base.representative_mpa * 10.0) / (
        base.representative_mpa * 10.0
    )
    check(
        "E と荷重を同時に10倍 → 応力は10倍(E は効かない)",
        rel < 1e-12,
        f"{both.representative_mpa:.10g} MPa (期待 {base.representative_mpa * 10.0:.10g})",
    )

    # 無次元側の総荷重を読み違えないこと。棒の荷重ベクトルの合力は 1.0。
    resultant = stress.load_resultant(model)
    check(
        "無次元側の合力を Model から読めている",
        abs(resultant - 1.0) < 1e-12,
        f"合力={resultant:.12f}",
    )
    RESULTS["base_representative_mpa"] = base.representative_mpa
    RESULTS["base_max_displacement_mm"] = base.max_displacement_mm


# ---------------------------------------------------------------------------
# 4. 代表値の規則が効いている
# ---------------------------------------------------------------------------
def build_tapered_plate(mult: int) -> tuple[fem.Model, np.ndarray, float]:
    """斜め(45度)に切った板を x 方向に引く系。**境界が階段になる。**

    x > L/3 から上端が45度で下がり、幅が半分になる。細い側の公称応力 F/A が分かって
    いるので、「本当の応力集中」と「階段の角の離散化ノイズ」を数字で分けられる。
    """
    bx, by, bz = STRESS_TAPERED_BASE
    nelx, nely, nelz = bx * mult, by * mult, bz * mult
    solid = np.zeros((nelx, nely, nelz), dtype=bool)
    for i in range(nelx):
        top = max(nely - max(0, i - nelx // 3), nely // 2)
        solid[i, :top, :] = True
    rho = solid.astype(float)

    ndof = 3 * (nelx + 1) * (nely + 1) * (nelz + 1)
    fixed = []
    for j in range(nely + 1):
        for k in range(nelz + 1):
            fixed.append(3 * node_id(0, j, k, nely, nelz) + 0)
    for i in range(nelx + 1):
        for k in range(nelz + 1):
            fixed.append(3 * node_id(i, 0, k, nely, nelz) + 1)
    for i in range(nelx + 1):
        for j in range(nely + 1):
            fixed.append(3 * node_id(i, j, 0, nely, nelz) + 2)
    narrow = nely // 2
    weights = face_weights(narrow, nelz)
    f = np.zeros(ndof)
    for j in range(narrow + 1):
        for k in range(nelz + 1):
            f[3 * node_id(nelx, j, k, nely, nelz) + 0] = weights[j, k]
    f /= f.sum()
    # 空要素にしか接していない節点は剛性 emin でしか繋がっていない。`fem.optimize` が
    # 内部でやっているのと同じ処理を、ここでも通す(通さないと K がほぼ特異になる)。
    model = fem.Model(
        nelx,
        nely,
        nelz,
        fixed_dofs=np.unique(np.array(fixed)),
        load=f,
        extra_fixed_dofs=np.flatnonzero(~np.repeat(anchor.active_nodes(solid).ravel(), 3)),
    )
    nominal = 1.0 / (narrow * nelz)  # 細い側の公称応力(無次元)
    return model, rho, nominal


def check_representative_rule() -> None:
    print("\n[4] 代表応力の規則 / 素の max との比較 (完了条件3)")
    rows = []
    for mult in STRESS_TAPERED_MULTS:
        model, rho, nominal = build_tapered_plate(mult)
        t0 = time.perf_counter()
        u = model.solve(rho, 3.0, solver="direct")
        field = stress.element_stress(model, u)
        rep = stress.representative_stress(field, rho)
        rows.append(
            {
                "grid": f"{model.nelx}x{model.nely}x{model.nelz}",
                "raw_max": rep.raw_max / nominal,
                "dense_max": rep.dense_max / nominal,
                "representative": rep.value / nominal,
                "seconds": time.perf_counter() - t0,
            }
        )
        print(
            f"    {rows[-1]['grid']:>12s}  素のmax={rows[-1]['raw_max']:7.2f}x  "
            f"rho>=0.5のmax={rows[-1]['dense_max']:6.3f}x  "
            f"代表(99.5%)={rows[-1]['representative']:6.3f}x  "
            f"({rows[-1]['seconds']:.1f}秒)"
        )

    ratios = [r["raw_max"] / r["representative"] for r in rows]
    check(
        "素の max は代表値の5倍以上(rho の低い要素が値を支配している)",
        min(ratios) > 5.0,
        "倍率=" + " / ".join(f"{x:.1f}" for x in ratios),
    )
    check(
        "素の max は細分するほど大きくなる(収束しない)",
        rows[0]["raw_max"] < rows[1]["raw_max"] < rows[2]["raw_max"],
        " / ".join(f"{r['raw_max']:.2f}" for r in rows),
    )
    # **rho で絞るだけでは足りない**ことを見る。ここが無いと「rho>=0.5 の max を採れば
    # よかった」という別解が素通りし、パーセンタイルの検出力が示せない。
    dense = [r["dense_max"] for r in rows]
    check(
        "rho>=0.5 に絞ってもなお max は細分で単調に増える(角の離散化ノイズ)",
        dense[0] < dense[1] < dense[2] and dense[2] / dense[0] > 1.10,
        " / ".join(f"{x:.3f}" for x in dense) + f" (増加率 {dense[2] / dense[0]:.3f})",
    )
    reps = [r["representative"] for r in rows]
    spread = (max(reps) - min(reps)) / min(reps)
    check(
        "採用した代表値は同じ細分で動かない(5%以内)",
        spread < 0.05,
        " / ".join(f"{x:.3f}" for x in reps) + f" (振れ幅 {spread * 100:.1f}%)",
    )
    check(
        "代表値は公称応力より大きい(本当の応力集中は捨てていない)",
        min(reps) > 1.10,
        " / ".join(f"{x:.3f}" for x in reps),
    )
    RESULTS["tapered_plate"] = rows


def _mixed_density_field(dims: tuple[int, int, int]) -> np.ndarray:
    """中間密度(灰色)を含む密度場を合成する(SIMPは回さない)。

    ランダムな二値の種をガウスぼかしして、境界に灰色の勾配帯が残る密度場を作る。
    [4b] が見たいのは「中間密度の
    要素が離散化ノイズで素の max を支配する」ことなので、SIMPで実際に収束させる必要はない
    —— 灰色帯を持つ密度場でありさえすればよい。
    """
    rng = np.random.default_rng(20261017)
    seed = (rng.random(dims) < 0.35).astype(float)
    smooth = ndimage.gaussian_filter(seed, sigma=1.0, mode="nearest")
    smooth = smooth / float(smooth.max())
    return np.clip(smooth, 1e-9, 1.0)


def check_representative_on_mixed_density_field() -> None:
    print("\n[4b] 中間密度(灰色)を含む密度場(合成)でも同じ規則が効く")
    t0 = time.perf_counter()
    model = build_cantilever(*STRESS_MIXED_DENSITY_GRID)
    rho = _mixed_density_field(STRESS_MIXED_DENSITY_GRID)
    u = model.solve(rho, 3.0)
    print(f"    解いた: {time.perf_counter() - t0:.1f}秒")
    scale = stress.UnitScale.for_model(
        model, element_mm=ELEMENT_MM, load_n=LOAD_N, youngs_modulus_mpa=E_MPA
    )
    report = stress.evaluate(model, rho, scale=scale, allowable_mpa=ALLOWABLE_MPA, u=u)
    rep = report.representative
    unfiltered = stress.representative_stress(report.field, rho, rho_min=0.0)
    low = int(np.count_nonzero(rho < stress.DEFAULT_RHO_MIN))
    print(
        f"    素のmax={rep.raw_max:10.4g} MPa  rho>=0.5のmax={rep.dense_max:10.4g} MPa  "
        f"代表={rep.value:10.4g} MPa  (rho<0.5 の要素 {low} / {rho.size})"
    )
    print(
        f"    rho の閾値を外した代表値={unfiltered.value:10.4g} MPa "
        f"(母集団 {unfiltered.element_count} 要素 / 閾値ありは {rep.element_count} 要素)"
    )
    # **rho 閾値は「素の max を下げる」ためのものではない**(実測ではこの形の max 要素は
    # rho=1 の荷重直下にあり、閾値では動かない)。効くのは**採用する代表値**のほうで、
    # 中間密度の要素を母集団から外すと 34.6% 下がる。ここを取り違えると
    # 「閾値は要らない」という誤った結論が出る。
    check(
        "rho の閾値が代表値を下げている(中間密度の要素を母集団から外す効果)",
        unfiltered.value > 1.15 * rep.value,
        f"閾値なし {unfiltered.value:.4g} / 閾値あり {rep.value:.4g} MPa "
        f"({unfiltered.value / rep.value:.3f}倍)",
    )
    check(
        "パーセンタイルが効いている(rho>=0.5 の max は代表値より大きく跳ねる)",
        rep.dense_max > 1.5 * rep.value,
        f"rho>=0.5のmax/代表={rep.dense_max / rep.value:.2f}倍",
    )
    check(
        "代表値 <= rho>=0.5 の max <= 素の max(規則の順序が壊れていない)",
        rep.value <= rep.dense_max <= rep.raw_max,
        f"{rep.value:.4g} <= {rep.dense_max:.4g} <= {rep.raw_max:.4g} MPa",
    )
    check(
        "最小安全率が有限の正の数で返る",
        np.isfinite(report.safety_factor) and report.safety_factor > 0.0,
        report.summary(),
    )
    # 母集団が空になる場合は落とすこと。**0 を返して安全率を無限大にしてはいけない。**
    try:
        stress.representative_stress(report.field, np.zeros_like(rho))
        raised = "(例外が出なかった)"
    except Exception as exc:  # 変異検査で別の例外に化けても検出できるように広く受ける
        raised = f"{type(exc).__name__}: {exc}"
    check(
        "rho>=閾値 の要素が空なら落ちる(安全率を無限大にしない)",
        "ValueError" in raised,
        raised[:120],
    )
    # 応力側に非有限が混ざったら落とすこと(nan は比較が常に False なので、
    # 黙って通すと「危険側」の判定にも引っかからないまま安全率が nan になる)。
    # **母集団(rho>=0.5)に入る要素を汚すこと** —— rho の低い要素は最初から
    # 母集団の外なので、そこを nan にしても検出力が無い(実際に一度踏んだ)。
    dense_index = tuple(int(x) for x in np.unravel_index(np.argmax(rho), rho.shape))
    poisoned_vm = report.field.von_mises.copy()
    poisoned_vm[dense_index] = np.nan
    poisoned = stress.StressField(
        von_mises=poisoned_vm,
        tensor=report.field.tensor,
        unit=report.field.unit,
    )
    try:
        stress.representative_stress(poisoned, rho)
        raised = "(例外が出なかった)"
    except Exception as exc:  # 変異検査で別の例外に化けても検出できるように広く受ける
        raised = f"{type(exc).__name__}: {exc}"
    check(
        "von Mises に非有限が混ざったら落ちる(nan の安全率を返さない)",
        "ValueError" in raised,
        raised[:120],
    )
    RESULTS["mixed_density_output"] = {
        "raw_max_mpa": rep.raw_max,
        "dense_max_mpa": rep.dense_max,
        "representative_mpa": rep.value,
        "representative_no_rho_filter_mpa": unfiltered.value,
        "safety_factor": report.safety_factor,
        "low_density_elements": low,
    }


# ---------------------------------------------------------------------------
# 5. 安全率が単調
# ---------------------------------------------------------------------------
def check_safety_factor(model: fem.Model, rho: np.ndarray, u: np.ndarray) -> None:
    print("\n[5] 最小安全率 = 許容応力 / 代表応力 (完了条件4)")
    scale = stress.UnitScale.for_model(
        model, element_mm=ELEMENT_MM, load_n=LOAD_N, youngs_modulus_mpa=E_MPA
    )

    def report(allowable):
        return stress.evaluate(
            model, rho, scale=scale, allowable_mpa=allowable, u=u
        )

    base = report(ALLOWABLE_MPA)
    doubled = report(ALLOWABLE_MPA * 2.0)
    rel = abs(doubled.safety_factor - 2.0 * base.safety_factor) / (2.0 * base.safety_factor)
    check(
        "許容応力を2倍にすると安全率がちょうど2倍",
        rel < 1e-12,
        f"{base.safety_factor:.6f} -> {doubled.safety_factor:.6f} (相対差 {rel:.2e})",
    )
    want = ALLOWABLE_MPA / base.representative_mpa
    check(
        "安全率 = 許容応力 / 代表応力",
        abs(base.safety_factor - want) / want < 1e-12,
        f"{base.safety_factor:.6f} (= {ALLOWABLE_MPA:.4f} / {base.representative_mpa:.6f})",
    )
    # 荷重を2倍にすると安全率は半分。安全率が「素のmax」で決まっていないことも合わせて見る。
    scale2 = stress.UnitScale.for_model(
        model, element_mm=ELEMENT_MM, load_n=LOAD_N * 2, youngs_modulus_mpa=E_MPA
    )
    halved = stress.evaluate(
        model, rho, scale=scale2, allowable_mpa=ALLOWABLE_MPA, u=u
    )
    rel = abs(halved.safety_factor - 0.5 * base.safety_factor) / (0.5 * base.safety_factor)
    check(
        "荷重を2倍にすると安全率が半分",
        rel < 1e-12,
        f"{base.safety_factor:.6f} -> {halved.safety_factor:.6f} (相対差 {rel:.2e})",
    )
    for label, kwargs in (
        ("許容応力が0", {"allowable_mpa": 0.0}),
        ("許容応力が負", {"allowable_mpa": -1.0}),
    ):
        try:
            stress.evaluate(model, rho, scale=scale, u=u, **kwargs)
            raised = "(例外が出なかった)"
        except Exception as exc:
            raised = f"{type(exc).__name__}"
        check(f"{label} なら落ちる", raised == "ValueError", raised)
    RESULTS["safety_factor"] = base.safety_factor
    RESULTS["safety_summary"] = base.summary()


def main() -> int:
    print("=" * 72)
    print("check_stress.py : von Mises応力と最小安全率の受入検査")
    print("=" * 72)
    check_b_matrix()
    model, rho, u = check_uniaxial_bar()
    check_cantilever_bending()
    check_unit_scaling(model, rho, u)
    check_representative_rule()
    check_representative_on_mixed_density_field()
    check_safety_factor(model, rho, u)

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
