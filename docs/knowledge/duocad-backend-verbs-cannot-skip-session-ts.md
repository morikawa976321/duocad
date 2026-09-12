---
name: duocad-backend-verbs-cannot-skip-session-ts
description: 運用知見 — duocadでverbを1つ足すと `web/src/session.ts` の型宣言(Applied union と ServerState)も同時に直さないと既存テストが赤になる。「画面側には手を付けない」というタスク指示と両立しないので、タスクを切るときは型宣言をバックエンド側に含める。ただし `ws="none"`(MCP専用)のverbは検査の対象外という抜け穴がある
created: 2026-08-31
updated: 2026-09-06
---

# duocadは「バックエンドだけ」で切れない —— session.ts の型宣言が検査対象

duocadで**verbを1つ足すと `web/src/session.ts` も必ず触ることになる**。
`tests/test_applied_type_sync.py` が2つの一致を機械で見ているため:

- `catalog.COMMANDS` のverbのうち状態配信になるもの == TSの `Applied` union の `verb:` リテラル
- `Session.state_meta()` が返すキー == TSの `ServerState` のフィールド名

したがって「コアとプロトコルだけやる。画面(`web/`)には手を付けない」という切り方をすると、
**バックエンドのタスクが自分では緑にできない**(2026-08-31の
`20260831-0108-duocad-optimize-mode-verbs-backend` で実際に踏んだ。4 verb と `state_meta` の
新キーで7本が赤になった)。触るのは**型宣言だけ**で画面の挙動は変わらないので、
`session.ts` はバックエンド側のタスクに含めるのが正しい線引きである。

**一方 `tests/test_ui_command_wiring.py` の `ENTER_REQUIRED` は逆で、画面側に含める。**
あちらは `web/src/commands/*.ts` の `CommandDef` からの導出と件数一致を見るので、
`CommandDef` を書く画面タスクと同時でなければ緑にならない
(バックエンドだけで4件足すと赤くなる)。タスクファイルが「ENTER_REQUIRED に4件足せ」と
「web/ に手を付けるな」を同時に書いていて矛盾していた。

**切り分けの目安**: **宣言・型・種別の表** = バックエンド /
**引数の集め方・描画** = フロントエンド。ファイル名で切ると外す ——
`web/src/commands/command.ts` は「引数の集め方」の側だが、`SelectableKind` と
`KIND_COUNTERS`(`Record<SelectableKind, string>`)という**コアの `SELECTABLE_KINDS` の写し**を
持っており、コアに選択種別を足したらここも同時に埋めないと**件数表示が `undefined` になる**
(型検査も実行時例外も出ない)。同タスクの親QAがこれを拾った。

**この種のずれは `tsc` では出ない。** TS側の参照の道具は `ref as ShapeRef` の形の
キャストで書かれているので、ユニオンを広げてもコンパイルは通ってしまう。**宣言そのものを
静的に読んで突き合わせる検査**(`tests/test_optimize_setup.py` の
`test_the_typescript_selectable_kinds_match_the_core`)を置いて初めて赤で出る。

**例外: `ws="none"`(MCP専用)のverbはこの検査の対象外**(2026-09-06実発、
`mirror_sketch_entities` 新設時)。`test_applied_type_sync.py::_ws_document_changing_commands()`
の対象条件は `command.ws != "none" and command.changes_document` であり、「ブラウザから
踏めるもの」を対象にする設計だからである。ところが `add_mesh_body`(既存)や
`mirror_sketch_entities`(新設)のように、**ブラウザには開けていない(`ws="none"`)のに
操作ログには載る本物のverb**が存在し、これらはMCP経由で呼ばれても開いている全ブラウザへ
`Session._emit`→`add_listener`経由で状態配信される(`ws` は WebSocket*受信*側の開放範囲を
決めるだけで、Session内部の変更通知の配信先を絞る仕組みではない)。にもかかわらず
`Applied` union に型が無くてもこの検査は緑のまま通る。**MCP専用verbを足すとき、
Applied型を省略してよいかどうかはこの抜け穴を踏まえて都度判断すること**(型を足さない
選択をした場合は「なぜ対象外か」をverbのcatalog宣言・taskの契約節に明記する)。
