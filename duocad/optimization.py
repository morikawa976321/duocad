"""最適化1回の実行、直近のステップと再仕上げの前提、結果の配送と控え。"""
from __future__ import annotations

import threading
from typing import Any

from . import kernel, optimize_run
from .autosave import Autosave
from .document import Document
from .document_store import DocumentStore
from .oplog import verb_handler
from .optimize_steps import OptimizationSteps
from .speculative import fork


class OptimizationRecord:
    """実行後も残すステップと、結果を除く操作ログの照合。"""

    def __init__(self, document: Document, name: str | None) -> None:
        self.document = document
        self.name = name
        self.steps: OptimizationSteps | None = None
        self._inputs = tuple(document.log.entries)
        self._results: list = []

    def matches(self, document: Document, name: str | None) -> bool:
        return self.document is document or (self.name is not None and self.name == name)

    @property
    def has_finisher(self) -> bool:
        return self.steps is not None and self.steps.plan is not None

    def start_search(self) -> OptimizationSteps:
        self.steps = OptimizationSteps()
        return self.steps

    def require_unchanged(self, document: Document) -> None:
        current = tuple(operation for operation in document.log.entries if operation not in self._results)
        if current != self._inputs:
            raise optimize_run.OptimizeInputError(
                "文書が最適化の実行の後で変わった。実行したときの条件で作った結果は、いまの文書へ足せない"
                "(変更を元に戻すか、最適化をやり直してください)"
            )

    def remember_result(self, operation: Any) -> None:
        self._results.append(operation)


class OptimizationRun:
    """開始元と深い作業コピーを固定し、中断と仕上げ要求をスレッド間で受け渡す。"""

    def __init__(self, document: Document, name: str | None, created: float | None) -> None:
        # 同じ形の三角形分割を画面と計算が同時に書き換えないよう、開始前に深く写す。
        self.work, _ = fork(document, kernel.copy_shape)
        self.document = document
        self.name = name
        self.created = created
        self._work_entries = len(self.work.log.entries)
        self._cancel = threading.Event()
        self._finish_request: int | None = None
        self._finish_open = True
        self._finish_lock = threading.Lock()
        self.record: OptimizationRecord | None = None

    def matches(self, document: Document, name: str | None) -> bool:
        return self.document is document or (self.name is not None and self.name == name)

    def cancel(self) -> None:
        self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def start_search(self) -> OptimizationSteps:
        return self.record.start_search()

    def request_finish(self, step: int | None) -> bool:
        with self._finish_lock:
            if not self._finish_open:
                return False
            self._finish_request = -1 if step is None else int(step)
            return True

    def mark_finishing(self, step: int) -> None:
        with self._finish_lock:
            self._finish_open = False
            self._finish_request = int(step)

    def read_finish_request(self, final: bool = False) -> int | None:
        with self._finish_lock:
            if final:
                self._finish_open = False
            return self._finish_request

    def close(self) -> None:
        with self._finish_lock:
            self._finish_open = False

    def status(self) -> dict:
        status: dict = {"running": True}
        if self.cancelled():
            status["cancelling"] = True
        if self.read_finish_request() is not None:
            status["finishing"] = True
        return status

    def adopt_results(self) -> None:
        """イベントループで呼び、作業コピーが足した操作を一度だけ開始元へ反映する。"""
        added = self.work.log.entries[self._work_entries:]
        self._work_entries += len(added)
        for operation in added:
            moved = self.document.log.append(operation.verb, operation.args)
            verb_handler(operation.verb).do(self.document, moved)
            if self.record is not None:
                self.record.remember_result(moved)


class Optimization:
    """同時に1本の実行と、次の探索まで残す直近の記録の持ち主。"""

    def __init__(self) -> None:
        self._active: OptimizationRun | None = None
        self._latest: OptimizationRecord | None = None

    @property
    def active(self) -> OptimizationRun | None:
        return self._active

    def begin(self, document: Document, name: str | None, created: float | None) -> None:
        if self._active is not None:
            raise ValueError("最適化が既に走っている(中断してから)")
        self._active = OptimizationRun(document, name, created)

    def prepare_search(self) -> OptimizationRun:
        run = self._active
        if run.record is None:
            run.record = OptimizationRecord(run.document, run.name)
            self._latest = run.record
        return run

    def prepare_finish(self) -> OptimizationRun:
        run = self._active
        if run.record is None:
            run.record = self._latest
        if run.record is not None:
            run.record.require_unchanged(run.work)
        return run

    def mark_finishing(self, step: int) -> None:
        if self._active is not None:
            self._active.record = self._latest
            self._active.mark_finishing(step)

    def end(self) -> None:
        if self._active is not None:
            self._active.close()
        self._active = None

    def visible(self, document: Document, name: str | None) -> bool:
        return self._active is not None and self._active.matches(document, name)

    def steps_for(self, document: Document, name: str | None) -> OptimizationSteps | None:
        record = self._latest
        return record.steps if record is not None and record.matches(document, name) else None

    def has_finisher(self, document: Document, name: str | None) -> bool:
        record = self._latest
        return record is not None and record.matches(document, name) and record.has_finisher

    def finishable(self, document: Document, name: str | None) -> bool:
        if not self.has_finisher(document, name):
            return False
        try:
            self._latest.require_unchanged(document)
        except optimize_run.OptimizeInputError:
            return False
        return True


class OptimizationResults:
    """別文書への結果保存、書けなかった文書とまだ表示していない通知の控え。"""

    def __init__(self, store: DocumentStore, autosaver: Autosave) -> None:
        self.store = store
        self.autosaver = autosaver
        self._undelivered: dict[str, tuple[Document, float | None]] = {}
        self._unseen: dict[str, dict] = {}

    def deliver_elsewhere(self, run: OptimizationRun, result: dict) -> dict:
        label = run.name
        try:
            if label is None:
                label = self.store.unused_untitled_name()
            target = self.store.resolve_document_name(label)
            self.autosaver.write(target, run.document.log.to_json(created=run.created))
            label = target.name
            self.store.drop_thumbnail(target)
        except Exception as error:  # noqa: BLE001 - 書けない理由は何であれ結果を控える
            label = label or "untitled.json"
            self._undelivered[label] = (run.document, run.created)
            note = (
                f"最適化の結果を {label} へ書けなかった(次に {label} を開くと結果入りで開く): "
                f"{type(error).__name__}: {error}"
            )
            result = dict(result, warning=f"{result['warning']}\n{note}" if result.get("warning") else note)
        return dict(result, elsewhere=True, file=label)

    def take_document(self, name: str) -> tuple[Document, float | None] | None:
        return self._undelivered.pop(name, None)

    def keep_notice(self, name: str, payload: dict) -> None:
        self._unseen[name] = payload

    def take_notice(self, name: str | None) -> dict | None:
        return self._unseen.pop(name, None)

    def discard(self, name: str) -> None:
        self._undelivered.pop(name, None)
        self._unseen.pop(name, None)
