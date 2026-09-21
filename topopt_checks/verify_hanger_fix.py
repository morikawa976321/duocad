"""hanger での実機確認(`docs/FIX_BINARY_SEARCH_INITIALIZATION.md` §9)を1回分記録する。

    .\\.venv\\Scripts\\python.exe topopt_checks\\verify_hanger_fix.py --model models\\hanger_bar_attachment.json --spacing 4.8 --max-evaluations 12 --out output\\hanger_fix_verification\\spacing-4.8.json

記録するもの: モデルの内容ハッシュとコード版、導出出力のセル数・ハッシュ(基準値との一致は
記録するだけで合わせない)、幾何の表と探索用の確定表の診断の差、確定表の検証(荷重保存・
作用位置)、初期全材料マスクの6条件(実FEM)、探索の候補生成数・評価数・保持数・メモリ実測、
評価条件ハッシュの不変、導出回数1・候補中メッシュ化0回、採用マスクの再検査。
`--skip-search` で探索を省く(2.4mm のように1評価が重い解像度で入口と初期評価だけを見るとき)。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401
import _record
import numpy as np

from duocad import optimize_run
from duocad.session import Session
from duocad.topopt import acceptance, anchor, binary_search, design_space, mesh

REPO = Path(__file__).resolve().parents[1]
#: 導出出力の基準値(`docs/FIX_BINARY_SEARCH_INITIALIZATION.md` §9)。合わせるために導出や基準値を書き換えない。
EXPECTED_CELLS = {4.8: 47_232, 2.4: 367_390}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()
    except Exception as error:  # pragma: no cover - 記録の補助
        return f"unavailable: {type(error).__name__}"


def _face_rows(table, material, faces):
    live = acceptance.active_nodes(material).ravel()
    rows = []
    for side, group in faces:
        for face in group:
            a = table[int(face)]
            lost = int((~live[a.node_ids]).sum()) if a.node_count else 0
            rows.append(dict(condition=f"{side}_face_{int(face)}", nodes=int(a.node_count), uncovered_nodes=lost,
                             assigned_area_mm2=float(a.total_area), leak_ratio=float(a.leak_ratio),
                             unmapped_samples=int(a.unmapped_samples)))
    return rows


def _compact_mapping(mapping: dict) -> dict:
    out = json.loads(json.dumps(mapping))
    for row in out.get("faces", []):
        row["unmapped_positions"] = row.get("unmapped_positions", [])[:5]
    return out


def _safety_summary(report: dict) -> dict:
    safety = report["checks"]["safety"]
    return dict(status=report["status"], checks={k: v["status"] for k, v in report["checks"].items()},
                safety_factor=safety.get("safety_factor"), representative_mpa=safety.get("representative_mpa"),
                raw_max_mpa=safety.get("raw_max_mpa"), relative_residual=safety.get("relative_residual"),
                max_displacement_mm=safety.get("max_displacement_mm"), solver=safety.get("solver"),
                reason=safety.get("reason"), error=safety.get("error"),
                material_mm3=report["volumes"]["material_mm3"], material_sha256=report["material_sha256"],
                conditions=report["checks"]["conditions"])


def _save_masks(path: Path, space, table, best_mask) -> None:
    """設計空間と採用マスク(ボクセルのまま)、条件面の節点座標を npz に残す(撮影・見比べ用)。"""
    def nodes(faces):
        ids = [table.node_ids([int(f)]) for f in faces if int(f) not in set(table.empty_faces())]
        if not ids:
            return np.zeros((0, 3))
        return table.node_grid.positions_of(table.node_grid.indices_of_ids(np.unique(np.concatenate(ids))))

    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, design=space.mask, best=(np.zeros(0, bool) if best_mask is None else best_mask),
                        origin=np.asarray(space.grid.origin, dtype=float), spacing=float(space.grid.spacing),
                        fixed_nodes=nodes(space.fixed_faces), load_nodes=nodes(space.load_faces))


def verify(model: Path, spacing: float, target: float, max_evaluations: int, *, skip_search: bool,
           step_exponent: float = binary_search.DEFAULT_STEP_EXPONENT,
           max_step_ratio: float = binary_search.DEFAULT_MAX_STEP_RATIO,
           single_cell_patience: int | None = binary_search.DEFAULT_SINGLE_CELL_PATIENCE,
           target_tolerance: float | None = binary_search.DEFAULT_TARGET_TOLERANCE, trace_memory: bool = True,
           save_masks: Path | None = None) -> dict:
    session = Session(None, 0.2, models_dir=model.resolve().parent)
    session.open_document(model.name)
    document = session.document
    record: dict = dict(
        model=str(model), model_sha256=_sha256(model), git_head=_git("rev-parse", "HEAD"),
        git_status_short=_git("status", "--short", "--", "duocad", "tests", "topopt_checks"),
        code_sha256={name: _sha256(REPO / "duocad" / "topopt" / f"{name}.py")
                     for name in ("anchor", "binary_search", "acceptance", "design_space")},
        requested_spacing=spacing, target_safety_factor=target, max_evaluations=max_evaluations,
        step_exponent=step_exponent, max_step_ratio=max_step_ratio,
        single_cell_patience=single_cell_patience, target_tolerance=target_tolerance,
    )
    bundle = optimize_run.build_conditions(document)

    derive_calls = {"n": 0}
    original_derive = design_space.derive

    def counted_derive(conditions, **kwargs):
        derive_calls["n"] += 1
        return original_derive(conditions, **kwargs)

    mesh_calls = {"n": 0}

    def counted_mesh(*_args, **_kwargs):
        mesh_calls["n"] += 1
        raise AssertionError("候補評価中にメッシュ化が呼ばれた")

    with patch.object(design_space, "derive", counted_derive), \
            patch.object(mesh, "build_surface_from_mask", counted_mesh):
        started = time.perf_counter()
        space = design_space.derive(bundle.conditions, spacing=spacing)
        record["derive"] = dict(seconds=time.perf_counter() - started, grid=space.grid.as_dict(),
                                cells=int(space.element_count), mask_hash=space.mask_hash,
                                expected_cells=EXPECTED_CELLS.get(spacing),
                                matches_expected=(EXPECTED_CELLS.get(spacing) == int(space.element_count)),
                                connectivity_dropped=int(space.stats["connectivity"].get("dropped_elements", 0)))
        faces = (("fixed", space.fixed_faces), ("load", space.load_faces))
        record["geometry_table"] = dict(stats={k: v for k, v in space.table.stats.items() if k != "empty_faces"},
                                        faces=_face_rows(space.table, space.mask, faces))

        started = time.perf_counter()
        table, mapping = optimize_run._map_boundary_conditions(space, bundle)
        record["search_table"] = dict(seconds=time.perf_counter() - started,
                                      stats={k: v for k, v in table.stats.items() if k != "empty_faces"},
                                      faces=_face_rows(table, space.mask, faces), mapping=_compact_mapping(mapping))
        record["anchor_check"] = optimize_run._check_anchor_centroids(document, space, table=table)
        opts = optimize_run.OptimizeOptions(target_safety_factor=target, max_evaluations=max_evaluations,
                                            search_target_factor=1.0)
        conditions = optimize_run._build_search_conditions(document, space, bundle, opts, table=table, mapping=mapping)
        identifier_before = binary_search.evaluation_identifier(conditions)
        record["entry"] = dict(identifier=identifier_before,
                               uncovered=binary_search.uncovered_condition_nodes(space.mask, conditions),
                               empty_faces=[int(f) for f in table.empty_faces()])
        if mapping["status"] != "pass":
            record["result"] = dict(state="boundary_mapping_failed", warning=anchor.format_mapping_warning(mapping))
            return record

        started = time.perf_counter()
        survivors, island_diag = binary_search.resolve_islands(space.mask, conditions)
        full = acceptance.inspect(space.mask, space.mask, space.grid, **conditions.inspect_kwargs())
        record["initial_full_material"] = dict(seconds=time.perf_counter() - started, islands=island_diag,
                                               survivors=len(survivors), **_safety_summary(full))
        if skip_search:
            if save_masks is not None:
                _save_masks(save_masks, space, table, None)
            record["search"] = dict(skipped=True)
            record["invariants"] = dict(derive_calls=derive_calls["n"], mesh_calls=mesh_calls["n"])
            return record

        inspect_calls = {"n": 0}
        original_inspect = acceptance.inspect

        def counted_inspect(*args, **kwargs):
            inspect_calls["n"] += 1
            return original_inspect(*args, **kwargs)

        if trace_memory:
            tracemalloc.start()
        started = time.perf_counter()
        with patch.object(acceptance, "inspect", counted_inspect):
            result = binary_search.find_best_feasible(space.mask, conditions, max_evaluations=max_evaluations,
                                                      step_exponent=step_exponent, max_step_ratio=max_step_ratio,
                                                      single_cell_patience=single_cell_patience,
                                                      target_tolerance=target_tolerance)
        seconds = time.perf_counter() - started
        peak = 0
        if trace_memory:
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        if save_masks is not None:
            _save_masks(save_masks, space, table,
                        None if result.best_feasible is None else result.best_feasible.mask)
        identifier_after = binary_search.evaluation_identifier(conditions)
        history = [dict(index=t.index, parent=t.parent, operation=t.operation, outcome=t.outcome,
                        material_mm3=t.material_mm3, safety_factor=t.safety_factor, checks=t.checks, note=t.note)
                   for t in result.history]
        best = None
        if result.best_feasible is not None:
            recheck = original_inspect(result.best_feasible.mask, result.best_feasible.before_mask, space.grid,
                                       **conditions.inspect_kwargs(bbox_cache=None))
            best = dict(trial_index=result.best_feasible.trial_index, mask_hash=result.best_feasible.mask_hash,
                        material_mm3=result.best_feasible.material_mm3, safety_factor=result.best_feasible.safety_factor,
                        cells=int(result.best_feasible.mask.sum()), mask_read_only=(not result.best_feasible.mask.flags.writeable),
                        recheck=_safety_summary(recheck),
                        recheck_hash_matches=(recheck["material_sha256"] == result.best_feasible.mask_hash))
        record["search"] = dict(
            status=result.status, termination=result.termination, evaluations=result.evaluations,
            inspect_calls=inspect_calls["n"], seconds=seconds, seconds_per_evaluation=seconds / max(1, result.evaluations),
            diagnostics=result.diagnostics, mask_bytes=int(space.mask.nbytes),
            traced_peak_bytes=int(peak), traced_peak_in_masks=float(peak / space.mask.nbytes),
            history=history, best=best,
        )
        record["invariants"] = dict(derive_calls=derive_calls["n"], mesh_calls=mesh_calls["n"],
                                    identifier_unchanged=(identifier_before == identifier_after),
                                    result_identifier_matches=(result.conditions_identifier == identifier_before))
    return record


def _patience(text: str) -> int | None:
    if text.lower() in ("unlimited", "none"):
        return None
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("1 以上の整数か unlimited")
    return value


def _tolerance(text: str) -> float | None:
    return None if text.lower() in ("off", "none") else float(text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--spacing", type=float, required=True)
    parser.add_argument("--target", type=float, default=2.0)
    parser.add_argument("--max-evaluations", type=int, default=12)
    parser.add_argument("--skip-search", action="store_true")
    parser.add_argument("--step-exponent", type=float, default=binary_search.DEFAULT_STEP_EXPONENT,
                        help="段階4の削減割合の指数 θ(inf で --max-step-ratio の固定割合)")
    parser.add_argument("--max-step-ratio", type=float, default=binary_search.DEFAULT_MAX_STEP_RATIO)
    parser.add_argument("--no-memory-trace", action="store_true",
                        help="tracemalloc を使わない(numpy の確保を遅くするので、所要秒を測るときに付ける。メモリ実測は0になる)")
    parser.add_argument("--single-cell-patience", type=_patience, default=binary_search.DEFAULT_SINGLE_CELL_PATIENCE,
                        help="段階4で、削ったセルが1つで安全率が落ちたセルを外して続けるのを何セルで打ち切るか(整数。unlimited は順位が尽きるまで)")
    parser.add_argument("--target-tolerance", type=_tolerance, default=binary_search.DEFAULT_TARGET_TOLERANCE,
                        help="段階4で、合格形状の安全率が 目標×(1+これ) 以下になったら収束とみなす(off は見ない)")
    parser.add_argument("--save-masks", type=Path, default=None,
                        help="設計空間・採用マスク・条件面の節点座標を npz に書く(撮影用)")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    record = verify(args.model, args.spacing, args.target, args.max_evaluations, skip_search=args.skip_search,
                    step_exponent=args.step_exponent, max_step_ratio=args.max_step_ratio,
                    single_cell_patience=args.single_cell_patience, target_tolerance=args.target_tolerance,
                    trace_memory=not args.no_memory_trace,
                    save_masks=args.save_masks)
    _record.write(args.out, record, default=str)
    summary = {k: record.get(k) for k in ("derive", "entry", "invariants")}
    summary["search_table"] = record.get("search_table", {}).get("faces")
    summary["initial"] = {k: v for k, v in record.get("initial_full_material", {}).items() if k != "conditions"}
    search = record.get("search", {})
    summary["search"] = {k: v for k, v in search.items() if k not in ("history", "diagnostics")}
    summary["search_diagnostics"] = search.get("diagnostics")
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    print(f"書き出し: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
