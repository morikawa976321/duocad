"""段階9: #95 以前の B-rep 最終処理が `material-cut` で不成立になった形の切り分け。

`finalize` の手順のうち、平滑化メッシュ→ソリッド化→「保持+除外をまとめて引く」までを再現し、結果の部品数・体積・妥当性を記録する。
さらに「除外だけ引く」「保持だけ引く」を別々に試して、どちらのツールで不成立になるかを分ける。
各cutが取り除いた体積は、元のボクセルマスクと保持・除外ボディの共通体積(`acceptance.intersection_volume`)と突き合わせられるよう併記する。
1ジョブ=1子プロセス。wall timeoutは `run_09_final_shapes.py` が掛ける(ジョブ名 `cutdiag:<shape>`)。
結果: `output/idle_validation_20260928/09_work/<shape>_cutdiag.json`
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: E402,F401

import numpy as np  # noqa: E402
from OCP.BRepGProp import BRepGProp  # noqa: E402
from OCP.BOPAlgo import BOPAlgo_BOP, BOPAlgo_Operation  # noqa: E402
from OCP.BRepCheck import BRepCheck_Analyzer  # noqa: E402
from OCP.GProp import GProp_GProps  # noqa: E402
from OCP.TopAbs import TopAbs_SOLID  # noqa: E402

import _common  # noqa: E402
import final_shapes_09 as fs  # noqa: E402
from duocad import optimize_run
from duocad.mesh_geometry import make_material, topology  # noqa: E402
from duocad.topopt import acceptance  # noqa: E402
from duocad.topopt import mesh as topo_mesh  # noqa: E402

from duocad import (
    brep_checks as _brep_checks,
    brep_topology as _brep_topology,
)


def boolean(shape, tools, operation):
    """旧経路の診断用。製品のメッシュ集合演算とは独立に OCCT の警告も記録する。"""
    builder = BOPAlgo_BOP()
    builder.SetNonDestructive(True)
    builder.SetFuzzyValue(0.0)
    builder.AddArgument(shape)
    for tool in tools:
        builder.AddTool(tool)
    builder.SetOperation({'cut': BOPAlgo_Operation.BOPAlgo_CUT,
                          'fuse': BOPAlgo_Operation.BOPAlgo_FUSE}[operation])
    builder.Perform()
    errors, warnings = io.BytesIO(), io.BytesIO()
    builder.DumpErrors(errors)
    builder.DumpWarnings(warnings)
    return builder.Shape(), dict(has_errors=builder.HasErrors(), has_warnings=builder.HasWarnings(),
                                 errors=errors.getvalue().decode('utf-8', errors='replace'),
                                 warnings=warnings.getvalue().decode('utf-8', errors='replace'))


def is_valid(shape):
    return shape is not None and not shape.IsNull() and BRepCheck_Analyzer(shape).IsValid()


def volume(shape) -> float:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return float(props.Mass())


def describe(shape, diagnostics) -> dict:
    solids = _brep_topology.subshapes(shape, TopAbs_SOLID) if not shape.IsNull() else []
    volumes = sorted((volume(s) for s in solids), reverse=True)
    return dict(has_errors=diagnostics["has_errors"], has_warnings=diagnostics["has_warnings"],
                warnings=diagnostics["warnings"].strip().splitlines(), errors=diagnostics["errors"].strip().splitlines(),
                null=bool(shape.IsNull()), valid=bool(is_valid(shape)), is_solid=bool(_brep_checks.is_solid(shape)),
                solid_count=len(solids), solid_volumes_mm3_top8=volumes[:8],
                solid_volumes_below_1mm3=int(sum(v < 1.0 for v in volumes)),
                total_volume_mm3=float(sum(volumes)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shape", required=True, choices=sorted(fs.SHAPES))
    args = ap.parse_args()
    t0 = time.perf_counter()
    rec = dict(shape_id=args.shape, status="running")
    try:
        ld = fs.load_shape(args.shape)
        grid, doc, bundle = ld["ctx"].grid, ld["doc"], ld["bundle"]
        surface = topo_mesh.build_surface_from_mask(ld["mask"])
        world = np.asarray(optimize_run._mesh_to_world(surface.verts_smooth, grid)).reshape(-1, 3)
        _, components = topology(world, surface.faces)
        material, _ = make_material(world, surface.faces, components, check_intersections=False)
        keep = [doc.get_body(b).shape for b in bundle.keep_body_ids]
        excl = [doc.get_body(b).shape for b in bundle.exclude_body_ids]
        rec["material"] = dict(components=len(components), volume_mm3=volume(material), keep_bodies=len(keep),
                               exclude_bodies=len(excl),
                               keep_volumes_mm3=[volume(s) for s in keep], exclude_volumes_mm3=[volume(s) for s in excl])
        rec["voxel_mask_overlap_mm3"] = dict(
            keep_bodies=[acceptance.intersection_volume(ld["mask"], grid, s)[1] for s in keep],
            exclude_bodies=[acceptance.intersection_volume(ld["mask"], grid, s)[1] for s in excl])
        rec["voxel_mask_overlap_mm3"]["keep_total"] = float(sum(rec["voxel_mask_overlap_mm3"]["keep_bodies"]))
        for name, tools in (("cut_keep_and_exclude", keep + excl), ("cut_exclude_only", excl), ("cut_keep_only", keep)):
            t1 = time.perf_counter()
            result, diagnostics = boolean(material, tools, "cut")
            rec[name] = dict(describe(result, diagnostics), seconds=time.perf_counter() - t1)
            print(f"[{args.shape}] {name}: solids={rec[name]['solid_count']} valid={rec[name]['valid']}", flush=True)
        rec["status"] = "ok"
    except Exception as error:
        rec.update(status="error", error=f"{type(error).__name__}: {error}"[:3000], traceback=traceback.format_exc()[-3000:])
    rec["wall_seconds"] = time.perf_counter() - t0
    rec["finished_at"] = _common.now_iso()
    _common.write_atomic(fs.WORK / f"{args.shape}_cutdiag.json", json.dumps(rec, ensure_ascii=False, indent=1) + "\n")
    return 0 if rec["status"] == "ok" else 3


if __name__ == "__main__":
    raise SystemExit(main())
