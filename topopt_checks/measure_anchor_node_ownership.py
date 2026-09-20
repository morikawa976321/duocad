"""確定表(`anchor.build_anchors`)が拾う節点のうち、その面のボディ由来の節点が何%かを実測する。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_anchor_node_ownership.py
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_anchor_node_ownership.py --skip-synthetic-fine
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_anchor_node_ownership.py --out C:\\tmp\\ownership.json

タスク `20260918-1913-topopt-measure-anchor-node-ownership`(taskbox)の測定専用スクリプト。
前提タスク `20260918-1718-topopt-measure-member-thickness` の契約(`--out` を渡さない限り
ファイルを書かない・`trace.DEFAULT_ROOT` を一時フォルダへ向ける・`output/` に何も残さない)を
踏襲する。**判定はしない**(節点を「そのボディ由来」に限るかどうか・門の許容を変えるかどうかは
書かない)。結果は `docs/TOPOPT.md`「CADの面 → 格子の節点(anchor.py)」節に転記する。

## 何を測るか

`optimize_run._map_boundary_conditions` が作る確定表は、
`anchor.build_anchors(space.soup, space.grid, allowed_nodes=acceptance.active_nodes(space.mask))`
で条件面ごとに「設計空間の材料に接する節点」を拾う。設計空間(`space.mask`)は保持ボディの
凸包+余白で、**条件面を持つボディ自身の占有より広い** —— したがって拾った節点の一部は、
その面を持つボディの材料ではなく、設計空間の他の場所(最適化で消えうる一般材料)の節点でありうる。

「そのボディ由来」の判定は `optimize_run._region_representation` と**同じ式**:
`vx.triangles_from_shape(body.shape, deviation) → vx.occupancy(soup, grid)` で得た
そのボディの占有 ∩ `space.mask` → `anchor.active_nodes`(製品関数を呼ぶだけで計算式は変えない)。

## 制限したときの予行

`anchor.build_anchors` は `allowed_nodes` を1枚の配列でしか受けないので、面ごとに
`allowed_nodes = そのボディ由来の節点マスク` だけで**全面・既定の半径**(`faces=` は絞らない
——絞ると稜線の競争相手が減り、絞らずに作った表とは別の節点集合になる。
`design_space.derive` の docstring と同じ注意)で `build_anchors` をもう一度呼び、
`table[face]` だけを取り出して面ごとに集める。`anchor.AnchorTable` はただの dataclass なので、
面ごとに取り出した `FaceAnchor` を辞書に束ねれば、`anchor.condition_mapping` がそのまま読める
表になる(`condition_mapping` が使うのは `table[face]` と `table.radius`/`table.node_grid` だけ)。
その表で `condition_mapping` をもう一度呼び、元の確定表の検証結果と並べる。

## 題材

- 実機: `models/hanger_bar_attachment.json` を 2.4mm と、要素サイズの下限(実測 1.8749mm)で測る。
  1.2mm は要求して `OptimizeInputError` で断られることを確かめるだけ(`derive` は呼ばない)。
- 合成: 前タスク `measure_small_keep_bodies.py` の cube/cylinder ボス構成
  (`build_small_case(..., load_on_body=True)`)を 2.4mm と 1.2mm で測る。
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

DEFAULT_MODEL = _bootstrap._REPO_ROOT / "models" / "hanger_bar_attachment.json"
#: 合成題材の大きさ[mm]。前タスク `measure_small_keep_bodies.py` の `S_MM` と揃える
#: (「前タスクの cube / cylinder ボス」というタスク文書の指定そのもの)。
S_MM = 5.0


# ---------------------------------------------------------------------------
# 「そのボディ由来」の節点マスク(`optimize_run._region_representation` と同じ式)
# ---------------------------------------------------------------------------
def body_owned_node_mask(doc, body_id: str, space) -> np.ndarray:
    from duocad.topopt import anchor as an
    from duocad.topopt import voxelize as vx

    body = doc.get_body(body_id)
    deviation = float(space.stats.get("deviation") or (space.grid.spacing * vx.DEFAULT_DEVIATION_RATIO))
    soup = vx.triangles_from_shape(body.shape, deviation=deviation)
    try:
        mask, _stats = vx.occupancy(soup, space.grid)
    except ValueError:
        # `_region_representation` と同じ扱い: レイが1本も貫かない縮退は0要素とみなす。
        mask = np.zeros(space.grid.shape, dtype=bool)
    design = np.asarray(space.mask, dtype=bool)
    owned_elements = mask & design
    return an.active_nodes(owned_elements)


def face_owner_bodies(doc, bundle) -> dict:
    """製品文書(`doc.optimize`)から、まとめた形での面添字 → その面を持つボディID。

    `bundle.face_offsets[body] + condition["index"]` は `optimize_run.build_conditions` が
    `fixed_faces`/`load_faces` を作るのと同じ式(モジュール `_compound_with_offsets` 契約)。
    """
    owners: dict[int, str] = {}
    setup = doc.optimize
    for role in ("fixtures", "loads"):
        for condition in getattr(setup, role):
            body_id = condition["body"]
            global_face = bundle.face_offsets[body_id] + int(condition["index"])
            owners[global_face] = body_id
    return owners


# ---------------------------------------------------------------------------
# 面ごとの所有権 + 制限したときの予行
# ---------------------------------------------------------------------------
def measure_ownership(doc, bundle, space, owners: dict) -> dict:
    from duocad import optimize_run
    from duocad.topopt import anchor as an

    table, mapping = optimize_run._map_boundary_conditions(space, bundle)
    limit_mm = an.position_limit(table)

    owned_cache: dict[str, np.ndarray] = {}

    def owned_mask_for(body_id: str) -> np.ndarray:
        if body_id not in owned_cache:
            owned_cache[body_id] = body_owned_node_mask(doc, body_id, space)
        return owned_cache[body_id]

    faces_out = []
    restricted_anchors: dict[int, object] = {}
    restricted_build_errors: dict[str, str] = {}
    for row in mapping["faces"]:
        face = int(row["face"])
        body_id = owners.get(face)
        entry = {
            "condition": row["condition"],
            "side": row["side"],
            "face": face,
            "body_id": body_id,
            "nodes": row["nodes"],
            "status": row["status"],
            "reasons": row["reasons"],
        }
        if body_id is None:
            entry["error"] = "面の持ち主ボディが特定できなかった(owners に無い)"
            faces_out.append(entry)
            continue
        owned = owned_mask_for(body_id)
        anchor_row = table[face]
        node_ids = anchor_row.node_ids
        areas = anchor_row.areas
        owned_flat = owned.ravel()
        owned_bool = owned_flat[node_ids] if node_ids.size else np.zeros(0, dtype=bool)
        node_count = int(node_ids.size)
        owned_node_count = int(owned_bool.sum())
        total_area = float(areas.sum())
        owned_area = float(areas[owned_bool].sum()) if node_count else 0.0
        entry.update(
            owned_node_count=owned_node_count,
            node_fraction_owned=(owned_node_count / node_count if node_count else None),
            assigned_area_mm2=total_area,
            owned_area_mm2=owned_area,
            # `table[face].weights()` は areas/total(荷重はこの比で配る)なので、「重みのうち
            # ボディ由来へ行った割合」と「面積の割合」は数値として同じになる(タスク文書の2項目、
            # ここでは同じ計算を2つの呼び名で報告する)。
            area_fraction_owned=(owned_area / total_area if total_area > 0 else None),
            weight_fraction_owned=(owned_area / total_area if total_area > 0 else None),
        )
        try:
            # `faces=` を絞らない(モジュールdocstring: 絞ると稜線の競争相手が減り、絞らずに
            # 作った表とは別の節点集合になる)。allowed_nodes だけをそのボディ由来に変えて、
            # 全面で作り直した表から対象面だけ取り出す。
            full_restricted = an.build_anchors(space.soup, space.grid, allowed_nodes=owned)
            restricted_anchors[face] = full_restricted.anchors[face]
        except Exception as exc:  # noqa: BLE001 測れなかったことをそのまま記録する
            restricted_build_errors[row["condition"]] = f"{type(exc).__name__}: {exc}"
        faces_out.append(entry)

    restricted_mapping = None
    if len(restricted_anchors) == len(mapping["faces"]):
        restricted_table = an.AnchorTable(
            grid=space.grid,
            node_grid=table.node_grid,
            anchors=restricted_anchors,
            radius=table.radius,
            stats=dict(table.stats, mapping="material-body-owned"),
        )
        restricted_mapping = an.condition_mapping(
            restricted_table, space.fixed_faces, space.load_faces, bundle.forces, material=space.mask
        )

    return {
        "spacing_mm": float(space.grid.spacing),
        "design_elements": int(space.element_count),
        "position_limit_mm": limit_mm,
        "faces": faces_out,
        "original_mapping": mapping,
        "restricted_mapping": restricted_mapping,
        "restricted_build_errors": restricted_build_errors or None,
    }


# ---------------------------------------------------------------------------
# 実機(hanger)
# ---------------------------------------------------------------------------
def run_hanger(model: Path) -> dict:
    from duocad import optimize_run
    from duocad.session import Session
    from duocad.topopt import design_space as ds

    session = Session(None, 0.2, models_dir=model.resolve().parent)
    session.open_document(model.name)
    doc = session.document
    bundle = optimize_run.build_conditions(doc)
    owners = face_owner_bodies(doc, bundle)
    limits = optimize_run.resolution_limits(doc)

    out: dict = {
        "model": str(model),
        "resolution_limits_mm": {
            key: limits[key]
            for key in ("max_element_size_mm", "min_element_size_mm", "default_element_size_mm", "feasible")
        },
    }

    # 1.2mm を実際に要求して断られることを確かめる(`derive` は呼ばない・タスク文書の指示)。
    try:
        optimize_run._resolve_element_size(limits, 1.2)
        out["rejection_1_2mm"] = {"rejected": False, "note": "1.2mmが断られなかった(想定外)"}
    except optimize_run.OptimizeInputError as exc:
        out["rejection_1_2mm"] = {"rejected": True, "message": str(exc)}

    requested_sizes = [("2.4mm", 2.4), ("lower_bound", float(limits["min_element_size_mm"]))]
    out["cases"] = {}
    for label, requested in requested_sizes:
        t0 = time.perf_counter()
        try:
            resolved = optimize_run._resolve_element_size(limits, requested)
            spacing = optimize_run._snap_to_grid(limits["bbox_min"], limits["bbox_max"], resolved)
            space = ds.derive(bundle.conditions, spacing=spacing)
            result = measure_ownership(doc, bundle, space, owners)
            result["requested_element_size_mm"] = requested
            result["seconds"] = time.perf_counter() - t0
        except Exception as exc:  # noqa: BLE001
            result = {
                "error": f"{type(exc).__name__}: {exc}",
                "requested_element_size_mm": requested,
                "seconds": time.perf_counter() - t0,
            }
        print(
            f"  [hanger] {label} requested={requested:.4f}mm -> "
            f"{result.get('spacing_mm', 'N/A')} "
            f"({result.get('seconds', 0.0):.1f}秒)"
            + ("" if "error" not in result else f"  測定失敗: {result['error']}"),
            flush=True,
        )
        out["cases"][label] = result
    return out


# ---------------------------------------------------------------------------
# 合成題材(cube / cylinder ボス。前タスク measure_small_keep_bodies.py の構成を再利用)
# ---------------------------------------------------------------------------
def run_synthetic(spacings: list, shapes: list) -> dict:
    import measure_small_keep_bodies as msk

    out: dict = {"s_mm": S_MM, "cases": {}}
    for kind in shapes:
        out["cases"][kind] = {}
        for label, spacing in spacings:
            t0 = time.perf_counter()
            try:
                doc, bundle, space = msk.build_small_case(
                    kind=kind, s=S_MM, spacing=spacing, z_shift=0.0, load_on_body=True
                )
                # `build_small_case(load_on_body=True)` は fixed_pad(固定面)と obj(荷重面)を
                # keep ボディにする(モジュール docstring)。owners はこの構成そのものから決まる。
                owners = {int(bundle.fixed_faces[0]): "fixed_pad", int(bundle.load_faces[0]): "obj"}
                result = measure_ownership(doc, bundle, space, owners)
                result["requested_spacing_mm"] = spacing
                result["seconds"] = time.perf_counter() - t0
            except Exception as exc:  # noqa: BLE001
                result = {
                    "error": f"{type(exc).__name__}: {exc}",
                    "requested_spacing_mm": spacing,
                    "seconds": time.perf_counter() - t0,
                }
            print(
                f"  [synthetic] {kind:8s} {label} spacing={spacing}mm "
                f"({result.get('seconds', 0.0):.1f}秒)"
                + ("" if "error" not in result else f"  測定失敗: {result['error']}"),
                flush=True,
            )
            out["cases"][kind][label] = result
    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="確定表が拾う節点のうち、その面のボディ由来が何%かを実測する"
    )
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--skip-hanger", action="store_true", help="実機(hanger)の測定を省く")
    parser.add_argument("--skip-synthetic", action="store_true", help="合成題材の測定を省く")
    parser.add_argument(
        "--skip-synthetic-fine", action="store_true", help="合成題材の1.2mmを省く(重ければ)"
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    started = time.perf_counter()
    result: dict = {}
    with tempfile.TemporaryDirectory(prefix="topopt-trace-") as trace_root:
        from duocad.topopt import trace

        with patch.object(trace, "DEFAULT_ROOT", Path(trace_root)):
            if not args.skip_hanger:
                print("\n[hanger] 実機を測定する", flush=True)
                result["hanger"] = run_hanger(args.model)
            if not args.skip_synthetic:
                print("\n[synthetic] cube/cylinder ボスを測定する", flush=True)
                spacings = [("2.4mm", 2.4)]
                if not args.skip_synthetic_fine:
                    spacings.append(("1.2mm", 1.2))
                result["synthetic"] = run_synthetic(spacings, ["cube", "cylinder"])

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
