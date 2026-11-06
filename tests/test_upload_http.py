"""落ちたら、ブラウザのファイルを既存の取り込みへ安全に渡す HTTP の契約が崩れている。"""
from __future__ import annotations

import http.client
import json
import socket
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import kernel
from duocad.net import uploads
from duocad.net.http import MCP_HTTP_MAX_BODY, start_http_server
from duocad.net.uploads import UPLOAD_MAX_BYTES, UPLOAD_TTL_SECONDS


class HttpUpload:
    def __init__(self, server):
        self.server = server
        self.port = server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"

    def request(self, method, url, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request(method, url, body=body, headers=headers or {})
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw.startswith(b"{") else raw, dict(response.getheaders())
        finally:
            connection.close()

    def upload(self, name="part.step", body=b"STEP data", headers=None):
        sent = {"Origin": self.origin, "Content-Type": "application/octet-stream", **(headers or {})}
        sent = {key: value for key, value in sent.items() if value is not None}
        return self.request("POST", "/api/upload?filename=" + quote(name, safe=""), body, sent)


@pytest.fixture(scope="module")
def http_upload(tmp_path_factory):
    dist = tmp_path_factory.mktemp("upload-http")
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    server = start_http_server(dist, 0, "ws://127.0.0.1:1", config={"channel": "dev"})
    try:
        yield HttpUpload(server)
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("name", ["part.step", "part.StEp", "part.STP", "drawing.SVG", "日本語の部品.step"])
def test_upload_preserves_bytes_and_original_filename(http_upload, name):
    status, payload, headers = http_upload.upload(name, b"\x00\xffunchanged bytes")
    assert status == 200, payload
    path = Path(payload["path"])
    assert path.is_absolute()
    assert path.name == name
    assert path.parent.parent == http_upload.server.upload_store.root
    assert path.read_bytes() == b"\x00\xffunchanged bytes"
    assert headers["Cache-Control"] == "no-store"
    assert not any(key.lower().startswith("access-control-") for key in headers)


@pytest.mark.parametrize("headers", [
    {"Origin": None}, {"Origin": "http://evil.example"}, {"Origin": "null"},
    {"Origin": "http://localhost:5173"}, {"Host": "evil.example"}, {"Host": "localhost:1"},
])
def test_upload_requires_own_page_origin_and_allowed_host(http_upload, headers):
    before = set(http_upload.server.upload_store.root.iterdir())
    status, payload, received = http_upload.upload(body=b"x" * (2 * 1024 * 1024), headers=headers)
    assert status == 403
    assert payload["error"] == "forbidden"
    assert payload["message"]
    assert not any(key.lower().startswith("access-control-") for key in received)
    assert set(http_upload.server.upload_store.root.iterdir()) == before


def test_localhost_is_also_an_allowed_page_and_host(http_upload):
    host = f"localhost:{http_upload.port}"
    assert http_upload.upload(headers={"Host": host, "Origin": "http://" + host})[0] == 200


@pytest.mark.parametrize("name", ["a.exe", "part.step.exe", "no_extension", "part.stl"])
def test_other_extensions_are_refused(http_upload, name):
    status, payload, _ = http_upload.upload(name)
    assert status == 415
    assert payload["error"] == "unsupported_extension"


@pytest.mark.parametrize("name", [
    "../part.step", "folder/part.step", "folder\\part.step", "a..b.step", "..step",
    "C:part.step", "NUL.step", "con.svg", "COM1.STP", "Lpt9.step", "COM¹.step", "CON .step",
    "CONOUT$.step", "bad\x00.step", "bad\n.svg", "bad\x7f.step", "bad\u0085.step",
    "bad?.step", "bad*.svg", "bad|.step", "bad<.svg", 'bad".step',
    "part.step ", "part.step.", "a" * 121 + ".step", "",
])
def test_unsafe_filenames_never_create_a_directory(http_upload, name):
    before = set(http_upload.server.upload_store.root.iterdir())
    status, payload, _ = http_upload.upload(name)
    assert status == 400
    assert payload["error"] == "invalid_filename"
    assert set(http_upload.server.upload_store.root.iterdir()) == before


def test_missing_duplicate_and_invalid_utf8_names_are_refused(http_upload):
    for query in ("", "filename=a.step&filename=b.step", "filename=%FF.step"):
        status, payload, _ = http_upload.request(
            "POST", "/api/upload?" + query, b"data", {"Origin": http_upload.origin},
        )
        assert status == 400
        assert payload["error"] == "invalid_filename"


def _chunks(size):
    chunk = b"x" * (64 * 1024)
    while size:
        count = min(size, len(chunk))
        yield chunk[:count]
        size -= count


def test_body_larger_than_the_old_eight_mib_limit_is_saved(http_upload):
    size = MCP_HTTP_MAX_BODY + 1024 * 1024
    status, payload, _ = http_upload.upload(body=_chunks(size), headers={"Content-Length": str(size)})
    assert status == 200, payload
    path = Path(payload["path"])
    assert path.stat().st_size == size
    with path.open("rb") as stream:
        assert all(chunk == b"x" * len(chunk) for chunk in iter(lambda: stream.read(64 * 1024), b""))


def test_limit_is_published_and_oversized_body_gets_json_after_sending(http_upload):
    _, config, _ = http_upload.request("GET", "/duocad-config.json")
    assert config["upload_max_bytes"] == UPLOAD_MAX_BYTES
    before = set(http_upload.server.upload_store.root.iterdir())
    size = UPLOAD_MAX_BYTES + 1
    status, payload, _ = http_upload.upload(body=_chunks(size), headers={"Content-Length": str(size)})
    assert status == 413
    assert payload["error"] == "too_large"
    assert "128 MiB" in payload["message"]
    assert set(http_upload.server.upload_store.root.iterdir()) == before


def test_exact_limit_is_accepted(http_upload, monkeypatch):
    monkeypatch.setattr(uploads, "UPLOAD_MAX_BYTES", 1024)
    status, payload, _ = http_upload.upload(body=b"x" * 1024)
    assert status == 200
    assert Path(payload["path"]).stat().st_size == 1024
    assert http_upload.upload(body=b"x" * 1025)[0] == 413


def test_original_routes_keep_the_eight_mib_limit(http_upload):
    for path in ("/mcp", "/api/update"):
        status, _, _ = http_upload.request(
            "POST", path, b"", {"Content-Length": str(MCP_HTTP_MAX_BODY + 1)},
        )
        assert status == 413


def test_uploaded_step_runs_through_existing_session_and_logs_original_name(http_upload, tmp_path, session):
    original = tmp_path / "source.step"
    kernel.write_step(BRepPrimAPI_MakeBox(10.0, 20.0, 30.0).Shape(), str(original))
    status, payload, _ = http_upload.upload("日本語部品.STP", original.read_bytes())
    assert status == 200
    event = session.apply("import_step", {"path": payload["path"]})
    assert event["body_ids"]
    args = session.document.log.entries[-1].args
    assert args["source_name"] == "日本語部品.STP"
    assert "ISO-10303-21" in args["step"]
    assert "path" not in args


def test_uploaded_svg_runs_through_existing_sketch_import(http_upload, session):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="20"/></svg>'
    status, payload, _ = http_upload.upload("図面.svg", svg)
    assert status == 200
    sketch = session.apply("sketch_create", {"base_plane": "XY"})
    event = session.apply("import_svg", {"path": payload["path"], "sketch_id": sketch["sketch_id"]})
    assert event["source_name"] == "図面.svg"
    assert event["entity_count"] == 4
    assert "path" not in session.document.log.entries[-1].args


def test_next_upload_removes_expired_files_but_keeps_recent_files(http_upload, monkeypatch):
    clock = [uploads.time.monotonic()]
    monkeypatch.setattr(uploads, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    _, old, _ = http_upload.upload("old.step")
    old_path = Path(old["path"])
    clock[0] += UPLOAD_TTL_SECONDS - 1
    _, recent, _ = http_upload.upload("recent.step")
    recent_path = Path(recent["path"])
    assert old_path.exists()
    clock[0] += 2
    assert http_upload.upload("next.step")[0] == 200
    assert not old_path.parent.exists()
    assert recent_path.exists()


def test_parallel_uploads_of_the_same_name_do_not_overwrite_each_other(http_upload):
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(http_upload.upload, "same.step", content) for content in (b"a" * 300_000, b"b" * 300_000)]
        results = [future.result(timeout=15) for future in futures]
    assert all(status == 200 for status, _, _ in results)
    paths = [Path(payload["path"]) for _, payload, _ in results]
    assert paths[0] != paths[1]
    assert [path.read_bytes() for path in paths] == [b"a" * 300_000, b"b" * 300_000]


def test_cleanup_skips_an_upload_that_is_still_receiving(http_upload, monkeypatch):
    clock = [uploads.time.monotonic()]
    monkeypatch.setattr(uploads, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    started = threading.Event()
    store = http_upload.server.upload_store
    begin = store.begin

    def begin_and_signal(name):
        path = begin(name)
        if name == "receiving.step":
            started.set()
        return path

    monkeypatch.setattr(store, "begin", begin_and_signal)
    connection = http.client.HTTPConnection("127.0.0.1", http_upload.port, timeout=15)
    try:
        connection.putrequest("POST", "/api/upload?filename=receiving.step")
        connection.putheader("Origin", http_upload.origin)
        connection.putheader("Content-Length", "2")
        connection.endheaders()
        connection.send(b"a")
        assert started.wait(10)
        clock[0] += UPLOAD_TTL_SECONDS + 1
        assert http_upload.upload("trigger.step")[0] == 200
        connection.send(b"b")
        response = connection.getresponse()
        assert response.status == 200
        path = Path(json.loads(response.read())["path"])
        assert path.read_bytes() == b"ab"
    finally:
        connection.close()


def test_incomplete_upload_is_removed(http_upload):
    before = set(http_upload.server.upload_store.root.iterdir())
    connection = http.client.HTTPConnection("127.0.0.1", http_upload.port, timeout=15)
    try:
        connection.request("POST", "/api/upload?filename=partial.step", b"short", {
            "Origin": http_upload.origin, "Content-Length": "100",
        })
        connection.sock.shutdown(socket.SHUT_WR)
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read())["error"] == "incomplete_body"
    finally:
        connection.close()
    assert set(http_upload.server.upload_store.root.iterdir()) == before


def test_storage_failure_is_reported_after_the_body_is_sent(http_upload, monkeypatch):
    original_open = Path.open
    before = set(http_upload.server.upload_store.root.iterdir())

    def fail_upload_open(path, mode="r", *args, **kwargs):
        if mode == "xb" and path.parent.parent == http_upload.server.upload_store.root:
            raise PermissionError("storage unavailable")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_upload_open)
    status, payload, _ = http_upload.upload(body=b"x" * (2 * 1024 * 1024))
    assert status == 500
    assert payload["error"] == "storage_error"
    assert set(http_upload.server.upload_store.root.iterdir()) == before


def test_normal_process_exit_removes_the_owned_temporary_directory():
    script = """
from duocad.net.uploads import UploadStore
store = UploadStore()
path = store.begin('exit.step')
path.write_bytes(b'content')
store.finish(path, keep=True)
print(store.root)
"""
    process = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)
    assert process.returncode == 0, process.stderr
    assert process.stdout.strip()
    assert not Path(process.stdout.strip()).exists()


def test_each_server_owns_its_store_and_shutdown_removes_it(tmp_path, http_upload):
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    second = start_http_server(tmp_path, 0, "ws://127.0.0.1:1", config={"channel": "stable"}, updater=object())
    root = second.upload_store.root
    try:
        other = HttpUpload(second)
        status, payload, _ = other.upload("stable.step")
        assert status == 200
        assert Path(payload["path"]).parent.parent == root
        assert root != http_upload.server.upload_store.root
        assert http_upload.upload("dev.step")[0] == 200
    finally:
        second.shutdown()
        second.server_close()
    assert not root.exists()
