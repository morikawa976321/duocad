"""duocad — 自作3DCADのコア。M0がGUI無しの骨格(文書・操作ログ・STEP入出力)、
M1でビューポート(WebSocketサーバ + web/)と面の押し出しが載る。

サブモジュール:
    document — Document / Body のデータモデル
    oplog    — Operation / OpLog(操作ログ。undo/redo/永続化/再生)
    io_step  — STEP読み込み・STEP/STL書き出し
    query    — 問い合わせ層のインタフェース(build123dをラップする境界)
    kernel   — OCP(OCCT)を直接叩く層。duocad配下でOCP.*をimportしてよいのはここだけ
    sketch   — スケッチ(M2)。Sketch/Entity/Patternのデータモデルとスケッチの4 verb
    parameters — 名前つきの寸法値(M3)。set_parameter と値の登録・解決
    region   — 領域(M2)。Region/RegionRefと領域検出
    selection — 選択機構(M3.5)。可視性を含む「何が選べるか」の判定
    verbs    — verb層(1 verb 1モジュール)。M1で extrude_face / make_editable_copy、
               M2で extrude、M3で pattern_rectangular、M3.5で set_visible、
               2026-08-11の前倒しで fillet / chamfer(円弧はスケッチ側の図形なので sketch)、
               2026-08-15に import_step_editable(読んだら編集できる状態まで作る1本)
    server   — WebSocketサーバ + web/dist の静的配信(アプリ層との唯一の接点)
    mcp_server — MCPサーバ(M4の最小の先取り)。AIがコアを直接叩く口。
                 **アプリ層ではなくコアのもう一人のクライアント**で、ブラウザと同じ文書を叩く

パッケージのimport時に**verbを定義したモジュールを読み込む**。oplogのレジストリ
(verb名 -> (do, undo))はverb側が `register_verb()` で埋める設計なので、これが無いと
`OpLog.from_json(...).replay(doc)` だけを使う経路でverbが未登録になりうる。
verbが増えたらここに1行足す(oplogにverb名の分岐を書かないための対価)。

`server` / `mcp_server` はここでimportしない(サーバを立てない用途でパッケージを読むほうが
普通で、`python -m duocad.mcp_server` で起動すれば必要なものは自分でimportする)。
**起動コマンドは `duocad.mcp_server` の1本だけ**で、`server` は `Session` / `Server` /
静的配信を提供するモジュール(起動はしない)。
"""
from . import io_step as _io_step  # noqa: F401  verb "import_step" をレジストリへ登録する
from . import parameters as _parameters  # noqa: F401  verb "set_parameter" を登録する
from . import sketch as _sketch  # noqa: F401  スケッチの4 verb を登録する
from . import verbs as _verbs  # noqa: F401  extrude_face / make_editable_copy / extrude / pattern_rectangular を登録する
