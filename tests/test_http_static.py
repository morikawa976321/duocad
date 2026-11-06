"""画面の静的配信(duocad.net.http)のキャッシュ指示。"""
from __future__ import annotations

import http.client
import socket

from duocad.server import start_http_server


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_index_html_is_revalidated_but_hashed_assets_are_not(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>duocad</title>", encoding="utf-8")
    (tmp_path / "assets" / "app-abc123.js").write_text("1", encoding="utf-8")
    port = _free_port()
    httpd = start_http_server(tmp_path, port, "ws://127.0.0.1:1/")
    try:
        def get(path: str):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            try:
                conn.request("GET", path)
                response = conn.getresponse()
                response.read()
                return response.status, response.getheader("Cache-Control")
            finally:
                conn.close()

        assert get("/") == (200, "no-cache")
        assert get("/index.html") == (200, "no-cache")
        assert get("/index.html?x=1") == (200, "no-cache")
        assert get("/assets/app-abc123.js") == (200, None)
        assert get("/duocad-config.json") == (200, "no-store")
    finally:
        httpd.shutdown()
        httpd.server_close()
