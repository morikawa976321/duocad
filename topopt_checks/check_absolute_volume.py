"""体積目標を絶対量(mm^3)で指定する口の受入検査。

    .\\.venv\\Scripts\\python.exe topopt_checks\\check_absolute_volume.py

**このファイルが合否そのものである。** タスク `20260904-0142` の完了条件を assert する。
どれか1つでも落ちればプロセスは非ゼロで終わる。

検査の構成:

1. **分率と絶対量が一致する** — 同じ問題を `volfrac=0.3` と、それに相当する mm^3 で解いて、
   最終的な材料体積が一致する(相対 1e-9)。
2. **塊の大きさに依存しない(本丸)** — 同じ物理問題を、周囲の空きだけ変えた2つの塊で解く。
   **絶対量なら最終材料体積が一致し、分率では一致しない**ことを両方見る
   (片方だけでは、この検査が何かを守っている証拠にならない)。
   2a は `simp.optimize` を直に叩く合成問題(拘束・荷重・保持領域は同じ物理位置に置いたまま、
   余白の層数だけを変える)、
   2b は `design_space.derive` → `run_topopt` の実経路(余白比率だけが違う)。
3. **達成不能な目標は例外** — 総体積を超える指定と `solid_mask` を下回る指定の両方で、
   不足量つきの `simp.VolumeTargetError` が上がる。**ガードが無いと何が起きるかも測る**
   —— 実測では黙って端に張り付くのではなく、`ZeroDivisionError` や「K U = F が解けて
   いない」という**体積を名指ししない例外**になる(だからガードの値打ちは「落ちること」
   ではなく「どこがどれだけ足りないかを名指しすること」にある)。
   引数の形の誤り(排他違反・要素体積の欠落)と、上位2層への配線もここで見る。
4. **既定の挙動が1ビットも変わっていない** — 変更前の `simp.py` で採取した基準値
   (16進float と ρ 場の SHA-256)と厳密一致する。プリセットとパッシブ要素経路の2本。

**基準値の採り方(4)**: 2026-09-04、この変更を入れる前の `simp.py` で2回走らせて
1ビット違わないことを確認してから写した(下の `BASELINES`)。丸めた10進ではなく16進float
なので、`float()` の往復で桁が落ちない。
"""

from __future__ import annotations

import hashlib
import sys
import time
import types

import numpy as np

# **Windowsの既定コンソール(cp932)で落ちないようにする。** 記号が1つ混ざるだけで
# `UnicodeEncodeError` になり、検査が1件も走らないまま異常終了する(check_simp.py と同じ)。
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
STL_TMP = OUTPUT_DIR / "absolute_volume_tmp.stl"


def check(name: str, ok: bool, detail: str) -> None:
    """1件の検査結果を出力する。**落ちても即死せず全件を出してから終わる。**"""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def rel(a: float, b: float) -> float:
    """相対差(基準は b)。"""
    return abs(a - b) / abs(b)


# ---------------------------------------------------------------------------
# 合成問題: 基準の設計領域のまわりに空きの層を足した片持ち梁
# ---------------------------------------------------------------------------
BASE = bench.ABSOLUTE_VOLUME_BASE  # 物理として解く部分の要素数(pad によらず同じ)
SPACING = 2.0  # mm
ELEMENT_VOLUME = SPACING**3  # 8.0 mm^3


def padded_case(pad: int) -> dict:
    """基準領域の周り(+x, ±y, ±z)に `pad` 層の**動かせる**空要素を足した問題。

    余白は `void_mask` にしない —— 空要素にすると動かせる要素の数が変わらず、
    「塊が大きいと分率指定では材料が増える」という当のズレが再現しない。
    塊(＝動かせる領域)が大きくなることこそがこの検査の対象である。

    拘束(x=0 面の基準領域ぶんの節点)と荷重(基準領域の先端下の1節点)は
    **どちらの pad でも同じ物理位置**に置く。
    """
    bx, by, bz = BASE
    nelx, nely, nelz = bx + pad, by + 2 * pad, bz + 2 * pad
    nny, nnz = nely + 1, nelz + 1

    def node(i: int, j: int, k: int) -> int:
        return (i * nny + j) * nnz + k

    fixed = []
    for j in range(pad, pad + by + 1):
        for k in range(pad, pad + bz + 1):
            n = node(0, j, k)
            fixed.extend([3 * n, 3 * n + 1, 3 * n + 2])
    load = np.zeros(3 * (nelx + 1) * nny * nnz)
    load[3 * node(bx, pad + by // 2, pad) + 2] = -1.0

    # 保持領域(ρ=1)。絶対量の下限がゼロでない配置にして、solid を含む定義を効かせる。
    solid = np.zeros((nelx, nely, nelz), dtype=bool)
    solid[bx - 1, pad + by // 2 - 1 : pad + by // 2 + 1, pad] = True
    return {
        "shape": (nelx, nely, nelz),
        "fixed_dofs": np.array(sorted(set(fixed)), dtype=int),
        "load": load,
        "solid_mask": solid,
        "n_solid": int(solid.sum()),
        "n_movable": nelx * nely * nelz - int(solid.sum()),
    }


def solve(case: dict, **kwargs) -> simp.Result:
    """この検査の合成題材を1回解く。

    **`rmin` は既定で 1.5 を明示する**(2026-09-05)。この検査のしきい値 —— とくに
    「容量の98%は落ちるが95%は通る」というガードの位置の校正 —— は rmin=1.5 の
    目減り量で決めてある。`simp.DEFAULT_RMIN` に追随させると、既定が 2.5 になった
    時点で到達できる上限が 93.8% まで下がり、**校正した 95% の側も落ちる**。
    ここで見ているのは体積目標の口とガードの位置であってフィルタ半径ではない。
    """
    nelx, nely, nelz = case["shape"]
    kwargs.setdefault("max_iter", 12)
    kwargs.setdefault("rmin", 1.5)
    return simp.optimize(
        nelx,
        nely,
        nelz,
        change_tol=0.0,  # 反復数を固定する(収束の速さの違いを混ぜない)
        fixed_dofs=case["fixed_dofs"],
        load=case["load"],
        solid_mask=case["solid_mask"],
        element_volume_mm3=ELEMENT_VOLUME,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. 分率と絶対量が一致する
# ---------------------------------------------------------------------------
def check_equivalence() -> None:
    print("\n[1] 同じ問題を分率と絶対量で解くと一致する")
    case = padded_case(1)
    by_fraction = solve(case, volfrac=0.3)
    equivalent = (0.3 * case["n_movable"] + case["n_solid"]) * ELEMENT_VOLUME
    by_absolute = solve(case, material_mm3=equivalent)

    check(
        "目標体積(要素数)が一致",
        rel(by_absolute.target_volume, by_fraction.target_volume) < 1e-12,
        f"{by_absolute.target_volume!r} vs {by_fraction.target_volume!r}",
    )
    check(
        "実効volfracが一致",
        rel(by_absolute.volfrac, 0.3) < 1e-12,
        f"絶対量から逆算した分率={by_absolute.volfrac!r}",
    )
    a = float(by_absolute.rho.sum()) * ELEMENT_VOLUME
    f = float(by_fraction.rho.sum()) * ELEMENT_VOLUME
    check(
        "最終材料体積が一致(相対1e-9)",
        rel(a, f) < 1e-9,
        f"{a:.9f} vs {f:.9f} mm^3 (相対 {rel(a, f):.3e})",
    )
    check(
        "指定した絶対量そのものと一致(相対1e-9)",
        rel(a, equivalent) < 1e-9,
        f"指定 {equivalent:.9f} mm^3 / 出た値 {a:.9f} mm^3 (相対 {rel(a, equivalent):.3e})",
    )
    check(
        "mm^3の口(Result)が同じ値を返す",
        by_absolute.achieved_material_mm3 == a
        and abs(by_absolute.target_material_mm3 - equivalent) < 1e-9,
        f"achieved={by_absolute.achieved_material_mm3:.6f} "
        f"target={by_absolute.target_material_mm3:.6f}",
    )
    check(
        "ρ場そのものが一致(密度の最大差)",
        float(np.abs(by_absolute.rho - by_fraction.rho).max()) < 1e-9,
        f"max|Δρ|={float(np.abs(by_absolute.rho - by_fraction.rho).max()):.3e}",
    )
    check(
        "要素体積を渡さなければ mm^3 は None",
        simp.optimize(4, 4, 4, volfrac=0.3, max_iter=1).achieved_material_mm3 is None,
        "element_volume_mm3=None のとき achieved_material_mm3 is None",
    )


# ---------------------------------------------------------------------------
# 2. 塊の大きさに依存しない(本丸)
# ---------------------------------------------------------------------------
def check_padding_invariance() -> None:
    print("\n[2a] 余白の層数を変える(拘束・荷重は同じ物理位置・simp.optimize を直に叩く)")
    thin, thick = padded_case(1), padded_case(4)
    check(
        "塊(動かせる要素)の大きさが実際に違う",
        thick["n_movable"] > 3 * thin["n_movable"],
        f"pad=1: {thin['n_movable']} 要素 / pad=4: {thick['n_movable']} 要素 "
        f"({thick['n_movable'] / thin['n_movable']:.2f}倍)",
    )

    # 基準領域を 30% 埋める量。どちらの塊でも達成可能な範囲にある。
    material = (0.3 * (BASE[0] * BASE[1] * BASE[2] - thin["n_solid"]) + thin["n_solid"]) * ELEMENT_VOLUME
    abs_thin = float(solve(thin, material_mm3=material).rho.sum()) * ELEMENT_VOLUME
    abs_thick = float(solve(thick, material_mm3=material).rho.sum()) * ELEMENT_VOLUME
    check(
        "絶対量: 材料の総体積が一致(相対1e-6)",
        rel(abs_thick, abs_thin) < 1e-6,
        f"pad=1 {abs_thin:.6f} / pad=4 {abs_thick:.6f} mm^3 "
        f"(相対 {rel(abs_thick, abs_thin):.3e})",
    )
    check(
        "絶対量: どちらも指定どおり(相対1e-6)",
        rel(abs_thin, material) < 1e-6 and rel(abs_thick, material) < 1e-6,
        f"指定 {material:.6f} mm^3 / 相対誤差 {rel(abs_thin, material):.3e}, "
        f"{rel(abs_thick, material):.3e}",
    )

    vf_thin = float(solve(thin, volfrac=0.3).rho.sum()) * ELEMENT_VOLUME
    vf_thick = float(solve(thick, volfrac=0.3).rho.sum()) * ELEMENT_VOLUME
    check(
        "分率: 材料の総体積が一致しない(検出力の確認)",
        rel(vf_thick, vf_thin) > 0.5,
        f"pad=1 {vf_thin:.3f} / pad=4 {vf_thick:.3f} mm^3 "
        f"({vf_thick / vf_thin:.2f}倍 / 相対 {rel(vf_thick, vf_thin):.3f})",
    )

    print("\n[2b] 余白比率を変える(design_space.derive → run_topopt の実経路)")
    conditions = ds.demo_conditions()
    spaces = {}
    for ratio in (0.10, 0.40):
        t0 = time.perf_counter()
        spaces[ratio] = ds.derive(
            conditions, margin_ratio=ratio, spacing=bench.ABSOLUTE_VOLUME["spacing"]
        )
        print(
            f"    余白比率 {ratio}: 格子 {spaces[ratio].grid.shape} / 塊 "
            f"{spaces[ratio].element_count} 要素 / 要素体積 "
            f"{spaces[ratio].grid.element_volume:.6f} mm^3 "
            f"({time.perf_counter() - t0:.1f}秒)",
            flush=True,
        )
    small, large = spaces[0.10], spaces[0.40]
    check(
        "塊の大きさが実際に違う",
        large.element_count > 3 * small.element_count,
        f"{small.element_count} → {large.element_count} 要素 "
        f"({large.element_count / small.element_count:.2f}倍)",
    )
    # **2026-09-05に 100,000 → 70,000 mm^3 へ下げた。** `simp.DEFAULT_RMIN` が 1.5 → 2.5
    # になり、**塊の縁でのフィルタの目減りが増えて到達できる材料量の上限が下がった**
    # (この題材の小さい側は塊 295要素 = 127,464 mm^3 で、上限は約74%。100,000 は 78.5%
    # なので `VolumeTargetError` になる)。**ここは `ds.run_topopt` の実経路なので
    # `rmin` を明示せず既定のまま通す** —— 上の `solve()` と違い、この節が見たいのは
    # 「既定の設定で、塊の大きさに関わらず指定どおりの材料が出るか」だからである。
    # 容量の端の挙動は [3](OC の退化ガード)が別に見ている。
    target = 70000.0  # mm^3
    got = {}
    for name, space in (("small", small), ("large", large)):
        out = ds.run_topopt(
            space,
            material_mm3=target,
            max_iter=bench.ABSOLUTE_VOLUME["max_iter"],
            stl_path=STL_TMP,
        )
        got[name] = out["achieved_material_mm3"]
        check(
            f"絶対量({name}): 指定どおり(相対1e-6)",
            rel(got[name], target) < 1e-6,
            f"指定 {target:.3f} / 出た値 {got[name]:.6f} mm^3 "
            f"(要素体積 {out['element_volume_mm3']:.6f} / "
            f"実効volfrac {out['target_volfrac']:.4f})",
        )
    check(
        "絶対量: 余白比率が違っても材料の総体積が一致(相対1e-6)",
        rel(got["large"], got["small"]) < 1e-6,
        f"{got['small']:.6f} vs {got['large']:.6f} mm^3 "
        f"(相対 {rel(got['large'], got['small']):.3e})",
    )
    vf = {}
    for name, space in (("small", small), ("large", large)):
        out = ds.run_topopt(
            space,
            volfrac=0.3,
            max_iter=bench.ABSOLUTE_VOLUME["max_iter"],
            stl_path=STL_TMP,
        )
        vf[name] = out["achieved_material_mm3"]
    check(
        "分率: 余白比率を変えると材料の総体積が変わる(検出力の確認)",
        rel(vf["large"], vf["small"]) > 0.5,
        f"{vf['small']:.3f} vs {vf['large']:.3f} mm^3 "
        f"({vf['large'] / vf['small']:.2f}倍)",
    )


# ---------------------------------------------------------------------------
# 3. 達成不能な目標は例外
# ---------------------------------------------------------------------------
def check_unreachable() -> None:
    print("\n[3] 達成不能な目標・引数の誤り")
    case = padded_case(1)
    capacity = (case["n_movable"] + case["n_solid"]) * ELEMENT_VOLUME
    floor = case["n_solid"] * ELEMENT_VOLUME

    over = capacity * 1.5
    try:
        solve(case, material_mm3=over)
        check("総体積を超える指定で例外", False, "例外が上がらなかった")
    except simp.VolumeTargetError as exc:
        text = str(exc)
        expected = over - capacity
        check(
            "総体積を超える指定で例外",
            exc.side == "above_capacity" and rel(exc.shortfall_mm3, expected) < 1e-12,
            f"side={exc.side} 不足量={exc.shortfall_mm3:.3f} mm^3 (期待 {expected:.3f})",
        )
        check(
            "メッセージに不足量が入っている",
            f"{expected:.6g}" in text and f"{over:.6g}" in text,
            text[:120],
        )
    except Exception as exc:  # noqa: BLE001 - 別の例外で落ちるのも不合格
        check("総体積を超える指定で例外", False, f"別の例外: {type(exc).__name__}: {exc}")

    under = floor * 0.25
    try:
        solve(case, material_mm3=under)
        check("solid_mask を下回る指定で例外", False, "例外が上がらなかった")
    except simp.VolumeTargetError as exc:
        text = str(exc)
        expected = floor - under
        check(
            "solid_mask を下回る指定で例外",
            exc.side == "below_minimum" and rel(exc.shortfall_mm3, expected) < 1e-12,
            f"side={exc.side} 不足量={exc.shortfall_mm3:.3f} mm^3 (期待 {expected:.3f})",
        )
        check(
            "メッセージに不足量が入っている",
            f"{expected:.6g}" in text and f"{under:.6g}" in text,
            text[:120],
        )
    except Exception as exc:  # noqa: BLE001 - 別の例外で落ちるのも不合格
        check("solid_mask を下回る指定で例外", False, f"別の例外: {type(exc).__name__}: {exc}")

    # **ガードを外したら何が起きるかを実測する(検出力)。** 絶対量は分率へ畳んでから
    # OC に入るので、達成不能な指定に相当する分率をそのまま渡せば「ガードが無い世界」に
    # なる。**ガードの値打ちは「落ちるようにすること」ではなく「どこがどれだけ足りないかを
    # 名指しすること」である。**
    # **2026-09-04に取り直した**: OC の二分探索の退化そのものが体積の言葉で落ちるように
    # なった(`20260904-1400` の契約③の宿題)ので、「ガードが無いと `ZeroDivisionError`」は
    # もう成り立たない。**代わりに、上位ガードが無いと ①利用者が渡した mm^3 も不足量も
    # 文面に出ない ②1反復回してからでないと分からない** を見る。
    def outcome_without_guard(material: float) -> tuple[str, str]:
        fraction = (material / ELEMENT_VOLUME - case["n_solid"]) / case["n_movable"]
        try:
            solve(case, volfrac=fraction, max_iter=3)
            return "落ちない", ""
        except Exception as exc:  # noqa: BLE001 - 何で落ちるかを見るのが目的
            return type(exc).__name__, str(exc)

    over_kind, over_text = outcome_without_guard(over)
    under_kind, under_text = outcome_without_guard(under)
    check(
        "検出力: ガードが無いと総体積超過は mm^3 の言葉で落ちない(要素数の話になる)",
        over_kind != "落ちない"
        and f"{over:.6g}" not in over_text
        and f"{over - capacity:.6g}" not in over_text,
        f"分率 {(over / ELEMENT_VOLUME - case['n_solid']) / case['n_movable']:.3f} 相当 → "
        f"{over_kind}: {over_text[:60]}",
    )
    check(
        "検出力: ガードが無いと下限割れは体積と無関係な例外になる",
        "体積" not in under_text and under_kind != "落ちない",
        f"分率 {(under / ELEMENT_VOLUME - case['n_solid']) / case['n_movable']:.3f} 相当 → "
        f"{under_kind}: {under_text[:60]}",
    )

    nearly_full = capacity * 0.999
    stuck = float(solve(case, material_mm3=nearly_full, max_iter=1).rho.sum()) * ELEMENT_VOLUME
    check(
        "容量の直下(99.9%)は例外にならない",
        rel(stuck, nearly_full) < 1e-6,
        f"指定 {nearly_full:.3f} / 出た値 {stuck:.3f} mm^3",
    )

    # **上端ちょうど(容量と等しい指定)は体積の口では弾かない** —— 数学的には達成可能な
    # 指定だから。ただし OC の二分探索は「動かせる要素が全部 ρ=1」で退化して落ちる。
    # **2026-09-04に取り直した**: 退化そのものが `VolumeTargetError(above_capacity)` に
    # なった(それまでは `ZeroDivisionError`)。この検査は**どちらの落ち方をするか**を
    # 固定しておくためにあるので、名前が付いた側へ書き換える。
    try:
        solve(case, material_mm3=capacity, max_iter=1)
        outcome = "落ちない"
    except simp.VolumeTargetError as exc:
        outcome = f"VolumeTargetError({exc.side})"
    except ZeroDivisionError:
        outcome = "ZeroDivisionError"
    check(
        "上端ちょうどは体積の口では弾かれない(落ちるなら OC の退化として)",
        outcome in ("落ちない", "VolumeTargetError(above_capacity)"),
        f"容量 {capacity:.3f} mm^3 ちょうどの指定 → {outcome}",
    )

    # --- 区間の内側でも届かないことがある(2026-09-04に足したガード) -----------
    # **要素数で入る量でも、密度フィルタが塊の縁で目減りさせるぶん届かない。**
    # `20260904-1400` が案5の膨張0で踏んだのがこれで(目標が容量の 92.8% で
    # `ZeroDivisionError`)、**案5を既定にするなら名指しで落とす口が要る**というのが
    # その契約③の宿題だった。ここはその口の受入検査。
    slab = np.zeros(case["shape"], dtype=bool)
    slab[:, :, case["shape"][2] - 1 :] = True  # 塊の縁を増やす(フィルタの目減りを効かせる)
    slab_movable = int(np.count_nonzero(~slab & ~case["solid_mask"]))
    slab_capacity = (slab_movable + case["n_solid"]) * ELEMENT_VOLUME
    inside = slab_capacity * 0.98  # **区間の内側**(上端ではない)
    try:
        solve(case, material_mm3=inside, max_iter=1, void_mask=slab)
        inside_outcome, inside_text = "落ちない", ""
    except simp.VolumeTargetError as exc:
        inside_outcome, inside_text = f"VolumeTargetError({exc.side})", str(exc)
    except Exception as exc:  # noqa: BLE001 - 別の例外で落ちるのも不合格
        inside_outcome, inside_text = type(exc).__name__, str(exc)
    check(
        "区間の内側(容量の98%)でも届かない配置が体積の言葉で落ちる",
        inside_outcome == "VolumeTargetError(above_capacity)",
        f"容量 {slab_capacity:.1f} mm^3 の 98% を要求 → {inside_outcome}",
    )
    check(
        "その文面に目標・到達できた最大・容量・割合が入っている",
        all(w in inside_text for w in ("体積目標", "到達できる最大", "塊の容量", "%"))
        and "dilate_mm" in inside_text,
        inside_text[:150],
    )
    # **落ちるだけでは足りない**(緩い側に倒れていないこと)。同じ配置の 95% は通る。
    ok_sum = float(
        solve(case, material_mm3=slab_capacity * 0.95, max_iter=1, void_mask=slab).rho.sum()
    )
    check(
        "同じ配置で容量の95%は通る(ガードが手前で落ちていない)",
        rel(ok_sum * ELEMENT_VOLUME, slab_capacity * 0.95) < 1e-6,
        f"指定 {slab_capacity * 0.95:.3f} / 出た値 {ok_sum * ELEMENT_VOLUME:.3f} mm^3",
    )

    for name, kwargs, kind in (
        ("volfrac と material_mm3 の両方", dict(volfrac=0.3, material_mm3=100.0), ValueError),
        ("material_mm3 が非正", dict(material_mm3=0.0), ValueError),
    ):
        try:
            solve(case, **kwargs)
            check(f"{name} で例外", False, "例外が上がらなかった")
        except kind as exc:  # type: ignore[misc]
            check(f"{name} で例外", type(exc) is ValueError, f"{type(exc).__name__}: {exc}")

    try:
        simp.optimize(4, 4, 4, material_mm3=100.0, max_iter=1)
        check("要素体積なしの絶対量指定で例外", False, "例外が上がらなかった")
    except ValueError as exc:
        check(
            "要素体積なしの絶対量指定で例外",
            "element_volume_mm3" in str(exc),
            f"{exc}",
        )

    # 上位2層が排他を**格子を作る前に**弾くこと(重い計算を始めてから落ちない)。
    t0 = time.perf_counter()
    try:
        an.run_pipeline(volfrac=0.3, material_mm3=100.0)
        check("run_pipeline が排他を弾く", False, "例外が上がらなかった")
    except ValueError as exc:
        check(
            "run_pipeline が排他をボクセル化の前に弾く",
            "排他" in str(exc) and time.perf_counter() - t0 < 1.0,
            f"{time.perf_counter() - t0:.3f}秒で {type(exc).__name__}",
        )
    # `run_topopt` が material_mm3 を落としていれば、排他違反にならず例外が出ない。
    dummy = types.SimpleNamespace(
        soup=None,
        mask=None,
        grid=None,
        conditions=types.SimpleNamespace(keep=None),
        fixed_faces=0,
        load_faces=1,
    )
    try:
        ds.run_topopt(dummy, volfrac=0.3, material_mm3=100.0, stl_path=STL_TMP)
        check("run_topopt が material_mm3 を下へ渡している", False, "例外が上がらなかった")
    except ValueError as exc:
        check(
            "run_topopt が material_mm3 を下へ渡している",
            "排他" in str(exc),
            f"{type(exc).__name__}: {str(exc)[:80]}",
        )


# ---------------------------------------------------------------------------
# 4. 既定の挙動が1ビットも変わっていない
# ---------------------------------------------------------------------------
# **題材を変えたら取り直すのが正しい。** この検査の意味は「この変更の前後で1ビットも
# 変わらない」ことであって、特定の数値を守ることではない。下のプリセットの値は
# 2026-09-04 に格子を 40x16x16 → 20x8x8 へ縮めたときに取り直した(2回走らせて一致を確認)。
BASELINES = {
    "preset": {
        "n_iter": 60,
        "stopped_by": "max_iter",
        "c_first": "0x1.b857e1fe15ed2p+8",
        "c_last": "0x1.1258af5bb9daap+5",
        "vol_sum": "0x1.7fffffffc728cp+8",
        "rho_sha": "8d037d7e9074d5d956db86337b628742f44a26b1c78796091fc65e1c41e27cdd",
    },
    # **パッシブ要素経路の値は `void_mask` を渡す経路の指紋である。** この経路だけが
    # `Model(active_mask=...)` を通る(空要素を組み立てから外す)ので、**プリセット側が
    # 1ビットも動いていないこと**が「`void_mask` を渡さない呼び出しは組み立てが変わって
    # いない」の証拠になっている。3回走らせて一致を確認した(2026-09-09)。
    "passive": {
        "n_iter": 12,
        "stopped_by": "max_iter",
        "c_first": "0x1.b57e2ed4b77c0p+7",
        "c_last": "0x1.b39bd89537c4ap+4",
        "vol_sum": "0x1.173333330dd85p+8",
        "rho_sha": "5c7b4ff02b39455b03c0f66d991ee1a5fb4540405e031c2096af7c5c230d6d0a",
    },
}


def fingerprint(res: simp.Result) -> dict:
    return {
        "n_iter": len(res.history),
        "stopped_by": res.stopped_by,
        "c_first": float(res.history[0].compliance).hex(),
        "c_last": float(res.history[-1].compliance).hex(),
        "vol_sum": float(res.rho.sum()).hex(),
        "rho_sha": hashlib.sha256(
            np.ascontiguousarray(res.rho, dtype=np.float64).tobytes()
        ).hexdigest(),
    }


def check_unchanged_defaults() -> None:
    print("\n[4] 既定(volfrac だけ渡す)の挙動が変更前と1ビット違わない")
    t0 = time.perf_counter()
    preset = fingerprint(simp.run_preset())
    print(f"    プリセット {time.perf_counter() - t0:.1f}秒", flush=True)

    nelx, nely, nelz = 16, 8, 8
    void = np.zeros((nelx, nely, nelz), dtype=bool)
    solid = np.zeros((nelx, nely, nelz), dtype=bool)
    void[:, :, -2:] = True
    solid[0, :, :2] = True
    t0 = time.perf_counter()
    passive = fingerprint(
        simp.optimize(
            nelx,
            nely,
            nelz,
            volfrac=0.35,
            # **`rmin` を明示する。** 省くと `simp.DEFAULT_RMIN` に追随してしまい、
            # 既定値を動かした瞬間にこの指紋(SHA-256)が別物になる。基準値は 1.5 で
            # 採ってあるので、その 1.5 をここで固定する(値は採取時と同じ。2026-09-05に
            # 既定が 1.5 から動いたので明示に切り替えた。プリセット側は `simp.PRESET`
            # が元々明示しているので同じ問題が無い)。
            rmin=1.5,
            max_iter=12,
            change_tol=0.0,
            void_mask=void,
            solid_mask=solid,
        )
    )
    print(f"    パッシブ要素経路 {time.perf_counter() - t0:.1f}秒", flush=True)

    for name, got in (("プリセット", preset), ("パッシブ要素経路", passive)):
        want = BASELINES["preset" if name == "プリセット" else "passive"]
        for key in want:
            check(
                f"{name} {key}",
                got[key] == want[key],
                f"{got[key]} (基準 {want[key]})",
            )
    # 読み手のために10進でも出す(docs/TOPOPT.md の 292.2307 → 15.3817 と突き合わせるため)。
    print(
        f"    プリセット C: {float.fromhex(preset['c_first']):.4f} → "
        f"{float.fromhex(preset['c_last']):.4f}",
        flush=True,
    )


def main() -> int:
    started = time.perf_counter()
    print("=" * 72)
    print("体積目標を絶対量(mm^3)で指定する口の受入検査")
    print("=" * 72)
    check_equivalence()
    check_padding_invariance()
    check_unreachable()
    check_unchanged_defaults()

    print("\n" + "=" * 72)
    if FAILURES:
        print(f"FAIL: {len(FAILURES)} 件")
        for line in FAILURES:
            print(f"  - {line}")
        return 1
    print(f"PASS (全件緑 / {time.perf_counter() - started:.1f}秒)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
