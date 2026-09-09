"""安全率を入力に、材料量(mm^3)を二分探索する外側ループ。

材料量は「内部表現は絶対量(mm^3)、利用者の入力は安全率」の2層(engine
`safety-factor-goes-outside-the-optimizer`)。内側の2枚 —— `design_space.py`(絶対量の口)と
`stress.py`(応力と最小安全率)—— の上に立つ**外側の層**で、最適化そのものには手を入れない。
**応力制約トポロジー最適化(応力を制約に入れて最軽量を解く型)は採らない**(理由4点は同ノート)。
ここにあるのは「最適化を外から何度か回して材料量を決めるループ」だけ。

塊の扱い(docs/TOPOPT.md「塊が材料量に追従する」参照): **各段で塊を導出し直す(案a)を選んだ**。
`design_space.derive_default(conditions, material_mm3=material, ...)` を二分探索の
候補材料量そのままで毎回呼ぶ —— 塊と材料量が常に噛み合う。固定して使い回す案(c)と比べて
安全率・材料量ともに実測でよく一致したが(docs/TOPOPT.md表)、(a) は塊が段ごとに変わるため厳密には
「材料が増えたから安全率が上がったのか、塊が変わったからなのか」を切り分けられない。
コスト差は実測で無視できる大きさだったので、切り分けを諦めてでも塊と材料量を噛み合わせる
(a) を既定にした。

**SIMP を回すのは段ごとだが、塊の元になる ρ は探索を通して1本だけである**(既定の導出が
連続値保持になったため)。`_build_reference` が上端の評価より前に1回作り、以後の全段が
その順位から切り出す。段ごとに変わるのは「順位のどこまで取るか」だけになったので、上の
「塊が変わったのか材料が変わったのか」の切り分けにくさは、以前より小さい。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from . import design_space as ds
from . import simp
from . import stress

_HERE = Path(__file__).resolve().parent

# 二分探索は候補材料量ごとに `design_space.run_topopt` を呼ぶ(= STLを書く)。**既定の出力先を
# `design_space.run_topopt` の既定(`output/design_space_case1.stl`)や `anchor.run_pipeline` の
# 既定(`output/bracket.stl`)へ向けてはいけない** —— どちらも他の受入検査(`check_design_space.py`
# [5] / `check_anchor.py`)が「その検査自身の標準的な実行の成果物」として当てにしている。
# ここは**探索の途中経過の使い捨て**なので専用の scratch ファイルへ書く
# (`.gitignore` 対象。「最後に評価した候補」のメッシュが残るだけで、採用した材料量とは限らない)。
_SEARCH_STL_PATH = _HERE / "output" / "safety_loop_search.stl"

# ---------------------------------------------------------------------------
# 許容応力(本人決定)。**40 という数字だけをハードコードしない** ——
# 後で必ず「この40はどこから来た」と聞かれる。
# ---------------------------------------------------------------------------

# PETG の降伏応力。フィラメントのデータシート級の公称値。実際に使う銘柄の値へ差し替えられる。
PETG_YIELD_MPA = 50.0

# 3Dプリントを想定した低減係数。**留保**: 積層方向(Z)には楽観的 —— FDM の層間接着は
# ふつう XY 方向の40〜60%で、0.8 には届かない。さらにトポロジー最適化が出す形は、どの部分が
# どの向きに荷重を受けるかが最適化の時点では分からないので、造形姿勢が決まるまで一つの
# スカラーでは異方性を代表できない。**暫定値である。**
PRINT_DERATE = 0.8

# 許容応力 = 降伏応力 × 低減係数。**安全率(利用者入力)とは二重に効く** ——
# 0.8 は材料側の低減、安全率は設計側の余裕であり、両方掛かるのは意図どおり
# (片方に畳み込むと、造形法を変えたときにどちらを動かせばよいか分からなくなる)。
DEFAULT_ALLOWABLE_MPA = PETG_YIELD_MPA * PRINT_DERATE  # 40.0

# `stress.UnitScale` は正の有限値を要求するが、**安全率は E に依存しない**
# (`stress.py` の契約。E が効くのは変位だけ)。ここでは表示用の変位を出すためだけの
# 名目値(PETG のヤング率はおよそ1.5〜2.1GPa)を既定にしてある。安全率の結果には影響しない。
DEFAULT_YOUNGS_MODULUS_MPA = 2000.0

# 二分探索の既定値。根拠は docs/TOPOPT.md「収束条件と回数上限をどう決めたか」。
DEFAULT_REL_TOL = 0.08  # 材料量の相対幅(区間の [lo, hi] が (hi-lo)/hi <= 8% で収束と見なす)
DEFAULT_MAX_EVALUATIONS = 15  # 境界2回 + 二分探索の反復上限(8%まで詰めるのに必要な約3.6回に余裕)
DEFAULT_MAX_ITER = 40  # 各段の SIMP 反復数。anchor.run_pipeline の既定と揃えた


class SafetyTargetError(ValueError):
    """目標安全率が指定した材料量の範囲(`[material_min_mm3, material_max_mm3]`)で
    達成できない。**どちら側にどれだけ足りないかを名指しする。**

    `side`:

    - `"unreachable_within_max_material"` —— `material_max_mm3` まで材料を使っても
      目標安全率に届かない。`safety_factor` に実際に得られた最小安全率、
      `shortfall` に `target_safety_factor - safety_factor` が入る。
    - `"invalid_bound"` —— `material_min_mm3` か `material_max_mm3` そのものが
      塊として導出できない(接続できない・体積目標が達成できない、のいずれか)。
      元の例外は `__cause__` に連鎖してある。`bound` に `"material_min_mm3"` か
      `"material_max_mm3"` が入る。
    """

    def __init__(
        self,
        message: str,
        *,
        side: str,
        target_safety_factor: float,
        material_mm3: float,
        safety_factor: Optional[float] = None,
        shortfall: Optional[float] = None,
        bound: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.side = side
        self.target_safety_factor = target_safety_factor
        self.material_mm3 = material_mm3
        self.safety_factor = safety_factor
        self.shortfall = shortfall
        self.bound = bound


class ConditionFacesEmptyError(ValueError):
    """**本番解像度の最終solve**で拘束面・荷重面のアンカーが空だった。

    `design_space.run_topopt` は `anchor.run_pipeline` の契約どおり例外にせず
    `empty_condition_faces` を積んだ辞書を返し、`"result"` は `None` のままになる。
    `_derive_and_run` はそれを見ずに `run["result"].model` へ触っていたので、
    **生の `AttributeError` が上まで抜けていた**(`20260904-1556` の契約④が
    「未修正のまま残っている」と名指ししていた穴)。

    **`_evaluate` の分類(`"too_little"` / `"too_much"`)へ落とさない。** 条件面が空なのは
    要素サイズの問題であって材料量の問題ではないので、二分探索を続けても直らない
    (材料量を振っても格子は変わらない)。`design_space.CoarseRunConditionFacesEmptyError`
    (粗い段の同じ失敗)と同じ形で、`empty_condition_faces` に
    `anchor.AnchorTable.condition_face_diagnoses` と同じリストを持つ。
    """

    def __init__(self, empty_condition_faces: Sequence[dict]) -> None:
        diagnoses = [dict(item) for item in empty_condition_faces]
        faces = "、".join(
            f"{item.get('side')}側の面{item.get('face_index')}" for item in diagnoses
        )
        super().__init__(
            f"本番解像度で条件面のアンカーが空だった({faces})。"
            "材料量を振っても格子は変わらないので探索を続けても直らない —— "
            "要素サイズを下げるか、隣接する大きい面を指定すること"
        )
        self.empty_condition_faces = diagnoses


@dataclass(frozen=True, eq=False)
class SafetyLoopStep:
    """二分探索の1回の評価。**成功しなかった評価も残す**(履歴が探索の軌跡そのものになる)。"""

    material_mm3: float
    outcome: str  # "ok" / "too_little" / "too_much"
    safety_factor: Optional[float] = None
    elements: Optional[int] = None
    error: Optional[str] = None


@dataclass(frozen=True, eq=False)
class SafetyLoopResult:
    """安全率入力ループの答え。**材料量は「目標以上を満たす最小」**(ちょうどではない。
    docs/TOPOPT.md「探索の目標は『ちょうど』ではなく『以上で最小』」参照)。
    """

    material_mm3: float
    achieved_material_mm3: float
    safety_factor: float
    target_safety_factor: float
    allowable_mpa: float
    converged: bool
    evaluations: int
    history: list = field(default_factory=list)  # list[SafetyLoopStep]
    space: Any = None  # design_space.DesignSpace(最終採用した材料量で導出した塊)
    run: Optional[dict] = None  # design_space.run_topopt() の戻り値
    report: Any = None  # stress.SafetyReport

    def summary(self) -> str:
        conv = "収束" if self.converged else "未収束(回数上限)"
        return (
            f"材料 {self.material_mm3:.6g}mm^3 で最小安全率 {self.safety_factor:.4f}"
            f"(目標 {self.target_safety_factor:.4f} 以上) / {conv} / "
            f"評価 {self.evaluations} 回"
        )


def _derive_and_run(
    conditions: ds.Conditions,
    material_mm3: float,
    *,
    allowable_mpa: float,
    youngs_modulus_mpa: float,
    max_iter: int,
    force: Sequence[float],
    forces: Optional[Sequence[Sequence[float]]] = None,
    solver: Optional[str],
    threads: Optional[int],
    rho_min: float,
    percentile: float,
    derivation_kwargs: dict,
    reference: Any = None,
    stl_path: Optional[Any] = None,
    run_progress: Optional[Callable[[Any, Any], None]] = None,
) -> tuple[Any, dict, Any]:
    """1つの材料量について、塊を導出し直してから最適化して安全率まで出す(この段の中核)。

    **塊は毎回この材料量で導出し直す(選んだ案a)。** `derive_default` は既定の導出
    (`design_space.DEFAULT_DERIVATION`、いまは連続値保持)へ委ねるので、既定を案1へ戻すと
    `material_mm3` は `TypeError` になる(`derive_default` の契約どおり —— 黙って捨てない)。

    **`reference` は探索が最初に1回だけ作った参照 ρ。** これを渡さないと
    `derive_from_reference` が材料量ごとに参照を作り直し、案5より遅い形になる。
    """
    kwargs = dict(derivation_kwargs)
    kwargs.setdefault("material_mm3", material_mm3)
    if reference is not None:
        kwargs.setdefault("reference", reference)
    space = ds.derive_default(conditions, **kwargs)
    run = ds.run_topopt(
        space,
        material_mm3=material_mm3,
        max_iter=max_iter,
        force=force,
        forces=forces,
        solver=solver,
        threads=threads,
        keep_model=True,
        stl_path=_SEARCH_STL_PATH if stl_path is None else stl_path,
        progress=run_progress,
    )
    # **`run["result"]` は空条件面のとき `None` になる**(`run_topopt` の早期return)。
    # ここで見ずに `.model` へ触ると生の `AttributeError` になる —— 上の
    # `ConditionFacesEmptyError` の docstring 参照。
    if run.get("empty_condition_faces"):
        raise ConditionFacesEmptyError(run["empty_condition_faces"])
    result = run["result"]
    scale = stress.UnitScale.for_model(
        result.model,
        element_mm=space.grid.spacing,
        load_n=conditions.load_n,
        youngs_modulus_mpa=youngs_modulus_mpa,
    )
    # 変位解法は "mg" を明示する(simp.optimize の既定と揃える)。stress.evaluate 自体の既定
    # "cg" は 0143 が組んだ当時(マルチグリッド導入前)のままなので、ここでは触らずに上書きする。
    report = stress.evaluate(
        result.model,
        result.rho,
        scale=scale,
        allowable_mpa=allowable_mpa,
        solver="mg",
        rho_min=rho_min,
        percentile=percentile,
    )
    return space, run, report


def _build_reference(
    conditions: ds.Conditions,
    derivation_kwargs: dict,
    *,
    force: Sequence[float],
    forces: Optional[Sequence[Sequence[float]]],
    solver: Optional[str],
    threads: Optional[int],
    progress: Optional[Callable[[Any, Any], None]],
) -> Any:
    """探索の最初に1回だけ参照 ρ を作る。既定の導出が連続値保持でなければ `None`。

    **`derivation_kwargs` は切り出し側(`derive_from_reference`)の引数で書かれている**ので、
    参照を作る側(`build_reference`)の名前へ translate してから渡す。素通しにすると
    `k` などが `derive` まで流れて `TypeError` になる。

    **参照は全段で使い回す。** 材料量ごとに作り直すと「毎回1回余分に回す」形になり、
    案5に対する利点(SIMP を回すのが探索を通して1回)がそのまま消える。
    """
    if derivation_kwargs.get("derivation", ds.DEFAULT_DERIVATION) != "reference":
        return None
    kwargs = dict(derivation_kwargs)
    kwargs.pop("derivation", None)
    kwargs.pop("k", None)
    build: dict = {}
    if "reference_material_ratio" in kwargs:
        build["material_ratio"] = kwargs.pop("reference_material_ratio")
    if "reference_max_iter" in kwargs:
        build["max_iter"] = kwargs.pop("reference_max_iter")
    return ds.build_reference(
        conditions,
        force=force,
        forces=forces,
        solver=solver,
        threads=threads,
        progress=progress,
        **build,
        **kwargs,
    )


def _evaluate(conditions: ds.Conditions, material_mm3: float, **kwargs: Any) -> tuple[str, Any]:
    """材料量1点を評価する。**失敗も分類して返す**(例外を上まで素通しにしない)。

    - `"ok"` —— 通常どおり (space, run, report)。
    - `"too_little"` —— この材料量では塊が細すぎる(接続できない/体積目標が
      `below_minimum`/**残差検算が通らない**)。安全率で言えば「目標に届かない」と
      同じ側として扱う(この材料量とその下は不採用)。
    - `"too_much"` —— この材料量**そのものを目標に**導出した塊でも、密度フィルタの
      目減りで材料が収まらない(`VolumeTargetError(side="above_capacity")`)。
      安全率は測れていないので、この材料量とその上は判定を保留して引き返す。

    **`SolveResidualError` を `too_little` に落とすのは、拘束不足を握りつぶすためではない。**
    材料が少ないほど ρ が下限に張り付いて系が悪条件になり、残差が許容(`SOLVE_RESIDUAL_TOL`)を
    通らなくなる —— hanger 2.4mm の実測で、材料 5.42e4mm^3 のとき自由度が
    3,623,730 のうち 43,161 しか残らず、残差 2.93e-5 で落ちた(8.70e4mm^3 では通っている)。
    **本当の拘束不足なら上端の評価が同じ例外で落ち、`SafetyTargetError(side="invalid_bound")`
    として原因を連鎖したまま上がる**ので、探索の中で握っても検出力は落ちない。
    """
    try:
        space, run, report = _derive_and_run(conditions, material_mm3, **kwargs)
        return "ok", (space, run, report)
    except simp.VolumeTargetError as exc:
        return ("too_much" if exc.side == "above_capacity" else "too_little"), exc
    except (
        simp.SolveResidualError,
        ds.DesignSpaceDisconnectedError,
        ds.DesignSpaceTooSmallError,
    ) as exc:
        return "too_little", exc


def solve_for_safety_factor(
    conditions: ds.Conditions,
    *,
    target_safety_factor: float,
    material_min_mm3: float,
    material_max_mm3: float,
    allowable_mpa: float = DEFAULT_ALLOWABLE_MPA,
    youngs_modulus_mpa: float = DEFAULT_YOUNGS_MODULUS_MPA,
    max_iter: int = DEFAULT_MAX_ITER,
    rho_min: float = stress.DEFAULT_RHO_MIN,
    percentile: float = stress.DEFAULT_PERCENTILE,
    rel_tol: float = DEFAULT_REL_TOL,
    max_evaluations: int = DEFAULT_MAX_EVALUATIONS,
    force: Sequence[float] = (0.0, 0.0, -1.0),
    forces: Optional[Sequence[Sequence[float]]] = None,
    solver: Optional[str] = None,
    threads: Optional[int] = None,
    derivation_kwargs: Optional[dict] = None,
    progress: Optional[Callable[[SafetyLoopStep], None]] = None,
    run_progress: Optional[Callable[[Any, Any], None]] = None,
) -> SafetyLoopResult:
    """**安全率を入力に取る入口。** 目標安全率を満たす材料量で最適化した結果を返す。

    `[material_min_mm3, material_max_mm3]` の中で、**目標安全率以上を満たす最小の材料量**を
    二分探索する(「ちょうど」ではない。docs/TOPOPT.md「探索の目標」参照)。**単調性
    (材料量を増やすと最小安全率が上がる)を前提にする** —— docs/TOPOPT.md「単調性を測る」で
    複数の合成条件について実測して確かめてあるが、**理論的な保証ではない**。任意の `conditions` で
    単調とは限らないので、疑わしい形では `derivation_kwargs` を揃えて自分で数点測ること。

    材料量の探索範囲は呼び出し側が決める(このprobeは形ごとの「常識的な材料量」を知らない)。
    `material_min_mm3` は少なくとも保持ソリッドの体積より大きいこと、`material_max_mm3` は
    塊の容量の99.9%未満にすること(端ちょうどは `simp` の OC 二分探索が退化する。
    `20260904-0142` の罠)。

    達成不能なときは `SafetyTargetError` —— どちら側にどれだけ足りないかを名指しする
    (クラスdocstring参照)。

    `derivation_kwargs` は `design_space.derive_default` への追加引数(既定の連続値保持なら
    `margin_ratio` / `elements_long_axis` / `spacing` / `k` /
    `reference_material_ratio` / `reference_max_iter`)。`material_mm3` はここが二分探索の
    候補材料量で埋めるので**渡さないこと**(渡しても上書きされる)。`reference` も同じで、
    **探索の最初に1回だけ作って全段へ配る** —— 材料量ごとに作り直すと、SIMP を1回で済ませる
    という連続値保持の利点がそのまま消える。

    **STL は候補材料量ごとに書く(= 二分探索の回数ぶん上書きする)。** 既定の書き出し先は
    `output/safety_loop_search.stl`(scratch。`.gitignore` 対象)で、**探索が終わった時点の
    中身は最後に評価した候補のもので、採用した材料量(`result.material_mm3`)とは限らない**。
    採用した形の STL が欲しいときは、呼び出し側が `design_space.run_topopt(result.space,
    material_mm3=result.material_mm3, ..., stl_path=<自分のパス>)` をもう一度呼ぶこと
    (`optimize` は決定的なので、同じ入力からは同じ ρ 場が返る)。

    進捗は2段ある。**`progress` は候補材料量ごと**(`SafetyLoopStep`。1回が数十秒〜数分)で、
    **`run_progress` は SIMP の設計反復ごと**(`callback(Iteration, rho)` をそのまま
    `design_space.run_topopt` の `progress` へ渡す。既定 `None` = 何も渡さないので挙動は不変)。
    長い処理を外から止める手段は `run_progress` の中から例外を投げることで、その例外は
    `_evaluate` の分類(`ValueError` 系)に拾われない型にすること
    (`duocad/optimize_run.py` の `OptimizeCancelled` は `RuntimeError` にしてある)。
    **参照 run は本番の格子で回るので数分かかる。** `run_progress` はそこにも渡してあるので、
    進捗も中断も効く(`SafetyLoopStep` はまだ1つも出ていないので `evaluation` は 0 のまま)。
    """
    if not math.isfinite(target_safety_factor) or target_safety_factor <= 0.0:
        raise ValueError(f"target_safety_factor は有限の正の値であること(受け取った値: {target_safety_factor!r})")
    if not material_min_mm3 > 0.0 or not math.isfinite(material_min_mm3):
        raise ValueError(f"material_min_mm3 は有限の正の値であること(受け取った値: {material_min_mm3!r})")
    if not material_max_mm3 > material_min_mm3 or not math.isfinite(material_max_mm3):
        raise ValueError(
            "material_max_mm3 は material_min_mm3 より大きい有限の値であること"
            f"(min={material_min_mm3!r} / max={material_max_mm3!r})"
        )
    if not 0.0 < rel_tol < 1.0:
        raise ValueError(f"rel_tol は (0, 1) であること(受け取った値: {rel_tol!r})")
    if max_evaluations < 2:
        raise ValueError(f"max_evaluations は2以上であること(受け取った値: {max_evaluations!r})")

    eval_kwargs = dict(
        allowable_mpa=allowable_mpa,
        youngs_modulus_mpa=youngs_modulus_mpa,
        max_iter=max_iter,
        force=force,
        forces=forces,
        solver=solver,
        threads=threads,
        rho_min=rho_min,
        percentile=percentile,
        derivation_kwargs=dict(derivation_kwargs or {}),
        run_progress=run_progress,
    )
    # **参照 ρ を1回だけ作る。** 上端の評価より前に置くのは、上端も同じ参照から切り出した塊で
    # 評価しなければ「材料量を振ったのか塊の作り方を変えたのか」が段の間で揃わないため。
    eval_kwargs["reference"] = _build_reference(
        conditions,
        dict(derivation_kwargs or {}),
        force=force,
        forces=forces,
        solver=solver,
        threads=threads,
        progress=run_progress,
    )

    history: list[SafetyLoopStep] = []
    best: Optional[tuple[float, Any, Any, Any]] = None  # (material, space, run, report)

    def record(material: float, outcome: str, payload: Any) -> None:
        if outcome == "ok":
            _space, _run, report = payload
            step = SafetyLoopStep(
                material_mm3=material,
                outcome="ok",
                safety_factor=report.safety_factor,
                elements=_space.element_count,
            )
        else:
            step = SafetyLoopStep(material_mm3=material, outcome=outcome, error=str(payload))
        history.append(step)
        if progress is not None:
            progress(step)

    def evaluate(material: float) -> tuple[str, Any]:
        outcome, payload = _evaluate(conditions, material, **eval_kwargs)
        record(material, outcome, payload)
        return outcome, payload

    # --- 上端を評価する。ここが「材料が足りない」を判定する基準になる。---
    outcome_hi, payload_hi = evaluate(material_max_mm3)
    if outcome_hi != "ok":
        raise SafetyTargetError(
            f"material_max_mm3={material_max_mm3:.6g}mm^3 そのものが塊として導出できない"
            f"({outcome_hi}): {payload_hi}",
            side="invalid_bound",
            target_safety_factor=target_safety_factor,
            material_mm3=material_max_mm3,
            bound="material_max_mm3",
        ) from (payload_hi if isinstance(payload_hi, BaseException) else None)
    _space_hi, _run_hi, report_hi = payload_hi
    if report_hi.safety_factor < target_safety_factor:
        raise SafetyTargetError(
            f"material_max_mm3={material_max_mm3:.6g}mm^3 まで材料を使っても"
            f"最小安全率は {report_hi.safety_factor:.4f} で、目標 {target_safety_factor:.4f} に"
            f"{target_safety_factor - report_hi.safety_factor:.4f} 足りない"
            "(material_max_mm3 を上げるか、目標安全率を下げること)",
            side="unreachable_within_max_material",
            target_safety_factor=target_safety_factor,
            material_mm3=material_max_mm3,
            safety_factor=report_hi.safety_factor,
            shortfall=target_safety_factor - report_hi.safety_factor,
        )
    best = (material_max_mm3, _space_hi, _run_hi, report_hi)

    # --- 下端を評価する。既にここで目標を満たしていれば探索は要らない。---
    outcome_lo, payload_lo = evaluate(material_min_mm3)
    if outcome_lo == "too_much":
        raise SafetyTargetError(
            f"material_min_mm3={material_min_mm3:.6g}mm^3 そのものが塊として導出できない"
            f"(too_much): {payload_lo}",
            side="invalid_bound",
            target_safety_factor=target_safety_factor,
            material_mm3=material_min_mm3,
            bound="material_min_mm3",
        ) from (payload_lo if isinstance(payload_lo, BaseException) else None)
    if outcome_lo == "ok":
        _space_lo, _run_lo, report_lo = payload_lo
        if report_lo.safety_factor >= target_safety_factor:
            best = (material_min_mm3, _space_lo, _run_lo, report_lo)
            return _finish(best, target_safety_factor, allowable_mpa, history, converged=True)
    # `"too_little"` でも下端はもとから不採用側なので、どちらの outcome でもそのまま lo になる。
    lo = material_min_mm3
    hi = material_max_mm3

    # --- 二分探索本体。---
    while len(history) < max_evaluations:
        if (hi - lo) / hi <= rel_tol:
            break
        mid = 0.5 * (lo + hi)
        outcome_mid, payload_mid = evaluate(mid)
        if outcome_mid == "ok":
            _space_mid, _run_mid, report_mid = payload_mid
            if report_mid.safety_factor >= target_safety_factor:
                hi = mid
                if mid < best[0]:
                    best = (mid, _space_mid, _run_mid, report_mid)
            else:
                lo = mid
        elif outcome_mid == "too_little":
            lo = mid  # 材料不足の一種として扱う(docs/TOPOPT.md「② 依存してはいけないもの」参照)
        else:  # "too_much"
            hi = mid  # この材料量とその上はこの導出戦略では判定できない

    converged = (hi - lo) / hi <= rel_tol
    return _finish(best, target_safety_factor, allowable_mpa, history, converged=converged)


def _finish(
    best: tuple[float, Any, Any, Any],
    target_safety_factor: float,
    allowable_mpa: float,
    history: list,
    *,
    converged: bool,
) -> SafetyLoopResult:
    material, space, run, report = best
    return SafetyLoopResult(
        material_mm3=material,
        achieved_material_mm3=run["achieved_material_mm3"],
        safety_factor=report.safety_factor,
        target_safety_factor=target_safety_factor,
        allowable_mpa=allowable_mpa,
        converged=converged,
        evaluations=len(history),
        history=history,
        space=space,
        run=run,
        report=report,
    )
