"""密度フィルタ半径を mm で指定する口の受入検査。

    .\\.venv\\Scripts\\python.exe topopt_checks\\check_filter_radius.py

**このファイルが合否そのものである。** タスク
`20260904-1557-topopt-filter-radius-in-mm` の完了条件を assert する。
どれか1つでも落ちればプロセスは非ゼロで終わる。

検査の構成:

1. **引数の形**(`simp.check_rmin_target`)— `rmin` と `rmin_mm` は排他 / どちらも正の値であること /
   `rmin_mm` を渡すときは `element_volume_mm3` も要ること。
2. **下限を下回ると `simp.FilterRadiusError`**(逃げ道2つが数字で入っていること)— 属性
   (`min_rmin_mm` / `max_element_size_mm` / `min_elements_long_axis` / `shortfall_mm`)が
   手計算した期待値と一致することを見る。
3. **境界**(ちょうど1.5要素)は通り、そのわずか下は落ちる。
4. **既定の挙動**(両方省略)が `simp.DEFAULT_RMIN`(=1.5要素)に解決され、`simp.PRESET["rmin"]`
   と一致すること。
5. **mm指定と要素指定の等価性** — 同じ物理半径を単位だけ変えて指定すると、密度場が一致する。
6. **検出力(この下限が守っている性質)** — `rmin` を下限より小さくして解くと、隣接要素の
   密度差(離散ラプラシアンの絶対値平均)がチェッカーボードの指標として跳ね上がり、既定
   (1.5要素)以上では跳ね上がらないことを実測する。「1.5 を assert するだけ」にならないよう、
   その数値自体が何を守っているかをここで示す。
7. **上位2層への配線** — `anchor.run_pipeline` / `design_space.run_topopt` の両方が
   `rmin` / `rmin_mm` を受け取り、排他違反をボクセル化の前に弾き、実際に `simp.optimize` へ
   渡っていること(戻り値の `rmin` / `rmin_mm` が手計算と一致)。
"""

from __future__ import annotations

import sys
import time

import numpy as np

# **Windowsの既定コンソール(cp932)で落ちないようにする**(他の check_*.py と同じ理由)。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
from duocad.topopt import OUTPUT_DIR
from duocad.topopt import anchor as an
from duocad.topopt import bench
from duocad.topopt import design_space as ds
from duocad.topopt import simp

FAILURES: list[str] = []

# 実経路の検査が書き出す STL。**受入検査の成果物ではない**ので .gitignore にある。
STL_TMP = OUTPUT_DIR / "filter_radius_tmp.stl"


def check(name: str, ok: bool, detail: str) -> None:
    """1件の検査結果を出力する。**落ちても即死せず全件を出してから終わる。**"""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def rel(a: float, b: float) -> float:
    """相対差(基準は b)。"""
    return abs(a - b) / abs(b)


# ---------------------------------------------------------------------------
# 1. 引数の形
# ---------------------------------------------------------------------------
def check_argument_shape() -> None:
    print("\n[1] 引数の形(simp.check_rmin_target)")

    for name, kwargs in (
        ("rmin と rmin_mm の両方", dict(rmin=1.5, rmin_mm=3.0)),
        ("rmin が非正(0.0)", dict(rmin=0.0)),
        ("rmin が非正(負)", dict(rmin=-1.0)),
        ("rmin_mm が非正(0.0)", dict(rmin_mm=0.0)),
    ):
        try:
            simp.check_rmin_target(kwargs.get("rmin"), kwargs.get("rmin_mm"))
            check(f"{name} で例外", False, "例外が上がらなかった")
        except ValueError as exc:
            check(f"{name} で例外", type(exc) is ValueError, f"{exc}")

    # 形として正しい(rmin_mm だけ)組は check_rmin_target は通す。
    try:
        simp.check_rmin_target(None, 3.0)
        check("rmin_mm だけは check_rmin_target を通る", True, "例外なし")
    except ValueError as exc:
        check("rmin_mm だけは check_rmin_target を通る", False, f"{exc}")

    # `element_volume_mm3` が無いと optimize 自身が落とす(check_rmin_target は形しか見ない)。
    try:
        simp.optimize(4, 4, 4, rmin_mm=3.0, max_iter=1)
        check("要素体積なしの rmin_mm 指定で例外", False, "例外が上がらなかった")
    except ValueError as exc:
        check(
            "要素体積なしの rmin_mm 指定で例外",
            "element_volume_mm3" in str(exc) and not isinstance(exc, simp.FilterRadiusError),
            f"{exc}",
        )


# ---------------------------------------------------------------------------
# 2. 下限を下回ると FilterRadiusError(逃げ道2つが数字で入っている)
# ---------------------------------------------------------------------------
def check_below_minimum() -> None:
    print("\n[2] 下限を下回ると simp.FilterRadiusError")
    # 丸めのよい数で選ぶ(手計算しやすくするため)。要素サイズ 3.0mm の格子に
    # rmin_mm=3.0(=1.0要素)を要求する — 下限 1.5要素の 2/3 しかない。
    element_size_mm = 3.0
    element_volume_mm3 = element_size_mm**3
    nelx, nely, nelz = 10, 6, 6
    rmin_mm = 3.0  # → 1.0要素

    expected_min_rmin_mm = simp.DEFAULT_RMIN * element_size_mm
    expected_max_element_size_mm = rmin_mm / simp.DEFAULT_RMIN
    expected_elements_long_axis = max(nelx, nely, nelz)
    expected_length_mm = element_size_mm * expected_elements_long_axis
    expected_min_elements = int(
        np.ceil(expected_length_mm / expected_max_element_size_mm)
    )
    expected_shortfall = expected_min_rmin_mm - rmin_mm

    try:
        simp.optimize(
            nelx, nely, nelz,
            rmin_mm=rmin_mm, element_volume_mm3=element_volume_mm3, max_iter=1,
        )
        check("下限を割る rmin_mm で例外", False, "例外が上がらなかった")
        return
    except simp.FilterRadiusError as caught:
        check("下限を割る rmin_mm で例外", True, f"{caught}")
        exc = caught
        text = str(exc)

    check(
        "min_rmin_mm(逃げ道2: この解像度で指定できる下限)",
        rel(exc.min_rmin_mm, expected_min_rmin_mm) < 1e-12,
        f"{exc.min_rmin_mm:.6g} (期待 {expected_min_rmin_mm:.6g})",
    )
    check(
        "max_element_size_mm(逃げ道1: 要素サイズをいくつ以下にすればよいか)",
        rel(exc.max_element_size_mm, expected_max_element_size_mm) < 1e-12,
        f"{exc.max_element_size_mm:.6g} (期待 {expected_max_element_size_mm:.6g})",
    )
    check(
        "min_elements_long_axis(逃げ道1: それが最長軸の分割数でいくつに当たるか)",
        exc.min_elements_long_axis == expected_min_elements,
        f"{exc.min_elements_long_axis} (期待 {expected_min_elements})",
    )
    check(
        "shortfall_mm(不足量)",
        rel(exc.shortfall_mm, expected_shortfall) < 1e-12,
        f"{exc.shortfall_mm:.6g} (期待 {expected_shortfall:.6g})",
    )
    check(
        "element_size_mm / rmin_mm がそのまま属性に載っている",
        rel(exc.element_size_mm, element_size_mm) < 1e-12
        and rel(exc.rmin_mm, rmin_mm) < 1e-12,
        f"element_size_mm={exc.element_size_mm:.6g} rmin_mm={exc.rmin_mm:.6g}",
    )
    check(
        "メッセージに逃げ道2つの数字が両方入っている",
        f"{expected_max_element_size_mm:.6g}" in text
        and f"{expected_min_elements}" in text
        and f"{expected_min_rmin_mm:.6g}" in text,
        text[:200],
    )
    check(
        "VolumeTargetError ではなく FilterRadiusError(別の例外と混同していない)",
        not isinstance(exc, simp.VolumeTargetError),
        f"{type(exc).__name__}",
    )


# ---------------------------------------------------------------------------
# 3. 境界(ちょうど1.5要素)
# ---------------------------------------------------------------------------
def check_boundary() -> None:
    print("\n[3] 境界(ちょうど1.5要素)")
    element_size_mm = 2.5
    element_volume_mm3 = element_size_mm**3
    rmin_mm_at = simp.DEFAULT_RMIN * element_size_mm  # ちょうど1.5要素

    res = simp.optimize(
        8, 6, 6, rmin_mm=rmin_mm_at, element_volume_mm3=element_volume_mm3, max_iter=1,
    )
    check(
        "ちょうど1.5要素は例外にならず、実効rminがDEFAULT_RMINに一致する",
        rel(res.rmin, simp.DEFAULT_RMIN) < 1e-12,
        f"rmin={res.rmin:.10g} (期待 {simp.DEFAULT_RMIN})",
    )

    rmin_mm_below = rmin_mm_at * 0.999  # わずかに下限を割る
    try:
        simp.optimize(
            8, 6, 6, rmin_mm=rmin_mm_below, element_volume_mm3=element_volume_mm3, max_iter=1,
        )
        check("境界のわずか下(0.999倍)は例外", False, "例外が上がらなかった")
    except simp.FilterRadiusError as exc:
        check(
            "境界のわずか下(0.999倍)は例外",
            exc.shortfall_mm > 0.0,
            f"shortfall_mm={exc.shortfall_mm:.6g}",
        )


# ---------------------------------------------------------------------------
# 4. 既定の挙動(両方省略)
# ---------------------------------------------------------------------------
def check_default_unchanged() -> None:
    print("\n[4] 既定の挙動(rmin/rmin_mmを両方省略)")
    res = simp.optimize(6, 4, 4, max_iter=1)
    check(
        "省略時の実効rminがDEFAULT_RMIN(=1.5)に一致する",
        res.rmin == simp.DEFAULT_RMIN,
        f"res.rmin={res.rmin}",
    )
    check(
        "DEFAULT_RMIN が PRESET['rmin'] と一致する(既定を変えていない)",
        simp.DEFAULT_RMIN == simp.PRESET["rmin"],
        f"DEFAULT_RMIN={simp.DEFAULT_RMIN} PRESET['rmin']={simp.PRESET['rmin']}",
    )
    check(
        "要素体積を渡していなければ rmin_mm は None",
        res.rmin_mm is None,
        f"res.rmin_mm={res.rmin_mm!r}",
    )


# ---------------------------------------------------------------------------
# 5. mm指定と要素指定の等価性
# ---------------------------------------------------------------------------
def check_unit_equivalence() -> None:
    print("\n[5] mm指定と要素指定の等価性")
    element_size_mm = 3.0
    element_volume_mm3 = element_size_mm**3
    rmin_elements = 1.7

    r_elem = simp.optimize(12, 6, 6, rmin=rmin_elements, max_iter=8)
    r_mm = simp.optimize(
        12, 6, 6,
        rmin_mm=rmin_elements * element_size_mm,
        element_volume_mm3=element_volume_mm3,
        max_iter=8,
    )
    diff = float(np.abs(r_elem.rho - r_mm.rho).max())
    check(
        "同じ物理半径を単位だけ変えて指定すると密度場が一致する",
        diff < 1e-9,
        f"max|rho_elem - rho_mm|={diff:.3e}",
    )
    check(
        "mm指定側の Result.rmin_mm が指定値と一致する",
        rel(r_mm.rmin_mm, rmin_elements * element_size_mm) < 1e-9,
        f"{r_mm.rmin_mm:.6g} (期待 {rmin_elements * element_size_mm:.6g})",
    )


# ---------------------------------------------------------------------------
# 6. 検出力: 1.5要素という下限が守っている性質(チェッカーボード)
# ---------------------------------------------------------------------------
def _checkerboard_roughness(rho: np.ndarray) -> float:
    """隣接要素(6近傍)の離散ラプラシアンの絶対値平均(内部要素のみ)。

    市松模様(隣接要素が交互に0/1へ張り付く)では `6*rho[c] - sum(近傍)` が要素ごとに
    最大化される。滑らかな解ではほぼ0。**しきい値の根拠は下の実測**(下限より下で約1.4、
    既定=1.5要素で約0.4 と3倍以上離れているので、中間の1.0を境目に取る)。
    """
    interior = rho[1:-1, 1:-1, 1:-1]
    lap = (
        6 * interior
        - rho[0:-2, 1:-1, 1:-1]
        - rho[2:, 1:-1, 1:-1]
        - rho[1:-1, 0:-2, 1:-1]
        - rho[1:-1, 2:, 1:-1]
        - rho[1:-1, 1:-1, 0:-2]
        - rho[1:-1, 1:-1, 2:]
    )
    return float(np.abs(lap).mean())


CHECKERBOARD_ROUGHNESS_THRESHOLD = 1.0  # 実測(下記)で below≈1.41 / at≈0.42 の中間


def check_checkerboard_detection_power() -> None:
    print("\n[6] 検出力: 1.5要素という下限が守っている性質(チェッカーボード)")
    g = bench.FILTER_RADIUS_CHECKERBOARD
    t0 = time.perf_counter()

    # **below**: 下限(1.5要素)を大きく下回る1.0要素。`_filter_kernel` はここで r=0
    # (`int(ceil(1.0))-1=0`)になり、フィルタが実質「近傍を見ない」= 未フィルタと同じになる。
    below = simp.optimize(g["nelx"], g["nely"], g["nelz"], rmin=1.0, max_iter=g["max_iter"], volfrac=g["volfrac"])
    rough_below = _checkerboard_roughness(below.rho)

    # **at**: 既定(1.5要素)。
    at = simp.optimize(g["nelx"], g["nely"], g["nelz"], rmin=simp.DEFAULT_RMIN, max_iter=g["max_iter"], volfrac=g["volfrac"])
    rough_at = _checkerboard_roughness(at.rho)

    # **above**: 2.5要素。単調性(半径を増やすほど滑らかになる)も見ておく。
    above = simp.optimize(g["nelx"], g["nely"], g["nelz"], rmin=2.5, max_iter=g["max_iter"], volfrac=g["volfrac"])
    rough_above = _checkerboard_roughness(above.rho)

    elapsed = time.perf_counter() - t0
    print(
        f"  rmin=1.0(下限より下): roughness={rough_below:.4f} / "
        f"rmin=1.5(既定): roughness={rough_at:.4f} / "
        f"rmin=2.5: roughness={rough_above:.4f} ({elapsed:.1f}秒)"
    )

    check(
        "下限より下(1.0要素)はチェッカーボードの指標がしきい値を超える",
        rough_below > CHECKERBOARD_ROUGHNESS_THRESHOLD,
        f"roughness={rough_below:.4f} > {CHECKERBOARD_ROUGHNESS_THRESHOLD}",
    )
    check(
        "既定(1.5要素)はしきい値を超えない",
        rough_at < CHECKERBOARD_ROUGHNESS_THRESHOLD,
        f"roughness={rough_at:.4f} < {CHECKERBOARD_ROUGHNESS_THRESHOLD}",
    )
    check(
        "半径を増やすほど単調に滑らかになる(below > at > above)",
        rough_below > rough_at > rough_above,
        f"{rough_below:.4f} > {rough_at:.4f} > {rough_above:.4f}",
    )

    # **1.5 という値そのものの値打ち**: mm指定でこの下限を割ると、割った先の実効半径は
    # ちょうど「チェッカーボードが出る側」に落ちる、という結びつけ。要素サイズ 4mm の格子で
    # 下限ぎりぎり(rmin_mm=6.0mm=1.5要素、既定と同じ)と、それを大きく割る配置
    # (rmin_mm=4.0mm=1.0要素)の実効半径が、上で実測した below/at と同じ要素数になっている
    # ことを確認する(=ガードが弾く領域が、実際にチェッカーボードの出る領域と一致している)。
    element_size_mm = 4.0
    element_volume_mm3 = element_size_mm**3
    ok = simp.optimize(
        g["nelx"], g["nely"], g["nelz"],
        rmin_mm=simp.DEFAULT_RMIN * element_size_mm, element_volume_mm3=element_volume_mm3,
        max_iter=1,
    )
    check(
        "ガードが通す下限(rmin_mm=1.5要素相当)の実効rminは、しきい値内側の rmin=1.5 と一致する",
        rel(ok.rmin, simp.DEFAULT_RMIN) < 1e-9,
        f"ok.rmin={ok.rmin:.6g}",
    )
    try:
        simp.optimize(
            g["nelx"], g["nely"], g["nelz"],
            rmin_mm=1.0 * element_size_mm, element_volume_mm3=element_volume_mm3,
            max_iter=1,
        )
        check("ガードが弾く配置(rmin_mm=1.0要素相当)は例外", False, "例外が上がらなかった")
    except simp.FilterRadiusError as exc:
        check(
            "ガードが弾く配置(rmin_mm=1.0要素相当)は例外",
            rel(exc.rmin_mm / exc.element_size_mm, 1.0) < 1e-9,
            f"換算後 {exc.rmin_mm / exc.element_size_mm:.4g}要素(={rough_below:.4f} のチェッカーボード実測と同じ側)",
        )


# ---------------------------------------------------------------------------
# 7. 上位2層への配線(anchor.run_pipeline / design_space.run_topopt)
# ---------------------------------------------------------------------------
def check_wiring() -> None:
    print("\n[7] 上位2層への配線")

    # --- 排他をボクセル化の前に弾く(重い解像度でも一瞬で落ちること) --------------
    t0 = time.perf_counter()
    try:
        an.run_pipeline(elements_long_axis=200, rmin=1.5, rmin_mm=3.0, max_iter=1)
        check("run_pipeline が rmin/rmin_mm の排他を弾く", False, "例外が上がらなかった")
    except ValueError as exc:
        check(
            "run_pipeline が排他をボクセル化の前に弾く",
            "排他" in str(exc) and time.perf_counter() - t0 < 1.0,
            f"{time.perf_counter() - t0:.3f}秒で {type(exc).__name__}",
        )

    # --- 実際に simp.optimize へ渡っている(戻り値の rmin/rmin_mm が手計算と一致) ----
    # **`stl_path` を必ず明示する。** 省略すると既定の共有パス(`output/bracket.stl`)へ書き、
    # `check_anchor.py` が回帰の成果物として当てにしているファイルを上書きしてしまう
    # (`20260904-1437` の契約「踏んだ罠」と同じ形)。
    out = an.run_pipeline(elements_long_axis=10, max_iter=2, stl_path=STL_TMP)
    element_size_mm = out["model"].grid.spacing
    rmin_mm = element_size_mm * 2.0
    out_mm = an.run_pipeline(
        elements_long_axis=10, max_iter=2, rmin_mm=rmin_mm, stl_path=STL_TMP,
    )
    check(
        "run_pipeline の戻り値 rmin/rmin_mm が指定と一致する",
        rel(out_mm["rmin"], 2.0) < 1e-9 and rel(out_mm["rmin_mm"], rmin_mm) < 1e-9,
        f"rmin={out_mm['rmin']:.6g} rmin_mm={out_mm['rmin_mm']:.6g} (指定 rmin_mm={rmin_mm:.6g})",
    )
    check(
        "rmin/rmin_mm を省略すると DEFAULT_RMIN(=1.5要素)のまま(既定不変)",
        out["rmin"] == simp.DEFAULT_RMIN,
        f"out['rmin']={out['rmin']}",
    )

    # --- run_pipeline が下限違反を FilterRadiusError として上げる ------------------
    try:
        an.run_pipeline(
            elements_long_axis=10, max_iter=1, rmin_mm=element_size_mm * 0.5,
        )
        check("run_pipeline が下限違反で FilterRadiusError", False, "例外が上がらなかった")
    except simp.FilterRadiusError as exc:
        check(
            "run_pipeline が下限違反で FilterRadiusError",
            exc.shortfall_mm > 0.0,
            f"shortfall_mm={exc.shortfall_mm:.6g}",
        )

    # --- design_space.run_topopt が rmin/rmin_mm を下へ渡している ------------------
    # `check_absolute_volume.py` の material_mm3 配線検査と同じ手口: `an.run_pipeline` の
    # 排他チェックはボクセル化(`space` の中身)へ触る前に効くので、空の `DesignSpace` もどきで
    # 弾けることが「下へ渡っている」ことの証拠になる。
    import types

    dummy = types.SimpleNamespace(
        soup=None,
        mask=None,
        grid=None,
        conditions=types.SimpleNamespace(keep=None),
        fixed_face=0,
        load_face=1,
    )
    try:
        ds.run_topopt(dummy, rmin=1.5, rmin_mm=3.0, stl_path=STL_TMP)
        check("run_topopt が rmin/rmin_mm を下へ渡している", False, "例外が上がらなかった")
    except ValueError as exc:
        check(
            "run_topopt が rmin/rmin_mm を下へ渡している",
            "排他" in str(exc),
            f"{type(exc).__name__}: {str(exc)[:80]}",
        )


def main() -> int:
    print("=" * 72)
    print("check_filter_radius.py : 密度フィルタ半径を mm で指定する口の受入検査")
    print("=" * 72)
    check_argument_shape()
    check_below_minimum()
    check_boundary()
    check_default_unchanged()
    check_unit_equivalence()
    check_checkerboard_detection_power()
    check_wiring()

    print("\n" + "=" * 72)
    if FAILURES:
        print(f"NG: {len(FAILURES)}件が不合格")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("OK: 全検査に合格")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
