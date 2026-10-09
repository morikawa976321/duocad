"""pytest 全体の下ごしらえ。

**`Session` の既定の保存先をテストごとの使い捨てディレクトリへ向ける。** 1操作ごとの
自動保存(`Session.autosave`。issue #5)は `models_dir` を渡していない `Session(None, 0.2)`
でも走るので、そのままだとリポジトリ直下の実 `models/` へ `untitled-<n>.json` が溜まる。
`Session.__init__` は `duocad.session.DEFAULT_MODELS_DIR` を読むので、その名前を差し替える。
"""
from __future__ import annotations

import pytest

from duocad.server import Session
from duocad.topopt import trace
from document_support import document_with_box
from session_support import session_with_box


@pytest.fixture(autouse=True)
def _default_models_dir_in_tmp(tmp_path, monkeypatch):
    from duocad import session as session_module

    monkeypatch.setattr(session_module, "DEFAULT_MODELS_DIR", tmp_path / "default-models")
    monkeypatch.setattr(session_module, "DEFAULT_EXPORTS_DIR", tmp_path / "default-export")


@pytest.fixture(autouse=True)
def _optimization_runs_go_to_a_temporary_directory(tmp_path_factory, monkeypatch):
    """製品経路を通すテストの実行記録を一時フォルダへ向ける。外すと `tests/` を1回回すごとに
    `output/optimization_runs/` へ記録フォルダが12個・約18MB溜まる(2026-09-17 実測)。"""
    monkeypatch.setattr(trace, 'DEFAULT_ROOT', tmp_path_factory.mktemp('optimization_runs'))


@pytest.fixture(autouse=True)
def _no_claude_api_key(tmp_path, monkeypatch):
    """ClaudeのAPIキーの読み口(環境変数・`duocad_secrets.json`)を空にする。

    外すと、キーを置いたマシンでだけ `Server` が `anthropic` から始まり、既定のプロバイダを見る
    検査が落ちる(キーを使う検査は自分で置く)。実キーで課金される経路を検査から断つ意味もある。
    """
    from duocad.agent import providers

    monkeypatch.delenv(providers.ANTHROPIC_API_KEY_ENV, raising=False)
    monkeypatch.delenv(providers.ANTHROPIC_MODEL_ENV, raising=False)
    monkeypatch.setattr(providers, "SECRETS_PATH", tmp_path / "no-secrets.json")


@pytest.fixture()
def session(tmp_path) -> Session:
    """STEPを読まない空の文書。`models_dir` を使い捨てにしないと、保存がリポジトリの `duocad/models/` を汚す。"""
    return Session(None, 0.2, models_dir=tmp_path / "models", exports_dir=tmp_path / "export")


@pytest.fixture()
def box_session() -> Session:
    """`id="box"` の 30×30×40 の直方体を1つ直に置いたセッション(`session_support.session_with_box`)。"""
    return session_with_box()


@pytest.fixture()
def doc():
    """`id="box"` の 30×30×40 の直方体を1つ持つ文書(`document_support.document_with_box`)。"""
    return document_with_box()
