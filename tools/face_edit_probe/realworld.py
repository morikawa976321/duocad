"""実物の STEP(履歴の無いボディ)での成功率。

    .venv/Scripts/python.exe -m tools.face_edit_probe.realworld <file.step> [...]
(1試行ずつ別プロセス。OCCT が落ちる・止まらない試行があるため。hook_plate_screw で A が CPU 9 分を越えて返らなかった)

各ソリッドで次を1つずつ試し、worker.gate と同じ関門で判定する(期待する形は無いので一致は見ない):
- 丸めの連なり(半径 BLEND_MAX_R 以下の円筒・トーラス・球で、2辺以上が接線でつながる面を、接線でつながる塊ごと)を消す — A と C
  (実物には r=25・r=40 のような接線でつながる意匠の曲面があり、半径で絞らないと全体が1つの塊になる。hook_plate_screw で実測)
- 穴(内向きの全周円筒)を1枚ずつ消す — A と C
- 平面を +0.3 / −0.3 mm 動かす(1ソリッドあたり最大 MOVE_LIMIT 枚)。接線でつながる隣が無い平面(`move_free`)は B0 と C、
  有る平面(`move_tangent`)は B0 と DB
"""
from __future__ import annotations

import json
import math
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MOVE_LIMIT = 12
MOVE_DISTANCE = 0.3
BLEND_MAX_R = 3.0
#: 1試行の時間切れ[秒]。置いた値。これを越えたものは「止まらない」と数える
TRIAL_TIMEOUT_S = 60


def _read(path):
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPControl import STEPControl_Reader

    from .shapes import _quiet_occt

    _quiet_occt()
    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise ValueError("unreadable")
    reader.TransferRoots()
    return reader.OneShape()


def _groups(shape):
    """丸めの塊と穴の面。"""
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    from . import geometry as g
    from .blend import _tangent_pairs

    pairs = _tangent_pairs(shape)
    curved = [f for f in g.faces_of(shape) if g.surface_type(f) != "plane"]

    def tangent_degree(face):
        return sum(1 for a, b in pairs if a.IsSame(face) or b.IsSame(face))

    holes = []
    for f in curved:
        if g.surface_type(f) != "cylinder":
            continue
        adaptor = BRepAdaptor_Surface(f)
        span = math.degrees(adaptor.LastUParameter() - adaptor.FirstUParameter())
        if span < 350:
            continue
        axis = adaptor.Cylinder().Axis()
        c = g.face_centroid(f)
        n = g.face_normal(f)
        # 重心は軸の上にあるので、法線が軸へ向くか(内向き=穴)は面上の1点で見る
        from .custom import _sample

        p, _ = _sample(f)
        loc, d = axis.Location(), axis.Direction()
        v = (p.X() - loc.X(), p.Y() - loc.Y(), p.Z() - loc.Z())
        along = v[0] * d.X() + v[1] * d.Y() + v[2] * d.Z()
        radial = (v[0] - along * d.X(), v[1] - along * d.Y(), v[2] - along * d.Z())
        if radial[0] * n[0] + radial[1] * n[1] + radial[2] * n[2] < 0:
            holes.append(f)
        del c
    def small_radius(face):
        adaptor = BRepAdaptor_Surface(face)
        kind = g.surface_type(face)
        if kind == "cylinder":
            return adaptor.Cylinder().Radius() <= BLEND_MAX_R
        if kind == "torus":
            return adaptor.Torus().MinorRadius() <= BLEND_MAX_R
        if kind == "sphere":
            return adaptor.Sphere().Radius() <= BLEND_MAX_R
        return False

    blends = [f for f in curved if small_radius(f) and tangent_degree(f) >= 2 and not any(f.IsSame(h) for h in holes)]
    chains = []
    for f in blends:
        if any(f.IsSame(x) for chain in chains for x in chain):
            continue
        chain, frontier = [f], [f]
        while frontier:
            current = frontier.pop()
            for a, b in pairs:
                other = b if a.IsSame(current) else a if b.IsSame(current) else None
                if other is None or not any(other.IsSame(x) for x in blends) or any(other.IsSame(x) for x in chain):
                    continue
                chain.append(other)
                frontier.append(other)
        chains.append(chain)
    planes = [f for f in g.faces_of(shape) if g.surface_type(f) == "plane"]
    tangent = [p for p in planes if any((a.IsSame(p) or b.IsSame(p)) for a, b in pairs)]
    free = [p for p in planes if not any(p.IsSame(t) for t in tangent)]
    return chains, holes, free, tangent


def _trials(path):
    from . import geometry as g

    shape = _read(path)
    trials = []
    for solid_index, solid in enumerate(g.solids_of(shape)):
        chains, holes, free, tangent = _groups(solid)
        items = [("delete_blend", chain, 0.0) for chain in chains] + [("delete_hole", [h], 0.0) for h in holes]
        items += [("move_free", [p], d) for p in free[:MOVE_LIMIT] for d in (MOVE_DISTANCE, -MOVE_DISTANCE)]
        items += [("move_tangent", [p], d) for p in tangent[:MOVE_LIMIT] for d in (MOVE_DISTANCE, -MOVE_DISTANCE)]
        for kind, faces, distance in items:
            trials.append((solid_index, solid, kind, faces, distance))
    return trials


METHODS_FOR = {"delete_blend": ("A", "C"), "delete_hole": ("A", "C"), "move_free": ("B0", "C"), "move_tangent": ("B0", "DB")}


def list_trials(path):
    from . import geometry as g

    return [
        {"index": i, "solid": t[0], "solid_faces": len(g.faces_of(t[1])), "kind": t[2], "faces": len(t[3]), "distance": t[4]}
        for i, t in enumerate(_trials(path))
    ]


def one_trial(path, index, method):
    from .shapes import Case
    from .worker import gate, run

    solid_index, solid, kind, faces, distance = _trials(path)[index]
    case = Case("real", "delete" if kind.startswith("delete") else "move", solid, faces, None, distance)
    started = time.perf_counter()
    try:
        result = run(case, method)
        reason = gate(case, result, following_blends=method == "DB")
    except Exception as error:  # noqa: BLE001
        reason = f"{type(error).__name__}: {str(error)[:80]}"
    return {"ok": reason is None, "reason": reason, "ms": 1000 * (time.perf_counter() - started)}


def _child(args, timeout):
    try:
        done = subprocess.run(
            [sys.executable, "-m", "tools.face_edit_probe.realworld", *args],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, "hang"
    lines = [line for line in done.stdout.splitlines() if line.startswith(("[", "{"))]
    if done.returncode != 0 or not lines:
        return None, f"crash {done.returncode & 0xFFFFFFFF:#x}"
    return json.loads(lines[-1]), None


def main(paths):
    from concurrent.futures import ThreadPoolExecutor

    summary = {}
    for path in paths:
        started = time.perf_counter()
        name = Path(path).name
        listing, problem = _child(["--list", path], 600)
        if problem:
            print(f"{name}: listing failed ({problem})")
            continue
        jobs = [(t, m) for t in listing for m in METHODS_FOR[t["kind"]]]

        def go(job):
            trial, method = job
            outcome, problem = _child(["--trial", path, str(trial["index"]), method], TRIAL_TIMEOUT_S)
            if problem:
                outcome = {"ok": False, "reason": problem, "ms": float("nan")}
            return {**trial, "method": method, **outcome}

        with ThreadPoolExecutor(max_workers=6) as pool:
            rows = list(pool.map(go, jobs))
        faces = sum({r["solid"]: r["solid_faces"] for r in listing}.values())
        print(f"\n{name}: solids={len({r['solid'] for r in listing})} faces={faces} trials={len(jobs)} wall={time.perf_counter() - started:.1f}s")
        for kind in ("delete_blend", "delete_hole", "move_free", "move_tangent"):
            for method in sorted({r["method"] for r in rows if r["kind"] == kind}):
                sel = [r for r in rows if r["kind"] == kind and r["method"] == method]
                ok = sum(r["ok"] for r in sel)
                ms = [r["ms"] for r in sel if r["ms"] == r["ms"]]
                reasons = {}
                for r in sel:
                    if not r["ok"]:
                        key = r["reason"].split("(")[0][:60]
                        reasons[key] = reasons.get(key, 0) + 1
                med = statistics.median(ms) if ms else float("nan")
                print(f"  {kind:12s} {method:3s} {ok:3d}/{len(sel):3d}  median={med:7.1f}ms max={max(ms) if ms else float('nan'):8.1f}ms  {reasons}")
                agg = summary.setdefault((kind, method), [0, 0, 0, 0])
                agg[0] += ok
                agg[1] += len(sel)
                agg[2] += sum(1 for r in sel if r["reason"] == "hang")
                agg[3] += sum(1 for r in sel if str(r["reason"]).startswith("crash"))
                for r in sel:
                    if r["reason"] == "hang" or str(r["reason"]).startswith("crash"):
                        print(f"      {r['reason']}: --trial {name} {r['index']} {method} (solid {r['solid']}, {r['faces']} face(s), d={r['distance']})")
    print("\nTOTAL (ok/n, hang, crash)")
    for (kind, method), (ok, n, hang, crash) in sorted(summary.items()):
        print(f"  {kind:12s} {method:3s} {ok}/{n} = {100 * ok / max(n, 1):.0f}%  hang={hang} crash={crash}")


if __name__ == "__main__":
    if sys.argv[1] == "--list":
        print(json.dumps(list_trials(sys.argv[2])))
    elif sys.argv[1] == "--trial":
        print(json.dumps(one_trial(sys.argv[2], int(sys.argv[3]), sys.argv[4])))
    else:
        main(sys.argv[1:])
