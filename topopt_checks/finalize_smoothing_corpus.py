"""平滑化の検証材料 64 個に、製品と同じ最終処理(`finalize` の保持・除外のメッシュ集合演算)をかけて、
最終の完成形を `output/smoothing_corpus/final<suffix>/` へ保存する。

    .\\.venv\\Scripts\\python.exe topopt_checks\\finalize_smoothing_corpus.py            # 全条件(済んだ条件は飛ばす)
    .\\.venv\\Scripts\\python.exe topopt_checks\\finalize_smoothing_corpus.py --one F0200_h2.4_w2
    .\\.venv\\Scripts\\python.exe topopt_checks\\finalize_smoothing_corpus.py --suffix _mesh   # 別の出力名で全条件

入力は `voxel/<条件名>.npz` の mask。平滑化後の面は `mesh.build_surface_from_mask(mask)` を呼び直して作る(決定的で、
`smoothed/<条件名>.stl` と同じ三角形)。保持・除外は `models/hanger_bar_attachment.json` のボディ(荷重だけを変えた
スイープなので全条件で共通)。
出力: `final<suffix>/<条件名>.stl`(成功した条件だけ)と `final_records<suffix>/<条件名>.json`(成否・失敗の文言・秒数・
三角形数・検査の内訳)。`--suffix` は前の経路の結果(`final/`・`final_records/`)を残したまま別名で回すためのもの。
1条件 = 1子プロセス。失敗やメモリを条件の外へ持ち越さない。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import traceback
from pathlib import Path

import _bootstrap  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL = REPO_ROOT / "models" / "hanger_bar_attachment.json"
OUT = REPO_ROOT / "output" / "smoothing_corpus"
PARALLEL = 3
#: 子を起動する前に要る空きメモリ[GB]の既定。B-rep ブーリアンの経路が OOM でPCを落とした実績があるので
#: 控えめに取る(分析者の置いた値)。メッシュ集合演算の経路の実測は記録の `peak_working_set_mb`(`--min-free-gb` で下げる)。
MIN_FREE_GB = 8.0


def _free_gb() -> float:
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return status.ullAvailPhys / 2**30


def _peak_working_set_mb() -> float:
    """この子プロセスのピーク常駐メモリ[MB](Windows)。条件ごとの記録に残し、`--min-free-gb` の根拠にする。"""
    import ctypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi = ctypes.windll.psapi
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return float("nan")
    return counters.PeakWorkingSetSize / 2**20


def _dirs(suffix: str) -> tuple[Path, Path, Path]:
    return OUT / f"final{suffix}", OUT / f"final_records{suffix}", OUT / f"final_manifest{suffix}.json"


def run_one(name: str, suffix: str) -> int:
    import numpy as np
    from _documents import open_session
    from duocad.topopt import mesh, trace
    from duocad.topopt.finalize import finalize

    final_dir, records_dir, _ = _dirs(suffix)
    record = dict(name=name)
    stages: dict = {}
    t0 = time.perf_counter()
    try:
        data = np.load(OUT / "voxel" / f"{name}.npz")
        surface = mesh.build_surface_from_mask(data["mask"])
        world = np.asarray(data["origin"], dtype=float) + surface.verts_smooth * float(data["spacing"])
        doc = open_session(MODEL).document
        setup = doc.optimize
        # 各段階の検査結果は trace の snapshot に出るので、記録を立てて拾う(失敗した条件でも途中までの段階が残る)
        with trace.recording(root=records_dir / "_trace") as recorder:
            try:
                vertices, faces = finalize(world, surface.faces,
                                           [doc.get_body(bid).shape for bid in setup.preserve_bodies],
                                           [doc.get_body(bid).shape for bid in setup.exclude_bodies])
            finally:
                archive = trace.RunArchive(recorder.path)
                for key in ("smoothed-mesh-checks", "mesh-boolean", "final-mesh-checks"):
                    found = archive.snapshots(key)
                    if found:
                        raw = archive.raw(found[-1])
                        stages[key] = {k: v for k, v in raw.items() if k != "volumes"}
                        if "self_intersections" in stages[key]:
                            stages[key]["self_intersections"] = stages[key]["self_intersections"]["counts"]
        mesh.write_stl(final_dir / f"{name}.stl", vertices, faces)
        record.update(status="ok", triangles=int(len(faces)), smoothed_triangles=int(len(surface.faces)),
                      volume_mm3=mesh.mesh_volume(vertices, faces))
    except Exception as error:  # 失敗は条件ごとに記録して次へ進む
        record.update(status="error", error=f"{type(error).__name__}: {error}"[:4000],
                      traceback=traceback.format_exc()[-4000:])
    record["seconds"] = time.perf_counter() - t0
    record["peak_working_set_mb"] = _peak_working_set_mb()
    record["stages"] = stages
    (records_dir / f"{name}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if record["status"] == "ok" else 1


def drive(suffix: str, min_free_gb: float) -> int:
    final_dir, records_dir, manifest = _dirs(suffix)
    final_dir.mkdir(exist_ok=True)
    records_dir.mkdir(exist_ok=True)
    names = sorted(p.stem for p in (OUT / "voxel").glob("*.npz"))
    todo = [n for n in names if not (records_dir / f"{n}.json").exists()]
    running: dict[str, subprocess.Popen] = {}
    while todo or running:
        for name, proc in list(running.items()):
            if proc.poll() is not None:
                del running[name]
                print(f"{time.strftime('%H:%M:%S')} {name} exit={proc.returncode}", flush=True)
        if todo and len(running) < PARALLEL and _free_gb() >= min_free_gb:
            name = todo.pop(0)
            running[name] = subprocess.Popen([sys.executable, __file__, "--one", name, "--suffix", suffix])
            time.sleep(5)  # 起動直後はメモリがまだ増えていないので、次の判定までずらす
            continue
        time.sleep(2)
    records = [json.loads((records_dir / f"{n}.json").read_text(encoding="utf-8")) for n in names]
    manifest.write_text(json.dumps(
        dict(ok=sum(r["status"] == "ok" for r in records), error=sum(r["status"] == "error" for r in records),
             rows=[{k: v for k, v in r.items() if k != "traceback"} for r in records]),
        ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--one")
    parser.add_argument("--suffix", default="")
    parser.add_argument("--min-free-gb", type=float, default=MIN_FREE_GB,
                        help="子を起動する前に要る空きメモリ[GB]。記録の peak_working_set_mb × 並列数を目安に下げてよい")
    args = parser.parse_args()
    return run_one(args.one, args.suffix) if args.one else drive(args.suffix, args.min_free_gb)


if __name__ == "__main__":
    sys.exit(main())
