"""duocad — 自作3DCADのコア。M0がGUI無しの骨格(文書・操作ログ・STEP入出力)、
M1でビューポート(WebSocketサーバ + web/)と面の押し出しが載る。

サブモジュール:
    document — Document / Body のデータモデル
    oplog    — Operation / OpLog(操作ログ。undo/redo/永続化/再生)
    io_step  — STEP読み込み・STEP/STL書き出し
    query    — 問い合わせ層のインタフェース(build123dをラップする境界)
    kernel   — OCP(OCCT)を直接叩く層。duocad配下でOCP.*をimportしてよいのはここだけ
    verbs    — verb層(1 verb 1モジュール)。M1で extrude_face / make_editable_copy
    server   — WebSocketサーバ + web/dist の静的配信(アプリ層との唯一の接点)

パッケージのimport時に**verbを定義したモジュールを読み込む**。oplogのレジストリ
(verb名 -> (do, undo))はverb側が `register_verb()` で埋める設計なので、これが無いと
`OpLog.from_json(...).replay(doc)` だけを使う経路でverbが未登録になりうる。
verbが増えたらここに1行足す(oplogにverb名の分岐を書かないための対価)。

`server` はここでimportしない(webサーバを立てない用途でパッケージを読むほうが普通で、
`python -m duocad.server` で起動すれば必要なものは自分でimportする)。
"""
from . import io_step as _io_step  # noqa: F401  verb "import_step" をレジストリへ登録する
from . import verbs as _verbs  # noqa: F401  verb "extrude_face" / "make_editable_copy" を登録する
