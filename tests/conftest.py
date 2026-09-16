"""pytest 全体の下ごしらえ。

**`Session` の既定の保存先をテストごとの使い捨てディレクトリへ向ける。** 1操作ごとの
自動保存(`Session.autosave`。issue #5)は `models_dir` を渡していない `Session(None, 0.2)`
でも走るので、そのままだとリポジトリ直下の実 `models/` へ `untitled-<n>.json` が溜まる。
`Session.__init__` は `duocad.session.DEFAULT_MODELS_DIR` を読むので、その名前を差し替える。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _default_models_dir_in_tmp(tmp_path, monkeypatch):
    from duocad import session as session_module

    monkeypatch.setattr(session_module, "DEFAULT_MODELS_DIR", tmp_path / "default-models")
