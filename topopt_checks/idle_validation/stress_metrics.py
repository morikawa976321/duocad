"""タスク3(評価式選定)の候補指標。旧 TOPOPT_IDLE_SOLUTIONS 5.1節の表に挙げた8候補(定義は `docs/topopt_idle_validation/10_SYNTHESIS.md` 付録B-1)を
数式として実装する。全て「材料セルの von Mises 応力配列 + 母集団を選ぶマスク」から1つのスカラー値
(または部位別ならゾーンごとのスカラー)を返す純粋関数で、FEMは呼ばない(`metric_eval.py` が解いた
場を渡す)。

候補ごとの物理的意味・母数依存・上界/下界・領域移動・特異点への挙動の**判断と実測根拠**は
`docs/topopt_idle_validation/10_SYNTHESIS.md 付録B(旧 03_METRICS)` 5節にまとめてある(コードコメントではなくそちらが正)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass(frozen=True)
class MetricResult:
    """1候補×1形状の評価結果。`value` は主指標(pass/fail 判定に使う値)。"""

    name: str
    value: float
    allowable_mpa: Optional[float]
    passed: Optional[bool]
    population_count: int
    detail: dict = field(default_factory=dict)


def _population_values(von_mises_mpa: np.ndarray, population_mask: np.ndarray) -> np.ndarray:
    mask = np.asarray(population_mask, dtype=bool)
    vals = np.asarray(von_mises_mpa, dtype=float)[mask]
    bad = int(np.count_nonzero(~np.isfinite(vals)))
    if bad:
        raise ValueError(f"母集団に非有限値が{bad}個ある(全{vals.size}要素)")
    return vals


def _verdict(value: float, allowable_mpa: Optional[float]) -> Optional[bool]:
    if allowable_mpa is None:
        return None
    return bool(value <= allowable_mpa)


# ---------------------------------------------------------------------------
# M1: 全材料99.5%点(現行製品と同じ規則。duocad/topopt/stress.representative_stress を参照実装として使う)
# ---------------------------------------------------------------------------
def metric_full_population_percentile(
    von_mises_mpa: np.ndarray,
    material_mask: np.ndarray,
    *,
    percentile: float = 99.5,
    allowable_mpa: Optional[float] = None,
) -> MetricResult:
    vals = _population_values(von_mises_mpa, material_mask)
    if vals.size == 0:
        raise ValueError("材料セルが1つも無い")
    value = float(np.percentile(vals, percentile))
    return MetricResult(
        "full_population_percentile", value, allowable_mpa, _verdict(value, allowable_mpa),
        vals.size, dict(percentile=percentile),
    )


# ---------------------------------------------------------------------------
# M2: R2除外99.5%点(保持部ゾーンを母集団から丸ごと除いた99.5%点)
# ---------------------------------------------------------------------------
def metric_excluded_population_percentile(
    von_mises_mpa: np.ndarray,
    material_mask: np.ndarray,
    exclude_mask: np.ndarray,
    *,
    percentile: float = 99.5,
    allowable_mpa: Optional[float] = None,
) -> MetricResult:
    mat = np.asarray(material_mask, dtype=bool)
    population = mat & ~np.asarray(exclude_mask, dtype=bool)
    vals = _population_values(von_mises_mpa, population)
    if vals.size == 0:
        raise ValueError("除外後の母集団が空(除外領域が材料全体を覆っている)")
    value = float(np.percentile(vals, percentile))
    return MetricResult(
        "excluded_population_percentile", value, allowable_mpa, _verdict(value, allowable_mpa),
        vals.size, dict(percentile=percentile, excluded_count=int((mat & exclude_mask).sum())),
    )


# ---------------------------------------------------------------------------
# M3: 部位別99.5%点(全ゾーンが合格して初めて合格。1つでも不合格ならfail)
# ---------------------------------------------------------------------------
def metric_zone_percentile(
    von_mises_mpa: np.ndarray,
    zone_masks: dict[str, np.ndarray],
    *,
    percentile: float = 99.5,
    allowable_mpa: Optional[float] = None,
) -> MetricResult:
    per_zone: dict[str, dict] = {}
    worst_value = float("-inf")
    worst_zone = None
    total = 0
    for zone_name, zone_mask in zone_masks.items():
        vals = _population_values(von_mises_mpa, zone_mask)
        total += vals.size
        if vals.size == 0:
            per_zone[zone_name] = dict(value=None, count=0, passed=None)
            continue
        v = float(np.percentile(vals, percentile))
        p = _verdict(v, allowable_mpa)
        per_zone[zone_name] = dict(value=v, count=int(vals.size), passed=p)
        if v > worst_value:
            worst_value = v
            worst_zone = zone_name
    passed = None
    if allowable_mpa is not None:
        judged = [z["passed"] for z in per_zone.values() if z["passed"] is not None]
        # 全ゾーンが空(judgedが空)だと `all([])` が真になり「材料が無いのに合格」という
        # 空虚な真になる。材料が1つも無ければ判定不能として扱う(合格として扱わない)。
        passed = all(judged) if judged else False
    return MetricResult(
        "zone_percentile", worst_value, allowable_mpa, passed, total,
        dict(percentile=percentile, per_zone=per_zone, worst_zone=worst_zone),
    )


# ---------------------------------------------------------------------------
# M4: 固定物理体積の上位順位/上位平均
# ---------------------------------------------------------------------------
def _top_k_from_volume(count: int, spacing_mm: float, top_volume_mm3: float) -> int:
    if not spacing_mm > 0:
        raise ValueError(f"spacing_mm は正であること(受け取った値: {spacing_mm!r})")
    if not top_volume_mm3 > 0:
        raise ValueError(f"top_volume_mm3 は正であること(受け取った値: {top_volume_mm3!r})")
    cell_volume = spacing_mm ** 3
    k = int(round(top_volume_mm3 / cell_volume))
    return max(1, min(k, count))


def metric_top_volume_rank(
    von_mises_mpa: np.ndarray,
    material_mask: np.ndarray,
    *,
    spacing_mm: float,
    top_volume_mm3: float,
    allowable_mpa: Optional[float] = None,
) -> MetricResult:
    vals = _population_values(von_mises_mpa, material_mask)
    if vals.size == 0:
        raise ValueError("材料セルが1つも無い")
    k = _top_k_from_volume(vals.size, spacing_mm, top_volume_mm3)
    sorted_desc = np.sort(vals)[::-1]
    value = float(sorted_desc[k - 1])
    return MetricResult(
        "top_volume_rank", value, allowable_mpa, _verdict(value, allowable_mpa), vals.size,
        dict(top_volume_mm3=top_volume_mm3, spacing_mm=spacing_mm, k=k),
    )


def metric_top_volume_average(
    von_mises_mpa: np.ndarray,
    material_mask: np.ndarray,
    *,
    spacing_mm: float,
    top_volume_mm3: float,
    allowable_mpa: Optional[float] = None,
) -> MetricResult:
    vals = _population_values(von_mises_mpa, material_mask)
    if vals.size == 0:
        raise ValueError("材料セルが1つも無い")
    k = _top_k_from_volume(vals.size, spacing_mm, top_volume_mm3)
    sorted_desc = np.sort(vals)[::-1]
    value = float(sorted_desc[:k].mean())
    return MetricResult(
        "top_volume_average", value, allowable_mpa, _verdict(value, allowable_mpa), vals.size,
        dict(top_volume_mm3=top_volume_mm3, spacing_mm=spacing_mm, k=k),
    )


# ---------------------------------------------------------------------------
# M5: 限度超過体積/超過量積分(合否は「超過体積が許容体積以下」で判定。既定は0=1要素も超えない)
# ---------------------------------------------------------------------------
def metric_exceedance_volume(
    von_mises_mpa: np.ndarray,
    material_mask: np.ndarray,
    *,
    spacing_mm: float,
    allowable_mpa: float,
    tolerance_mm3: float = 0.0,
) -> MetricResult:
    vals = _population_values(von_mises_mpa, material_mask)
    over_count = int(np.count_nonzero(vals > allowable_mpa))
    value = float(over_count * spacing_mm ** 3)
    return MetricResult(
        "exceedance_volume", value, allowable_mpa, bool(value <= tolerance_mm3), vals.size,
        dict(spacing_mm=spacing_mm, tolerance_mm3=tolerance_mm3, over_count=over_count),
    )


def metric_exceedance_integral(
    von_mises_mpa: np.ndarray,
    material_mask: np.ndarray,
    *,
    spacing_mm: float,
    allowable_mpa: float,
    tolerance_mpa_mm3: float = 0.0,
) -> MetricResult:
    """単位 MPa·mm^3(超過応力 × 超過セルの体積の総和)。"""
    vals = _population_values(von_mises_mpa, material_mask)
    over = vals[vals > allowable_mpa]
    value = float((over - allowable_mpa).sum() * spacing_mm ** 3)
    return MetricResult(
        "exceedance_integral", value, allowable_mpa, bool(value <= tolerance_mpa_mm3), vals.size,
        dict(spacing_mm=spacing_mm, tolerance_mpa_mm3=tolerance_mpa_mm3, over_count=int(over.size)),
    )


# ---------------------------------------------------------------------------
# M6: p平均(正規化。N で割るので N→∞ で薄まる)
# ---------------------------------------------------------------------------
def metric_p_mean(
    von_mises_mpa: np.ndarray,
    material_mask: np.ndarray,
    *,
    p: float,
    allowable_mpa: Optional[float] = None,
) -> MetricResult:
    if not p > 0:
        raise ValueError(f"p は正であること(受け取った値: {p!r})")
    vals = _population_values(von_mises_mpa, material_mask)
    if vals.size == 0:
        raise ValueError("材料セルが1つも無い")
    value = float(np.mean(vals ** p) ** (1.0 / p))
    return MetricResult(
        "p_mean", value, allowable_mpa, _verdict(value, allowable_mpa), vals.size, dict(p=p),
    )


# ---------------------------------------------------------------------------
# M7: 非正規化pノルム・KS(N で割らない。数値安定のためmaxシフトを使う)
# ---------------------------------------------------------------------------
def metric_p_norm_unnormalized(
    von_mises_mpa: np.ndarray,
    material_mask: np.ndarray,
    *,
    p: float,
    allowable_mpa: Optional[float] = None,
) -> MetricResult:
    if not p > 0:
        raise ValueError(f"p は正であること(受け取った値: {p!r})")
    vals = _population_values(von_mises_mpa, material_mask)
    if vals.size == 0:
        raise ValueError("材料セルが1つも無い")
    vmax = float(vals.max())
    if vmax <= 0.0:
        value = 0.0
    else:
        # (sum (v_i)^p)^(1/p) を vmax でスケールしてから戻す(大きな p でのオーバーフロー回避)。
        scaled = vals / vmax
        value = vmax * float(np.sum(scaled ** p) ** (1.0 / p))
    return MetricResult(
        "p_norm_unnormalized", value, allowable_mpa, _verdict(value, allowable_mpa), vals.size, dict(p=p),
    )


def metric_ks_aggregation(
    von_mises_mpa: np.ndarray,
    material_mask: np.ndarray,
    *,
    rho: float,
    allowable_mpa: Optional[float] = None,
) -> MetricResult:
    """Kreisselmeier-Steinberger 集約(標準形=総和・N で割らない):
    KS = vmax + (1/rho) * ln( sum_i exp(rho*(v_i - vmax)) )。
    """
    if not rho > 0:
        raise ValueError(f"rho は正であること(受け取った値: {rho!r})")
    vals = _population_values(von_mises_mpa, material_mask)
    if vals.size == 0:
        raise ValueError("材料セルが1つも無い")
    vmax = float(vals.max())
    value = vmax + float(np.log(np.sum(np.exp(rho * (vals - vmax)))) / rho)
    return MetricResult(
        "ks_aggregation", value, allowable_mpa, _verdict(value, allowable_mpa), vals.size, dict(rho=rho),
    )


ALL_METRIC_NAMES = (
    "full_population_percentile",
    "excluded_population_percentile",
    "zone_percentile",
    "top_volume_rank",
    "top_volume_average",
    "exceedance_volume",
    "exceedance_integral",
    "p_mean",
    "p_norm_unnormalized",
    "ks_aggregation",
)
