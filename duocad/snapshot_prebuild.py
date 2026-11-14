"""起動後のアイドル時に、最近の文書の再生結果のスナップショット(`document_snapshot`)を先に作る(issue #310)。

スナップショットは、再生に時間がかかる文書を一度再生して開いた後にできる。新しく作った文書・他の環境で編集した文書・コードを更新した後の
文書の1回目は再生で開くことになるので、サーバの起動後に裏で先に再生しておく。

- **再生は子プロセスで行う。** サーバのプロセスは GIL を使わず、画面・MCP の応答を塞がない(OCP は GIL を離さない呼び出しがある。
  docs/PERFORMANCE.md「GIL」)。子は `worker_process` の部品で立て、サーバが落ちれば標準入力が閉じて終わる。優先度は下げる(子の子の
  `kernel_worker` も継ぐ)。
- 子は文書を **開くだけで書き換えない**(`Session.open_document` は再生に時間がかかったとき自分でスナップショットを預ける。文書の JSON は書かない)。
  再生が速かった文書には「速い」の印(`document_snapshot.light_marker`)を預け、次の起動でまた調べないようにする。
- 対象は更新日時の新しい `LIMIT` 件のうち、鍵の合うスナップショットも印も無い文書。いま開いている文書は飛ばす(自動保存が預ける)。
  先読み中の文書を人が開いたら、その子の結果を待つ(`Prebuilder.wait_for`)。
- 起動直後はサーバ自身の準備(import・子の先立て)と競合するので `START_DELAY_SECONDS` 待ってから始める。1文書ずつ順に。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import document_snapshot, worker_process
from .document_store import DocumentStore

#: 先読みの対象にする文書の数(更新日時の新しい順)。
LIMIT = 5
#: サーバが起動してから先読みを始めるまで[秒]。
START_DELAY_SECONDS = 20.0
#: 1文書の再生に待つ上限[秒]。超えたら子を止めて次へ(その文書は人が開いたときに再生で開く)。
PER_DOCUMENT_SECONDS = 300.0
#: 人が先読み中の文書を開いたとき、子の終わりを待つ上限[秒]。
WAIT_SECONDS = 120.0

_CHILD_COMMAND = [sys.executable, "-c", "from duocad import snapshot_prebuild; snapshot_prebuild.child_main()"]


class Prebuilder:
    """サーバが持つ先読みの係。`start()` で裏のスレッドが子を立てて順に文書を再生させ、`stop()` で子ごと止める。"""

    def __init__(
        self,
        store: DocumentStore,
        *,
        current_name: Callable[[], Optional[str]] = lambda: None,
        limit: int = LIMIT,
        start_delay: float = START_DELAY_SECONDS,
        per_document_seconds: float = PER_DOCUMENT_SECONDS,
    ) -> None:
        self._store = store
        self._current_name = current_name
        self._limit = limit
        self._start_delay = start_delay
        self._per_document_seconds = per_document_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._worker: Optional[worker_process.WorkerProcess] = None
        self._lock = threading.Lock()
        self._building: Optional[str] = None
        self._built = threading.Event()
        self._built.set()
        #: 子に作らせた文書の数・失敗した数(テストと診断が見る)。
        self.built = 0
        self.failed = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="snapshot-prebuild", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """先読みをやめ、走っている子を止める。"""
        self._stop.set()
        worker = self._worker
        if worker is not None:
            worker.close()
        self._built.set()

    def wait_for(self, name: str, timeout: float = WAIT_SECONDS) -> None:
        """`name` を子が再生している最中なら、終わるまで待つ(人が同じ文書を開いたとき。重ねて再生しない)。"""
        with self._lock:
            waiting = self._building == name
        if waiting:
            self._built.wait(timeout)

    def run_once(self) -> None:
        """待たずに今すぐ1巡する(テスト)。"""
        self._run_all()

    # ---------------------------------------------------------------- 中身
    def _run(self) -> None:
        if self._stop.wait(self._start_delay):
            return
        try:
            self._run_all()
        finally:
            worker, self._worker = self._worker, None
            if worker is not None:
                worker.close()

    def _candidates(self) -> List[str]:
        """鍵の合うスナップショットも印も無い文書(更新日時の新しい順に `limit` 件の中から)。"""
        try:
            paths = sorted(self._store.models_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        except OSError:
            return []
        wanted = []
        for path in paths[: self._limit]:
            if self._stop.is_set():
                break
            try:
                digest = document_snapshot.file_digest(path.read_bytes())
            except OSError:
                continue
            if self._store.snapshot_status(path, digest) == "missing":
                wanted.append(path.name)
        return wanted

    def _run_all(self) -> None:
        for name in self._candidates():
            if self._stop.is_set():
                return
            if name == self._current_name():
                continue  # 開いている文書は自動保存が預ける
            if not self._build(name):
                if self._worker is None:
                    return  # 子を立てられない

    def _build(self, name: str) -> bool:
        with self._lock:
            self._building = name
            self._built.clear()
        try:
            if self._worker is None:
                self._worker = worker_process.WorkerProcess(_CHILD_COMMAND)
            self._worker.request(
                {"op": "prebuild", "models_dir": str(self._store.models_dir), "name": name},
                self._per_document_seconds,
            )
            self.built += 1
            return True
        except worker_process.WorkerRemoteError:
            self.failed += 1  # 開けない文書(壊れている等)。子は生きているので続ける
            return True
        except worker_process.WorkerError:
            self.failed += 1
            worker, self._worker = self._worker, None
            if worker is not None:
                worker.close()
            return not self._stop.is_set() and self.failed < 3
        finally:
            with self._lock:
                self._building = None
            self._built.set()


# ------------------------------------------------------------------------------------------------
# 子
# ------------------------------------------------------------------------------------------------
_SESSION: Any = None


def _lower_priority() -> None:
    """子の優先度を下げる(人の操作が先。子の子の計算プロセスも継ぐ)。"""
    try:
        if sys.platform == "win32":
            import ctypes

            BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS)
        else:
            import os

            os.nice(10)
    except Exception:  # noqa: BLE001 - 下げられなくても先読みはできる
        pass


def _handle(message: Dict[str, Any]) -> Dict[str, Any]:
    global _SESSION
    if message.get("op") != "prebuild":
        raise ValueError(f"未知の要求: {message.get('op')!r}")
    from . import kernel
    from .session import Session

    models_dir = Path(message["models_dir"])
    name = str(message["name"])
    if _SESSION is None or _SESSION.models_dir != models_dir:
        _SESSION = Session(kernel.DEFAULT_DEVIATION, models_dir=models_dir, opened=False)
    session = _SESSION
    path = session.store.resolve_document_name(name)
    digest = document_snapshot.file_digest(path.read_bytes())  # 開く前のファイルの鍵(再生の間に書き換わった文書の印にしない)
    session.open_document(name)  # 再生に時間がかかれば、ここでスナップショットを預ける。文書の JSON は書かない
    status = session.store.snapshot_status(path, digest)
    if status == "missing":
        session.store.write_snapshot(path, document_snapshot.light_marker(), file_sha256=digest)
        status = "light"
    session.close_document()  # 作った文書を手放す(次の文書の前に形を持ち続けない)
    return {"name": name, "status": status}


def child_main() -> None:
    _lower_priority()
    worker_process.run_child(_handle)
