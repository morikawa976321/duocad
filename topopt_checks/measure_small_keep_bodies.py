"""小さい塊状の保持ソリッド(立方体・円柱ボス)で警告帯(1〜7要素)を踏ませて測る(C・D、可能ならE)。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_small_keep_bodies.py
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_small_keep_bodies.py --section c
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_small_keep_bodies.py --section d
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_small_keep_bodies.py --out C:\\tmp\\small.json

タスク `20260918-1330-topopt-measure-small-keep-bodies`(taskbox)の測定専用スクリプト。
**判定はしない**(`MIN_REGION_ELEMENTS` が妥当かどうかは書かない)。結果は
`docs/TOPOPT_THICKNESS.md` の節C・D・Eに転記する。

出力は既定で標準出力の JSON だけ(`--out` を渡さない限り `output/` を含めどこにもファイルを
残さない)。

## C(小さい塊状の保持ソリッドでの「個数」判定)

前タスクB節の板と同じ作り(離れた場所へ固定パッド・荷重パッドを置き、パッドは常に閾値
`MIN_REGION_ELEMENTS` を大きく超える大きさにする)を、板ではなく**立方体(1辺s)**と
**円柱ボス(直径s・高さs)**に差し替えて繰り返す。前タスクの
`topopt_checks/measure_thickness_layers.py`(既存ファイル、書き換えない)から
`PAD_SIZE`/`PAD_GAP_X`/`verify_against_product`/`verdict_of` を import して再利用する。

## D(警告帯のとき条件が何節点に付くか)

同じ題材で、荷重パッドを置かず**対象自身の面**(z軸方向、シフト後の上面)に荷重をかける
構成をもう1通り作る。`optimize_run._map_boundary_conditions` が通る確定表で、拘束面・荷重面が
何節点に対応付いたかを記録する。

## E(できれば: 節点数が少ない構成での応力の跳ね)

D で要素数1〜7(警告帯)かつ確定表が成立したケースから1〜2個選び、**ρ=1固定**
(`acceptance.inspect` に `material=before=space.mask` を渡す — 探索はしない、1回だけ解く)で
代表応力[MPa]を測り、同じ物理配置を要素サイズだけ細かく(spacing/3)した参照解と比べる。
探索(`binary_search`)は呼ばない。
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import numpy as np

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)

# 前タスクのスクリプト(既存・無改変)を import して再利用する。同じ topopt_checks/
# ディレクトリにあるので、python がスクリプト自身のディレクトリを sys.path へ足す既定の
# 挙動だけで解決できる(パッケージ化はしていない)。
import measure_thickness_layers as mtl

RESULTS: dict = {}

S_MM = 5.0  # 前タスクB節の板厚(5mm)と揃え、同じ軸で比較できるようにする。
RATIOS_DEFAULT = [0.5, 0.8, 1.0, 1.2, 1.5, 1.8, 2.0, 2.5, 3.0]
SHIFT_FRACTIONS = (0.0, 0.25, 0.5)  # spacing に対する比率(前タスクB節と同じ0・1/4・2/4)。
SHAPES_DEFAULT = ["cube", "cylinder"]


# ---------------------------------------------------------------------------
# 題材とケース構築
# ---------------------------------------------------------------------------
def small_body_shape(kind: str, s: float):
    from build123d import Box, Cylinder

    if kind == "cube":
        return Box(s, s, s)
    if kind == "cylinder":
        return Cylinder(radius=s / 2.0, height=s)
    raise ValueError(f"未知の題材: {kind}")


def build_small_case(*, kind: str, s: float, spacing: float, z_shift: float, load_on_body: bool):
    """小さい保持ソリッド(立方体/円柱ボス)+ 固定パッド(+荷重パッド)の最小の文書を作る。

    位置ずらしはz軸のみ(立方体・円柱ボスはx/y/z対称に近い形状なので、1軸のずらしでも
    格子との位置合わせで境界がどれだけ振れるかの代表値が取れると考えた。前タスクB節と
    同じ式 `spacing * {0, 1/4, 2/4}`)。

    `load_on_body=False`(C節): 前タスクB節と同じく固定・荷重の両パッドを離れた場所に置き、
    測っているのは対象(`obj`)の行だけ(パッドは常に `MIN_REGION_ELEMENTS` を大きく超える)。
    `load_on_body=True`(D節): 荷重パッドを置かず、対象自身の上面(z+)に荷重をかける
    (`obj` の roles に `load` を足す)。
    """
    from build123d import Box, Pos

    from duocad import optimize_run
    from duocad.document import Body, Document
    from duocad.topopt import anchor as an
    from duocad.topopt import design_space as ds
    from duocad.topopt import voxelize as vx

    obj = Pos(0, 0, z_shift) * small_body_shape(kind, s)
    fixed_pad = Pos(-mtl.PAD_GAP_X, 0, 0) * Box(mtl.PAD_SIZE, mtl.PAD_SIZE, mtl.PAD_SIZE)
    load_pad = None if load_on_body else Pos(mtl.PAD_GAP_X, 0, 0) * Box(
        mtl.PAD_SIZE, mtl.PAD_SIZE, mtl.PAD_SIZE
    )
    keep = obj + fixed_pad if load_on_body else obj + fixed_pad + load_pad

    soup = vx.triangles_from_shape(vx._unwrap(keep), deviation=0.2)
    fixed_hits = an.faces_matching(
        soup, kind="plane", normal=(-1, 0, 0),
        near_point=(-mtl.PAD_GAP_X - mtl.PAD_SIZE / 2, 0, 0),
        normal_tol=1e-6, near_tol=1e-4,
    )
    if load_on_body:
        top_z = z_shift + s / 2.0
        load_hits = an.faces_matching(
            soup, kind="plane", normal=(0, 0, 1), near_point=(0, 0, top_z),
            normal_tol=1e-6, near_tol=1e-4,
        )
    else:
        load_hits = an.faces_matching(
            soup, kind="plane", normal=(1, 0, 0),
            near_point=(mtl.PAD_GAP_X + mtl.PAD_SIZE / 2, 0, 0),
            normal_tol=1e-6, near_tol=1e-4,
        )
    if len(fixed_hits) != 1 or len(load_hits) != 1:
        raise RuntimeError(
            f"面を一意に選べなかった(kind={kind} load_on_body={load_on_body} "
            f"fixed={fixed_hits} load={load_hits})"
        )

    doc = Document()
    doc.add_body(Body(id="fixed_pad", name="fixed_pad", kind="solid", shape=vx._unwrap(fixed_pad)))
    doc.add_body(Body(id="obj", name="obj", kind="solid", shape=vx._unwrap(obj)))
    bodies = [ds.condition_body("fixed_pad", fixed_pad, roles=("keep", "fixed"))]
    if load_on_body:
        bodies.append(ds.condition_body("obj", obj, roles=("keep", "load")))
        keep_body_ids = ["fixed_pad", "obj"]
        anchor_ids = ["fixed_pad", "obj"]
        forces = ((0.0, 0.0, -1.0),)
    else:
        doc.add_body(Body(id="load_pad", name="load_pad", kind="solid", shape=vx._unwrap(load_pad)))
        bodies.append(ds.condition_body("load_pad", load_pad, roles=("keep", "load")))
        bodies.append(ds.condition_body("obj", obj, roles=("keep",)))
        keep_body_ids = ["fixed_pad", "load_pad", "obj"]
        anchor_ids = ["fixed_pad", "load_pad"]
        forces = ()

    conditions = ds.Conditions(
        keep=keep, fixed_faces=fixed_hits[0], load_faces=load_hits[0], bodies=tuple(bodies)
    )
    space = ds.derive(conditions, spacing=spacing)

    bundle = optimize_run.ConditionBundle(
        conditions=conditions,
        anchor_body_ids=anchor_ids,
        face_offsets={},
        fixed_faces=conditions.fixed_faces,
        load_faces=conditions.load_faces,
        keep_body_ids=keep_body_ids,
        exclude_body_ids=[],
        forces=forces,
        load_n=1.0,
    )
    return doc, bundle, space


def measure_body_region(doc, space, body_id: str = "obj") -> dict:
    """対象の occupancy を測り、要素数(製品と同じ計算)と各軸の層数を出す(前タスクB節の
    `measure_plate_region` を、厚み軸(z)だけでなくx/y/zの3軸へ一般化したもの)。
    """
    from duocad.topopt import voxelize as vx

    body = doc.get_body(body_id)
    deviation = float(
        space.stats.get("deviation") or (space.grid.spacing * vx.DEFAULT_DEVIATION_RATIO)
    )
    soup = vx.triangles_from_shape(body.shape, deviation=deviation)
    try:
        mask, _stats = vx.occupancy(soup, space.grid)
    except ValueError:
        mask = np.zeros(space.grid.shape, dtype=bool)
    design = np.asarray(space.mask, dtype=bool)
    grid_elements = int(np.count_nonzero(mask))
    inside_design = int(np.count_nonzero(mask & design))
    return {
        "grid_elements": grid_elements,
        "inside_design": inside_design,
        "layers_x": int(np.count_nonzero(np.any(mask, axis=(1, 2)))),
        "layers_y": int(np.count_nonzero(np.any(mask, axis=(0, 2)))),
        "layers_z": int(np.count_nonzero(np.any(mask, axis=(0, 1)))),
        "spacing_mm": float(space.grid.spacing),
    }


# ---------------------------------------------------------------------------
# C. 小さい塊状の保持ソリッドでの「個数」判定
# ---------------------------------------------------------------------------
def run_section_c(s: float, ratios: list[float], shapes: list[str]) -> dict:
    from duocad import optimize_run

    print(f"\n[C] 小さい塊状の保持ソリッドでの「個数」判定(s={s}mm)", flush=True)
    threshold = optimize_run.MIN_REGION_ELEMENTS
    rows = []
    for kind in shapes:
        for ratio in ratios:
            spacing = s / ratio
            shift_rows = []
            for frac in SHIFT_FRACTIONS:
                shift = frac * spacing
                t0 = time.perf_counter()
                error = None
                measured = None
                try:
                    doc, bundle, space = build_small_case(
                        kind=kind, s=s, spacing=spacing, z_shift=shift, load_on_body=False
                    )
                    measured = measure_body_region(doc, space, "obj")
                    mtl.verify_against_product(doc, bundle, space, "obj", measured)
                except Exception as exc:  # noqa: BLE001  測れなかったことをそのまま記録する
                    error = f"{type(exc).__name__}: {exc}"
                seconds = time.perf_counter() - t0
                row = {
                    "kind": kind,
                    "ratio_s_over_spacing": ratio,
                    "requested_spacing_mm": spacing,
                    "z_shift_mm": shift,
                    "z_shift_over_spacing": frac,
                    "seconds": seconds,
                    "error": error,
                }
                if measured is not None:
                    row.update(measured)
                    row["verdict"] = mtl.verdict_of(measured["inside_design"], threshold)
                    print(
                        f"    {kind:8s} 比={ratio:>4.2f} spacing={measured['spacing_mm']:.4f}mm "
                        f"shift={frac:.2f}*sp  個数(格子/塊内)={measured['grid_elements']}/"
                        f"{measured['inside_design']}  層(x/y/z)={measured['layers_x']}/"
                        f"{measured['layers_y']}/{measured['layers_z']}  判定={row['verdict']}  "
                        f"({seconds:.2f}秒)",
                        flush=True,
                    )
                else:
                    print(f"    {kind:8s} 比={ratio:>4.2f} shift={frac:.2f}*sp  測定失敗: {error}", flush=True)
                shift_rows.append(row)
            rows.append(
                {"kind": kind, "ratio_s_over_spacing": ratio, "requested_spacing_mm": spacing, "shifts": shift_rows}
            )
    return {"s_mm": s, "threshold": threshold, "rows": rows}


# ---------------------------------------------------------------------------
# D. 警告帯のとき条件が何節点に付くか
# ---------------------------------------------------------------------------
def run_section_d(s: float, ratios: list[float], shapes: list[str]) -> dict:
    from duocad import optimize_run

    print(f"\n[D] 荷重面を対象自身に載せたときの節点対応(s={s}mm)", flush=True)
    threshold = optimize_run.MIN_REGION_ELEMENTS
    rows = []
    for kind in shapes:
        for ratio in ratios:
            spacing = s / ratio
            shift_rows = []
            for frac in SHIFT_FRACTIONS:
                shift = frac * spacing
                t0 = time.perf_counter()
                error = None
                measured = None
                mapping_status = None
                fixed_nodes = load_nodes = None
                fixed_reasons = load_reasons = None
                try:
                    doc, bundle, space = build_small_case(
                        kind=kind, s=s, spacing=spacing, z_shift=shift, load_on_body=True
                    )
                    measured = measure_body_region(doc, space, "obj")
                    mtl.verify_against_product(doc, bundle, space, "obj", measured)
                    _table, mapping = optimize_run._map_boundary_conditions(space, bundle)
                    mapping_status = mapping["status"]
                    for face_row in mapping["faces"]:
                        if face_row["side"] == "fixed":
                            fixed_nodes = face_row["nodes"]
                            fixed_reasons = face_row["reasons"]
                        elif face_row["side"] == "load":
                            load_nodes = face_row["nodes"]
                            load_reasons = face_row["reasons"]
                except Exception as exc:  # noqa: BLE001  測れなかったことをそのまま記録する
                    error = f"{type(exc).__name__}: {exc}"
                seconds = time.perf_counter() - t0
                row = {
                    "kind": kind,
                    "ratio_s_over_spacing": ratio,
                    "requested_spacing_mm": spacing,
                    "z_shift_mm": shift,
                    "z_shift_over_spacing": frac,
                    "seconds": seconds,
                    "error": error,
                    "mapping_status": mapping_status,
                    "fixed_nodes": fixed_nodes,
                    "load_nodes": load_nodes,
                    "fixed_reasons": fixed_reasons,
                    "load_reasons": load_reasons,
                }
                if measured is not None:
                    row.update(measured)
                    row["verdict"] = mtl.verdict_of(measured["inside_design"], threshold)
                    print(
                        f"    {kind:8s} 比={ratio:>4.2f} spacing={measured['spacing_mm']:.4f}mm "
                        f"shift={frac:.2f}*sp  塊内要素={measured['inside_design']}  "
                        f"判定={row['verdict']}  節点(固定/荷重)={fixed_nodes}/{load_nodes}  "
                        f"確定表={mapping_status}  ({seconds:.2f}秒)",
                        flush=True,
                    )
                else:
                    print(f"    {kind:8s} 比={ratio:>4.2f} shift={frac:.2f}*sp  測定失敗: {error}", flush=True)
                shift_rows.append(row)
            rows.append(
                {"kind": kind, "ratio_s_over_spacing": ratio, "requested_spacing_mm": spacing, "shifts": shift_rows}
            )
    return {"s_mm": s, "threshold": threshold, "rows": rows}


# ---------------------------------------------------------------------------
# E. できれば: 節点数が少ない構成での応力の跳ね(ρ=1固定・1回だけ解く)
# ---------------------------------------------------------------------------
def measure_dense_solve(bundle, space) -> dict:
    """ρ=1固定(`space.mask` をそのまま材料とみなす)で1回だけ解き、代表応力[MPa]を返す。

    探索(`binary_search`)は呼ばない。`acceptance.inspect` は `material`/`before` を渡す
    製品の入口そのもの(段階2の§5)なので、ここでも `material=before=space.mask` として渡す。
    """
    from duocad import optimize_run
    from duocad.topopt import acceptance

    table, mapping = optimize_run._map_boundary_conditions(space, bundle)
    if mapping["status"] != "pass":
        return {"error": f"確定表が不成立: {mapping['status']}", "mapping_status": mapping["status"]}
    material = np.asarray(space.mask, dtype=bool)
    report = acceptance.inspect(
        material, material, space.grid,
        table=table, fixed_faces=space.fixed_faces, load_faces=space.load_faces,
        forces=bundle.forces, preserves=(), excludes=(), target_safety_factor=1.0,
    )
    safety = report["checks"]["safety"]
    if safety.get("status") not in ("pass", "fail"):
        return {"error": f"安全率が計算できなかった: {safety}"}
    return {
        "representative_mpa": safety["representative_mpa"],
        "safety_factor": safety["safety_factor"],
        "material_elements": int(material.sum()),
        "grid_shape": [int(v) for v in space.grid.shape],
    }


def run_section_e(section_d: dict, *, fine_divisor: float = 3.0, max_cases: int = 2) -> dict:
    """Dの結果から警告帯(1〜7要素)かつ確定表が成立したケースを選び、ρ=1固定で1回だけ解いて
    参照解(要素サイズを `fine_divisor` 分の1にした同じ物理配置)と比べる。
    """
    print(f"\n[E] 警告帯ケースでのρ=1応力(参照は要素サイズ1/{fine_divisor:g})", flush=True)
    candidates = []
    for kind_row in section_d["rows"]:
        for shift_row in kind_row["shifts"]:
            if (
                shift_row.get("error") is None
                and isinstance(shift_row.get("verdict"), str)
                and shift_row["verdict"].startswith("warn")
                and shift_row.get("mapping_status") == "pass"
            ):
                candidates.append(shift_row)
    if not candidates:
        return {"skipped": "Dに確定表成立の警告帯(1〜7要素)ケースが1件も無かった", "cases": []}

    # 節点数が少ないケースほど答えたい問い(1〜2節点に集中するか)に近いので優先する。
    candidates.sort(key=lambda r: (r.get("load_nodes") if r.get("load_nodes") is not None else 1_000_000))
    chosen = candidates[:max_cases]

    cases = []
    for row in chosen:
        kind = row["kind"]
        s = S_MM
        spacing_coarse = row["requested_spacing_mm"]
        z_shift = row["z_shift_mm"]
        t0 = time.perf_counter()
        result: dict = {
            "kind": kind,
            "ratio_s_over_spacing": row["ratio_s_over_spacing"],
            "z_shift_over_spacing": row["z_shift_over_spacing"],
            "elements_inside_design": row["inside_design"],
            "load_nodes": row["load_nodes"],
            "fixed_nodes": row["fixed_nodes"],
        }
        try:
            doc, bundle, space = build_small_case(
                kind=kind, s=s, spacing=spacing_coarse, z_shift=z_shift, load_on_body=True
            )
            coarse = measure_dense_solve(bundle, space)
            result["coarse"] = coarse
            spacing_fine = spacing_coarse / fine_divisor
            doc_f, bundle_f, space_f = build_small_case(
                kind=kind, s=s, spacing=spacing_fine, z_shift=z_shift, load_on_body=True
            )
            fine = measure_dense_solve(bundle_f, space_f)
            result["fine"] = fine
            if "representative_mpa" in coarse and "representative_mpa" in fine and fine["representative_mpa"]:
                result["coarse_over_fine_ratio"] = coarse["representative_mpa"] / fine["representative_mpa"]
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{type(exc).__name__}: {exc}"
        result["seconds"] = time.perf_counter() - t0
        print(
            f"    {kind:8s} 比={result['ratio_s_over_spacing']:.2f} shift={result['z_shift_over_spacing']:.2f}*sp "
            f"要素={result['elements_inside_design']} 荷重節点={result['load_nodes']}  "
            f"→ {result.get('coarse', {}).get('representative_mpa')} MPa (粗) / "
            f"{result.get('fine', {}).get('representative_mpa')} MPa (細) "
            f"比={result.get('coarse_over_fine_ratio')}  ({result['seconds']:.1f}秒)"
            if "error" not in result
            else f"    {kind:8s} 測定失敗: {result['error']}",
            flush=True,
        )
        cases.append(result)
    return {"fine_divisor": fine_divisor, "cases": cases}


def main() -> int:
    parser = argparse.ArgumentParser(description="小さい保持ソリッドで警告帯(1〜7要素)を踏ませて測る")
    parser.add_argument("--section", choices=["c", "d", "e", "all"], default="all")
    parser.add_argument("--s", type=float, default=S_MM, help="対象の大きさ[mm](1辺 or 直径・高さ)")
    parser.add_argument("--ratios", type=float, nargs="+", default=RATIOS_DEFAULT)
    parser.add_argument("--shapes", type=str, nargs="+", default=SHAPES_DEFAULT, choices=["cube", "cylinder"])
    parser.add_argument("--out", type=str, default=None, help="結果JSONの書き出し先(既定は書き出さない)")
    args = parser.parse_args()

    started = time.perf_counter()
    if args.section in ("c", "all"):
        RESULTS["section_c"] = run_section_c(args.s, args.ratios, args.shapes)
    if args.section in ("d", "all"):
        RESULTS["section_d"] = run_section_d(args.s, args.ratios, args.shapes)
    if args.section in ("e", "all"):
        if "section_d" not in RESULTS:
            RESULTS["section_d"] = run_section_d(args.s, args.ratios, args.shapes)
        RESULTS["section_e"] = run_section_e(RESULTS["section_d"])
    RESULTS["total_seconds"] = time.perf_counter() - started

    print("\n" + "=" * 72)
    print(f"合計 {RESULTS['total_seconds']:.1f} 秒")
    if args.out:
        from pathlib import Path

        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(RESULTS, ensure_ascii=False, indent=2, default=float), encoding="utf-8"
        )
        print(f"書き出し: {out_path}")
    else:
        print("MEASURED " + json.dumps(RESULTS, ensure_ascii=False, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
