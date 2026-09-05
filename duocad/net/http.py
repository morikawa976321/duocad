"""`web/dist` の静的配信(`WebHandler` / `start_http_server`)と `/mcp`。

ブラウザが開くページを配り、同じポートでMCP(JSON-RPC 2.0 over HTTP)も喋る。
WebSocket側は `duocad/net/ws.py`。
"""
from __future__ import annotations

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

#: MCPのHTTPトランスポートの口。ブラウザが開くページと同じ8766に生やす
#: —— 人もClaude Codeも覚えるURLが1つで済む。
MCP_HTTP_PATH = "/mcp"

#: `/mcp` が受け取るリクエストボディの上限。WebSocketの `max_size` と揃える。
MCP_HTTP_MAX_BODY = 8 * 1024 * 1024

class WebHandler(SimpleHTTPRequestHandler):
    """`web/dist` を配る。加えて `/duocad-config.json` でWebSocketのURLを教え、
    `/mcp` でMCP(JSON-RPC 2.0 over HTTP)を喋る。

    ポートを別にしているのは、1つのポートでHTTPとWebSocketを同時に喋る実装が
    websocketsのバージョンに依存するため(壊れやすい側を避けた)。
    """

    config_payload: bytes = b"{}"
    #: `/mcp` を処理する `McpServer`(`duocad/mcp_server.py`)。`start_http_server` が挿す。
    #: None のままなら `/mcp` は 404 —— 静的配信だけ立てる検査のため。
    mcp_server: Any = None
    #: `Host` ヘッダとして許す綴り(DNSリバインディング対策。下記 `_mcp_forbidden`)。
    mcp_hosts: frozenset = frozenset()
    #: `McpServer` の状態(`initialized` 等)を2スレッドで同時に触らせない。
    #: `ThreadingHTTPServer` は接続ごとにスレッドを立てるので、直列化はここで作る。
    mcp_lock = threading.Lock()

    def do_POST(self) -> None:  # noqa: N802  (BaseHTTPRequestHandlerの命名に従う)
        """MCPのHTTPトランスポート。**`/mcp` 以外にPOSTの口は無い**。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.send_error(400)
            return
        if length > MCP_HTTP_MAX_BODY:
            # 大きすぎる本文は読み捨てない(読み捨てること自体が的になる)。接続ごと閉じる。
            self.send_error(413)
            return

        # **検査より先に本文を読み切る。** 読まずに 4xx を返して接続を閉じると、まだ書いている
        # クライアントの側で接続が中断される(Windowsでは `ConnectionAbortedError`)——
        # 拒否は届いているのにクライアントからは通信エラーに見える、という紛らわしい形になる。
        # `tests/test_mcp_http.py` の403の検査がこの競合で**散発的に赤くなる**(1回目は緑・
        # 2回目で赤)。上限で頭打ちにしてあるので読み切って安全である。
        raw = self.rfile.read(length) if length > 0 else b""

        if self.path.split("?")[0] != MCP_HTTP_PATH or self.mcp_server is None:
            self.send_error(404)
            return
        if self._mcp_forbidden():
            return
        if not raw:
            self.send_error(400)
            return

        try:
            message = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            # JSON-RPCのパースエラー封筒(-32700)を返すのが仕様上は丁寧だが、そのために
            # `mcp_server.py` の定数をここへ写すと**同じ番号が2箇所**になる(server.py を
            # importしているのは mcp_server.py の側なので、逆向きのimportは循環する)。
            # 行儀のよいクライアントはここへ来ないので、素の400で足りると判断した。
            self.send_error(400)
            return

        with self.mcp_lock:
            response = self.mcp_server.handle(message)
        # `handle` の None は「返事を書いてはいけない」(通知)。HTTPでは202が対応する。
        self._send_json(202 if response is None else 200, response)

    def _mcp_forbidden(self) -> bool:
        """`/mcp` を**ブラウザから叩かれない**ようにする検査(通ったら False)。

        **なぜ要るか**: `127.0.0.1` へのbindは防御にならない —— 攻撃側のコードは利用者の
        ブラウザの中で動くからである(`allowed_origins` と同じ理屈)。しかもJSON-RPCの本文は
        `Content-Type: text/plain` にすれば**プリフライトの要らない「単純リクエスト」**として
        送れるので、悪意のあるページは応答を読めないまま `/mcp` に副作用だけ起こせる。

        防ぐのはこの2点で足りる:

        - **`Origin` があれば拒否する**。ブラウザは他オリジンへのPOSTに必ず `Origin` を付け、
          JavaScriptから偽装できない。duocad自身のページは `/mcp` を叩かないので、
          「`Origin` 付きのリクエストは全部よそ者」と言い切れる。
        - **`Host` を許可リストで見る**。`127.0.0.1:<port>` 以外の名前で解決して来たものを
          落とす(DNSリバインディング)。

        **CORSのヘッダは一切返さない**(だからプリフライトも通らない)。

        **トークンは置かない。** 守れる相手が増えないためである —— このPCでコードを実行できる
        相手は、トークンを書いたファイルも読める。`allowed_origins` が「`Origin` ヘッダ無しを
        意図的に許可する」と決めたのと同じ線引きで、その外側は防御対象にしない。おかげで
        `claude mcp add` はURL1つで済む。
        """
        if self.headers.get("Origin") is not None:
            self.send_error(403)
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self.mcp_hosts:
            self.send_error(403)
            return True
        return False

    def _send_json(self, status: int, payload: Any) -> None:
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
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
            # **サーバ→クライアントのSSEストリームは提供しない**(duocadからMCPクライアントへ
            # pushするものが無い。ツールの顔ぶれは起動中変わらないので `listChanged` も出さない)。
            # 仕様はその場合405を返せと言っている。
            self.send_error(405)
            return
        if path == "/favicon.ico":
            # ブラウザが必ず取りに来る。404を返すとコンソールがエラーで汚れ、
            # 計測中に本物のエラーを見落とす(自動検査もこれで落ちた)。中身は要らない。
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
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

    `localhost` と `127.0.0.1` は別の綴りなので両方を挙げる(WebSocketの `allowed_origins`
    が両方を挙げるのと同じ理由)。
    """
    return frozenset(f"{host}:{port}" for host in ("127.0.0.1", "localhost"))


def start_http_server(
    dist_dir: Path, port: int, ws_url: str, mcp: Any = None
) -> ThreadingHTTPServer:
    """静的配信をデーモンスレッドで立てる。`web/dist` が無ければ何も立てずに例外。

    `mcp` を渡すと同じポートで `/mcp`(MCPのHTTPトランスポート)も喋る。**`Session` を触るのは
    イベントループのスレッドだけ**という不変条件があるので、渡す `McpServer` の道具は
    `ToolsOnLoop` で包んであること —— 包まないと `ThreadingHTTPServer` のワーカースレッドが
    直に文書を書き換える。
    """
    if not (dist_dir / "index.html").exists():
        raise FileNotFoundError(
            f"{dist_dir} に index.html が無い。先に web をビルドすること:\n"
            f"    cd {dist_dir.parent}\n    npm install\n    npm run build"
        )
    WebHandler.config_payload = json.dumps({"ws_url": ws_url}).encode("utf-8")
    WebHandler.mcp_server = mcp
    WebHandler.mcp_hosts = mcp_http_hosts(port)
    handler = partial(WebHandler, directory=str(dist_dir))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, name="duocad-http", daemon=True)
    thread.start()
    return httpd
