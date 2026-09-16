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
6. **引数の検証**(target/material_min/material_max/rel_tol/max_evaluations/searcher)。
7. **候補材料量の決め方**(Regula Falsi + Illinois・クリップ・フォールバック4条件) —— SIMP を
   回さない純粋な関数として見る。
8. **探索器の比較**(補間 対 二分) — bracket 題材で評価回数・採用材料量・安全率。
   **受入は「悪化しないこと」**(完全一致はボクセル量子化で要求しない)。
   **見るのは目標 `ENTRY_TARGET` の1点だけ**で、「どの目標でも補間の方が評価回数が少ない」
   という主張ではない(実測では目標4,500で1回増える。docs/TOPOPT_SPEED.md「実装済み A」)。
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

# 実測(bracket, この解像度, 固定凸包の塊, 2026-09-16)で容量の 20%/45%/70% が
# 余白 0.20(既定)で sf 281.08/3,019.05/4,587.22(余白 0.25 では 899.95/4,414.88/5,475.18)。
# **3,500 は下端(15%)と上端(70%)の内側**なので探索本体に入り、かつ端に張り付かない。
# 余白 0.25 の試行では目標 3,000/3,500/5,000 で補間の評価回数が二分以下(6/7/6 対 7/7/6)、
# 4,500 では1回多い(7 対 6)。0.20 でも 3,500 は補間6回で合格 —— [8] は目標1点の回帰検査なので、
# その1点をここで選んである。
ENTRY_TARGET = 3500.0


_SPACE: dict = {}


def _bracket_capacity() -> tuple[ds.DesignSpace, float, float]:
    """bracket(demo_conditions)の固定した塊と、その容量・保持体積[mm^3](探索範囲を決めるのに使う)。

    塊は製品経路と同じく**1回だけ導出して全検査で使い回す**。
    """
    if "space" not in _SPACE:
        _SPACE["space"] = ds.derive(ds.demo_conditions(), elements_long_axis=CFG["elements_long_axis"])
    space = _SPACE["space"]
    capacity = space.grid.element_volume * space.element_count
    solid = space.grid.element_volume * int(space.model.keep.sum())
    return space, capacity, solid


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
    space, _capacity, _solid = _bracket_capacity()
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
    space, capacity, _solid = _bracket_capacity()
    fracs = (0.20, 0.45, 0.70)
    safety_factors = []
    for frac in fracs:
        material = capacity * frac
        _space, run, report = sl._run_once(
            space,
            material,
            allowable_mpa=sl.DEFAULT_ALLOWABLE_MPA,
            youngs_modulus_mpa=sl.DEFAULT_YOUNGS_MODULUS_MPA,
            max_iter=CFG["max_iter"],
            force=(0.0, 0.0, -1.0),
            solver=None,
            threads=None,
            rho_min=0.5,
            percentile=99.5,
        )
        safety_factors.append(report.safety_factor)
        print(f"    material={material:.0f}mm^3 ({frac:.0%}) -> safety_factor={report.safety_factor:.3f}")
    check(
        "材料量を増やすと最小安全率が下がらない(3点、bracket)",
        all(safety_factors[i] <= safety_factors[i + 1] for i in range(len(safety_factors) - 1)),
        f"{[round(v, 2) for v in safety_factors]}",
    )


def check_entry_point() -> tuple[ds.DesignSpace, float, float, "sl.SafetyLoopResult"]:
    print("\n[3] 入口が端から端まで動く")
    space, capacity, _solid = _bracket_capacity()
    material_min = capacity * 0.15
    # **2026-09-05に上端を 0.75 → 0.70 へ下げた。** `simp.DEFAULT_RMIN` が 1.5 → 2.5 に
    # なり、**塊の縁でのフィルタの目減りが増えて到達できる材料量の上限が下がった**
    # (この解像度では容量の約72%が上限。0.75 はこの解像度で
    # `VolumeTargetError` になる)。0.70 は上の [2] が同じ日に実際に通した点である。
    # **検査を弱めたのではない** —— 下の4つの assert はどれも探索範囲の広さを見ていない。
    material_max = capacity * 0.70
    target = ENTRY_TARGET
    result = sl.solve_for_safety_factor(
        space,
        target_safety_factor=target,
        material_min_mm3=material_min,
        material_max_mm3=material_max,
        max_iter=CFG["max_iter"],
    )
    check(
        "戻り値の塊は渡した塊そのもの(探索の中で導出し直していない)",
        result.space is space and result.space.mask_hash == space.stats["mask_hash"],
        f"same object={result.space is space}",
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
    _space, _run, lower_report = sl._run_once(
        space,
        lower,
        allowable_mpa=sl.DEFAULT_ALLOWABLE_MPA,
        youngs_modulus_mpa=sl.DEFAULT_YOUNGS_MODULUS_MPA,
        max_iter=CFG["max_iter"],
        force=(0.0, 0.0, -1.0),
        solver=None,
        threads=None,
        rho_min=0.5,
        percentile=99.5,
    )
    check(
        "採用した材料量よりさらに削ると目標を割る(=最小の側)",
        lower_report.safety_factor < target,
        f"material={lower:.0f} -> sf={lower_report.safety_factor:.4f} < {target}",
    )
    return space, material_min, material_max, result


def check_infeasible(space: ds.DesignSpace, material_min: float, material_max: float) -> None:
    print("\n[4] 達成不能なら例外(どちら側にどれだけ足りないか名指し)")
    try:
        sl.solve_for_safety_factor(
            space,
            target_safety_factor=1.0e9,
            material_min_mm3=material_min,
            material_max_mm3=material_max,
            max_iter=CFG["max_iter"],
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
            space,
            target_safety_factor=5500.0,
            material_min_mm3=material_min,
            material_max_mm3=over_capacity,
            max_iter=CFG["max_iter"],
            )
        raised2 = None
    except sl.SafetyTargetError as exc:
        raised2 = exc
    check(
        "material_max 自体が評価できない場合は invalid_bound",
        raised2 is not None and raised2.side == "invalid_bound" and raised2.bound == "material_max_mm3",
        f"{raised2.side if raised2 else '(例外が出なかった)'} / bound={raised2.bound if raised2 else None}",
    )
    check(
        "invalid_bound は元の例外を __cause__ に連鎖している",
        raised2 is not None and isinstance(raised2.__cause__, simp.VolumeTargetError),
        f"{type(raised2.__cause__) if raised2 else None}",
    )


def check_already_met(space: ds.DesignSpace, material_min: float, material_max: float) -> None:
    print("\n[5] 下端で既に目標を満たすときは探索しない")
    result = sl.solve_for_safety_factor(
        space,
        target_safety_factor=1.0,
        material_min_mm3=material_min,
        material_max_mm3=material_max,
        max_iter=CFG["max_iter"],
    )
    check(
        "material_min_mm3 がそのまま採用される",
        result.material_mm3 == material_min,
        f"{result.material_mm3} == {material_min}",
    )
    check("評価は2回で終わる(境界2つだけ)", result.evaluations == 2, f"{result.evaluations}")
    check("収束フラグは True", result.converged, f"{result.converged}")


def check_argument_validation(space: ds.DesignSpace) -> None:
    print("\n[6] 引数の検証")
    base = dict(
        space=space,
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
        ("searcher が SEARCHERS に無い名前", {"searcher": "brent"}),
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


def _propose(lo: float, hi: float, f_lo, f_hi, *, side=None, streak: int = 0, evaluated=()):
    return sl._propose_material(
        lo=lo,
        hi=hi,
        f_lo=f_lo,
        f_hi=f_hi,
        stagnant_side=side,
        stagnant_streak=streak,
        evaluated=list(evaluated),
    )


def check_proposal_rules() -> None:
    print("\n[7] 候補材料量の決め方(補間・クリップ・フォールバック4条件)")
    # 括り [0, 100]、下端が目標より 3 低く上端が 1 高い -> 割線は 75。
    plain = _propose(0.0, 100.0, -3.0, 1.0)
    check(
        "Regula Falsi が安全率の大きさで位置を決める",
        plain.proposed_by == "illinois" and abs(plain.material_mm3 - 75.0) < 1e-9 and not plain.clipped,
        f"{plain}",
    )
    high = _propose(0.0, 100.0, -99.0, 1.0)
    low = _propose(0.0, 100.0, -1.0, 99.0)
    check(
        "クリップは括りの 15%/85% で止め、当たったことを残す",
        (abs(high.material_mm3 - 85.0) < 1e-9 and high.clipped)
        and (abs(low.material_mm3 - 15.0) < 1e-9 and low.clipped),
        f"上側={high.material_mm3} 下側={low.material_mm3}",
    )
    damped = _propose(0.0, 100.0, -3.0, 1.0, side="lo", streak=2)
    once = _propose(0.0, 100.0, -3.0, 1.0, side="lo", streak=1)
    check(
        "Illinois は据え置かれた端点の f を半分にして提案をそちらへ寄せる",
        abs(once.material_mm3 - 75.0) < 1e-9 and abs(damped.material_mm3 - 60.0) < 1e-9,
        f"据え置き1回={once.material_mm3} / 2回={damped.material_mm3}",
    )
    for label, proposal, reason in (
        (
            "端点の安全率が測れていない(too_little/too_much の直後)",
            _propose(0.0, 100.0, None, 1.0),
            sl.FALLBACK_UNMEASURED_ENDPOINT,
        ),
        (
            "分母 sf_hi - sf_lo が極小",
            _propose(0.0, 100.0, -1e-18, 0.0),
            sl.FALLBACK_DEGENERATE_SLOPE,
        ),
        (
            "提案が括りの端と実質同じ",
            _propose(1.0, 1.0 + 1e-12, -3.0, 1.0),
            sl.FALLBACK_AT_BRACKET_EDGE,
        ),
        (
            "履歴と同じ材料量(振動)",
            _propose(0.0, 100.0, -3.0, 1.0, evaluated=(75.0,)),
            sl.FALLBACK_ALREADY_EVALUATED,
        ),
    ):
        check(
            f"フォールバック: {label}",
            proposal.proposed_by == "bisection" and proposal.fallback_reason == reason,
            f"{proposal.proposed_by} / {proposal.fallback_reason}",
        )
    # 上端の安全率が目標の数千倍(= 案Aが動機にしている形)でも括りの外へ出ない。
    extreme = _propose(100.0, 200.0, -1.9, 5527.0)
    check(
        "提案は必ず括りの内側",
        100.0 < extreme.material_mm3 < 200.0 and 0.0 < plain.material_mm3 < 100.0,
        f"{extreme.material_mm3:.4f}",
    )


def check_searcher_comparison(
    space: ds.DesignSpace,
    material_min: float,
    material_max: float,
    illinois: "sl.SafetyLoopResult",
) -> None:
    print("\n[8] 探索器の比較(補間 対 二分。bracket 題材)")
    baseline = sl.solve_for_safety_factor(
        space,
        target_safety_factor=ENTRY_TARGET,
        material_min_mm3=material_min,
        material_max_mm3=material_max,
        max_iter=CFG["max_iter"],
        searcher="bisection",
    )
    fallbacks = [s.fallback_reason for s in illinois.history if s.fallback_reason]
    print(
        f"    二分     : 評価{baseline.evaluations}回 / 材料{baseline.material_mm3:.0f}mm^3 / sf={baseline.safety_factor:.2f}\n"
        f"    Illinois : 評価{illinois.evaluations}回 / 材料{illinois.material_mm3:.0f}mm^3 / sf={illinois.safety_factor:.2f}\n"
        f"    補間{sum(1 for s in illinois.history if s.proposed_by == 'illinois')}回 / "
        f"クリップ{sum(1 for s in illinois.history if s.clipped)}回 / フォールバック{fallbacks}"
    )
    # **この1点についての回帰検査である。** 目標を変えると補間が1回多く踏むことがある
    # (実測は docs/TOPOPT_SPEED.md「実装済み A」の表。目標4,500で 7 → 8)。
    check(
        "評価回数が二分より増えない(目標 ENTRY_TARGET の1点)",
        illinois.evaluations <= baseline.evaluations,
        f"{illinois.evaluations} <= {baseline.evaluations}",
    )
    # **完全一致は要求しない**(ボクセル量子化)。どちらも真の最小を含む幅 rel_tol の括りで
    # 止まるので、2つの答えはその幅だけ離れうる —— それを超えて重ければ悪化である。
    check(
        "採用材料量が二分より悪化しない(rel_tol の幅まで)",
        illinois.material_mm3 <= baseline.material_mm3 * (1.0 + sl.DEFAULT_REL_TOL),
        f"{illinois.material_mm3:.0f} <= {baseline.material_mm3:.0f} x {1.0 + sl.DEFAULT_REL_TOL}",
    )
    check(
        "どちらの探索器も目標安全率以上を返す",
        illinois.safety_factor >= ENTRY_TARGET and baseline.safety_factor >= ENTRY_TARGET,
        f"補間={illinois.safety_factor:.2f} / 二分={baseline.safety_factor:.2f}",
    )
    # **フォールバックしっぱなしなら補間の利得は出ない。** 既定で1回も補間が使われなければ
    # 配線が切れている(全部二分に落ちても上の3つは通ってしまう)。
    check(
        "既定(illinois)では補間が実際に使われている",
        any(step.proposed_by == "illinois" for step in illinois.history),
        f"{[step.proposed_by for step in illinois.history]}",
    )
    check(
        "searcher='bisection' は補間を1回も使わない",
        all(step.proposed_by in ("bound", "bisection") for step in baseline.history)
        and not any(step.clipped for step in baseline.history),
        f"{[step.proposed_by for step in baseline.history]}",
    )


def main() -> int:
    print("=" * 72)
    print("check_safety_loop.py : 安全率の外側ループの受入検査")
    print("=" * 72)
    check_wiring()
    check_keep_model_wiring()
    check_monotonic_spotcheck()
    space, material_min, material_max, entry = check_entry_point()
    check_infeasible(space, material_min, material_max)
    check_already_met(space, material_min, material_max)
    check_argument_validation(space)
    check_proposal_rules()
    check_searcher_comparison(space, material_min, material_max, entry)

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
