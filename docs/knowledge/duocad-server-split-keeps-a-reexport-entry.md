---
name: duocad-server-split-keeps-a-reexport-entry
description: duocadのserver.py(2170行)分割は2段構えで、server.py自体は再輸出だけの入口として残すと決めた(2026-08-27)
created: 2026-08-27
updated: 2026-08-27
---

2026-08-27、duocadの `server.py`(2170行・リポジトリ最大)の分割方針を本人が承認した
(コード調査 `docs/CODE_SURVEY_ACTIONS.md` の項目3.1)。理由の明言は無い。決まったのは以下:

- **2段構えにする。** 第1段=クラス境界だけで割る(振る舞いゼロ変更)、第2段=`Session` から
  配信側(`state`/`delta`/版/キャッシュ)を `DocumentView` として切り出す。
  taskboxに `20260827-2330` / `-2331` として積んだ。
- **`duocad/server.py` 自体は「再輸出だけの薄い入口」として残す。** `duocad.server` から
  名前を取っているのは `mcp_server.py` が6つ・テストが17本で計23箇所あり、入口を残せば
  **呼び出し側を1行も触らずに分割できる**。移動と呼び出し側の変更を同時にやらないための措置。
- **verbの薄いラッパ16本の1本化(項目3.2)と `_dispatch` の表駆動化(項目3.4)は分割に混ぜない。**
  ファイル分割で解決する問題ではなく、混ぜると後で作り直しになる。

第2段が主に狙うのは行数ではなく**キャッシュの寿命管理が1クラスに閉じること**(いまは
`_replace_document` の `.clear()` 5連発・`preview()` の変則・剪定がバラバラの場所にある)。

同じ調査からの判断は [[duocad-code-survey-decisions]] /
[[duocad-assumes-a-single-browser]]。
