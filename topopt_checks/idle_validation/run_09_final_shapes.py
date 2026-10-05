"""段階9の実行ドライバ。各ジョブを新しいOSプロセスで走らせ、wall timeoutを超えたら子の木を止めて台帳へ残す。

    .venv\\Scripts\\python.exe topopt_checks\\idle_validation\\run_09_final_shapes.py --job convert:S2_hanger200_step20
    ジョブ: convert:<shape> / resolve:<shape>:<final|smoothed> / cutdiag:<shape>(最終形のブーリアン不成立の切り分け。FEMなし)

timeout: 小型(S1*)は1200秒、hanger(S2/S3)の形状変換は1800秒・FEMは2700秒(計画の上限)。
台帳: `output/idle_validation_20260928/09_runs.json`(ジョブごとの status: ok / error / timeout)。
子の結果JSONは `09_work/<shape>_<action>[_<source>].json`。終了コード0だけでは合格とせず、結果JSONの status を読む。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _common  # noqa: E402
import final_shapes_09 as fs  # noqa: E402

LEDGER = fs.OUT / "09_runs.json"


def timeout_seconds(action: str, shape: str) -> int:
    if shape.startswith("S1"):
        return 1200
    return 2700 if action == "resolve" else 1800


def kill_tree(pid: int) -> None:
    subprocess.call(["taskkill", "/F", "/T", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_job(job: str) -> int:
    parts = job.split(":")
    action, shape = parts[0], parts[1]
    source = parts[2] if len(parts) > 2 else "final"
    script = "diagnose_cut_09.py" if action == "cutdiag" else "final_shapes_09.py"
    cmd = [sys.executable, str(HERE / script)] + ([] if action == "cutdiag" else [action]) + ["--shape", shape]
    name = f"{shape}_{action}"
    if action == "resolve":
        cmd += ["--source", source]
        name += f"_{source}"
    limit = timeout_seconds(action, shape)
    result_path = fs.WORK / f"{name}.json"
    if result_path.exists():
        result_path.replace(result_path.with_suffix(".prev.json"))
    t0 = time.time()
    proc = subprocess.Popen(cmd)
    try:
        code = proc.wait(timeout=limit)
        status = "ok" if code == 0 else "error"
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)
        proc.wait()
        code, status = None, "timeout"
    child, final_status = None, None
    if result_path.exists():
        body = json.loads(result_path.read_text(encoding="utf-8"))
        child, final_status = body.get("status"), (body.get("final") or {}).get("status")
        if status == "ok" and child != "ok":
            status = "error"
    elif status == "ok":
        status = "error"
    record = dict(case_id=job, job=job, status=status, child_status=child, final_status=final_status, exit_code=code, wall_seconds=time.time() - t0,
                  wall_timeout_seconds=limit, finished_at=_common.now_iso())
    ledger = _common.load_ledger(LEDGER)
    _common.upsert_case(ledger, job, record)
    ledger["generated_at"] = _common.now_iso()
    _common.write_atomic(LEDGER, json.dumps(ledger, ensure_ascii=False, indent=1) + "\n")
    print(f"[{job}] {status} child={child} ({record['wall_seconds']:.0f} s)", flush=True)
    return 0 if status == "ok" else 3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    return run_job(ap.parse_args().job)


if __name__ == "__main__":
    raise SystemExit(main())
