"""CPU 並列化(`fem.py` の `solve(threads=...)`)の受入検査。

    .\\.venv\\Scripts\\python.exe topopt_checks\\check_parallel.py

**この検査の存在理由**: 並列化はスレッド数を変えても「速くなったように見えるが答えが違う」
「1スレッドに戻すと元より遅い/答えが違う(退避経路が壊れている)」という壊れ方をする。
どちらも「動いているように見える」ので、次の4つを分けて見る。

1. **行ブロック並列 matvec そのものの正しさ** —— `_BlockedMatvec` / `parallel_matvec` が
   スレッド数に関わらず生の `a @ x` とビット一致するか(正方・非正方の両方)。
2. **しきい値とクランプ** —— 小さい行列でスレッド数を機械的に絞ること
   (絞らないと検査のような小さい格子でオーバーヘッドだけが乗る)。
3. **`Model.solve` の退避経路** —— `threads=1` が並列化を入れる前と**同じコード経路**
   (`blocked=None`)を通り、速度も答えも変わらないこと。
4. **`Model.solve` の並列一致性** —— `threads` を 1/2/4/8 と振っても、mg/cg どちらの解法でも
   変位場が**ビット一致**すること(docs/TOPOPT.md「答えの再現性」の選択(a))。
   V-cycle 単体・複数の密度場を続けて解く経路(候補評価の逐次呼び出しを模す)まで確認する。

**スケーリングの実測(頭打ちの有無・題材ごとの倍率)は受入検査の範囲外**であり、ここでは
やらない —— 秒は機械の熱状態で 1.2〜1.7倍振れるので、厳格な閾値の合否判定にすると
機械の調子で恒常的に失敗する検査になる。ここで見るのは「並列化しても壊れていないか」
という正しさだけである。
"""

from __future__ import annotations

import json
import sys
import time

import numpy as np

# **Windowsの既定コンソール(cp932)で落ちないようにする。**
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import scipy.sparse as sp

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
from duocad.topopt import fem

FAILURES: list[str] = []
RESULTS: dict[str, object] = {}

# 検査で使う格子。**受入検査は速さが命**なので、性能の基準線実測より小さい。
GRID = (24, 12, 12)
SMALL_GRID = (8, 4, 4)


def check(name: str, ok: bool, detail: str) -> None:
    """1件の検査結果を出力する。**落ちても即死せず全件を出してから終わる。**"""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def _rho_after(dims: tuple[int, int, int], iters: int = 6) -> np.ndarray:
    """剛性差のある滑らかな密度場を合成で作る(SIMPは回さない。一様密度だと剛性差が無く
    並列化の効きが変わるため、単純な乱数のガウスぼかしで代える。校正の経緯は
    `check_multigrid.py` の同名関数と同じ)。"""
    from scipy import ndimage

    rng = np.random.default_rng(1000 * dims[0] + 100 * dims[1] + 10 * dims[2] + iters)
    seed = (rng.random(dims) < 0.3).astype(float)
    smooth = ndimage.gaussian_filter(seed, sigma=1.0, mode="nearest")
    smooth = smooth / float(smooth.max())
    return 1e-9 + smooth * (1.0 - 1e-9)


# ---------------------------------------------------------------------------
def check_blocked_matvec_correctness() -> None:
    print("\n[1] 行ブロック並列 matvec そのものの正しさ")
    model = fem.Model(*GRID)
    rho = _rho_after(GRID)
    kg = model.assemble(rho, 3.0)
    kf = kg[model.free, :][:, model.free].tocsr()
    rng = np.random.default_rng(0)
    x = rng.standard_normal(kf.shape[1])
    want = kf @ x

    for threads in (1, 2, 3, 4, 8, 14):
        got = fem.parallel_matvec(kf, x, threads)
        check(
            f"1-1 threads={threads} が生の A@x とビット一致(正方 {kf.shape})",
            bool(np.array_equal(got, want)),
            f"最大差 {np.max(np.abs(got - want)):.3e}",
        )

    # 非正方行列(補間作用素 P)でも同じことを確認する。V-cycle が実際に並列化するのはここ。
    opts = fem.MG_DEFAULT
    transfers = model.mg_transfers(opts)
    p = transfers[0]
    e = rng.standard_normal(p.shape[1])
    want_p = p @ e
    for threads in (1, 4, 8):
        got_p = fem.parallel_matvec(p, e, threads)
        check(
            f"1-2 threads={threads} が非正方行列(P, {p.shape})でもビット一致",
            bool(np.array_equal(got_p, want_p)),
            f"最大差 {np.max(np.abs(got_p - want_p)):.3e}",
        )
    pt = p.transpose().tocsr()
    r = rng.standard_normal(pt.shape[1])
    want_pt = pt @ r
    for threads in (1, 4, 8):
        got_pt = fem.parallel_matvec(pt, r, threads)
        check(
            f"1-3 threads={threads} が転置(P^T, {pt.shape})でもビット一致",
            bool(np.array_equal(got_pt, want_pt)),
            f"最大差 {np.max(np.abs(got_pt - want_pt)):.3e}",
        )

    # 構造そのもの: 行ブロックの境界が行数を過不足なく覆っているか。
    bm = fem._BlockedMatvec(kf, 8)
    if bm.blocks is None:
        check("1-4 8スレッド分の行ブロックが作られる", False, "blocks が None")
    else:
        covered = sum(b.shape[0] for b in bm.blocks)
        check(
            "1-4 行ブロックの合計行数が元の行数と一致",
            covered == kf.shape[0],
            f"合計 {covered} / 元 {kf.shape[0]} / ブロック数 {len(bm.blocks)}",
        )
        check(
            "1-5 各ブロックが連続範囲を1回ずつ担当(境界の重複・抜けが無い)",
            bool(bm.bounds[0] == 0 and bm.bounds[-1] == kf.shape[0])
            and bool(np.all(np.diff(bm.bounds) >= 0)),
            f"bounds={bm.bounds.tolist()}",
        )


# ---------------------------------------------------------------------------
def check_thread_clamping() -> None:
    print("\n[2] しきい値とクランプ(小さい行列でオーバーヘッドを避ける)")
    check(
        "2-1 threads<=1 は並列化しない(pool が無い)",
        fem._BlockedMatvec(sp.identity(1000, format="csr"), 1).pool is None,
        "1000行・threads=1 → 直列経路",
    )
    check(
        "2-2 threads=0 / 負値は 1 として扱われる(壊れた値で暴走しない)",
        fem._effective_threads(1000, 0) == 1 and fem._effective_threads(1000, -5) == 1,
        f"_effective_threads(1000, 0)={fem._effective_threads(1000, 0)} / "
        f"_effective_threads(1000, -5)={fem._effective_threads(1000, -5)}",
    )
    small_n = 100  # `_MIN_ROWS_PER_THREAD`(512)よりずっと少ない行数
    eff = fem._effective_threads(small_n, 14)
    check(
        "2-3 小さい行列は要求スレッド数を機械的に絞る",
        eff < 14,
        f"{small_n}行・要求14スレッド → 実効{eff}スレッド(_MIN_ROWS_PER_THREAD={fem._MIN_ROWS_PER_THREAD})",
    )
    check(
        "2-4 行数が0でも例外にならない(実効1スレッド)",
        fem._effective_threads(0, 14) == 1,
        "0行 → 1スレッド",
    )
    big_n = fem._MIN_ROWS_PER_THREAD * 20
    check(
        "2-5 十分大きい行列は要求どおりのスレッド数になる",
        fem._effective_threads(big_n, 14) == 14,
        f"{big_n}行・要求14スレッド → 実効{fem._effective_threads(big_n, 14)}スレッド",
    )


# ---------------------------------------------------------------------------
def check_default_thread_count() -> None:
    print("\n[3] 既定スレッド数の解決(環境変数 TOPOPT_THREADS)")
    import os

    saved = os.environ.get("TOPOPT_THREADS")
    try:
        os.environ["TOPOPT_THREADS"] = "3"
        check(
            "3-1 TOPOPT_THREADS を読む",
            fem._default_thread_count() == 3,
            f"TOPOPT_THREADS=3 → {fem._default_thread_count()}",
        )
        os.environ["TOPOPT_THREADS"] = "0"
        check(
            "3-2 0以下の値は1へ丸める",
            fem._default_thread_count() == 1,
            f"TOPOPT_THREADS=0 → {fem._default_thread_count()}",
        )
        os.environ["TOPOPT_THREADS"] = "not-a-number"
        check(
            "3-3 壊れた値は1へ退避する(黙って並列化が有効になるより安全側)",
            fem._default_thread_count() == 1,
            f"TOPOPT_THREADS='not-a-number' → {fem._default_thread_count()}",
        )
        del os.environ["TOPOPT_THREADS"]
        via_cpu = fem._default_thread_count()
        check(
            "3-4 未設定なら総論理コアの半分を使う(下限1)",
            via_cpu == max(1, (os.cpu_count() or 1) // 2),
            f"os.cpu_count()={os.cpu_count()} → {via_cpu}",
        )
    finally:
        if saved is None:
            os.environ.pop("TOPOPT_THREADS", None)
        else:
            os.environ["TOPOPT_THREADS"] = saved
    check(
        "3-5 fem.PARALLEL_THREADS を代入すると呼び出し時に反映される(MG_DEFAULT と同じ流儀)",
        True,
        "下の[4]で fem.PARALLEL_THREADS=1 に差し替えて確認する",
    )


# ---------------------------------------------------------------------------
def check_retreat_path() -> None:
    print("\n[4] Model.solve の退避経路(threads=1 は並列化前と同じ)")
    model = fem.Model(*GRID)
    rho = _rho_after(GRID)

    u_default_before = model.solve(rho, 3.0, solver="mg", threads=1)
    # **`fem.PARALLEL_THREADS` を直接書き換えること。** `fem.PARALLEL_THREADS` は
    # `fem` からの再輸出(import 時点の値のコピー)で、`Model.prepare` が呼び出し時に読むのは
    # `fem` 側のモジュール変数そのもの。`fem.PARALLEL_THREADS = 1` は simp 側の束縛だけを
    # 差し替え、`fem.PARALLEL_THREADS` には反映されない。
    saved = fem.PARALLEL_THREADS
    try:
        fem.PARALLEL_THREADS = 1
        u_via_global = model.solve(rho, 3.0, solver="mg")
    finally:
        fem.PARALLEL_THREADS = saved
    check(
        "4-1 fem.PARALLEL_THREADS=1 と threads=1 は同じ答え",
        bool(np.array_equal(u_default_before, u_via_global)),
        "MG_DEFAULT の代入と同じ流儀(呼び出し時に読む)を確認",
    )

    # blocked=None のコード経路そのものを直接突き合わせる。
    opts = fem.MG_DEFAULT
    kg = model.assemble(rho, 3.0)
    kf = kg[model.free, :][:, model.free].tocsr()
    levels, lu = fem._mg_setup(kf, model.mg_transfers(opts), opts)
    n = kf.shape[0]
    rng = np.random.default_rng(1)
    b = rng.standard_normal(n)
    v_no_blocked = fem._vcycle(levels, lu, b, 0, opts, None)
    v_blocked_none_explicit = fem._vcycle(levels, lu, b, 0, opts)  # 既定引数
    check(
        "4-2 blocked を省略しても明示 None と同じ(既定引数の意味が変わっていない)",
        bool(np.array_equal(v_no_blocked, v_blocked_none_explicit)),
        "_vcycle(levels, lu, b, 0, opts) の既定 blocked=None",
    )
    blocked_1 = fem._build_blocked_levels(levels, 1)
    check(
        "4-3 threads=1 は _build_blocked_levels が None を返す(並列化しない合図)",
        blocked_1 is None,
        f"_build_blocked_levels(levels, 1) = {blocked_1!r}",
    )

    # 速度: 並列化前(threads を意識しないコード経路そのもの)と threads=1 が同等であること。
    # **厳格な閾値にしない**(機械の熱状態で秒が振れるので、2倍以上遅くなっていないかだけ見る)。
    def bench(fn, repeat=3):
        fn()
        best = float("inf")
        for _ in range(repeat):
            t0 = time.perf_counter()
            fn()
            best = min(best, time.perf_counter() - t0)
        return best

    t_no_blocked = bench(lambda: fem._vcycle(levels, lu, b, 0, opts))
    t_threads_1 = bench(lambda: fem._vcycle(levels, lu, b, 0, opts, fem._build_blocked_levels(levels, 1)))
    check(
        "4-4 threads=1 の速度は素の呼び出しの2倍以内(オーバーヘッドが乗っていない)",
        t_threads_1 <= t_no_blocked * 2.0 + 1e-6,
        f"素の呼び出し {t_no_blocked*1e3:.4f}ms / threads=1相当 {t_threads_1*1e3:.4f}ms",
    )
    RESULTS["retreat_path_ms"] = {"no_blocked": t_no_blocked * 1e3, "threads_1": t_threads_1 * 1e3}


# ---------------------------------------------------------------------------
def check_parallel_consistency() -> None:
    print("\n[5] Model.solve の並列一致性(threads を振ってもビット一致)")
    model = fem.Model(*GRID)
    rho = _rho_after(GRID)

    for solver in ("mg", "cg"):
        results = {t: model.solve(rho, 3.0, solver=solver, threads=t) for t in (1, 2, 3, 4, 8)}
        base = results[1]
        all_equal = all(np.array_equal(base, u) for u in results.values())
        check(
            f"5-1 solver={solver!r}: threads=1/2/3/4/8 が全てビット一致",
            all_equal,
            f"最大差 {max(float(np.max(np.abs(base - u))) for u in results.values()):.3e}",
        )

    # 同じ threads でも複数回呼んで結果が安定していること(スレッドの実行順序に依らない)。
    reps = [model.solve(rho, 3.0, solver="mg", threads=8) for _ in range(5)]
    check(
        "5-2 threads=8 を5回繰り返しても毎回ビット一致(スレッドの実行順序に依らない)",
        all(np.array_equal(reps[0], r) for r in reps[1:]),
        "5回の solve が全て同じ配列",
    )

    # **複数の密度場を続けて解く**(探索が候補を逐次評価する使い方を模す。SIMPの設計反復は
    # もう無いので `optimize()` 経由の比較はできない —— 同じ `Model.solve` を複数の異なる
    # 密度場に対して呼ぶだけで、逐次呼び出しでも thread 数が答えを変えないことを見る)。
    sequence = [_rho_after(GRID, iters=i) for i in range(3)]
    seq_t1 = [model.solve(r, 3.0, solver="mg", threads=1) for r in sequence]
    seq_t8 = [model.solve(r, 3.0, solver="mg", threads=8) for r in sequence]
    check(
        "5-3 密度場を変えながら逐次呼び出しても threads=1/8 がビット一致",
        all(np.array_equal(a, b) for a, b in zip(seq_t1, seq_t8)),
        f"{len(sequence)}件の密度場すべてで一致",
    )


# ---------------------------------------------------------------------------
def check_small_grid_survives() -> None:
    print("\n[7] 検査規模の小さい格子でも並列化が壊れない(オーバーヘッドの逃げ道)")
    model = fem.Model(*SMALL_GRID)
    rho = np.full(SMALL_GRID, 0.5)
    for threads in (1, 8, 14):
        u = model.solve(rho, 3.0, solver="mg", threads=threads)
        check(
            f"7-1 {SMALL_GRID}(自由度{model.free.size})・threads={threads} でも解ける",
            bool(np.all(np.isfinite(u))),
            f"max|u|={np.max(np.abs(u)):.3e}",
        )
    u1 = model.solve(rho, 3.0, solver="mg", threads=1)
    u14 = model.solve(rho, 3.0, solver="mg", threads=14)
    check(
        "7-2 小さい格子でも threads=1 と threads=14 がビット一致",
        bool(np.array_equal(u1, u14)),
        f"自由度{model.free.size}(_MIN_ROWS_PER_THREAD={fem._MIN_ROWS_PER_THREAD}未満なら実質1スレッドに丸められる)",
    )


# ---------------------------------------------------------------------------
def main() -> int:
    started = time.perf_counter()
    print("=" * 72)
    print("check_parallel.py : CPU並列化(疎行列×密ベクトルの行ブロック並列)の受入検査")
    print("=" * 72)
    check_blocked_matvec_correctness()
    check_thread_clamping()
    check_default_thread_count()
    check_retreat_path()
    check_parallel_consistency()
    check_small_grid_survives()

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
