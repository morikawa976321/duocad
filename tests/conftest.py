import pytest

from duocad.server import Session
from duocad.topopt import trace
from document_support import document_with_box
from session_support import session_with_box


@pytest.fixture(autouse=True)
def _optimization_runs_go_to_a_temporary_directory(tmp_path_factory, monkeypatch):
    """製品経路を通すテストの実行記録を一時フォルダへ向ける。外すと `tests/` を1回回すごとに
    `output/optimization_runs/` へ記録フォルダが12個・約18MB溜まる(2026-09-17 実測)。"""
    monkeypatch.setattr(trace, 'DEFAULT_ROOT', tmp_path_factory.mktemp('optimization_runs'))


@pytest.fixture()
def session(tmp_path) -> Session:
    """STEPを読まない空の文書。`models_dir` を使い捨てにしないと、保存がリポジトリの `duocad/models/` を汚す。"""
    return Session(None, 0.2, models_dir=tmp_path / "models")


@pytest.fixture()
def box_session() -> Session:
    """`id="box"` の 30×30×40 の直方体を1つ直に置いたセッション(`session_support.session_with_box`)。"""
    return session_with_box()


@pytest.fixture()
def doc():
    """`id="box"` の 30×30×40 の直方体を1つ持つ文書(`document_support.document_with_box`)。"""
    return document_with_box()
