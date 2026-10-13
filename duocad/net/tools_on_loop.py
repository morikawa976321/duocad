"""別スレッドから入ってくる道具呼び出しをイベントループへ限定する覆い(`ToolsOnLoop`)。

**入ってくる経路が2本ある**理由と、投げ返さない例外はクラスのdocstringが持つ。
"""
from __future__ import annotations

import asyncio
from typing import Any

class ToolsOnLoop:
    """`ToolSurface`(`descriptors`/`descriptor`/`call`/`error_envelope`/`current_seq`)を
    **イベントループのスレッドへ限定する**覆い。

    `Session` は「イベントループのスレッドだけが触る」という前提の上に立っているが、
    そこへ**別のスレッドから入ってくる経路が2本ある**:

    - **アプリ内AI** —— `AgentLoop.run()` はモデルへのHTTPリクエストを含み数十秒かかりうるので、
      `Server._ai_start` が `run_in_executor` で実行器スレッドへ逃がす
    - **HTTPトランスポートのMCP** —— `/mcp` は `ThreadingHTTPServer` のワーカースレッドで
      走る(`WebHandler.do_POST`)

    どちらも直接 `DocumentTools` を叩くと2スレッドが同時に文書を書き換えうる。ここでは
    `.call()` / `.error_envelope()` / `.current_seq()` を `asyncio.run_coroutine_threadsafe`
    でイベントループへ投げ、その場で完了を待つ。**プロバイダへのHTTPリクエストはこの覆いを
    経由しない**。
    """

    def __init__(self, tools: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._tools = tools
        self._loop = loop
        self.hidden_features = getattr(tools, "hidden_features", frozenset())

    def descriptors(self) -> list:
        # 起動時に組んだ静的なリストを読むだけで文書を触らないので、投げ返さない。
        return self._tools.descriptors()

    def descriptor(self, name: str) -> Any:
        # `descriptors()` と同じく静的な引き当てなので投げ返さない。
        return self._tools.descriptor(name)

    def call(self, name: str, arguments: dict) -> dict:
        return self._run_on_loop(self._tools.call, name, arguments)

    def call_and_seq(self, name: str, arguments: dict) -> tuple:
        # 実行と直後の版の読み取りを1回の投げ返しで行う。別々だと間に人の操作が割り込み、その変更が
        # 「実行直後の版」に吸われて次のツールの直前チェックで見えなくなる(楽観ロックの時間窓。issue #48)。
        return self._run_on_loop(self._tools.call_and_seq, name, arguments)

    def error_envelope(self, tool: str, error: Exception) -> dict:
        return self._run_on_loop(self._tools.error_envelope, tool, error)

    def current_seq(self) -> Any:
        # 読み取りだけでも `self.session.document` を触るので投げ返す。
        return self._run_on_loop(self._tools.current_seq)

    def _run_on_loop(self, fn: Any, *args: Any) -> Any:
        # 既にイベントループのスレッド上なら直接呼ぶ。`run_coroutine_threadsafe(...).result()` は
        # 自分が塞いでいるループの完了を待つことになり必ず固まる(実測。`tests/test_agent_ws.py`)。
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            return fn(*args)

        async def _once() -> Any:
            return fn(*args)

        return asyncio.run_coroutine_threadsafe(_once(), self._loop).result()
