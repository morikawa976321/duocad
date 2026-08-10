"""minicad — 自作3DCADのコア。M0はGUI無しの骨格(文書・操作ログ・STEP入出力)のみ。

サブモジュール:
    document — Document / Body のデータモデル
    oplog    — Operation / OpLog(操作ログ。undo/redo/永続化/再生)
    io_step  — STEP読み込み・STEP/STL書き出し
    query    — 問い合わせ層のインタフェース(build123dをラップする境界)
    kernel   — OCP(OCCT)を直接叩く層。minicad配下でOCP.*をimportしてよいのはここだけ
"""
