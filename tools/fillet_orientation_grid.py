"""曲面上面のフィレットを公開経路・未統合の Boolean 入力で逐次測定する。

    python tools/capped_run.py --gb 3 -- <python> tools/fillet_orientation_grid.py --output output/fillet_grid.jsonl

各セルは別プロセス・既定60秒。MakeFillet.Build の待ち時間切れだけは直接経路も別プロセスで測る。
生データは output/ または一時フォルダに置き、文書には表と再現コマンドを残す。
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
STATUSES = ("success", "pole_declined", "failure", "native_timeout")


def cells():
    return [
        (kind, direction, tilt, degree)
        for kind, tilts, step in (
            ("ellipse", (0, .01, .03, .1, .3, 1., math.pi / 2), 30),
            ("teardrop", (0, .03, .3, math.pi / 2), 60),
        )
        for direction in ("Y", "X") for tilt in tilts for degree in range(0, 360, step)
    ] + [("ellipsoid", "Z", 0, degree) for degree in range(0, 360, 30)]


def emit(value):
    print(json.dumps(value, ensure_ascii=True), flush=True)


def measure(cell, direct):
    sys.path.insert(0, str(ROOT))
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire, BRepBuilderAPI_Transform
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism, BRepPrimAPI_MakeRevol, BRepPrimAPI_MakeSphere
    from OCP.GC import GC_MakeArcOfCircle
    from OCP.Geom import Geom_Ellipse
    from OCP.GeomAbs import GeomAbs_SurfaceType
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopoDS import TopoDS
    from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Elips, gp_Pnt, gp_Trsf, gp_Vec
    from duocad import kernel, kernel_tight

    kind, direction, tilt, degrees = cell
    row = dict(kind=kind, direction=direction, tilt_rad=tilt, rotation_deg=degrees,
               status="failure", message="", direct=direct)
    started = time.perf_counter()
    try:
        if kind == "teardrop":
            nx, ny = 2.5 / 6, math.sqrt(1 - (2.5 / 6) ** 2)
            a, b = gp_Pnt(3 * nx, 3 * ny, 0), gp_Pnt(3 * nx, -3 * ny, 0)
            c, d = gp_Pnt(6 + .5 * nx, .5 * ny, 0), gp_Pnt(6 + .5 * nx, -.5 * ny, 0)
            wire = BRepBuilderAPI_MakeWire()
            for edge in (BRepBuilderAPI_MakeEdge(a, c).Edge(),
                         BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(c, gp_Pnt(6.5, 0, 0), d).Value()).Edge(),
                         BRepBuilderAPI_MakeEdge(d, b).Edge(),
                         BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(b, gp_Pnt(-3, 0, 0), a).Value()).Edge()):
                wire.Add(edge)
        else:
            wire = BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(
                gp_Elips(gp_Ax2(gp_Pnt(), gp_Dir(0, 0, 1)), 6, 2)).Edge())
        post = BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(wire.Wire()).Face(), gp_Vec(0, 0, 8)).Shape()
        if kind == "ellipsoid":
            ellipse = Geom_Ellipse(gp_Ax2(gp_Pnt(0, 0, -20), gp_Dir(0, 1, 0), gp_Dir(0, 0, 1)), 26, 24)
            arc = BRepBuilderAPI_MakeEdge(ellipse, 0, math.pi).Edge()
            axis = BRepBuilderAPI_MakeEdge(ellipse.Value(math.pi), ellipse.Value(0)).Edge()
            profile = BRepBuilderAPI_MakeFace(BRepBuilderAPI_MakeWire(arc, axis).Wire()).Face()
            tool = BRepPrimAPI_MakeRevol(profile, gp_Ax1(gp_Pnt(), gp_Dir(0, 0, 1))).Shape()
            transform = gp_Trsf()
            transform.SetRotation(gp_Ax1(gp_Pnt(), gp_Dir(0, 0, 1)), math.radians(degrees))
            tool = BRepBuilderAPI_Transform(tool, transform, True).Shape()
            top_kind = GeomAbs_SurfaceType.GeomAbs_SurfaceOfRevolution
        else:
            pole = (gp_Dir(0, math.sin(tilt), math.cos(tilt)) if direction == "Y"
                    else gp_Dir(math.sin(tilt), 0, math.cos(tilt)))
            axes = gp_Ax2(gp_Pnt(0, 0, -20), pole)
            axes.Rotate(gp_Ax1(axes.Location(), axes.Direction()), math.radians(degrees))
            tool = BRepPrimAPI_MakeSphere(axes, 26).Shape()
            top_kind = GeomAbs_SurfaceType.GeomAbs_Sphere
        shape = BRepAlgoAPI_Common(post, tool).Shape()
        tops = [face for face in kernel_tight._faces(shape)
                if BRepAdaptor_Surface(TopoDS.Face_s(face)).GetType() == top_kind]
        row["raw_top_faces"] = len(tops)
        original_volume = kernel.volume(shape)
        assert BRepCheck_Analyzer(shape).IsValid(), "invalid input"
        top_edges = [kernel_tight._edges(face) for face in tops]
        boundary = [edge for face, edges in zip(tops, top_edges) for edge in edges
                    if not BRep_Tool.IsClosed_s(edge, TopoDS.Face_s(face)) and not BRep_Tool.Degenerated_s(edge)
                    and sum(any(edge.IsSame(other) for other in edges) for edges in top_edges) == 1]
        radius = 1.0 if kind == "teardrop" else 1.5
        radii = {index: radius for index in range(1, kernel.edge_count(shape) + 1)
                 if any(kernel.edge_of(shape, index).IsSame(edge) for edge in boundary)}

        native_type = kernel.BRepFilletAPI_MakeFillet

        class ObservedNative:
            def __init__(self, *args):
                self.maker = native_type(*args)

            def __getattr__(self, name):
                return getattr(self.maker, name)

            def Build(self):
                emit({"stage": "native_build"})
                try:
                    return self.maker.Build()
                finally:
                    emit({"stage": "native_returned"})

        kernel.BRepFilletAPI_MakeFillet = ObservedNative
        actual_round = kernel_tight.round_tight_loop

        def inspected_round(*args, **kwargs):
            tight = actual_round(*args, **kwargs)
            assert tight.lens.ShapeType() == TopAbs_FACE and BRepCheck_Analyzer(tight.lens).IsValid(), "invalid Tight.lens"
            row["tight_lens_valid"] = True
            return tight

        kernel_tight.round_tight_loop = inspected_round
        emit({"stage": "direct" if direct else "public", "raw_top_faces": len(tops)})
        if direct:
            rounded = kernel._tight_fillet(shape, kernel._edge_map(shape), radii, list(radii), original_volume)
        else:
            rounded = kernel.fillet_edges(shape, radii)
        result, provenance = rounded["shape"], rounded["provenance"]
        assert BRepCheck_Analyzer(result).IsValid() and len(kernel.split_solids(result)) == 1, "invalid output"
        assert 0 < rounded["volume_after"] < original_volume, "wrong volume"
        assert not provenance["deleted"] and all(len(v) == 1 for v in provenance["face_map"].values()), "missing or multiple successors"
        for index in range(1, kernel.face_count(shape) + 1):
            old = kernel.face_of(shape, index)
            new = kernel.face_of(result, provenance["face_map"][str(index)][0])
            assert BRepAdaptor_Surface(TopoDS.Face_s(old)).GetType() == BRepAdaptor_Surface(TopoDS.Face_s(new)).GetType(), "successor support type changed"
        successors = [provenance["face_map"][str(index)] for index in range(1, kernel.face_count(shape) + 1)
                      if any(kernel.face_of(shape, index).IsSame(top) for top in tops)]
        assert successors and all(value == successors[0] for value in successors), "top fragments do not share the lens"
        lenses = [face for face in kernel_tight._faces(result)
                  if BRepAdaptor_Surface(TopoDS.Face_s(face)).GetType() == top_kind]
        assert len(lenses) == 1 and lenses[0].IsSame(kernel.face_of(result, successors[0][0])), "not one lens"
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(lenses[0], props, 1e-9)
        row["lens_area_mm2"] = props.Mass()
        # 楕円は独立積分の値、他の2種は基準方向の測定値 (docs/FILLET_OVERFLOW.md)。
        reference = {"ellipse": 5.831554138, "teardrop": 18.147750439, "ellipsoid": 5.962699251}[kind]
        row["reference_delta_mm2"] = props.Mass() - reference
        assert abs(row["reference_delta_mm2"]) < 2e-6, "lens area changed with orientation"
        assert BRepCheck_Analyzer(shape).IsValid() and abs(kernel.volume(shape) - original_volume) < 1e-9, "input changed"
        row["status"] = "success"
    except Exception as error:
        row["message"] = f"{type(error).__name__}: {error}"
        if "surface parameter pole is near a contact curve" in str(error):
            row["status"] = "pole_declined"
    row["seconds"] = time.perf_counter() - started
    emit({"result": row})


def run_cell(cell, timeout, direct=False):
    command = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--cell", json.dumps(cell)]
    if direct:
        command.append("--direct")
    started = time.perf_counter()
    process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", start_new_session=os.name != "nt")
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        # Windows の venv ランチャーの子も止め、次のセルと重ならないようにする。
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=True)
        else:
            os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
    stage, result, raw_tops = "startup", None, None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        stage = event.get("stage", stage)
        raw_tops = event.get("raw_top_faces", raw_tops)
        result = event.get("result", result)
    if result is None:
        kind, direction, tilt, degree = cell
        result = dict(kind=kind, direction=direction, tilt_rad=tilt, rotation_deg=degree, direct=direct,
                      raw_top_faces=raw_tops,
                      status="native_timeout" if timed_out and stage == "native_build" else "failure",
                      message=f"timeout after {timeout:g}s at {stage}" if timed_out else f"worker exit {process.returncode}: {stderr[-2000:]}")
    result["process_seconds"] = time.perf_counter() - started
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "fillet_orientation_grid.jsonl")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--kind", choices=("ellipse", "teardrop", "ellipsoid"))
    parser.add_argument("--cell", help=argparse.SUPPRESS)
    parser.add_argument("--direct", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.cell:
        measure(json.loads(args.cell), args.direct)
        return 0
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    selected = [cell for cell in cells() if args.kind is None or cell[0] == args.kind]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    with args.output.open("w", encoding="utf-8") as out:
        for index, cell in enumerate(selected, 1):
            print(f"[{index}/{len(selected)}] {cell}", flush=True)
            row = run_cell(cell, args.timeout)
            if row["status"] == "native_timeout":
                row["direct_result"] = run_cell(cell, args.timeout, direct=True)
            out.write(json.dumps(row, ensure_ascii=True) + "\n")
            out.flush()
            rows.append(row)
            emit(row)
    for kind in ("ellipse", "teardrop", "ellipsoid"):
        subset = [row for row in rows if row["kind"] == kind]
        counts = Counter(row["status"] for row in subset)
        emit(dict(kind=kind, total=len(subset), **{status: counts[status] for status in STATUSES}))
    emit({"non_success": [row for row in rows if row["status"] != "success"]})
    return int(any(row["status"] == "failure" or row.get("direct_result", {}).get("status", "success") != "success" for row in rows))


if __name__ == "__main__":
    sys.exit(main())
