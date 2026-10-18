"""検査・分析スクリプトが文書を扱う定型(`Session` を立てて `open_document`、または使い捨ての `Session`)。"""
import atexit
import shutil
import tempfile
from pathlib import Path

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
from duocad import kernel
from duocad.session import Session


def open_session(path) -> Session:
    """`path` の文書(JSON)を開いた `Session`。`models_dir` はその親で、偏差は画面の既定 `kernel.DEFAULT_DEVIATION`。"""
    path = Path(path).resolve()
    session = Session(kernel.DEFAULT_DEVIATION, models_dir=path.parent)
    session.open_document(path.name)
    return session


def scratch_session(deviation: float) -> Session:
    """使い捨ての `models_dir` / `exports_dir` を持つ `Session`(プロセスの終了時に消える)。

    `models_dir` を渡さない `Session` は、1操作ごとの自動保存が無題の文書(`untitled-<n>.json`)をリポジトリの
    `models/`(本人の開発版が使う場所)へ作り、文書一覧(`list_documents`)を件数に比例して遅くする。
    文書を作って検査・測定するだけのスクリプトはこれで作る。
    """
    root = Path(tempfile.mkdtemp(prefix="duocad-checks-"))
    atexit.register(shutil.rmtree, root, True)
    return Session(deviation, models_dir=root / "models", exports_dir=root / "export")
