"""コマンドを、仮想メモリの上限つきの Windows ジョブで走らせる。暴走しても上限で止まり、PC を落とさない。

    .venv/Scripts/python.exe tools/capped_run.py --gb 3 -- .venv/Scripts/python.exe -X utf8 my_experiment.py

大きい形(PC ケースの STEP など)で OCCT の重い処理を総当たりする実験は、これを通して走らせる
(2026-10-22、フィレットの吸収を総当たりした検証で python が仮想メモリを 84GB 使い、PC がブルースクリーンで落ちた)。
上限を超えた割り当ては失敗し、OCCT は `Standard_OutOfMemory` を投げて子が終わる。終了コードは子のものを返す。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import subprocess
import sys

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
LIMIT_PROCESS_MEMORY = 0x100
LIMIT_JOB_MEMORY = 0x200
LIMIT_KILL_ON_JOB_CLOSE = 0x2000
CREATE_SUSPENDED = 0x4
PROCESS_ALL_ACCESS = 0x1F0FFF


class _Basic(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wt.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wt.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wt.DWORD),
        ("SchedulingClass", wt.DWORD),
    ]


class _Extended(ctypes.Structure):
    _fields_ = [
        ("Basic", _Basic),
        ("Io", ctypes.c_ulonglong * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _ProcessIds(ctypes.Structure):
    _fields_ = [("Assigned", wt.DWORD), ("InList", wt.DWORD), ("Ids", ctypes.c_size_t * 32)]


def run(command: list, limit_gb: float) -> int:
    job = kernel32.CreateJobObjectW(None, None)
    limits = _Extended()
    limits.Basic.LimitFlags = LIMIT_PROCESS_MEMORY | LIMIT_JOB_MEMORY | LIMIT_KILL_ON_JOB_CLOSE
    limits.ProcessMemoryLimit = limits.JobMemoryLimit = int(limit_gb * 1024 ** 3)
    if not kernel32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject")
    # 外部の罠: venv の python.exe は本物のインタプリタを子として起こすランチャー。走り出してからジョブに入れると、
    # その子がジョブの外に出うる。停止状態で作り、ジョブに入れてから動かす。
    child = subprocess.Popen(command, creationflags=CREATE_SUSPENDED)
    handle = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, child.pid)
    if not kernel32.AssignProcessToJobObject(job, handle):
        child.kill()
        raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject")
    ctypes.WinDLL("ntdll").NtResumeProcess(handle)
    code = child.wait()
    kernel32.QueryInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits), None)
    print(f"[capped_run] exit {code}, peak {limits.PeakJobMemoryUsed / 1024 ** 3:.2f} GB of {limit_gb:g} GB", file=sys.stderr)
    kernel32.CloseHandle(handle)
    kernel32.CloseHandle(job)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gb", type=float, default=3.0, help="子とその子孫が使える仮想メモリの合計の上限(GB)")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- のあとに走らせるコマンド")
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("走らせるコマンドを -- のあとに書く")
    return run(command, args.gb)


if __name__ == "__main__":
    sys.exit(main())
