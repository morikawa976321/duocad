"""リポジトリの基準パスと、そこから出る既定の置き場。

`DEFAULT_WEB_DIST`(静的配信・`duocad/net/http.py`)と `DEFAULT_MODELS_DIR`(保存/開く・
`duocad/session.py`)は同じ `REPO_ROOT` から出るので、`Path(__file__).resolve().parent.parent`
をここ1箇所だけに書く。
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WEB_DIST = REPO_ROOT / "web" / "dist"
#: 保存/開くの既定の置き場。**リポジトリ直下・gitignore対象** —— 毎回パスを手打ちする
#: 代わりに、決まったフォルダの中身を一覧から選ぶ形にする。無ければ
#: `Session._resolve_document_name` が作る。テストは `Session(models_dir=tmp_path)` で
#: ここを差し替えて実フォルダを触らない。
DEFAULT_MODELS_DIR = REPO_ROOT / "models"
