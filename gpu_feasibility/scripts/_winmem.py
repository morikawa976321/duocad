"""Windows のメモリ量を標準ライブラリだけで読む。"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import datetime as dt
from pathlib import Path


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def available_gb() -> float:
    """ホストの利用可能な物理メモリ(GB)。"""
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return status.ullAvailPhys / 2**30


def peak_working_set_gb() -> float:
    """このプロセスの常駐メモリのピーク(GB)。"""
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    get_info = ctypes.windll.psapi.GetProcessMemoryInfo
    get_info.argtypes = [wt.HANDLE, ctypes.POINTER(_ProcessMemoryCounters), wt.DWORD]
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    get_info(ctypes.c_void_p(handle), ctypes.byref(counters), counters.cb)
    return counters.PeakWorkingSetSize / 2**30


def log_load_window(results_dir: Path, start: dt.datetime, what: str, cpu_threads: int) -> None:
    """07 の `wall_seconds` が膨らみうる区間を後から特定できるように、CPU を使った実行の時刻を残す。"""
    path = results_dir / "load_windows.csv"
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as fh:
        if new:
            fh.write("start,end,cpu_threads,what\n")
        end = dt.datetime.now().astimezone()
        fh.write(f"{start.isoformat(timespec='seconds')},{end.isoformat(timespec='seconds')},{cpu_threads},{what}\n")
