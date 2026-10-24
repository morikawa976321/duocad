"""duocad — 自作3DCADのコア。GUI無しの骨格(文書・操作ログ・STEP入出力)に加えて、
ビューポート(WebSocketサーバ + web/)と面の押し出しを持つ。

層をまたぐ契約:
    kernel — duocad配下で OCP.* を import してよいのはここだけ
    catalog — verbの単一宣言。server.py の4つの表も mcp_server.py のツール定義もここから導出
    mcp_server — アプリ層ではなく**コアのもう一人のクライアント**(ブラウザと同じ文書を叩く)

下のimportは未使用に見えるが必須。oplogのレジストリ(verb名 -> (do, undo))はverb側が
`register_verb()` で埋めるので、これが無いと `OpLog.from_json(...).replay(doc)` だけを使う
経路でverbが未登録になる。verbが増えたらここに1行足す。
"""
import os

# 外部の罠: numpy と scipy は別々に OpenBLAS を持ち、読み込んだ時点で論理コア数ぶんの作業域を確保する(14コアの機械で
# サーバ本体 1.11 → 0.29 GB、面の編集・干渉チェックの子 0.55 → 0.14 GB。2026-10-24 実測、docs/DEV_NOTES.md「環境」)。
# 読み込んだ後に threadpoolctl で絞っても確保済みの分は返らないので、numpy より先に読まれるここで決める。子は環境を継ぐ。
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from . import parameters as _parameters  # noqa: F401  verb "set_parameter" を登録する
from . import sketch as _sketch  # noqa: F401  スケッチの4 verb を登録する
from . import verbs as _verbs  # noqa: F401  extrude / import_step / pattern_rectangular / move_* を登録する
