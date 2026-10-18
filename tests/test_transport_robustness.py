"""入口(WebSocket / MCP / stdio)が不正な形の入力で例外を漏らさず、接続やプロセスが落ちない契約。
落ちたとき何を意味するか: object でない JSON や深すぎる入れ子の1通で、接続ハンドラが終わる(WebSocket は 1011 で閉じる)、
サーバ全体が落ちる(stdio)、保存済みの API キーが消える、`NaN` を載せた配信が画面の `JSON.parse` を壊す。
"""
from __future__ import annotations

import asyncio
import io
import json

import pytest

from duocad.agent import providers
from duocad.mcp_server import DocumentTools, McpServer, serve_stdio
from duocad.net import wire
from duocad.server import Server, Session
from session_support import FakeConnection, browser, session_with_box

DEEP = "[" * 200_000 + "]" * 200_000


async def _send_raw(server: Server, client: FakeConnection, raw: str) -> dict | None:
    before = len(client.payloads)
    await server._dispatch(client, raw)
    return client.payloads[-1] if len(client.payloads) > before else None


def _scenario(session: Session, *raws: str, hidden_features: frozenset = frozenset()) -> list:
    async def run() -> list:
        server = Server(session, hidden_features=hidden_features)
        server.bind_loop(asyncio.get_running_loop())
        client = FakeConnection()
        server.clients.add(client)
        await server._push(client, applied=None, only=client)
        client.payloads.clear()
        return [await _send_raw(server, client, raw) for raw in raws]

    return asyncio.run(run())


@pytest.mark.parametrize("raw", ["[1]", "5", "null", '"x"', "true"])
def test_a_json_value_that_is_not_an_object_is_a_parse_error_not_a_dropped_connection(raw: str):
    session = session_with_box()
    answers = _scenario(session, raw, json.dumps({"cmd": "get_state"}))
    assert answers[0]["type"] == "error" and answers[0]["where"] == "parse"
    assert answers[1]["type"] == "state"  # 同じ接続で次の要求が通る


def test_a_deeply_nested_message_is_a_parse_error():
    answers = _scenario(session_with_box(), DEEP, json.dumps({"cmd": "get_state"}))
    assert answers[0]["type"] == "error" and answers[0]["where"] == "parse"
    assert answers[1]["type"] == "state"


@pytest.mark.parametrize("ack", [[1], {"a": 1}, True, 1.5, "2", "x"])
def test_an_ack_id_that_cannot_be_a_key_is_refused(ack):
    answers = _scenario(session_with_box(), json.dumps({"cmd": "get_state", "ack_id": ack}))
    assert answers[0]["type"] == "error" and "ack_id" in answers[0]["message"]


def test_a_valid_ack_id_still_gets_done():
    answers = _scenario(session_with_box(), json.dumps({"cmd": "get_state", "ack_id": 7}))
    assert answers[-1] == {"type": "done", "ack_id": 7}


@pytest.mark.parametrize("visible", ["false", "true", 0, 1, None, []])
def test_set_visible_takes_only_a_boolean(visible):
    session = session_with_box()
    answers = _scenario(session, json.dumps({"cmd": "set_visible", "body_id": "box", "visible": visible}))
    assert answers[0]["type"] == "error" and "visible" in answers[0]["message"]
    assert session.document.get_body("box").visible is True


def test_set_visible_false_hides_the_body():
    session = session_with_box()
    _scenario(session, json.dumps({"cmd": "set_visible", "body_id": "box", "visible": False}))
    assert session.document.get_body("box").visible is False


def test_ai_set_api_key_without_a_key_does_not_delete_the_saved_key(tmp_path, monkeypatch):
    secrets = tmp_path / "secrets.json"
    secrets.write_text(json.dumps({providers.ANTHROPIC_API_KEY_ENV: "sk-ant-keep"}), encoding="utf-8")
    monkeypatch.setattr(providers, "SECRETS_PATH", secrets)
    answers = _scenario(session_with_box(), json.dumps({"cmd": "ai_set_api_key"}))
    assert answers[0]["type"] == "error" and "key" in answers[0]["message"]
    assert "sk-ant-keep" in secrets.read_text(encoding="utf-8")


def test_ai_set_api_key_with_an_explicit_null_still_deletes_it(tmp_path, monkeypatch):
    secrets = tmp_path / "secrets.json"
    secrets.write_text(json.dumps({providers.ANTHROPIC_API_KEY_ENV: "sk-ant-gone"}), encoding="utf-8")
    monkeypatch.setattr(providers, "SECRETS_PATH", secrets)
    _scenario(session_with_box(), json.dumps({"cmd": "ai_set_api_key", "key": None}))
    assert not secrets.exists() or "sk-ant-gone" not in secrets.read_text(encoding="utf-8")


@pytest.mark.parametrize("key", ["sk-ant-キー", "sk-ant-ｋｅｙ", "sk-ant-é"])
def test_an_api_key_with_non_ascii_characters_is_refused(key: str):
    assert providers.key_problem(key) is not None
    assert providers.key_problem("sk-ant-api03-abcDEF_123") is None


def test_fem_set_device_is_refused_where_shape_optimization_is_hidden(tmp_path, monkeypatch):
    """GPU の設定を書く口が、最適化を隠した版(安定版)で開いていた。読むだけの `fem_get_status` は画面が接続のたびに呼ぶので隠さない。"""
    from duocad import local_settings

    monkeypatch.setattr(local_settings, "PATH", tmp_path / "settings.json")
    answers = _scenario(
        session_with_box(),
        json.dumps({"cmd": "fem_set_device", "device": "gpu"}),
        hidden_features=frozenset({"shape_optimization"}),
    )
    assert answers[0]["type"] == "error"
    assert not (tmp_path / "settings.json").exists()


def test_fem_get_status_stays_available_where_shape_optimization_is_hidden():
    answers = _scenario(
        session_with_box(), json.dumps({"cmd": "fem_get_status"}), hidden_features=frozenset({"shape_optimization"})
    )
    assert answers[0]["type"] == "fem_status"


def test_a_non_finite_number_in_a_delivery_is_sent_as_null():
    text = wire.dumps({"a": [1.0, float("nan")], "b": {"c": float("inf"), "d": -float("inf")}, "e": "x"})
    assert json.loads(text) == {"a": [1.0, None], "b": {"c": None, "d": None}, "e": "x"}


def test_a_frame_with_a_non_finite_number_still_decodes():
    frame = wire.encode({"type": "state", "state": {"volume": float("nan"), "bodies": []}})
    assert wire.decode(frame)["state"]["volume"] is None


def test_finite_payloads_are_encoded_exactly_as_before():
    payload = {"name": "日本語", "values": [0.1, 2, True, None]}
    assert wire.dumps(payload) == json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize("params", [[1], 5, "x", True])
def test_mcp_params_that_are_not_an_object_are_an_invalid_params_error(params):
    server = McpServer(DocumentTools(session_with_box()))
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
    assert response["error"]["code"] == -32602


def test_mcp_params_may_be_omitted():
    server = McpServer(DocumentTools(session_with_box()))
    assert "result" in server.handle({"jsonrpc": "2.0", "id": 1, "method": "ping"})


def test_stdio_survives_a_deeply_nested_line_and_answers_the_next_one():
    server = McpServer(DocumentTools(session_with_box()))
    lines = (DEEP + "\n" + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n").encode("utf-8")
    out = io.BytesIO()
    asyncio.run(serve_stdio(server, io.BytesIO(lines), out))
    answers = [json.loads(line) for line in out.getvalue().decode("utf-8").splitlines()]
    assert answers[0]["error"]["code"] == -32700
    assert answers[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_an_unencodable_string_echoed_in_a_response_does_not_stop_stdio():
    """要求の `id` や未知の `method` は応答にそのまま写る。孤立サロゲートが紛れても、応答を符号化できず後続の要求まで
    巻き込んでサーバが終わる、ということが起きない(JSON のエスケープ表記で送る)。"""
    server = McpServer(DocumentTools(session_with_box()))
    first = b'{"jsonrpc": "2.0", "id": "\\ud800", "method": "ping"}'
    second = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}).encode("utf-8")
    out = io.BytesIO()
    asyncio.run(serve_stdio(server, io.BytesIO(first + b"\n" + second + b"\n"), out))
    answers = [json.loads(line) for line in out.getvalue().decode("utf-8").splitlines()]
    assert answers[0]["id"] == "\ud800" and answers[0]["result"] == {}
    assert answers[1]["id"] == 2


def test_wire_dumps_falls_back_to_escapes_for_a_string_utf8_cannot_hold():
    text = wire.dumps({"id": "\ud800", "name": "日本語"})
    text.encode("utf-8")  # 符号化できる
    assert json.loads(text) == {"id": "\ud800", "name": "日本語"}
    assert wire.dumps({"name": "日本語"}) == json.dumps({"name": "日本語"}, ensure_ascii=False)


def test_mixing_integer_and_string_ack_ids_cannot_break_the_done_bookkeeping():
    """整数と文字列の `ack_id` が混ざると、済みの番号を比べる段が TypeError で接続を終わらせていた。"""
    async def run() -> list:
        server = Server(session_with_box())
        server.bind_loop(asyncio.get_running_loop())
        client = FakeConnection()
        server.clients.add(client)
        await server._push(client, applied=None, only=client)
        client.payloads.clear()
        await _send_raw(server, client, json.dumps({"cmd": "get_state", "ack_id": 1}))
        await _send_raw(server, client, json.dumps({"cmd": "get_state", "ack_id": "2"}))
        await _send_raw(server, client, json.dumps({"cmd": "get_state", "ack_id": 3}))
        return client.of_type("done")

    done = asyncio.run(run())
    assert [d["ack_id"] for d in done] == [1, 3]


def test_a_document_that_cannot_be_compared_does_not_open_the_door_to_a_hard_link(tmp_path, monkeypatch):
    """壊れたジャンクションのように照合が OSError になる文書が先に並ぶと、検査全体が打ち切られ、後ろの本物の文書への
    ハードリンクを書き出しで潰せた(保存済みの文書が STEP の本文に置き換わる)。"""
    import os
    from pathlib import Path

    models = tmp_path / "models"
    models.mkdir()
    (models / "aaa-broken.json").write_text("{}", encoding="utf-8")
    (models / "precious.json").write_text("{}", encoding="utf-8")
    alias = tmp_path / "alias.step"
    os.link(models / "precious.json", alias)
    session = Session(0.2, models_dir=models, exports_dir=tmp_path / "exports")

    real = Path.samefile

    def flaky(self, other):
        if Path(other).name == "aaa-broken.json":
            raise OSError("照合できない")
        return real(self, other)

    monkeypatch.setattr(Path, "samefile", flaky)
    with pytest.raises(ValueError, match="precious.json"):
        session._resolve_export_path(str(alias), (".step",))
    assert (models / "precious.json").read_text(encoding="utf-8") == "{}"
