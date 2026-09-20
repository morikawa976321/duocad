"""採用マスクに1〜2要素厚の部材が出ているかを、局所厚さの分布として実測する(節F)。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_member_thickness.py
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_member_thickness.py --skip-stress
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_member_thickness.py --out C:\\tmp\\thickness_member.json

タスク `20260918-1718-topopt-measure-member-thickness`(taskbox)の測定専用スクリプト。
**判定はしない**(厚みの下限を課すか・応力評価を直すかは書かない)。結果は
`docs/TOPOPT_THICKNESS.md` 節Fに転記する。

出力は既定で標準出力の JSON だけ(`--out` を渡さない限り `output/` を含めどこにもファイルを
残さない)。**採用マスクそのものは 2026-09-18 時点でディスク上に無い**ので、
`topopt_checks/verify_hanger_fix.py`(既存・無改変)の `verify()` 関数を直接呼んで1回だけ探索し、
`save_masks=` を一時フォルダへ渡して作る。実行中は `duocad.topopt.trace.DEFAULT_ROOT` も
一時フォルダへ向け、`output/` に何も残さない(`tests/conftest.py` と同じやり方)。

## 局所厚さの測り方

`scipy.ndimage.distance_transform_edt(material) * 2`。**格子の外を空とみなすか材料とみなすかで
境界付近の値が変わる**ので両方を計算する(既定の呼び方は「配列の外は何も無い」という前提を
暗黙に混ぜるので、実際には両端に1層パディングして明示する): `outside=empty`(パディングを
False=空にする。**これを主系列として報告する** — 設計空間の外に実材料は無いという物理的な
事実に合わせた)と `outside=material`(パディングを True=材料にする。対照用)。

裏取りとして `scipy.ndimage.binary_opening` を 2×2×2 と 3×3×3 の立方体構造要素で1回ずつ掛け、
残る要素の割合を距離変換ベースの割合と突き合わせる(構造要素が立方体・距離変換の球状距離とは
測り方が違うので、**数字が食い違っても寄せずにそのまま書く**)。

## 保持ソリッド由来の分離

`design_space.DesignSpace.keep`(= `model.keep`。保持ソリッド由来の占有マスク、格子・形は
`mask`/採用マスクと同じ)を `design_space.derive` の呼び出しをラップして捕捉する
(検証スクリプト自身が使っている「呼び出しをpatchで包んで実測値を横取りする」手口 ——
`verify_hanger_fix.py` が `design_space.derive`/`acceptance.inspect` に対して既にやっている
のと同じパターンで、製品コードは1行も変えない)。採用マスクから `keep` を引いた
(`material & ~keep`)ものを「最適化が作った部分」として節Fの集計に使う。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401
import _record
import numpy as np
from scipy import ndimage

import verify_hanger_fix as vhf

DEFAULT_MODEL = _bootstrap._REPO_ROOT / "models" / "hanger_bar_attachment.json"


# ---------------------------------------------------------------------------
# 探索を1回だけ回して採用マスクを作る(output/ に何も残さない)
# ---------------------------------------------------------------------------
def run_search_capturing_space(model: Path, spacing: float, target: float, max_evaluations: int,
                                save_masks: Path):
    """`verify_hanger_fix.verify()` を1回呼び、探索記録と `DesignSpace`(`.keep` 目当て)を返す。

    `design_space.derive` をラップして最後に作られた `DesignSpace` を横取りする —— `verify()` は
    戻り値の record(JSON化可能な辞書)にしか結果を入れないため、`.keep`(保持ソリッド由来マスク)
    はこの経路でしか取れない。ラップした関数は本物の `derive` を呼ぶだけで計算を変えない。
    """
    from duocad.topopt import design_space as ds
    from duocad.topopt import trace

    captured: dict = {}
    true_derive = ds.derive

    def capturing_derive(conditions, **kwargs):
        space = true_derive(conditions, **kwargs)
        captured["space"] = space
        return space

    with tempfile.TemporaryDirectory(prefix="topopt-trace-") as trace_root:
        with patch.object(trace, "DEFAULT_ROOT", Path(trace_root)), \
                patch.object(ds, "derive", capturing_derive):
            record = vhf.verify(model, spacing, target, max_evaluations, skip_search=False,
                                save_masks=save_masks)
    if "space" not in captured:
        raise RuntimeError("design_space.derive が一度も呼ばれず、DesignSpace を捕捉できなかった")
    return record, captured["space"]


# ---------------------------------------------------------------------------
# 局所厚さ(要素単位・mm単位、境界の扱い2通り)
# ---------------------------------------------------------------------------
def local_thickness(material: np.ndarray, *, outside: str) -> np.ndarray:
    """`material`(bool, 格子と同じ形)の距離変換×2。`outside` は格子の外の扱い
    (`"empty"` = パディングをFalse、`"material"` = パディングをTrue)。
    """
    if outside == "empty":
        padded = np.pad(material, 1, mode="constant", constant_values=False)
    elif outside == "material":
        padded = np.pad(material, 1, mode="constant", constant_values=True)
    else:
        raise ValueError(f"未知の outside: {outside}")
    distance = ndimage.distance_transform_edt(padded)
    return distance[1:-1, 1:-1, 1:-1] * 2.0


def thickness_stats(thickness_elem: np.ndarray, region: np.ndarray, spacing: float) -> dict:
    """`region`(bool マスク、集計対象の要素)内の局所厚さの割合・分布を返す。"""
    values = thickness_elem[region]
    n = int(values.size)
    if n == 0:
        return {"element_count": 0, "note": "対象領域が空(要素0件)"}
    fractions = {
        f"lt_{k}_elem": float(np.count_nonzero(values < k) / n) for k in (2, 3, 4)
    }
    counts = {
        f"lt_{k}_elem_count": int(np.count_nonzero(values < k)) for k in (2, 3, 4)
    }
    # ヒストグラム(要素単位): 0-1,1-2,...,9-10,10+ の11ビン。
    edges_elem = list(range(0, 11)) + [np.inf]
    hist_elem, _ = np.histogram(values, bins=edges_elem)
    # ヒストグラム(mm単位): 同じ境界を spacing 倍して使う(要素は等方なので比例するだけ)。
    values_mm = values * spacing
    edges_mm = [e * spacing if np.isfinite(e) else np.inf for e in edges_elem]
    hist_mm, _ = np.histogram(values_mm, bins=edges_mm)
    return {
        "element_count": n,
        **fractions,
        **counts,
        "min_elem": float(values.min()),
        "max_elem": float(values.max()),
        "mean_elem": float(values.mean()),
        "median_elem": float(np.median(values)),
        "histogram_elem_bins": [str(edges_elem[i]) + "-" + str(edges_elem[i + 1]) for i in range(len(edges_elem) - 1)],
        "histogram_elem_counts": [int(c) for c in hist_elem],
        "histogram_mm_bins": [f"{edges_mm[i]:.3f}-{edges_mm[i + 1]:.3f}" if np.isfinite(edges_mm[i + 1])
                              else f"{edges_mm[i]:.3f}+" for i in range(len(edges_mm) - 1)],
        "histogram_mm_counts": [int(c) for c in hist_mm],
        "mean_mm": float(values_mm.mean()),
        "median_mm": float(np.median(values_mm)),
    }


def opening_cross_check(material: np.ndarray, size: int) -> dict:
    """`size`×`size`×`size` の立方体構造要素で binary_opening し、残存割合を返す。"""
    structure = np.ones((size, size, size), dtype=bool)
    opened = ndimage.binary_opening(material, structure=structure)
    total = int(material.sum())
    remaining = int(opened.sum())
    return {
        "structure": f"{size}x{size}x{size}",
        "material_before": total,
        "material_after": remaining,
        "removed": total - remaining,
        "removed_fraction": float((total - remaining) / total) if total else None,
    }


# ---------------------------------------------------------------------------
# F. 応力の分位(できれば) — stress.evaluate をラップして von Mises 場を横取りする
# ---------------------------------------------------------------------------
def stress_percentiles_by_thickness(space, table, bundle, material: np.ndarray,
                                    thickness_elem: np.ndarray, thin_threshold: float = 3.0) -> dict:
    """ρ=1固定(`material` をそのまま材料とみなす)で1回だけ解き、局所厚さが `thin_threshold`
    要素未満の要素の von Mises 分位と、材料全体の分位を比べる。探索(`binary_search`)は呼ばない。
    """
    from duocad.topopt import acceptance
    from duocad.topopt import stress as stress_mod

    captured: dict = {}
    true_evaluate = stress_mod.evaluate

    def capturing_evaluate(*args, **kwargs):
        report = true_evaluate(*args, **kwargs)
        captured["report"] = report
        return report

    with patch.object(stress_mod, "evaluate", capturing_evaluate):
        result = acceptance.inspect(
            material, material, space.grid,
            table=table, fixed_faces=space.fixed_faces, load_faces=space.load_faces,
            forces=bundle.forces, preserves=(), excludes=(), target_safety_factor=1.0,
        )
    safety = result["checks"]["safety"]
    if safety.get("status") not in ("pass", "fail") or "report" not in captured:
        return {"error": f"安全率・応力場が計算できなかった: {safety}"}
    von_mises = np.asarray(captured["report"].field.von_mises, dtype=float)
    if von_mises.shape != material.shape:
        return {"error": f"von_mises の形 {von_mises.shape} が material {material.shape} と不一致"}
    all_values = von_mises[material]
    thin_mask = material & (thickness_elem < thin_threshold)
    thin_values = von_mises[thin_mask]
    percentiles = (50, 90, 95, 99, 99.5, 100)
    out = {
        "thin_threshold_elem": thin_threshold,
        "safety_factor": safety.get("safety_factor"),
        "representative_mpa": safety.get("representative_mpa"),
        "all_elements": int(all_values.size),
        "thin_elements": int(thin_values.size),
        "all_percentiles_mpa": {str(p): float(np.percentile(all_values, p)) for p in percentiles} if all_values.size else None,
        "thin_percentiles_mpa": {str(p): float(np.percentile(thin_values, p)) for p in percentiles} if thin_values.size else None,
    }
    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def run(model: Path, spacing: float, target: float, max_evaluations: int, *, do_stress: bool) -> dict:
    out: dict = {"model": str(model), "spacing": spacing, "target_safety_factor": target,
                "max_evaluations": max_evaluations}
    with tempfile.TemporaryDirectory(prefix="topopt-masks-") as tmp:
        masks_path = Path(tmp) / f"masks-{spacing}.npz"
        t0 = time.perf_counter()
        record, space = run_search_capturing_space(model, spacing, target, max_evaluations, masks_path)
        out["search_seconds"] = time.perf_counter() - t0
        out["search_status"] = record.get("search", {}).get("status")
        out["search_evaluations"] = record.get("search", {}).get("evaluations")
        best = record.get("search", {}).get("best")
        out["accepted_material_mm3"] = best.get("material_mm3") if best else None
        out["accepted_safety_factor"] = best.get("safety_factor") if best else None

        with np.load(masks_path) as npz:
            design = np.asarray(npz["design"], dtype=bool)
            best_mask = np.asarray(npz["best"], dtype=bool)

        if best_mask.size == 0 or not best_mask.any():
            out["error"] = "採用マスクが空(探索が feasible な結果を返さなかった)"
            return out

        if best_mask.shape != design.shape:
            out["error"] = f"best({best_mask.shape}) と design({design.shape}) の形が一致しない"
            return out

        keep = np.asarray(space.keep, dtype=bool)
        if keep.shape != best_mask.shape:
            out["keep_separation"] = f"分離できなかった(keep({keep.shape}) と best({best_mask.shape}) の形が不一致)"
        optimized_only = best_mask & ~keep if keep.shape == best_mask.shape else None
        kept_in_accepted = best_mask & keep if keep.shape == best_mask.shape else None

        faces = [best_mask[0, :, :], best_mask[-1, :, :], best_mask[:, 0, :],
                 best_mask[:, -1, :], best_mask[:, :, 0], best_mask[:, :, -1]]
        out["accepted_touches_grid_boundary"] = bool(any(face.any() for face in faces))

        out["accepted_elements"] = int(best_mask.sum())
        out["design_space_elements"] = int(design.sum())
        if keep.shape == best_mask.shape:
            out["kept_elements_in_design_space"] = int(keep.sum())
            out["kept_elements_in_accepted"] = int(kept_in_accepted.sum())
            out["optimizer_created_elements"] = int(optimized_only.sum())

        spacing_mm = float(record["derive"]["grid"]["spacing"])
        out["actual_spacing_mm"] = spacing_mm

        thickness_by_outside = {}
        for outside in ("empty", "material"):
            thickness_by_outside[outside] = local_thickness(best_mask, outside=outside)

        out["local_thickness_boundary_convention_primary"] = "empty"
        out["thickness"] = {}
        for outside, thickness_elem in thickness_by_outside.items():
            out["thickness"][outside] = {
                "whole_accepted_mask": thickness_stats(thickness_elem, best_mask, spacing_mm),
            }
            if optimized_only is not None:
                out["thickness"][outside]["optimizer_created_only"] = thickness_stats(
                    thickness_elem, optimized_only, spacing_mm)
                if kept_in_accepted.any():
                    out["thickness"][outside]["kept_region_only"] = thickness_stats(
                        thickness_elem, kept_in_accepted, spacing_mm)
                else:
                    out["thickness"][outside]["kept_region_only"] = {"element_count": 0,
                                                                      "note": "保持ソリッド由来の要素が採用マスクに0件"}

        out["opening_cross_check"] = {
            "2x2x2": opening_cross_check(best_mask, 2),
            "3x3x3": opening_cross_check(best_mask, 3),
        }

        if do_stress:
            from duocad import optimize_run
            from duocad.session import Session

            t1 = time.perf_counter()
            # `_map_boundary_conditions` には `bundle`(拘束・荷重の条件一式)が要る。`verify()`
            # の record には JSON化できる要約しか入っていないので、同じモデルから
            # `Session` 経由で作り直す(`verify_hanger_fix.verify()` の冒頭と同じ手順)。
            session = Session(None, 0.2, models_dir=model.resolve().parent)
            session.open_document(model.name)
            bundle = optimize_run.build_conditions(session.document)
            table, mapping = optimize_run._map_boundary_conditions(space, bundle)
            if mapping["status"] != "pass":
                out["stress_percentiles"] = {"error": f"確定表が不成立: {mapping['status']}"}
            else:
                out["stress_percentiles"] = stress_percentiles_by_thickness(
                    space, table, bundle, best_mask, thickness_by_outside["empty"])
            out["stress_seconds"] = time.perf_counter() - t1
        else:
            out["stress_percentiles"] = {"skipped": "「--skip-stress」が指定された"}

    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="採用マスクの局所厚さを実測する(節F)")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--spacing", type=float, default=4.8)
    parser.add_argument("--target", type=float, default=2.0)
    parser.add_argument("--max-evaluations", type=int, default=60)
    parser.add_argument("--skip-stress", action="store_true", help="できれば節(応力分位)を省く")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    started = time.perf_counter()
    result = run(args.model, args.spacing, args.target, args.max_evaluations,
                do_stress=not args.skip_stress)
    result["total_seconds"] = time.perf_counter() - started

    print(f"\n合計 {result['total_seconds']:.1f} 秒", flush=True)
    if args.out:
        _record.write(args.out, result, default=float)
        print(f"書き出し: {args.out}")
    else:
        print("MEASURED " + json.dumps(result, ensure_ascii=False, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
