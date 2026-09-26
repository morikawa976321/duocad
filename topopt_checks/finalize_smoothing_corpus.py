"""平滑化の検証材料 64 個に、製品と同じ最終処理(`finalize` の保持・除外のブーリアン → `solid_mesh`)をかけて、
最終の完成形を `output/smoothing_corpus/final/` へ保存する。

    .\\.venv\\Scripts\\python.exe topopt_checks\\finalize_smoothing_corpus.py            # 全条件(済んだ条件は飛ばす)
    .\\.venv\\Scripts\\python.exe topopt_checks\\finalize_smoothing_corpus.py --one F0200_h2.4_w2

入力は `voxel/<条件名>.npz` の mask。平滑化後の面は `mesh.build_surface_from_mask(mask)` を呼び直して作る(決定的で、
`smoothed/<条件名>.stl` と同じ三角形)。保持・除外は `models/hanger_bar_attachment.json` のボディ(荷重だけを変えた
スイープなので全条件で共通)。
出力: `final/<条件名>.stl`(成功した条件だけ)と `final_records/<条件名>.json`(成否・失敗の文言・秒数・三角形数)。
1条件 = 1子プロセス。OCCT の失敗やメモリを条件の外へ持ち越さない。
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
#: 子を起動する前に要る空きメモリ[GB]。2026-09-26 に OOM でPCが落ちているので控えめに取る(分析者の置いた値)。
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


def run_one(name: str) -> int:
    import numpy as np
    from _documents import open_session
    from duocad.topopt import mesh
    from duocad.topopt.finalize import finalize, solid_mesh

    record = dict(name=name)
    t0 = time.perf_counter()
    try:
        data = np.load(OUT / "voxel" / f"{name}.npz")
        surface = mesh.build_surface_from_mask(data["mask"])
        world = np.asarray(data["origin"], dtype=float) + surface.verts_smooth * float(data["spacing"])
        doc = open_session(MODEL).document
        setup = doc.optimize
        shape = finalize(world, surface.faces,
                         [doc.get_body(bid).shape for bid in setup.preserve_bodies],
                         [doc.get_body(bid).shape for bid in setup.exclude_bodies])
        vertices, faces = solid_mesh(shape)
        mesh.write_stl(OUT / "final" / f"{name}.stl", vertices, faces)
        record.update(status="ok", triangles=int(len(faces)), smoothed_triangles=int(len(surface.faces)),
                      volume_mm3=mesh.mesh_volume(vertices, faces))
    except Exception as error:  # 失敗は条件ごとに記録して次へ進む
        record.update(status="error", error=f"{type(error).__name__}: {error}"[:4000],
                      traceback=traceback.format_exc()[-4000:])
    record["seconds"] = time.perf_counter() - t0
    (OUT / "final_records" / f"{name}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if record["status"] == "ok" else 1


def drive() -> int:
    (OUT / "final").mkdir(exist_ok=True)
    (OUT / "final_records").mkdir(exist_ok=True)
    names = sorted(p.stem for p in (OUT / "voxel").glob("*.npz"))
    todo = [n for n in names if not (OUT / "final_records" / f"{n}.json").exists()]
    running: dict[str, subprocess.Popen] = {}
    while todo or running:
        for name, proc in list(running.items()):
            if proc.poll() is not None:
                del running[name]
                print(f"{time.strftime('%H:%M:%S')} {name} exit={proc.returncode}", flush=True)
        if todo and len(running) < PARALLEL and _free_gb() >= MIN_FREE_GB:
            name = todo.pop(0)
            running[name] = subprocess.Popen([sys.executable, __file__, "--one", name])
            time.sleep(20)  # 起動直後はメモリがまだ増えていないので、次の判定までずらす
            continue
        time.sleep(5)
    records = [json.loads((OUT / "final_records" / f"{n}.json").read_text(encoding="utf-8")) for n in names]
    (OUT / "final_manifest.json").write_text(json.dumps(
        dict(ok=sum(r["status"] == "ok" for r in records), error=sum(r["status"] == "error" for r in records),
             rows=[{k: v for k, v in r.items() if k != "traceback"} for r in records]),
        ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--one")
    args = parser.parse_args()
    return run_one(args.one) if args.one else drive()


if __name__ == "__main__":
    sys.exit(main())
