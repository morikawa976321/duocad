"""リポジトリの基準パスと、そこから出る既定の置き場。

`DEFAULT_WEB_DIST`(静的配信)・`DEFAULT_MODELS_DIR`(保存/開く)・`DEFAULT_EXPORTS_DIR`
(STEP/STL書き出し)は同じ `REPO_ROOT` から出る。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WEB_DIST = REPO_ROOT / "web" / "dist"
#: 保存/開くの既定の置き場。リポジトリ直下・gitignore対象。無ければ
#: `DocumentStore.resolve_document_name` が作る。テストは `Session(models_dir=tmp_path)` で差し替える。
DEFAULT_MODELS_DIR = REPO_ROOT / "models"
#: STEP/STL書き出しの既定の置き場。リポジトリ直下・gitignore対象。相対パスはここ基準。
#: テストは `Session(exports_dir=tmp_path)` か `conftest` の差し替えで実フォルダを触らない。
DEFAULT_EXPORTS_DIR = REPO_ROOT / "export"


def resolve_user_path(path: Any) -> Path:
    """受け取ったパスを絶対パスにする(相対はサーバの作業ディレクトリ基準)。

    ブラウザからは `model.json` のような相対パスが来る。解決はここ1箇所で行う。
    """
    resolved = Path(str(path)).expanduser()
    return resolved if resolved.is_absolute() else (Path.cwd() / resolved).resolve()
