"""幾何マルチグリッド前処理(`fem.py` の `solver="mg"`)の受入検査。

    .\\.venv\\Scripts\\python.exe topopt_checks\\check_multigrid.py

**この検査の存在理由**: マルチグリッド前処理が壊れていても、**CG は遅くなるだけで
正しい答えに収束する**。だから「速くなった」「答えが合っている」の2つを見ても実装の
正しさは示せない。ここでは次の4つを分けて見る。

1. **階層そのもの** —— 補間が単位分割か・剛体並進を厳密に再現するか・列フルランクか・
   **薄い軸を間引いていないか**(半粗視化)。
2. **前処理が対称正定値か** —— V-cycle の平滑化を前後で同じ回数にしないと前処理は
   非対称になり、CG は収束しないか嘘の解を返す。ここは対称性そのものを数字で見て、
   さらに**わざと非対称にした版でこの検査が落ちること**(検出力)まで確認する。
3. **部品が効いているか** —— 粗格子補正を捨てる・平滑化を弱める・重みを下げる、で
   **反復数が増えることを確認する**。増えないならその部品は最初から効いていない。
   同時に「劣化させても答えは合っている」ことも見せる(だから答えの一致は証拠にならない)。
4. **反復数が格子に依らないか** —— 段数を揃えた3解像度で反復数の比を見る。比較のため
   Jacobi 側の比も測り、**そちらは開くこと**を確認する(検査に意味があることの確認)。

**答えが変わっていないこと**(既定を Jacobi からマルチグリッドへ切り替えた影響)は [5]。
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
from duocad.topopt import fem

FAILURES: list[str] = []
RESULTS: dict[str, object] = {}

# 検査で使う格子。**受入検査は速さが命**なので、性能の基準線実測より小さい。
# 反復数の格子非依存性はここでも見えている(実測は docs/TOPOPT.md「マルチグリッド前処理」)。
GRIDS = ((20, 8, 8), (28, 12, 12), (36, 14, 14))
# 薄板題材と同じ「厚み方向だけ極端に薄い」格子。粗視化が一番効かない形。
THIN_GRID = (36, 14, 5)
# 密行列にしてスペクトルを直接見る格子(自由度が小さいこと自体が条件)。
SPECTRUM_GRID = (8, 4, 4)


def check(name: str, ok: bool, detail: str) -> None:
    """1件の検査結果を出力する。**落ちても即死せず全件を出してから終わる。**"""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def _rho_after(dims: tuple[int, int, int], iters: int = 8) -> np.ndarray:
    """滑らかに勾配のある剛性差つきの密度場を合成で作る(SIMPは回さない)。

    **一様密度で測ってはいけない。** 一様なら剛性差が無く、マルチグリッドは教科書どおりに
    効いてしまう(実測: 32x16x16 一様で段数を4段まで増やしても 14→18反復)。実際に効き方を
    決めるのは `emin=1e-9` が作る 10^9 の剛性差と、境界に残る「灰色」の勾配帯である。
    ランダムな二値の種を `sigma=1.0` でガウスぼかしして両方を作る。`sigma` はフィルタ半径の
    物理量ではなく、この検査群(4-2 の Jacobi 反復数の開き等)の閾値に対する校正値
    (実測: 1.0 で閾値と明確に分離する。0.6〜0.8 では 4-2 が閾値を割り込んだ)。
    `iters` は種の乱数系列を変えるためだけに使う(旧`max_iter` 引数の名残の引数名)。
    """
    rng = np.random.default_rng(1000 * dims[0] + 100 * dims[1] + 10 * dims[2] + iters)
    seed = (rng.random(dims) < 0.3).astype(float)  # 材料要素の目標割合 ~= 0.3
    smooth = ndimage.gaussian_filter(seed, sigma=1.0, mode="nearest")
    smooth = smooth / float(smooth.max())
    return 1e-9 + smooth * (1.0 - 1e-9)


def _iterations(model: fem.Model, rho: np.ndarray, **kwargs) -> tuple[int, np.ndarray]:
    with fem.collect_solve_stats() as stats:
        u = model.solve(rho, 3.0, **kwargs)
    return int(stats["cg_iterations"]), u


# ---------------------------------------------------------------------------
def check_hierarchy() -> None:
    print("\n[1] 階層(補間作用素)の作り")
    for dims in (GRIDS[2], THIN_GRID, (29, 13, 11)):
        pos = [
            (fem._coarse_axis(d, 4) if fem._coarse_axis(d, 4) is not None else np.arange(d + 1))
            for d in dims
        ]
        p = fem._prolongation(dims, pos)
        rows = np.asarray(p.sum(axis=1)).ravel()
        check(
            f"1-1 単位分割 {dims}",
            bool(np.allclose(rows, 1.0, atol=1e-12)),
            f"行和の最小 {rows.min():.15f} / 最大 {rows.max():.15f}",
        )
        check(
            f"1-2 重みが非負 {dims}",
            bool(p.data.min() >= 0.0),
            f"最小の重み {p.data.min():.3f}",
        )
        # 剛体並進: 粗格子で全節点が同じ変位なら、細格子でも同じ変位でなければならない。
        # (補間が節点番号の規約を取り違えていると、ここが真っ先に落ちる)
        coarse = np.tile(np.array([1.0, -2.0, 3.0]), p.shape[1] // 3)
        fine = p @ coarse
        want = np.tile(np.array([1.0, -2.0, 3.0]), p.shape[0] // 3)
        check(
            f"1-3 剛体並進を厳密に再現 {dims}",
            bool(np.allclose(fine, want, atol=1e-12)),
            f"最大誤差 {np.abs(fine - want).max():.3e}",
        )
        inject = np.asarray((p.T @ p).diagonal()).ravel()
        check(
            f"1-4 列フルランク(各粗DOFに単射の行) {dims}",
            bool(inject.min() >= 1.0),
            f"P^T P の対角の最小 {inject.min():.3f}",
        )

    # 半粗視化: 薄い軸は間引かない。**これが無いと薄板で粗格子が形をなさなくなる。**
    thin_model = fem.Model(*THIN_GRID)
    dims = list(THIN_GRID)
    seq = [tuple(dims)]
    for _ in fem.build_mg_transfers(THIN_GRID, thin_model.free):
        dims = [
            (len(fem._coarse_axis(d, 4)) - 1 if fem._coarse_axis(d, 4) is not None else d)
            for d in dims
        ]
        seq.append(tuple(dims))
    RESULTS["thin_grid_sequence"] = seq
    check(
        "1-5 薄い軸は間引かれない(半粗視化)",
        all(t[2] >= 3 for t in seq),
        f"厚み方向の要素数 {[t[2] for t in seq]}(格子の並び {seq})",
    )
    check(
        "1-6 長い軸は間引かれている",
        seq[-1][0] < seq[0][0],
        f"最長軸 {[t[0] for t in seq]}",
    )
    check(
        "1-7 奇数要素の軸も間引ける",
        fem._coarse_axis(29, 4) is not None and len(fem._coarse_axis(29, 4)) - 1 == 15,
        f"29要素 → {len(fem._coarse_axis(29, 4)) - 1}要素(端だけ幅1の粗セル)",
    )
    check(
        "1-8 閾値未満の軸は間引かない",
        fem._coarse_axis(3, 4) is None and fem._coarse_axis(4, 4) is not None,
        "3要素 → 間引かない / 4要素 → 間引く",
    )


# ---------------------------------------------------------------------------
def _asymmetric_cycle(levels, lu, b, level, opts, post: int):
    """**わざと非対称にした V-cycle**(後平滑化の回数を変える)。検出力の確認にだけ使う。"""
    if level >= len(levels):
        return lu.solve(b)
    a, s, p = levels[level]
    x = s * b
    for _ in range(opts.smooth_steps - 1):
        x += s * (b - a @ x)
    x += p @ _asymmetric_cycle(levels, lu, p.T @ (b - a @ x), level + 1, opts, post)
    for _ in range(post):
        x += s * (b - a @ x)
    return x


def _symmetry_error(apply, n: int, seed: int = 3) -> float:
    """`|v^T M w - w^T M v|` を `|v^T M w|` で正規化した最大値(乱数4組)。"""
    rng = np.random.default_rng(seed)
    worst = 0.0
    for _ in range(4):
        v = rng.standard_normal(n)
        w = rng.standard_normal(n)
        a = float(v @ apply(w))
        b = float(w @ apply(v))
        worst = max(worst, abs(a - b) / max(abs(a), abs(b), 1e-300))
    return worst


def check_symmetric_positive_definite() -> None:
    print("\n[2] 前処理が対称正定値(V-cycle の順序の罠)")
    dims = GRIDS[2]
    model = fem.Model(*dims)
    rho = _rho_after(dims)
    kg = model.assemble(rho, 3.0)
    kf = kg[model.free, :][:, model.free].tocsr()
    opts = fem.MG_DEFAULT
    levels, lu = fem._mg_setup(kf, model.mg_transfers(opts), opts)
    n = kf.shape[0]

    err = _symmetry_error(lambda b: fem._vcycle(levels, lu, b, 0, opts), n)
    check("2-1 前処理が対称", err < 1e-10, f"対称性の相対誤差 {err:.3e} < 1e-10")

    bad = _symmetry_error(lambda b: _asymmetric_cycle(levels, lu, b, 0, opts, post=0), n)
    RESULTS["symmetry_error"] = {"default": err, "post0": bad}
    check(
        "2-2 検出力: 後平滑化を0にすると 2-1 が落ちる",
        bad > 1e-6,
        f"非対称版の相対誤差 {bad:.3e}(既定 {err:.3e} の {bad / max(err, 1e-300):.0f}倍)",
    )

    rng = np.random.default_rng(11)
    quads = [float(v @ fem._vcycle(levels, lu, v, 0, opts)) for v in rng.standard_normal((8, n))]
    check(
        "2-3 前処理が正定値(v^T M v > 0)",
        min(quads) > 0.0,
        f"最小 {min(quads):.6g} / 最大 {max(quads):.6g}",
    )

    # 小格子では前処理付き作用素の固有値をそのまま見る。**M A の固有値が全て正で 1 の
    # まわりに集まっていること**が「前処理として効いている」の定義そのもの。
    small = fem.Model(*SPECTRUM_GRID)
    rho_s = np.full(SPECTRUM_GRID, 0.4)
    rho_s[:, :, -1] = 1e-3  # 剛性差を作る(一様だと簡単すぎる)
    kg_s = small.assemble(rho_s, 3.0)
    kf_s = kg_s[small.free, :][:, small.free].tocsr()
    levels_s, lu_s = fem._mg_setup(kf_s, small.mg_transfers(opts), opts)
    ns = kf_s.shape[0]
    m_dense = np.column_stack(
        [fem._vcycle(levels_s, lu_s, e, 0, opts) for e in np.eye(ns)]
    )
    # **絶対値で比べない。** M は A の逆に近いので要素の大きさは剛性の逆数(ここでは 1e12 台)。
    # 見るべきは「最大要素に対する非対称成分の比」。
    asym = float(np.abs(m_dense - m_dense.T).max() / np.abs(m_dense).max())
    check(
        "2-4 前処理行列そのものが対称",
        asym < 1e-12,
        f"|M - M^T| / max|M| = {asym:.3e} < 1e-12",
    )
    eig_m = np.linalg.eigvalsh(0.5 * (m_dense + m_dense.T))
    check("2-5 前処理の固有値が全て正", eig_m.min() > 0.0, f"最小固有値 {eig_m.min():.3e}")
    a_dense = kf_s.toarray()
    # M A は M^(1/2) A M^(1/2) と相似なので固有値は実。条件数の比が効き目そのもの。
    eig_ma = np.linalg.eigvals(m_dense @ a_dense).real
    kappa_mg = eig_ma.max() / eig_ma.min()
    # D^-1 A の固有値は D^(-1/2) A D^(-1/2)(対称)のそれと等しい。**A/d を対称化しても
    # 相似にならない**(最初そう書いて負の固有値が出た)。
    root = np.sqrt(kf_s.diagonal())
    eig_jac = np.linalg.eigvalsh(a_dense / root[:, None] / root[None, :])
    kappa_jac = eig_jac.max() / eig_jac.min()
    RESULTS["kappa"] = {"mg": kappa_mg, "jacobi": kappa_jac, "dofs": ns}
    check(
        "2-6 前処理後の条件数が Jacobi より小さい",
        kappa_mg < kappa_jac,
        f"κ(MA)={kappa_mg:.2f} < κ(D^-1A)={kappa_jac:.2f}({kappa_jac / kappa_mg:.1f}倍の改善)",
    )
    check("2-7 M A の固有値が全て正", eig_ma.min() > 0.0, f"最小 {eig_ma.min():.4f}")


# ---------------------------------------------------------------------------
def check_detection_power() -> None:
    print("\n[3] 部品が効いているか(劣化させると反復が増える)")
    dims = GRIDS[2]
    model = fem.Model(*dims)
    rho = _rho_after(dims)
    base, u_base = _iterations(model, rho, solver="mg")
    check("3-0 既定の反復数(基準)", base > 0, f"{base}回 / 格子 {dims}")

    cases = {
        "粗格子補正を捨てる": fem.MGOptions(coarse_correction=False),
        "平滑化を1回に減らす": fem.MGOptions(smooth_steps=1),
        "平滑化の重みを1/5にする": fem.MGOptions(weight_scale=0.2),
        "粗い段の平滑化を増やさない": fem.MGOptions(smooth_growth=1, direct_dofs=1200),
    }
    ratios = {}
    for label, opts in cases.items():
        it, u_bad = _iterations(model, rho, solver="mg", mg=opts)
        ref = base
        if label == "粗い段の平滑化を増やさない":
            # 粗い段が存在しないと比較にならないので、同じ段数の既定側と比べる。
            ref, _ = _iterations(
                model, rho, solver="mg", mg=fem.MGOptions(direct_dofs=1200)
            )
        ratios[label] = {"degraded": it, "reference": ref}
        check(
            f"3-1 {label} → 反復が増える",
            it > ref,
            f"{ref}回 → {it}回({it / ref:.2f}倍)",
        )
        # **劣化させても答えは合っている。** これが「答えの一致」を実装の証拠にできない理由。
        rel = float(np.linalg.norm(u_bad - u_base) / np.linalg.norm(u_base))
        check(
            f"3-2 {label} でも答えは同じ",
            rel < 1e-6,
            f"変位の相対差 {rel:.3e} < 1e-6(だから答えの一致は前処理の正しさの証拠にならない)",
        )
    # 弱める側の口しか無いこと。**1 より大きい重みは前処理を非正定値にする**
    # (委譲レビューの実測: weight_scale=5.0 で v^T M v が負になり CG が収束しない)。
    try:
        _iterations(model, rho, solver="mg", mg=fem.MGOptions(weight_scale=5.0))
        rejected = False
        detail = "例外が上がらなかった"
    except ValueError as exc:
        rejected = "weight_scale" in str(exc)
        detail = f"ValueError: {str(exc)[:60]}"
    check("3-4 重みを1より大きくする口は塞がれている", rejected, detail)
    RESULTS["degradation"] = ratios
    check(
        "3-3 粗格子補正を捨てると反復が3倍以上に増える",
        ratios["粗格子補正を捨てる"]["degraded"] >= 3 * base,
        f"{base}回 → {ratios['粗格子補正を捨てる']['degraded']}回"
        "(**粗格子が効いていないなら、この検査は落ちる**)",
    )


# ---------------------------------------------------------------------------
def check_grid_independence() -> None:
    print("\n[4] 反復数が格子に依らない")
    mg_counts: list[int] = []
    jac_counts: list[int] = []
    dof_list: list[int] = []
    # **段数を揃える**(`max_levels=1`)。段を増やすと SIMP の剛性差のせいで粗格子の補正が
    # 劣化するので、「格子非依存かどうか」と「段を足した影響」が混ざる。ここで見たいのは前者。
    two_level = fem.MGOptions(max_levels=1, direct_dofs=1)
    for dims in GRIDS:
        model = fem.Model(*dims)
        rho = _rho_after(dims)
        it_mg, _ = _iterations(model, rho, solver="mg", mg=two_level)
        it_jac, _ = _iterations(model, rho, solver="cg")
        mg_counts.append(it_mg)
        jac_counts.append(it_jac)
        dof_list.append(int(model.free.size))
        print(f"    {dims} 自由度 {model.free.size:,}: mg {it_mg}回 / jacobi {it_jac}回")
    RESULTS["grid_independence"] = {
        "grids": [list(g) for g in GRIDS],
        "dofs": dof_list,
        "mg": mg_counts,
        "jacobi": jac_counts,
    }
    span_mg = max(mg_counts) / min(mg_counts)
    span_jac = max(jac_counts) / min(jac_counts)
    # 閾値の根拠: 実測は 1.0〜1.1倍(自由度が 4.5倍 開いても反復数はほぼ動かない)。
    # 1.4 は「実測 + 3割の余裕」で、Jacobi 側の 1.7倍台とははっきり離れている
    # (`_rho_after` を旧SIMP経路から合成密度場へ差し替えた際に再校正した値。
    # 差し替え前は 2.4倍、差し替え後は 1.7倍台で安定する)。
    check(
        "4-1 段数を揃えれば反復数は格子にほぼ依らない",
        span_mg <= 1.4,
        f"最大/最小 = {span_mg:.2f} <= 1.4(反復数 {mg_counts} / 自由度 {dof_list})",
    )
    check(
        "4-2 比較: Jacobi は同じ格子で反復数が開く",
        span_jac >= 1.6,
        f"最大/最小 = {span_jac:.2f} >= 1.6(反復数 {jac_counts})。"
        "**ここが開かないなら 4-1 は何も示していない**",
    )
    # 伸びの指数(反復数 ∝ 自由度^p)。Jacobi は 0.4〜0.5、マルチグリッドは 0 に近い。
    p_mg = float(np.polyfit(np.log(dof_list), np.log(mg_counts), 1)[0])
    p_jac = float(np.polyfit(np.log(dof_list), np.log(jac_counts), 1)[0])
    RESULTS["exponent"] = {"mg": p_mg, "jacobi": p_jac}
    check(
        "4-3 伸びの指数が下がっている",
        p_mg < 0.15 and p_jac > 0.3,
        f"反復数 ∝ 自由度^p: mg p={p_mg:.3f} / jacobi p={p_jac:.3f}",
    )
    # 薄板(粗視化が一番効かない形)でも反復数が同じ水準に収まること。
    thin = fem.Model(*THIN_GRID)
    rho_thin = _rho_after(THIN_GRID)
    it_thin, _ = _iterations(thin, rho_thin, solver="mg", mg=two_level)
    it_thin_jac, _ = _iterations(thin, rho_thin, solver="cg")
    RESULTS["thin"] = {"mg": it_thin, "jacobi": it_thin_jac, "dofs": int(thin.free.size)}
    check(
        "4-4 薄い格子でも反復数が同じ水準",
        it_thin <= 1.6 * max(mg_counts),
        f"薄板 {THIN_GRID} で {it_thin}回(厚い格子の最大 {max(mg_counts)}回)。"
        f"同じ格子の Jacobi は {it_thin_jac}回",
    )


# ---------------------------------------------------------------------------
def check_same_answer() -> None:
    print("\n[5] 答えが変わっていない(既定を Jacobi から切り替えた影響)")
    dims = (16, 8, 8)
    model = fem.Model(*dims)
    rho = _rho_after(dims, iters=6)
    _, u_mg = _iterations(model, rho, solver="mg")
    _, u_cg = _iterations(model, rho, solver="cg")
    u_direct = model.solve(rho, 3.0, solver="direct")
    scale = float(np.linalg.norm(u_direct))
    rel_mg = float(np.linalg.norm(u_mg - u_direct) / scale)
    rel_cg = float(np.linalg.norm(u_cg - u_direct) / scale)
    RESULTS["displacement_vs_direct"] = {"mg": rel_mg, "cg": rel_cg}
    # 許容差の根拠: `cg_rtol=1e-9` で解いているので残差は 1e-9 相当、変位はその数倍。
    # 直接解法との差が 1e-7 に収まることは Jacobi 経路でも同じ(下の 5-2 が基準)。
    check("5-1 mg の変位が直接解法と一致", rel_mg < 1e-7, f"相対差 {rel_mg:.3e} < 1e-7")
    check("5-2 cg の変位が直接解法と一致(比較)", rel_cg < 1e-7, f"相対差 {rel_cg:.3e} < 1e-7")

    # void/solid の混じったパッシブ要素経路でも一致すること。**同じ K・同じ F を
    # mg/cg 2通りの解法で解くだけ**(SIMPの設計反復は回さない)なので、5-1/5-2 と
    # 同じ「線形解法の選択が答えを変えない」性質の、パッシブ要素版という位置づけ。
    # **`active_mask` では外さない**(空要素にしか接していない節点が浮いた自由度になり、
    # `fem._floating_dofs` で拘束へ足さない限り多重格子の前処理が特異になって落ちる ——
    # 実際に踏んだ)。ここで見たいのは解法の一致であって組み立て除外の効果ではないので、
    # 空要素は ρ=emin に固定するだけ(除外はしない)で済ませる。
    void = np.zeros(dims, dtype=bool)
    solid = np.zeros(dims, dtype=bool)
    void[:, :, -2:] = True
    solid[0, :, :2] = True
    passive_rho = rho.copy()
    passive_rho[void] = 1e-9
    passive_rho[solid] = 1.0
    u_mg2 = model.solve(passive_rho, 3.0, solver="mg")
    u_cg2 = model.solve(passive_rho, 3.0, solver="cg")
    c_mg, _ = model.compliance(passive_rho, 3.0, u_mg2)
    c_cg, _ = model.compliance(passive_rho, 3.0, u_cg2)
    d_c = abs(c_mg - c_cg) / abs(c_cg)
    d_u = float(np.linalg.norm(u_mg2 - u_cg2) / np.linalg.norm(u_cg2))
    RESULTS["passive_mg_vs_cg"] = {
        "compliance_mg": c_mg,
        "compliance_cg": c_cg,
        "rel_compliance": d_c,
        "rel_displacement": d_u,
    }
    check(
        "5-3 パッシブ要素経路のコンプライアンスが解法に依らない",
        d_c < 1e-6,
        f"mg {c_mg:.10f} / cg {c_cg:.10f}(相対差 {d_c:.3e} < 1e-6)",
    )
    check(
        "5-4 パッシブ要素経路の変位が解法に依らない",
        d_u < 1e-6,
        f"相対差 {d_u:.3e} < 1e-6",
    )
    check(
        "5-5 退避経路が残っている",
        all(
            fem.Model(4, 2, 2).solve(np.full((4, 2, 2), 0.5), 3.0, solver=s) is not None
            for s in ("cg", "direct", "mg")
        ),
        "solver は cg / direct / mg の3つとも解ける",
    )
    check(
        "5-6 既定の解法がマルチグリッド",
        fem.Model.solve.__kwdefaults__["solver"] == "mg",
        "Model.solve の既定が solver='mg'",
    )


# ---------------------------------------------------------------------------
def main() -> int:
    started = time.perf_counter()
    print("=" * 72)
    print("check_multigrid.py : 幾何マルチグリッド前処理の受入検査")
    print("=" * 72)
    check_hierarchy()
    check_symmetric_positive_definite()
    check_detection_power()
    check_grid_independence()
    check_same_answer()

    print("\n" + "=" * 72)
    print("MEASURED " + json.dumps(RESULTS, ensure_ascii=False, default=float))
    print("=" * 72)
    if FAILURES:
        print(f"NG: {len(FAILURES)}件が不合格 / {time.perf_counter() - started:.1f}秒")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"OK: 全検査に合格 / {time.perf_counter() - started:.1f}秒")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
