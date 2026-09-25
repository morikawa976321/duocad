"""検査・分析スクリプトが文書ファイルを開く定型(`Session` を立てて `open_document`)。"""
from pathlib import Path

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
from duocad import kernel
from duocad.session import Session


def open_session(path) -> Session:
    """`path` の文書(JSON)を開いた `Session`。`models_dir` はその親で、偏差は画面の既定 `kernel.DEFAULT_DEVIATION`。"""
    path = Path(path).resolve()
    session = Session(None, kernel.DEFAULT_DEVIATION, models_dir=path.parent)
    session.open_document(path.name)
    return session
