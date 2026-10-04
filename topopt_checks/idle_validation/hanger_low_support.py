"""タスク6(hanger低荷重: 骨組み回復・格子依存)が共有する条件定義とヘルパー。

条件はH1(200N・要素2.0mm・最小幅は実寸10mm固定)。結果と解釈は`docs/topopt_idle_validation/06_HANGER_LOW.md`。
実測: hangerは64万要素で、1FEM解あたり約27〜114秒(step20約48秒・適応約114秒)。
製品コード(`duocad.topopt.*`)は変更しない。`search_methods.py`・`synthetic_matrix_support.py`を無変更で再利用する。
"""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import numpy as np  # noqa: E402

from duocad.topopt import acceptance, binary_search  # noqa: E402

import refine_shape as rs  # noqa: E402
import search_methods as sm  # noqa: E402
import synthetic_matrix_support as sup  # noqa: E402

HANGER_MODEL = rs.HANGER_MODEL
OUT = sup.OUT
MASK_DIR = OUT / "06_masks"
LOCK = sup.LOCK  # task4/5/hanger_external_checkと単一FEM実行ロックを共有(1実行ずつ)

# H1条件。min_widthは実寸10mm固定: 5セル指定だと解像度ごとに実現幅が変わる(2.0mmで9.99mm・2.2mmで11.00mm)。
BASELINE = dict(model=HANGER_MODEL, load_n=200.0, element_size_mm=2.0, min_width_mm=10.0,
                tag="hanger_h1_baseline", cause="原因2(低荷重・太幅)代表。H1と同一条件")

# 実寸min_width=10mm固定のまま格子解像度だけ振る(2.0mmはBASELINEが兼ねるので新規実行しない)
EXTRA_RESOLUTIONS_MM = (1.8, 2.2, 2.4)

COARSE_SOURCE_RESOLUTION_MM = 2.4  # 「粗い骨組み開始」の投影元(run_06_grid_resolution.pyのstep20結果)


def project_mask_to_context(src_mask: np.ndarray, src_origin, src_spacing: float, ctx: sm.SearchContext) -> np.ndarray:
    """`src_mask`(`src_origin`/`src_spacing`は投影元自身の格子)を`ctx`の格子へ投影する。
    **投影元自身のorigin/spacingを渡すこと**(05が踏んだ罠: 目的格子のorigin/spacingを誤って渡すと
    位置がずれたまま投影される。05_SYNTHETIC.md「踏んだ罠」参照)。`hanger_external_check.py`の
    `_project_current_shape`と同じ手順(投影→設計空間との積→最大連結成分)。
    """
    mask = rs._project_mask(np.asarray(src_mask, dtype=bool), np.asarray(src_origin, dtype=float),
                            float(src_spacing), ctx.space) & ctx.space.mask
    survivors, diag = binary_search.resolve_islands(mask, ctx.cond)
    if not survivors:
        raise RuntimeError(f"投影後に連結成分が残らなかった: {diag}")
    return max(survivors, key=lambda c: c.sum())


def repair_projection(raw: np.ndarray, ctx: sm.SearchContext) -> tuple[np.ndarray, dict]:
    """格子をまたいで投影した形(`project_mask_raw`の結果)が`resolve_islands`で全滅するとき、
    保持セル(`keep_cells`)のうち欠けたものを足して条件を満たす。補修量(セル数・体積)と、補修後も
    残った落とした島を返す(共通指示書「幾何的に成立しない初期形は補修量と理由を記録」)。
    実測: 2.4mm格子のstep20結果を2.0mm格子へ投影すると、保持セル2,552のうち244が欠け(荷重面の
    節点65/1,174も材料に接しない)、保持セルを足すだけで荷重面の被覆も回復した。足しても成立しなければ例外。
    """
    design = ctx.space.mask
    mask = np.array(raw, dtype=bool, copy=True) & design
    keep = binary_search._keep_cells(ctx.cond, mask.shape)
    added = int((keep & design & ~mask).sum()) if keep is not None else 0
    if keep is not None:
        mask |= keep & design
    survivors, diag = binary_search.resolve_islands(mask, ctx.cond)
    if not survivors:
        raise RuntimeError(f"保持セルを足しても条件が成立しない: raw={int(raw.sum())} added_keep={added} {diag}")
    best = max(survivors, key=lambda c: c.sum())
    return best, dict(raw_cells=int(np.asarray(raw).sum()), added_keep_cells=added,
                      added_volume_mm3=float(added * ctx.grid.element_volume), final_cells=int(best.sum()),
                      dropped_islands=diag["removed_islands"])


def load_transplant_native() -> tuple[np.ndarray, np.ndarray, float]:
    """`hanger_transplant`(2.2mm由来、01_stage1_shapes.json)の保存マスク・origin・spacingを返す。
    保存済みnpz自体が既にF0200_h2.0_w5格子へ投影済み(ファイル名`..._to_F0200_h2.0_w5_...`)だが、
    本モジュールのBASELINE(min_width実寸10mm指定)で組んだ`ctx.space`と完全に同一格子である保証は
    無いため、呼び出し側は`project_mask_to_context`で再投影すること(同一格子なら実質恒等写像になる)。
    """
    kind, _default = rs.HANGER_DEFAULTS["hanger_transplant"]
    entries = json.loads(rs.STAGE1.read_text(encoding="utf-8"))["entries"]
    entry = next(e for e in entries
                if e.get("shape_kind") == kind and e.get("dst_condition") == "F0200_h2.0_w5")
    npz_path = REPO / entry["files"]["npz"]
    data = np.load(npz_path)
    return (np.asarray(data["mask"], dtype=bool), np.asarray(data["origin"], dtype=float),
            float(data["spacing"]))


def project_mask_raw(src_mask: np.ndarray, src_origin, src_spacing: float, ctx: sm.SearchContext) -> np.ndarray:
    """`project_mask_to_context`から島処理(条件成立の判定)を除いた投影。集合の重なりを測るだけの用途
    (`truss_only_cells`)で使う。実測: 2.0mm格子の移植トラスも現行形(hanger_base)も、2.4mm格子へ
    投影すると`resolve_islands`が唯一の成分(1.4万〜1.8万セル)を条件不成立として落とし空集合になる
    (1.8/2.2/2.4mmの3解像度ともここで落ちる)。投影自体は有効。
    """
    return rs._project_mask(np.asarray(src_mask, dtype=bool), np.asarray(src_origin, dtype=float),
                            float(src_spacing), ctx.space) & ctx.space.mask


@contextmanager
def capture_unknown_safety():
    """`with`の間、`acceptance.inspect`の安全率が`unknown`(判定不能)だった呼び出しの
    (材料セル数・reason・error先頭)をリストへ集める。`binary_search`は`stop_reason='repeated_evaluation_error'`
    としか残さず、原因の例外文が`SearchResult`から読めないための計測用。
    """
    original = acceptance.inspect
    captured: list[dict] = []

    def wrapped(material, before, grid, **kwargs):
        report = original(material, before, grid, **kwargs)
        safety = report.get("checks", {}).get("safety", {})
        if safety.get("status") == "unknown":
            captured.append(dict(material_cells=int(np.asarray(material).sum()), reason=safety.get("reason"),
                                 error=(str(safety.get("error"))[:600] if safety.get("error") is not None else None)))
        return report

    acceptance.inspect = wrapped
    try:
        yield captured
    finally:
        acceptance.inspect = original


def unknown_safety_summary(captured: list[dict]) -> dict:
    by_reason: dict = {}
    for c in captured:
        by_reason[str(c["reason"])] = by_reason.get(str(c["reason"]), 0) + 1
    errors = [c for c in captured if c.get("reason") == "evaluation_error"]
    return dict(unknown_count=len(captured), by_reason=by_reason, evaluation_errors=errors[:8])


# 2.2mmトラスにだけある部分(斜めの部材)= TOPOPT_IDLE_MATERIAL.md 2-1節・
# measure_idle_divergence.pyと同じ定義。task06ではH1条件(BASELINE)の設計空間へ投影して使う。
def truss_only_cells(ctx: sm.SearchContext, *, resolve_islands: bool = True) -> np.ndarray:
    """2.2mmトラスをctxの格子へ投影したマスクのうち、V字形(=hanger_base、製品現行形)に無い部分。
    「斜めの部材のあたり」(TOPOPT_IDLE_MATERIAL.md 2-1節)の定量的な定義そのもの。
    `resolve_islands=False`は島処理なしの素の投影(2.0mm以外の格子では`True`だと空集合になりうる)。
    """
    project = project_mask_to_context if resolve_islands else project_mask_raw
    truss_mask, truss_origin, truss_spacing = load_transplant_native()
    truss_in_ctx = project(truss_mask, truss_origin, truss_spacing, ctx)
    current_cfg = rs._resolve_shape("hanger_base", "F0200_h2.0_w5")
    current_in_ctx = project(
        np.asarray(current_cfg["mask"], dtype=bool), np.asarray(current_cfg["origin"], dtype=float),
        float(current_cfg["spacing"]), ctx,
    )
    return truss_in_ctx & ~current_in_ctx


def truss_residual_fraction(mask: np.ndarray, truss_only: np.ndarray) -> float:
    if truss_only.sum() == 0:
        return float("nan")
    return float((mask & truss_only).sum() / truss_only.sum())
