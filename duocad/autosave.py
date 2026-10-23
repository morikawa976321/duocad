"""文書の書き込み順序と排他、自動保存の完了・失敗の回収。"""
from __future__ import annotations

import concurrent.futures
import contextlib
import threading
from pathlib import Path
from typing import Any

from .document_store import DocumentStore
from .oplog import OpLog


class Autosave:
    """保存部品を通して書き、文書を参照せずに保存の進行と未保存状態を所有する。"""

    def __init__(self, store: DocumentStore) -> None:
        self.store = store
        self._save_lock = threading.Lock()
        self._background_autosave = 0
        self._writer: concurrent.futures.ThreadPoolExecutor | None = None
        self._pending_write: concurrent.futures.Future | None = None
        self._write_seq = 0
        self._unsaved = False

    def locked(self) -> contextlib.AbstractContextManager:
        """ファイル操作と一覧の読み取りにも共通の排他を使う。複数回使える文脈を返す。"""
        return self._save_lock

    @contextlib.contextmanager
    def background(self):
        self._background_autosave += 1
        try:
            yield
        finally:
            self._background_autosave -= 1

    @property
    def background_active(self) -> bool:
        return self._background_autosave > 0

    @property
    def pending_write(self) -> concurrent.futures.Future | None:
        return self._pending_write

    @property
    def saved(self) -> bool:
        return not self._unsaved

    def mark_saved(self) -> None:
        self._unsaved = False

    def failed(self, error: Exception) -> bool:
        self._unsaved = True
        self.store.warnings.append(f"自動保存できなかった: {type(error).__name__}: {error}")
        return False

    def settle(self) -> bool:
        """完了済みの最後の要求を回収する。書けなかったときだけ False。"""
        future = self._pending_write
        if future is None or not future.done():
            return True
        self._pending_write = None
        error = future.exception()
        if error is not None:
            return self.failed(error)
        self.mark_saved()
        return True

    def drain(self) -> None:
        """最後の要求まで書き終えるのを待ち、その完了・失敗を回収する。"""
        future = self._pending_write
        if future is not None:
            concurrent.futures.wait([future])
            self.settle()

    def supersede(self) -> None:
        self._write_seq += 1

    def write(self, target: Path, payload: Any) -> None:
        """他文書への結果配送も、自動保存と同じ一時ファイルの排他に従う。"""
        with self.locked():
            self.store.write_document_file(target, payload)

    def save_current(self, name: str, log: OpLog, created: float | None) -> None:
        if self.background_active:
            target = self.store.resolve_writable_name(name)
            # 呼び手が変更を配る前に中身を確定し、ワーカーにはログを渡さない。
            payload = log.to_file_chunks(created=created)
            self.supersede()
            seq = self._write_seq
            if self._writer is None:
                self._writer = concurrent.futures.ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="duocad-autosave"
                )
            self._pending_write = self._writer.submit(self._write_queued, target, payload, seq)
        else:
            self.drain()
            self.supersede()
            target = self.store.resolve_writable_name(name)
            payload = log.to_file_chunks(created=created)
            with self.locked():
                self.store.write_document_file(target, payload)
                self.mark_saved()

    def _write_queued(self, target: Path, payload: list, seq: int) -> None:
        if seq != self._write_seq:
            return
        self.write(target, payload)
