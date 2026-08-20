"""アプリ内AIエージェント —— **duocadを「道具の提供者」から「AIの呼び出し側」へ回す層**。

MCPサーバ(`duocad/mcp_server.py`)はMCPクライアントがduocadのプロセスを所有する形なので、
起動のトリガーも人間用UIの寿命もクライアントのセッションに縛られる。こちらは逆向きで、
**先にアプリが立っていて、アプリの中からAIを選んで繋ぐ**。両方の道は共存する
(MCP経路は残す)。

- `providers` … `anthropic` / `openai_compat` の2アダプタ(素のHTTP・非streaming)
- `loop`      … プロバイダ非依存の tool-use ループ(打ち切り・進捗・自己修復)

道具の定義はここに書かない —— `DocumentTools.descriptors()` を渡すだけである。
システムプロンプトも持たない —— `catalog.agent_instructions()`(MCPと同じ文)を読む。
UIは持たない(画面はこの上に載る)。
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
    AnthropicProvider,
    OpenAICompatProvider,
    Provider,
    ProviderError,
    ProviderReply,
    ToolCall,
    api_key_configured,
    build_provider,
    load_api_key,
)

__all__ = [
    "AgentLoop",
    "AgentRun",
    "AnthropicProvider",
    "Cancellation",
    "Limits",
    "OpenAICompatProvider",
    "Provider",
    "ProviderError",
    "ProviderReply",
    "ToolCall",
    "ToolOutcome",
    "ToolSurface",
    "api_key_configured",
    "build_provider",
    "load_api_key",
]
