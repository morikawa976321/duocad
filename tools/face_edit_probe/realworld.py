"""実物の STEP(履歴の無いボディ)での成功率。

    .venv/Scripts/python.exe -m tools.face_edit_probe.realworld [--methods strict|legacy] [--parallel N] <file.step> [...]

部品ごとに子プロセスを1つ立て、子は STEP の読み込みと面の分類を1回だけ行ってから試行を順に回す。親は**計算の部分だけに**
TRIAL_TIMEOUT_S の期限を掛け、期限切れ(止まらない)・異常終了(落ちる)なら子を止めて記録し、次の試行から子を立て直す
(OCCT は落ちる・返らないことがある。初版は1試行ずつ子を立てて読み込みまで期限に含めたため、止まった数を多く数えた)。

各ソリッドで次を1つずつ試す(期待する形は無いので一致は見ない):
- 丸めの連なり(半径 BLEND_MAX_R 以下の円筒・トーラス・球で、2辺以上が接線でつながる面を、接線でつながる塊ごと)を消す
  (実物には r=25・r=40 のような接線でつながる意匠の曲面があり、半径で絞らないと全体が1つの塊になる。hook_plate_screw で実測)
- 穴(内向きの全周円筒)を1枚ずつ消す
- 平面を +0.3 / −0.3 mm 動かす(1ソリッドあたり最大 MOVE_LIMIT 枚)。接線でつながる隣が無い平面(`move_free`)と有る平面(`move_tangent`)

`--methods strict`(既定): 削除は SA、移動は SB(接線の隣なし)/ SD(あり)。厳しい版の関門(strict.py)。
`--methods legacy`: 削除は A と C、移動は B0 と C / B0 と DB。緩い関門(worker.gate)。
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
#: 1ソリッドあたりの試行の上限(丸めの連なり・穴・平面それぞれ)。PCケースは穴が数百あるため
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
        items = [("delete_blend", chain, 0.0) for chain in chains[:MOVE_LIMIT]] + [("delete_hole", [h], 0.0) for h in holes[:MOVE_LIMIT]]
        items += [("move_free", [p], d) for p in free[:MOVE_LIMIT] for d in (MOVE_DISTANCE, -MOVE_DISTANCE)]
        items += [("move_tangent", [p], d) for p in tangent[:MOVE_LIMIT] for d in (MOVE_DISTANCE, -MOVE_DISTANCE)]
        for kind, faces, distance in items:
            trials.append((solid_index, solid, kind, faces, distance))
    return trials


METHODS = {
    "legacy": {"delete_blend": ("A", "C"), "delete_hole": ("A", "C"), "move_free": ("B0", "C"), "move_tangent": ("B0", "DB")},
    "strict": {"delete_blend": ("SA",), "delete_hole": ("SA",), "move_free": ("SB",), "move_tangent": ("SD",)},
}
#: 読み込みと面の分類の期限[秒](大きい部品で数十秒かかる)。置いた値
SETUP_TIMEOUT_S = 900


def _attempt(case, method):
    if method.startswith("S"):
        from . import strict

        strict.run(case, method)
        return None
    from .worker import gate, run

    return gate(case, run(case, method), following_blends=method == "DB")


def child(path, start, method_set):
    """子プロセス側: 読み込みを1回だけ行い、(試行, 方式) を `start` 番目から順に回して1行ずつ報告する。"""
    from . import geometry as g
    from .shapes import Case

    trials = _trials(path)
    jobs = [(i, m) for i, t in enumerate(trials) for m in METHODS[method_set][t[2]]]
    described = [{"kind": trials[i][2], "method": m, "solid": trials[i][0], "faces": len(trials[i][3]), "distance": trials[i][4]} for i, m in jobs]
    print(json.dumps({"ready": len(jobs), "jobs": described}), flush=True)
    for position in range(start, len(jobs)):
        index, method = jobs[position]
        solid_index, solid, kind, faces, distance = trials[index]
        print(json.dumps({"start": position}), flush=True)
        case = Case("real", "delete" if kind.startswith("delete") else "move", solid, faces, None, distance)
        started = time.perf_counter()
        try:
            reason = _attempt(case, method)
        except Exception as error:  # noqa: BLE001
            reason = f"{type(error).__name__}: {str(error)[:80]}"
        print(json.dumps({
            "done": position, "solid": solid_index, "solid_faces": len(g.faces_of(solid)), "kind": kind,
            "faces": len(faces), "distance": distance, "method": method, "ok": reason is None, "reason": reason,
            "ms": 1000 * (time.perf_counter() - started),
        }), flush=True)


def _run_model(path, method_set):
    """親側: 子を立て、止まったら殺して次から立て直す。"""
    import queue
    import threading

    rows, position, total, described = [], 0, None, []
    while total is None or position < total:
        process = subprocess.Popen(
            [sys.executable, "-m", "tools.face_edit_probe.realworld", "--child", path, str(position), method_set],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
        )
        lines = queue.Queue()
        threading.Thread(target=lambda: [lines.put(line) for line in process.stdout] + [lines.put(None)], daemon=True).start()
        deadline, current = time.monotonic() + SETUP_TIMEOUT_S, None
        while True:
            try:
                line = lines.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                line = "TIMEOUT"
            if line is None or line == "TIMEOUT":
                problem = "hang" if line == "TIMEOUT" else f"crash {process.wait() & 0xFFFFFFFF:#x}"
                process.kill()
                if total is None:
                    return rows, f"setup {problem}"
                if current is None:
                    current = position
                rows.append({"done": current, **described[current], "ok": False, "reason": problem, "ms": float("nan")})
                position = current + 1
                break
            if not line.startswith("{"):
                continue
            message = json.loads(line)
            if "ready" in message:
                total = message["ready"]
                described = message["jobs"]
                deadline = time.monotonic() + TRIAL_TIMEOUT_S
                if position >= total:
                    process.kill()
                    break
            elif "start" in message:
                current = message["start"]
                deadline = time.monotonic() + TRIAL_TIMEOUT_S
            elif "done" in message:
                rows.append(message)
                position = message["done"] + 1
                current = None
                if position >= total:
                    process.wait()
                    break
    return rows, None


def main(argv):
    from concurrent.futures import ThreadPoolExecutor

    method_set, parallel = "strict", 2
    if "--methods" in argv:
        method_set = argv[argv.index("--methods") + 1]
        argv = [a for a in argv if a not in ("--methods", method_set)]
    if "--parallel" in argv:
        parallel = int(argv[argv.index("--parallel") + 1])
        argv = [a for a in argv if a not in ("--parallel", str(parallel))]

    def go(path):
        started = time.perf_counter()
        rows, problem = _run_model(path, method_set)
        rows_log = __import__("os").environ.get("FACE_EDIT_ROWS")
        if rows_log:  # 長い試験が途中で止まっても、終わった部品の結果は残す
            with open(rows_log, "a", encoding="utf-8") as log:
                log.write(json.dumps({"model": Path(path).name, "problem": problem, "rows": rows}, ensure_ascii=False) + "\n")
        return path, rows, problem, time.perf_counter() - started

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        outcomes = list(pool.map(go, argv))
    summary = {}
    for path, rows, problem, wall in outcomes:
        name = Path(path).name
        if problem:
            print(f"\n{name}: {problem}")
            continue
        print(f"\n{name}: trials={len(rows)} wall={wall:.1f}s")
        for kind in ("delete_blend", "delete_hole", "move_free", "move_tangent"):
            for method in sorted({r["method"] for r in rows if r.get("kind") == kind}):
                sel = [r for r in rows if r.get("kind") == kind and r["method"] == method]
                ok = sum(r["ok"] for r in sel)
                ms = sorted(r["ms"] for r in sel if r["ms"] == r["ms"])
                reasons = {}
                for r in sel:
                    if not r["ok"]:
                        key = str(r["reason"]).split("(")[0][:60]
                        reasons[key] = reasons.get(key, 0) + 1
                med = statistics.median(ms) if ms else float("nan")
                print(f"  {kind:12s} {method:3s} {ok:3d}/{len(sel):3d}  median={med:7.1f}ms max={max(ms) if ms else float('nan'):8.1f}ms  {reasons}")
                agg = summary.setdefault((kind, method), [0, 0, 0, 0, []])
                agg[0] += ok
                agg[1] += len(sel)
                agg[2] += sum(1 for r in sel if r["reason"] == "hang")
                agg[3] += sum(1 for r in sel if str(r["reason"]).startswith("crash"))
                agg[4] += ms
    print("\nTOTAL (ok/n, hang, crash, median/p90/max ms of finished trials)")
    for (kind, method), (ok, n, hang, crash, ms) in sorted(summary.items()):
        ms = sorted(ms)
        p90 = ms[int(0.9 * (len(ms) - 1))] if ms else float("nan")
        print(f"  {kind:12s} {method:3s} {ok}/{n} = {100 * ok / max(n, 1):.0f}%  hang={hang} crash={crash}  "
              f"{statistics.median(ms) if ms else float('nan'):.0f}/{p90:.0f}/{max(ms) if ms else float('nan'):.0f}")


if __name__ == "__main__":
    if sys.argv[1] == "--child":
        child(sys.argv[2], int(sys.argv[3]), sys.argv[4])
    else:
        main(sys.argv[1:])
