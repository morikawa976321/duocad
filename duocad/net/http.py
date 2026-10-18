"""`web/dist` の静的配信(`WebHandler` / `start_http_server`)と `/mcp`。

ブラウザが開くページを配り、同じポートでMCP(JSON-RPC 2.0 over HTTP)も喋る。
WebSocket側は `duocad/net/ws.py`。
"""
from __future__ import annotations

import json
import os
import threading
import urllib.parse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from . import wire

#: MCPのHTTPトランスポートの口。ブラウザが開くページと同じポートに生やす。
MCP_HTTP_PATH = "/mcp"

#: 安定版の更新の口(`duocad/channel.py`)。GETで状態、POSTで開始。
UPDATE_HTTP_PATH = "/api/update"

#: `/mcp` が受け取るリクエストボディの上限。WebSocketの `max_size` と揃える。
MCP_HTTP_MAX_BODY = 8 * 1024 * 1024

class WebHandler(SimpleHTTPRequestHandler):
    """`web/dist` を配る。加えて `/duocad-config.json` でWebSocketのURLを教え、
    `/mcp` でMCP(JSON-RPC 2.0 over HTTP)を喋る。

    HTTPとWebSocketでポートを別にしているのは、同一ポートで喋る実装が websockets の
    バージョンに依存するため。
    """

    config_payload: bytes = b"{}"
    #: `/mcp` を処理する `McpServer`(`duocad/mcp_server.py`)。`start_http_server` が挿す。
    #: None のままなら `/mcp` は 404 —— 静的配信だけ立てる検査のため。
    mcp_server: Any = None
    #: `Host` ヘッダとして許す綴り(DNSリバインディング対策。下記 `_mcp_forbidden`)。
    mcp_hosts: frozenset = frozenset()
    #: `/api/update` を答える `StableUpdater`(`duocad/channel.py`)。開発版では None で、口は 404。
    updater: Any = None
    #: `/api/update` へのPOSTで許す `Origin`(= duocad自身が配るページ)。
    page_origins: frozenset = frozenset()
    #: `McpServer` の状態を2スレッドで同時に触らせない(`ThreadingHTTPServer` は接続ごとにスレッド)。
    mcp_lock = threading.Lock()

    def do_POST(self) -> None:  # noqa: N802  (BaseHTTPRequestHandlerの命名に従う)
        """MCPのHTTPトランスポートと、安定版の更新の開始。POSTの口はこの2つだけ。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.send_error(400)
            return
        if length > MCP_HTTP_MAX_BODY:
            # 大きすぎる本文は読み捨てない(読み捨てること自体が的になる)。接続ごと閉じる。
            self.send_error(413)
            return

        # 検査より先に本文を読み切る。読まずに 4xx を返して閉じると、まだ書いているクライアントの
        # 接続が中断され(Windowsでは `ConnectionAbortedError`)、拒否が通信エラーに見える
        # (`tests/test_mcp_http.py` の403の検査が散発的に赤くなった)。上限があるので安全。
        raw = self.rfile.read(length) if length > 0 else b""

        path = self.path.split("?")[0]
        if path == UPDATE_HTTP_PATH and self.updater is not None:
            self._post_update()
            return
        if path != MCP_HTTP_PATH or self.mcp_server is None:
            self.send_error(404)
            return
        if self._mcp_forbidden():
            return
        if not raw:
            self.send_error(400)
            return

        try:
            message = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            # JSON-RPCのパースエラー封筒(-32700)は返さない: `mcp_server.py` の定数を写すと同じ番号が
            # 2箇所になり、逆向きのimportは循環する。素の400で足りる。
            self.send_error(400)
            return

        with self.mcp_lock:
            response = self.mcp_server.handle(message)
        # `handle` の None は「返事を書いてはいけない」(通知)。HTTPでは202が対応する。
        self._send_json(202 if response is None else 200, response)

    def _mcp_forbidden(self) -> bool:
        """`/mcp` を**ブラウザから叩かれない**ようにする検査(通ったら False)。

        `127.0.0.1` へのbindは防御にならない(攻撃側のコードは利用者のブラウザの中で動く)。
        JSON-RPCの本文は `Content-Type: text/plain` にすれば**プリフライトの要らない
        「単純リクエスト」**として送れ、悪意のあるページは応答を読めないまま副作用だけ起こせる。

        - **`Origin` があれば拒否する**。ブラウザは他オリジンへのPOSTに必ず `Origin` を付け、
          JavaScriptから偽装できない。duocad自身のページは `/mcp` を叩かない。
        - **`Host` を許可リストで見る**(DNSリバインディング)。

        **CORSのヘッダは一切返さない**。**トークンは置かない**: このPCでコードを実行できる相手は
        トークンのファイルも読める(`allowed_origins` が `Origin` 無しを意図的に許可するのと同じ線引き)。
        """
        if self.headers.get("Origin") is not None:
            self.send_error(403)
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self.mcp_hosts:
            self.send_error(403)
            return True
        return False

    def _post_update(self) -> None:
        """更新の開始。**duocad自身のページからだけ受ける**(`Origin` を許可リストと照合し、無しも落とす)。
        MCPのツールにはしない(AIに再起動させる理由が無い)。
        """
        if self.headers.get("Origin") not in self.page_origins:
            self.send_error(403)
            return
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self.mcp_hosts:
            self.send_error(403)
            return
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        status, payload = self.updater.start(mode=(query.get("mode") or ["update"])[0])
        self._send_json(status, payload)

    def _send_json(self, status: int, payload: Any) -> None:
        body = b"" if payload is None else wire.dumps(payload).encode("utf-8")
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802  (BaseHTTPRequestHandlerの命名に従う)
        path = self.path.split("?")[0]
        if path == MCP_HTTP_PATH:
            # サーバ→クライアントのSSEストリームは提供しない(pushするものが無く、ツールの顔ぶれは
            # 起動中変わらないので `listChanged` も出さない)。仕様はその場合405を返せと言っている。
            self.send_error(405)
            return
        if path == "/favicon.ico":
            # ブラウザが必ず取りに来る。404だとコンソールが汚れ、計測中に本物のエラーを見落とす。
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == UPDATE_HTTP_PATH:
            if self.updater is None:
                self.send_error(404)
                return
            # 読むだけの口でも `git fetch` を走らせる。DNSリバインディングと、よそのページの
            # `<img src=...?force=1>` 等の埋め込み(`Sec-Fetch-Site: cross-site` は偽装できない)を落とす。
            # ヘッダの無い非ブラウザのクライアントは通す。
            host = (self.headers.get("Host") or "").strip().lower()
            site = self.headers.get("Sec-Fetch-Site")
            if host not in self.mcp_hosts or site not in (None, "same-origin", "none"):
                self.send_error(403)
                return
            force = "force=1" in self.path.split("?", 1)[-1]
            self._send_json(200, self.updater.status(force=force))
            return
        if path == "/duocad-config.json":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(self.config_payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(self.config_payload)
            return
        super().do_GET()

    def log_message(self, *args: Any) -> None:
        """アクセスログを出さない(計測中のコンソールを静かに保つ)。"""


def mcp_http_hosts(port: int) -> frozenset:
    """`/mcp` の `Host` ヘッダとして許す綴り(`WebHandler._mcp_forbidden`)。

    `localhost` と `127.0.0.1` は別の綴りなので両方を挙げる。
    """
    return frozenset(f"{host}:{port}" for host in ("127.0.0.1", "localhost"))


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """使用中のポートへ2つ目を bind させない。標準の `HTTPServer` は `SO_REUSEADDR` を立てるが、Windows では
    それが「リッスン中のポートへの二重 bind」を許す(Unix の TIME_WAIT の再利用とは意味が違う)。2026-10-18 実測:
    後から立てた側は起動成功と表示するのに HTTP を1つも受けず、`WebSocket` の二重起動の検出(bind 失敗)が `HTTP` には効かなかった。
    """

    allow_reuse_address = os.name != "nt"


def start_http_server(
    dist_dir: Path,
    port: int,
    ws_url: str,
    mcp: Any = None,
    config: Optional[dict] = None,
    updater: Any = None,
) -> ThreadingHTTPServer:
    """静的配信をデーモンスレッドで立てる。`web/dist` が無ければ何も立てずに例外。

    `mcp` を渡すと同じポートで `/mcp` も喋る。**`Session` を触るのはイベントループのスレッドだけ**
    なので、渡す `McpServer` の道具は `ToolsOnLoop` で包んであること。

    `config` は `/duocad-config.json` に `ws_url` と並べて載せるもの(チャンネル・版。
    `duocad/channel.py` の `describe`)。`updater` を渡すと `/api/update` が開く(安定版だけ)。
    """
    if not (dist_dir / "index.html").exists():
        raise FileNotFoundError(
            f"{dist_dir} に index.html が無い。先に web をビルドすること:\n"
            f"    cd {dist_dir.parent}\n    npm install\n    npm run build"
        )
    WebHandler.config_payload = json.dumps({"ws_url": ws_url, **(config or {})}).encode("utf-8")
    WebHandler.updater = updater
    WebHandler.page_origins = frozenset(f"http://{host}:{port}" for host in ("127.0.0.1", "localhost"))
    WebHandler.mcp_server = mcp
    WebHandler.mcp_hosts = mcp_http_hosts(port)
    handler = partial(WebHandler, directory=str(dist_dir))
    httpd = _ExclusiveHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, name="duocad-http", daemon=True)
    thread.start()
    return httpd
