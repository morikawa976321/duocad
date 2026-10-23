"""実行段階から進捗と診断記録へ通知する口。計画には通知先を保持しない。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .document import Document
    from .topopt.trace import Recorder


@dataclass
class RunEvents:
    recorder: Recorder
    progress: Callable[[dict], None] | None

    def report(self, event: dict) -> None:
        self.recorder.snapshot('progress', event)
        if self.progress is not None:
            self.progress(dict(event, diagnostic_path=str(self.recorder.path)))

    def snapshot(self, name: str, **values: Any) -> None:
        from .topopt import trace

        trace.snapshot(name, **values)

    def final_mesh(self, raw, smooth, faces, grid, *, final_vertices, final_faces) -> None:
        from .topopt import trace

        trace.final_mesh(raw, smooth, faces, grid, final_vertices=final_vertices, final_faces=final_faces)

    def result(self, doc: Document, result: dict) -> dict:
        result['diagnostic_path'] = str(self.recorder.path)
        self.recorder.snapshot('result', result)
        self.recorder.document(doc, 'document_after')
        self.recorder.finish(result['state'])
        return result
