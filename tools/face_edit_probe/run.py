"""全ケース × 全方式を1件ずつ別プロセスで回し、表にする(OCCT はプロセスごと落ちることがあるため)。

    .venv/Scripts/python.exe -m tools.face_edit_probe.run                 # 全部
    .venv/Scripts/python.exe -m tools.face_edit_probe.run d_fillet_1edge  # 名前で絞る(前方一致)
    .venv/Scripts/python.exe -m tools.face_edit_probe.run --json out.json

判定(`verdict`):
- `match`   — 有効な1ソリッドで、期待する形との対称差が体積の 1e-6 以下
- `wrong`   — 有効な1ソリッドだが期待と違う形(期待が「失敗」のときに通ったものもここ)
- `refused` — 方式が例外・警告・検査落ちで断った(期待が「失敗」なら正しい)
- `crash`   — プロセスごと落ちた / 時間切れ
- `recorded`— 正解を決めていないケースで、有効な1ソリッドが出た
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TIMEOUT_S = 120

DELETE_METHODS = ["A", "C"]
MOVE_METHODS = ["B0", "B1", "C", "DB", "DC"]


def _plan(prefixes):
    sys.path.insert(0, str(ROOT))
    from tools.face_edit_probe import shapes

    plan = []
    for case in shapes.all_cases():
        if prefixes and not any(case.name.startswith(p) for p in prefixes):
            continue
        for method in DELETE_METHODS if case.kind == "delete" else MOVE_METHODS:
            plan.append((case.name, method))
    return plan


def _run_one(item):
    name, method = item
    started = time.perf_counter()
    try:
        done = subprocess.run(
            [sys.executable, "-m", "tools.face_edit_probe.worker", name, method],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {"case": name, "method": method, "verdict": "crash", "error": f"timeout {TIMEOUT_S}s"}
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    if done.returncode != 0 or not lines:
        return {
            "case": name, "method": method, "verdict": "crash",
            "error": f"exit {done.returncode & 0xFFFFFFFF:#x} {done.stderr.strip()[-200:]}",
            "wall_s": time.perf_counter() - started,
        }
    return json.loads(lines[-1])


def main(argv):
    out = None
    if "--json" in argv:
        out = argv[argv.index("--json") + 1]
        argv = [a for a in argv if a not in ("--json", out)]
    plan = _plan(argv)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(_run_one, plan))
    for r in results:
        detail = r.get("error") or ""
        print(
            f"{r['case']:30s} {r['method']:3s} {r['verdict']:9s} "
            f"t={r.get('ms', float('nan')):7.1f}ms V={r.get('volume', float('nan')):10.3f} "
            f"F={r.get('faces', '-')!s:>3} expF={r.get('expected_faces', '-')!s:>3} "
            f"symdiff={r.get('symdiff', float('nan')):.3g} {detail[:110]}"
        )
    if out:
        Path(out).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
