"""`tools/`・`topopt_checks/`・`web/scripts/` の道具のテストが共有する足場。"""
from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

from duocad.paths import REPO_ROOT


def load_tool(monkeypatch, name: str):
    """`tools/<name>.py` を読み込む(`tools/` は import できるパッケージではない)。"""
    path = REPO_ROOT / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def cp932_stdout() -> io.TextIOWrapper:
    """日本語 Windows でコンソールでない標準出力(パイプ・リダイレクト)と同じ、cp932 で厳密に書く出力。"""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp932", errors="strict")


def written(stream: io.TextIOWrapper) -> str:
    stream.flush()
    return stream.buffer.getvalue().decode("utf-8")


def default_models_dir_is_untouched(tmp_path: Path) -> bool:
    """`conftest` が既定の保存先を `tmp_path/default-models` に向けてある。そこに何も作られていないこと。"""
    folder = tmp_path / "default-models"
    return not folder.exists() or not any(folder.iterdir())
