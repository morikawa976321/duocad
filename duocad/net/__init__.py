"""アプリ層(ブラウザ・MCPクライアント)との接続層。`duocad.session` の `Session` を配る側。

    `ws.py`            WebSocketサーバ(`Server`)とソケットから踏めるコマンドの表
    `http.py`          `web/dist` の静的配信と `/mcp`(MCPのHTTPトランスポート)
    `tools_on_loop.py` 別スレッドからの道具呼び出しをイベントループへ限定する覆い

**向きは `net/` -> `duocad/session.py` の一方向**(`session.py` から `net/` を参照しない)。
"""
