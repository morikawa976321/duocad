"""保存済みの最終ソリッド(B-rep)を製品と同じ経路で三角形分割し、境界交差の修復と成果物検査を実測する。

使い方(リポジトリ直下で):

    .venv/Scripts/python.exe topopt_checks/check_boundary_meshing.py \
        --brep-json output/finalization_checks/<run>/000021-final-solid.json \
        --out output/gap_causality_20260923/robust_meshing_final.json

`--brep` で生の BREP ファイルも受ける。`--deviation` の既定は製品の `solid_mesh` と同じ 0.2。
標準の一括メッシャー(`BRepMesh_IncrementalMesh`)との比較(`--compare`)も同じ JSON に書く。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from OCP.BRep import BRep_Builder
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools
from OCP.TopoDS import TopoDS, TopoDS_Shape

from _record import dumps
from duocad import kernel
from duocad.mesh_geometry import topology, weld_exact
from duocad.topopt.finalize import final_mesh_checks


def peak_working_set_mb() -> float | None:
    """Windows のピーク常駐メモリ(MB)。他 OS では None。"""
    if sys.platform != 'win32':
        return None

    class Counters(ctypes.Structure):
        _fields_ = [('cb', ctypes.c_uint32), ('PageFaultCount', ctypes.c_uint32),
                    ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                    ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                    ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]
    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi = ctypes.windll.psapi
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_uint32]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return None
    return counters.PeakWorkingSetSize / 1e6


def load_shape(args) -> TopoDS_Shape:
    shape = TopoDS_Shape()
    if args.brep_json:
        text = json.loads(Path(args.brep_json).read_text(encoding='utf-8'))['brep']
        import io
        BRepTools.Read_s(shape, io.BytesIO(text.encode('ascii')), BRep_Builder())
    else:
        BRepTools.Read_s(shape, str(args.brep), BRep_Builder())
    if shape.IsNull():
        raise SystemExit('B-rep を読めない')
    return shape


def welded(payload):
    chunks = [np.asarray(f['positions']).reshape(-1, 3)[np.asarray(f['indices']).reshape(-1, 3)]
              for f in payload['faces'] if f['indices']]
    return weld_exact(np.concatenate(chunks))


def standard(shape, deviation):
    started = time.perf_counter()
    BRepTools.Clean_s(shape)
    BRepMesh_IncrementalMesh(shape, deviation, False, kernel.DEFAULT_ANGULAR_DEFLECTION, True)
    face_map = kernel._face_map(shape)
    chunks, missing = [], []
    for index in range(1, face_map.Extent() + 1):
        positions, indices = kernel._face_triangles(TopoDS.Face_s(face_map.FindKey(index)))
        if not indices:
            missing.append(index)
            continue
        chunks.append(np.asarray(positions).reshape(-1, 3)[np.asarray(indices).reshape(-1, 3)])
    stats, _ = topology(*weld_exact(np.concatenate(chunks)))
    return dict(seconds=time.perf_counter() - started, untriangulated=missing, topology=stats)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--brep-json')
    parser.add_argument('--brep')
    parser.add_argument('--deviation', type=float, default=0.2)
    parser.add_argument('--out', required=True)
    parser.add_argument('--compare', action='store_true', help='標準の一括メッシャーでも測って並べる')
    args = parser.parse_args(argv)
    if not (args.brep_json or args.brep):
        parser.error('--brep-json か --brep が要る')

    started = time.perf_counter()
    shape = load_shape(args)
    report = dict(source=args.brep_json or args.brep, deviation=args.deviation,
                  faces=kernel.face_count(shape), load_seconds=time.perf_counter() - started)

    started = time.perf_counter()
    payload = kernel.tessellate(shape, args.deviation)
    tessellate_seconds = time.perf_counter() - started
    boundary = payload['boundary']
    report['robust'] = dict(
        seconds=tessellate_seconds, untriangulated=payload['untriangulated'],
        termination=boundary['termination'], rounds=boundary['rounds'],
        amplified_edges=boundary['amplified_edges'], amplifications=boundary['amplifications'],
        amplified_edge_ids=boundary['amplified_edge_ids'],
        points=(boundary['points_initial'], boundary['points_final']),
        unresolved=boundary['unresolved'], reinstated=boundary['reinstated'],
        rounds_log=boundary['rounds_log'], stage_seconds=boundary['seconds'])
    started = time.perf_counter()
    vertices, faces = welded(payload)
    checks = final_mesh_checks(vertices, faces)
    report['checks'] = dict(seconds=time.perf_counter() - started, triangles=int(len(faces)),
                            vertices=int(len(vertices)), **checks)
    if args.compare:
        report['standard'] = standard(shape, args.deviation)
    report['peak_working_set_mb'] = peak_working_set_mb()
    Path(args.out).write_text(dumps(report), encoding='utf-8')
    print(dumps(dict(faces=report['faces'], robust_seconds=tessellate_seconds, termination=boundary['termination'],
                     rounds=boundary['rounds'], amplified_edges=boundary['amplified_edges'],
                     untriangulated=payload['untriangulated'], failed=checks['failed'],
                     topology=checks['topology'], self_intersections=checks['self_intersections']['counts'],
                     peak_mb=report['peak_working_set_mb'])))
    return 0 if not checks['failed'] and not payload['untriangulated'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
