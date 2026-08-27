"""アプリ内AIエージェント —— **duocadを「道具の提供者」から「AIの呼び出し側」へ回す層**。

MCP経路(`duocad/mcp_server.py`)はMCPクライアントがduocadのプロセスを所有する形だが、こちらは
逆向きで、**先にアプリが立っていて、アプリの中からAIを選んで繋ぐ**。両方の道は共存する。

- `providers` … `openai_compat` アダプタ(素のHTTP・非streaming)。**APIキーは持たない**
  (ClaudeをAPIキーで叩く経路は2026-08-27に廃止。Claudeへ繋ぐ道はMCP経路だけ)
- `loop`      … プロバイダ非依存の tool-use ループ(打ち切り・進捗・自己修復)

道具の定義はここに書かない —— `DocumentTools.descriptors()` を渡すだけである。
システムプロンプトも持たない —— `catalog.agent_instructions()`(MCPと同じ文)を読む。
"""
from .loop import (
    AgentLoop,
    AgentRun,
    Cancellation,
    Limits,
    ToolOutcome,
    ToolSurface,
)
from .providers import (
    OpenAICompatProvider,
    Provider,
    ProviderError,
    ProviderReply,
    ProviderTimeout,
    ToolCall,
    build_provider,
)

__all__ = [
    "AgentLoop",
    "AgentRun",
    "Cancellation",
    "Limits",
    "OpenAICompatProvider",
    "Provider",
    "ProviderError",
    "ProviderReply",
    "ProviderTimeout",
    "ToolCall",
    "ToolOutcome",
    "ToolSurface",
    "build_provider",
]
