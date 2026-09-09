"""安全率の外側ループ(`safety_loop.py`)の受入検査。

    .\\.venv\\Scripts\\python.exe topopt_checks\\check_safety_loop.py

**このファイルが合否そのものである。** どれか1つでも落ちればプロセスは非ゼロで終わる。
題材の大きさは `bench.SAFETY_LOOP`(bracket = `demo_conditions` だけ)が正 ——
**単調性そのものの実測(複数条件・10点)は `check_*.py` ではなく docs/TOPOPT.md にある**。ここは
配線が壊れていないことだけを高速に見る回帰検査で、単調性は3点だけの粗いスポット確認に留める。

検査の構成:

0. **配線の下支え** — `Conditions` に `load_n`(荷重の実際の大きさ[N])が増えたこと /
   許容応力の定数が追える(`PETG_YIELD_MPA * PRINT_DERATE == DEFAULT_ALLOWABLE_MPA`)。
1. **`keep_model` が2層(`anchor.run_pipeline` / `design_space.run_topopt`)に通っている** ——
   `0143` が名指しで残した配線。
2. **単調性のスポット確認**(3点) — 材料量を増やすと最小安全率が下がらないこと
   (フル実測は docs/TOPOPT.md)。
3. **入口が動く(端から端まで)** — 安全率を入力に、目標以上を満たす最小の材料量が返る。
4. **達成不能なら例外**(どちら側にどれだけ足りないか名指し) —
   `material_max_mm3` まで使っても届かない場合 / 探索の境界そのものが導出できない場合。
5. **下端で既に目標を満たすときは探索しない**(2回の評価で終わる)。
6. **引数の検証**(target/material_min/material_max/rel_tol/max_evaluations)。
"""

from __future__ import annotations

import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import dataclasses

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
from duocad.topopt import OUTPUT_DIR
from duocad.topopt import bench
from duocad.topopt import design_space as ds
from duocad.topopt import safety_loop as sl
from duocad.topopt import simp

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    if not ok:
        FAILURES.append(f"{name}: {detail}")


CFG = bench.SAFETY_LOOP
# **既定の導出(連続値保持)の引数だけを渡す。** 案5の `coarse_*` を混ぜると、素通しの契約
# どおり `derive_from_reference` が `TypeError` で落ちる(黙って捨てない)。
DERIVATION_KW = dict(elements_long_axis=CFG["elements_long_axis"])


def _bracket_capacity() -> tuple[ds.Conditions, float, float]:
    """bracket(demo_conditions)の塊(案1)の容量[mm^3]。探索範囲を決めるのに使う。"""
    cond = ds.demo_conditions()
    fine = ds.derive(cond, elements_long_axis=CFG["elements_long_axis"])
    capacity = fine.grid.element_volume * fine.element_count
    solid = fine.grid.element_volume * int(fine.model.keep.sum())
    return cond, capacity, solid


def check_wiring() -> None:
    print("\n[0] 配線の下支え")
    field_names = {f.name for f in dataclasses.fields(ds.Conditions)}
    check(
        "Conditions.load_n が増えている(既定1.0)",
        "load_n" in field_names,
        f"{sorted(field_names)}",
    )
    default_load_n = dataclasses.fields(ds.Conditions)[
        [f.name for f in dataclasses.fields(ds.Conditions)].index("load_n")
    ].default
    check("load_n の既定値は 1.0", default_load_n == 1.0, f"{default_load_n!r}")
    check(
        "許容応力の導出が追える(PETG降伏応力 x 低減係数)",
        sl.DEFAULT_ALLOWABLE_MPA == sl.PETG_YIELD_MPA * sl.PRINT_DERATE == 40.0,
        f"{sl.PETG_YIELD_MPA} x {sl.PRINT_DERATE} = {sl.DEFAULT_ALLOWABLE_MPA}",
    )


def check_keep_model_wiring() -> None:
    print("\n[1] keep_model が2層に通っている")
    cond, _capacity, _solid = _bracket_capacity()
    space = ds.derive(cond, elements_long_axis=CFG["elements_long_axis"])
    # **`stl_path` を明示する。** 既定(`output/design_space_case1.stl`)は
    # `check_design_space.py` [5] 自身の成果物なので、そこへ書くとこの検査を走らせるたびに
    # 他の検査の成果物を上書きしてしまう。
    scratch = OUTPUT_DIR / "check_safety_loop_keep_model.stl"

    out_false = ds.run_topopt(space, volfrac=0.3, max_iter=3, keep_model=False, stl_path=scratch)
    check(
        "design_space.run_topopt: keep_model 既定(False)は model が None のまま",
        out_false["result"].model is None,
        f"model={out_false['result'].model!r}",
    )
    out_true = ds.run_topopt(space, volfrac=0.3, max_iter=3, keep_model=True, stl_path=scratch)
    check(
        "design_space.run_topopt: keep_model=True で result.model が入る",
        out_true["result"].model is not None,
        f"model is None? {out_true['result'].model is None}",
    )
    check(
        "keep_model の有無で数値は1ビットも変わらない(rho一致)",
        (out_false["result"].rho == out_true["result"].rho).all(),
        "rho が一致",
    )

    from duocad.topopt import anchor as an

    # **`stl_path` を明示する。** 既定(`output/bracket.stl`)は `check_anchor.py` 自身の
    # 標準実行の成果物なので、そこへ書くとこの検査を走らせるたびに他の検査の成果物を
    # 上書きしてしまう(`.gitignore` の「安全率の外側ループ」節参照)。
    scratch = OUTPUT_DIR / "check_safety_loop_keep_model.stl"
    demo_out_false = an.run_pipeline(
        elements_long_axis=12, max_iter=3, keep_model=False, stl_path=scratch
    )
    demo_out_true = an.run_pipeline(
        elements_long_axis=12, max_iter=3, keep_model=True, stl_path=scratch
    )
    check(
        "anchor.run_pipeline: keep_model 既定(False)は model が None のまま",
        demo_out_false["result"].model is None,
        f"model={demo_out_false['result'].model!r}",
    )
    check(
        "anchor.run_pipeline: keep_model=True で result.model が入る",
        demo_out_true["result"].model is not None,
        f"model is None? {demo_out_true['result'].model is None}",
    )


def check_monotonic_spotcheck() -> None:
    print("\n[2] 単調性のスポット確認(3点。フルの実測はdocs/TOPOPT.md)")
    cond, capacity, _solid = _bracket_capacity()
    fracs = (0.20, 0.45, 0.70)
    safety_factors = []
    for frac in fracs:
        material = capacity * frac
        space, run, report = sl._derive_and_run(
            cond,
            material,
            allowable_mpa=sl.DEFAULT_ALLOWABLE_MPA,
            youngs_modulus_mpa=sl.DEFAULT_YOUNGS_MODULUS_MPA,
            max_iter=CFG["max_iter"],
            force=(0.0, 0.0, -1.0),
            solver=None,
            threads=None,
            rho_min=0.5,
            percentile=99.5,
            derivation_kwargs=dict(DERIVATION_KW),
        )
        safety_factors.append(report.safety_factor)
        print(f"    material={material:.0f}mm^3 ({frac:.0%}) -> safety_factor={report.safety_factor:.3f}")
    check(
        "材料量を増やすと最小安全率が下がらない(3点、bracket)",
        all(safety_factors[i] <= safety_factors[i + 1] for i in range(len(safety_factors) - 1)),
        f"{[round(v, 2) for v in safety_factors]}",
    )


def check_entry_point() -> tuple[ds.Conditions, float, float]:
    print("\n[3] 入口が端から端まで動く")
    cond, capacity, _solid = _bracket_capacity()
    material_min = capacity * 0.15
    # **2026-09-05に上端を 0.75 → 0.70 へ下げた。** `simp.DEFAULT_RMIN` が 1.5 → 2.5 に
    # なり、**塊の縁でのフィルタの目減りが増えて到達できる材料量の上限が下がった**
    # (この解像度では容量の約72%が上限。0.75 はこの解像度で
    # `VolumeTargetError` になる)。0.70 は上の [2] が同じ日に実際に通した点である。
    # **検査を弱めたのではない** —— 下の4つの assert はどれも探索範囲の広さを見ていない。
    material_max = capacity * 0.70
    # 実測(bracket, この解像度, rmin=2.5)で 20%/45%/70% が sf 2938/5714/6228。
    # 5500 は 20%-45% の内側にあり、探索は境界に張り付かない。
    target = 5500.0
    result = sl.solve_for_safety_factor(
        cond,
        target_safety_factor=target,
        material_min_mm3=material_min,
        material_max_mm3=material_max,
        max_iter=CFG["max_iter"],
        derivation_kwargs=dict(DERIVATION_KW),
    )
    check(
        "達成した最小安全率が目標以上",
        result.safety_factor >= target,
        f"{result.safety_factor:.4f} >= {target}",
    )
    check(
        "材料量は探索範囲の中",
        material_min <= result.material_mm3 <= material_max,
        f"{material_min:.0f} <= {result.material_mm3:.0f} <= {material_max:.0f}",
    )
    check("収束した(材料量の相対幅が rel_tol 以内)", result.converged, f"evaluations={result.evaluations}")
    check(
        "評価回数が上限以内",
        result.evaluations <= sl.DEFAULT_MAX_EVALUATIONS,
        f"{result.evaluations} <= {sl.DEFAULT_MAX_EVALUATIONS}",
    )
    check(
        "history に評価の軌跡が残っている",
        len(result.history) == result.evaluations and result.history[-1].outcome == "ok",
        f"len={len(result.history)} last={result.history[-1]}",
    )
    check(
        "keep_model が実際に効いている(result.report が付いてくる)",
        result.report is not None and result.run["result"].model is not None,
        "report/model 両方とも None ではない",
    )
    # 「目標以上を満たす最小」であること: 収束幅の外側(区間をもう半分削った側)まで材料を
    # 削ると目標を割ることを確かめる(=最小の側に張り付いていること)。
    lower = result.material_mm3 * (1.0 - 3.0 * sl.DEFAULT_REL_TOL)
    _space, _run, lower_report = sl._derive_and_run(
        cond,
        lower,
        allowable_mpa=sl.DEFAULT_ALLOWABLE_MPA,
        youngs_modulus_mpa=sl.DEFAULT_YOUNGS_MODULUS_MPA,
        max_iter=CFG["max_iter"],
        force=(0.0, 0.0, -1.0),
        solver=None,
        threads=None,
        rho_min=0.5,
        percentile=99.5,
        derivation_kwargs=dict(DERIVATION_KW),
    )
    check(
        "採用した材料量よりさらに削ると目標を割る(=最小の側)",
        lower_report.safety_factor < target,
        f"material={lower:.0f} -> sf={lower_report.safety_factor:.4f} < {target}",
    )
    return cond, material_min, material_max


def check_infeasible(cond: ds.Conditions, material_min: float, material_max: float) -> None:
    print("\n[4] 達成不能なら例外(どちら側にどれだけ足りないか名指し)")
    try:
        sl.solve_for_safety_factor(
            cond,
            target_safety_factor=1.0e9,
            material_min_mm3=material_min,
            material_max_mm3=material_max,
            max_iter=CFG["max_iter"],
            derivation_kwargs=dict(DERIVATION_KW),
        )
        raised = None
    except sl.SafetyTargetError as exc:
        raised = exc
    check(
        "material_max まで使っても届かない場合は SafetyTargetError",
        raised is not None and raised.side == "unreachable_within_max_material",
        f"{raised.side if raised else '(例外が出なかった)'}",
    )
    check(
        "属性 shortfall / safety_factor が入っている",
        raised is not None and raised.shortfall is not None and raised.shortfall > 0
        and raised.safety_factor is not None,
        f"shortfall={raised.shortfall if raised else None}",
    )

    # 探索の境界そのものが導出できない(material_max が塊の容量を超えて above_capacity)。
    capacity = material_max / 0.70  # check_entry_point で 70% として作った値から逆算
    over_capacity = capacity * 0.999  # 99.9%は罠(0142)なのでその手前、だが容量近傍は above_capacity を踏みやすい
    try:
        sl.solve_for_safety_factor(
            cond,
            target_safety_factor=5500.0,
            material_min_mm3=material_min,
            material_max_mm3=over_capacity,
            max_iter=CFG["max_iter"],
            derivation_kwargs=dict(DERIVATION_KW),
        )
        raised2 = None
    except sl.SafetyTargetError as exc:
        raised2 = exc
    check(
        "material_max 自体が導出できない場合は invalid_bound",
        raised2 is not None and raised2.side == "invalid_bound" and raised2.bound == "material_max_mm3",
        f"{raised2.side if raised2 else '(例外が出なかった)'} / bound={raised2.bound if raised2 else None}",
    )
    check(
        "invalid_bound は元の例外を __cause__ に連鎖している",
        raised2 is not None and isinstance(raised2.__cause__, simp.VolumeTargetError),
        f"{type(raised2.__cause__) if raised2 else None}",
    )


def check_already_met(cond: ds.Conditions, material_min: float, material_max: float) -> None:
    print("\n[5] 下端で既に目標を満たすときは探索しない")
    result = sl.solve_for_safety_factor(
        cond,
        target_safety_factor=1.0,
        material_min_mm3=material_min,
        material_max_mm3=material_max,
        max_iter=CFG["max_iter"],
        derivation_kwargs=dict(DERIVATION_KW),
    )
    check(
        "material_min_mm3 がそのまま採用される",
        result.material_mm3 == material_min,
        f"{result.material_mm3} == {material_min}",
    )
    check("評価は2回で終わる(境界2つだけ)", result.evaluations == 2, f"{result.evaluations}")
    check("収束フラグは True", result.converged, f"{result.converged}")


def check_argument_validation(cond: ds.Conditions) -> None:
    print("\n[6] 引数の検証")
    base = dict(
        conditions=cond,
        target_safety_factor=5.0,
        material_min_mm3=1000.0,
        material_max_mm3=2000.0,
    )
    for label, overrides in (
        ("target_safety_factor <= 0", {"target_safety_factor": 0.0}),
        ("target_safety_factor が非有限", {"target_safety_factor": float("nan")}),
        ("material_min_mm3 <= 0", {"material_min_mm3": 0.0}),
        ("material_max_mm3 <= material_min_mm3", {"material_max_mm3": 1000.0}),
        ("rel_tol が範囲外(0)", {"rel_tol": 0.0}),
        ("rel_tol が範囲外(1以上)", {"rel_tol": 1.0}),
        ("max_evaluations が2未満", {"max_evaluations": 1}),
    ):
        kwargs = dict(base)
        kwargs.update(overrides)
        try:
            sl.solve_for_safety_factor(**kwargs)
            raised = "(例外が出なかった)"
        except ValueError:
            raised = "ValueError"
        except Exception as exc:  # 想定外の例外種別も検出できるように拾う
            raised = type(exc).__name__
        check(f"{label} なら ValueError", raised == "ValueError", raised)


def main() -> int:
    print("=" * 72)
    print("check_safety_loop.py : 安全率の外側ループの受入検査")
    print("=" * 72)
    check_wiring()
    check_keep_model_wiring()
    check_monotonic_spotcheck()
    cond, material_min, material_max = check_entry_point()
    check_infeasible(cond, material_min, material_max)
    check_already_met(cond, material_min, material_max)
    check_argument_validation(cond)

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
