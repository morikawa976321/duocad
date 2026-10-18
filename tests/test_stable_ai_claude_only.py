"""安定版(制作用)のアプリ内AIはClaude APIだけ(本人指示 2026-10-14、docs/STABLE.md)。

落ちたら、安定版の画面にローカルAI(llama-server)の選択肢が出るか、`ai_configure` で切り替えられるか、
開発版からローカルAIが消えている。
"""
from __future__ import annotations

from duocad import channel
from duocad.net import ws
from duocad.server import Server, Session


def _server(tmp_path, channel_name: str) -> Server:
    session = Session(0.2, models_dir=tmp_path / "models", exports_dir=tmp_path / "export")
    return Server(session, hidden_features=channel.hidden_features(channel_name))


def test_stable_offers_only_claude_even_without_a_key(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "api_key_configured", lambda: False)
    status = _server(tmp_path, "stable")._ai_status()
    assert status["providers"] == ["anthropic"] and status["provider"] == "anthropic"


def test_stable_refuses_switching_to_the_local_ai(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "api_key_configured", lambda: False)
    status = _server(tmp_path, "stable")._ai_configure("openai_compat", None, "http://127.0.0.1:8080/v1")
    assert "この版では使えない" in status["error"] and status["provider"] == "anthropic"


def test_dev_still_offers_the_local_ai(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "api_key_configured", lambda: False)
    server = _server(tmp_path, "dev")
    assert server._ai_status()["providers"] == ["openai_compat", "anthropic"]
    assert server._ai_status()["provider"] == "openai_compat"
    assert "error" not in server._ai_configure("openai_compat", None, None)
