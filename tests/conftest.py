"""pytest 全体の下ごしらえ。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from duocad.server import Session
from duocad.topopt import trace
from document_support import document_with_box
from session_support import session_with_box

#: 全件を並べるワーカー数。縛るのは CPU(14コア)ではなくメモリで、別のセッションが同時に回しても空きに収まる数にする
#: (4本でピーク 3.2 GB、8本で 4.9 GB・時間は 15 秒しか縮まない。2026-10-24 実測、docs/DEV_NOTES.md「環境」)。
WORKERS_FOR_THE_WHOLE_SUITE = 4

#: 形状最適化だけを守るテスト。開発も製品での公開も凍結中なので、既定の実行では集めない(docs/STABLE.md「安定版で出さない機能」)。
FROZEN_TOPOPT = Path(__file__).resolve().parent / "topopt"


@pytest.hookimpl(optionalhook=True)  # xdist を `-p no:xdist` で外したとき、未知のフックとして収集が止まる
def pytest_xdist_auto_num_workers(config):
    """`-n auto`(pyproject の既定)のワーカー数。ファイルやテストだけを名指ししたときは並べない
    (ワーカーの立ち上げに毎回約10秒かかり、1ファイルなら直列の方が速い。2026-10-24 実測)。"""
    if os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS"):
        return None
    if config.args and all("::" in arg or arg.endswith(".py") for arg in config.args):
        return 0
    return WORKERS_FOR_THE_WHOLE_SUITE


def pytest_addoption(parser):
    parser.addoption("--topopt", action="store_true", help="凍結中の形状最適化のテスト(tests/topopt/)も集める")


def pytest_report_header(config):
    if not _collects_topopt(config):
        return "tests/topopt/(凍結中の形状最適化)は集めない。集めるなら --topopt"


def pytest_ignore_collect(collection_path, config):
    if collection_path == FROZEN_TOPOPT and not _collects_topopt(config):
        return True
    return None


def _collects_topopt(config) -> bool:
    """`--topopt` か、`tests/topopt/` の中を名指ししたとき。"""
    if config.getoption("topopt"):
        return True
    named = (Path(config.invocation_params.dir, arg.split("::")[0]).resolve() for arg in config.args)
    return any(path == FROZEN_TOPOPT or FROZEN_TOPOPT in path.parents for path in named)


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


@pytest.fixture(autouse=True)
def _local_settings_in_tmp(tmp_path, monkeypatch):
    """この clone の設定ファイル(`duocad_settings.json`)を使い捨てにする。外すと、GPU を選んだ機械でだけ
    `Server` が `fem.FEM_DEVICE = "gpu"` で始まり、設定ダイアログの検査が実ファイルを書き換える。"""
    from duocad import local_settings
    from duocad.topopt import fem

    monkeypatch.setattr(local_settings, "PATH", tmp_path / "duocad_settings.json")
    monkeypatch.setattr(fem, "FEM_DEVICE", "cpu")


@pytest.fixture()
def session(tmp_path) -> Session:
    """STEPを読まない空の文書。`models_dir` を使い捨てにしないと、保存がリポジトリの `duocad/models/` を汚す。"""
    return Session(0.2, models_dir=tmp_path / "models", exports_dir=tmp_path / "export")


@pytest.fixture()
def box_session() -> Session:
    """`id="box"` の 30×30×40 の直方体を1つ直に置いたセッション(`session_support.session_with_box`)。"""
    return session_with_box()


@pytest.fixture()
def doc():
    """`id="box"` の 30×30×40 の直方体を1つ持つ文書(`document_support.document_with_box`)。"""
    return document_with_box()
