import pytest

from duocad.topopt import trace


@pytest.fixture(autouse=True)
def _optimization_runs_go_to_a_temporary_directory(tmp_path_factory, monkeypatch):
    """製品経路を通すテストの実行記録を一時フォルダへ向ける。外すと `tests/` を1回回すごとに
    `output/optimization_runs/` へ記録フォルダが12個・約18MB溜まる(2026-09-17 実測)。"""
    monkeypatch.setattr(trace, 'DEFAULT_ROOT', tmp_path_factory.mktemp('optimization_runs'))
