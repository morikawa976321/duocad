"""topopt不要材料検証タスク群(0〜10)の共通基盤。

生データ索引・版管理・実験の再開/重複排除・単一FEM実行制御・メモリ監視を1箇所にまとめる
(`docs/TOPOPT_IDLE_VALIDATION_TASKS_2026-09-28.md`「全段階の生データ索引、版管理…を共通化する」)。
ロック機構は `topopt_checks/sweep_smoothing_corpus.py`(`_acquire_lock`/`_release_lock`)の
OS排他ロック(`msvcrt.locking`)をタスク非依存の形へ切り出したもの——**プロセスが異常終了しても
OSがロックを放すので、死んだ錠の回収ロジックが要らない**(同スクリプトのコメントと同じ理由)。

新しいタスク(2〜10)は、このモジュールを直接 import して使う。コピーして書き換えない
(コピーすると単一実行制御の穴・記録形式の食い違いが再発する)。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def git_state(*, paths: tuple[str, ...] = ("duocad",)) -> dict:
    """現在のHEADと、指定パス配下(既定は製品コード `duocad/`)の未コミット差分の有無。

    `topopt_checks/idle_validation/`自身の未コミット差分は対象外(このモジュールを直接
    使う実験スクリプトはリポジトリ管理下にあるが、`output/idle_study/`のような旧世代の
    使い捨てスクリプトはgit管理外なので、コミットハッシュで版を特定できない。その場合は
    呼び出し側がファイルの sha256(`sha256_file`)で版を代用すること)。
    """
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True).stdout.strip()

    dirty = bool(git("status", "--porcelain", "--", *paths)) if paths else bool(git("status", "--porcelain"))
    return {"commit": git("rev-parse", "HEAD"), "dirty_paths": list(paths), "dirty": dirty}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_json(obj) -> str:
    """`obj`をjson.dumps(sort_keys=True)で正規化してhash化する(値の一致だけを見る。整形差は無視)。"""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=float).encode("utf-8")).hexdigest()


def write_atomic(path: Path, data: bytes | str) -> None:
    """`os.replace`は同一ボリューム内でアトミック(実行中断で壊れた途中ファイルを残さない)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    if isinstance(data, str):
        tmp.write_text(data, encoding="utf-8")
    else:
        tmp.write_bytes(data)
    os.replace(tmp, path)


def peak_memory_mb() -> dict:
    import psutil

    info = psutil.Process().memory_info()
    return {"peak_working_set_mb": round(info.peak_wset / 2**20, 1), "peak_commit_mb": round(info.peak_pagefile / 2**20, 1)}


def available_memory_mb() -> float:
    import psutil

    return round(psutil.virtual_memory().available / 2**20, 1)


class ExecutionLock:
    """1つの `lock_path` につき同時に1プロセスだけが `with` ブロックへ入れる。

    タスクをまたいで**同じファイル**を指定すれば「このPC上で重いFEMは1実行ずつ」を
    タスク横断で強制できる(`docs/TOPOPT_IDLE_VALIDATION_TASKS_2026-09-28.md`「計算の継続と資源」)。
    タスクごとに別ファイルを指定すれば、そのタスク内だけの多重起動防止になる。
    どちらの粒度で使うかは呼び出し側の判断(本モジュールは強制しない)。
    """

    def __init__(self, lock_path: Path, tag: str = ""):
        self.lock_path = lock_path
        self.tag = tag
        self._handle = None

    def acquire(self) -> bool:
        import msvcrt

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+b")
        try:
            handle.seek(1 << 20)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate(0)
        handle.write(f"{os.getpid()} {self.tag} {now_iso()}\n".encode())
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self):
        if not self.acquire():
            holder = self.lock_path.read_text(encoding="utf-8", errors="replace").strip() if self.lock_path.exists() else "?"
            raise RuntimeError(f"別の実行が {self.lock_path} を握っている({holder})。多重実行しない")
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def condition_id(**fields) -> str:
    """条件を人間可読な短いIDへ正規化する(例: `condition_id(load_n=200, size_mm=2.0, width_cells=5)`
    → `load200n_size2.0mm_width5c`)。**キーの並びは呼び出し順**(Python 3.7+の辞書順序)なので、
    同じ条件を複数箇所で組み立てるときは同じ引数順を使うこと(順序が違うと同じ条件が別IDになる)。
    """
    parts = []
    for key, value in fields.items():
        parts.append(f"{key}={value}")
    return "_".join(parts).replace(" ", "")


def load_ledger(path: Path) -> dict:
    """条件台帳(`cases`配列を持つJSON)を読む。無ければ空の台帳を返す(初回実行用)。"""
    if not path.exists():
        return {"generated_at": now_iso(), "cases": []}
    return json.loads(path.read_text(encoding="utf-8"))


def upsert_case(ledger: dict, case_id: str, record: dict) -> dict:
    """`case_id`が既にあれば置き換え、無ければ追加する(**重複排除**。実験の再開・再実行で
    同じ条件が台帳に2行できるのを防ぐ)。"""
    cases = ledger.setdefault("cases", [])
    for i, existing in enumerate(cases):
        if existing.get("case_id") == case_id:
            cases[i] = record
            return ledger
    cases.append(record)
    return ledger


def is_case_done(ledger: dict, case_id: str, *, required_status: tuple[str, ...] = ("ok", "no_solution")) -> bool:
    """既に確定済み(**再開時にスキップしてよい**)条件か。`status`が`required_status`のいずれかで、
    参照するファイル(`record["files"]`があれば)が実在することまで見る(記録だけ残ってファイルが
    消えている状態を「済み」と誤認しない)。"""
    for record in ledger.get("cases", []):
        if record.get("case_id") != case_id:
            continue
        if record.get("status") not in required_status:
            return False
        for rel in record.get("files", {}).values():
            if not (REPO_ROOT / rel).is_file():
                return False
        return True
    return False


class SingleFemRun:
    """1回のFEM/最適化実行の所要時間とメモリを計測する(`run_compact_search.py`と同じ二重計測:
    `tracemalloc`でPythonヒープのピーク、`peak_memory_mb()`〈psutil〉でプロセス全体のピークを
    別々に取る——OCCT/numpyのネイティブバッファはtracemallocに映らないため、どちらか一方だけでは
    ピークを見誤る)。`elapsed_seconds`・`traced_peak_mb`・`peak`(プロセス全体)は`__exit__`後に読める。
    ネストして使うと内側の`tracemalloc.stop()`が外側の計測も止めてしまうので、**ネストしないこと**。
    """

    def __init__(self):
        self.elapsed_seconds = None
        self.peak = None
        self.traced_peak_mb = None
        self._t0 = None

    def __enter__(self):
        import tracemalloc

        tracemalloc.start()
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        import tracemalloc

        self.elapsed_seconds = time.perf_counter() - self._t0
        _, traced_peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.traced_peak_mb = round(traced_peak_bytes / 1e6, 1)
        self.peak = peak_memory_mb()
        return False
