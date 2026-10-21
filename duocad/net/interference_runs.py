"""画面からの干渉チェックの走らせ方(`Server` から切り出した。Issue #186)。

計算は子プロセスを待つのでワーカースレッドで走らせ、イベントループは他の要求を捌き続ける(`Server._start_preview` の型)。
ワーカーは1本。**結果は要求した接続にだけ返す**(状態配信ではない)。同じ接続が続けて起動したら前の検査は取り消し(子を止める)、
別の接続の検査は止めない。接続が切れたらその接続の検査を取り消す。結果を描くかどうかは画面が `client_op` と `document_revision` で決める。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any, Awaitable, Callable, Dict, Optional

from ..interference import InterferenceCancelled


class InterferenceRuns:
    def __init__(self, send: Callable[[Any, dict], Awaitable[None]], alive: Callable[[Any], bool]) -> None:
        self._send = send
        self._alive = alive
        self._worker: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._jobs: Dict[Any, Any] = {}  # connection -> 走っている Job

    def start(self, connection: Any, message: dict, job: Any) -> Awaitable[None]:
        """検査をワーカーで走らせ始め、結果を届ける仕事(コルーチン)を返す(呼び手が `Server._spawn` で起こす)。"""
        self.cancel(connection)
        if self._worker is None:
            self._worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="duocad-interference")
        self._jobs[connection] = job
        future = asyncio.get_running_loop().run_in_executor(self._worker, job.run, True)
        return self._deliver(connection, message, job, future)

    def cancel(self, connection: Any) -> None:
        job = self._jobs.pop(connection, None)
        if job is not None:
            job.cancel()

    async def _deliver(self, connection: Any, message: dict, job: Any, future: "asyncio.Future") -> None:
        envelope = {"type": "interference", "client_op": message.get("client_op")}
        try:
            try:
                result = await future
            except InterferenceCancelled:
                return
            except Exception as error:  # noqa: BLE001  1つの失敗でサーバを落とさない。画面の「実行中」を必ず解く
                payload = {**envelope, "error": f"{type(error).__name__}: {error}"}
            else:
                payload = {**envelope, **result}
            if self._jobs.get(connection) is job and self._alive(connection):
                await self._send(connection, payload)
        finally:
            if self._jobs.get(connection) is job:
                self._jobs.pop(connection, None)
