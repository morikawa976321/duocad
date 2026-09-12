---
name: duocad-verb-single-declaration
description: 判断(2026-08-20) — duocadのverbメタ情報(名前・引数・カテゴリ・プレビュー可否・MCPスキーマ)は `duocad/catalog.py` の単一宣言が正で、server.pyの4表とMCPのツール定義は導出。verb追加時に触るのはverb本体+宣言1箇所
created: 2026-08-20
updated: 2026-08-20
---

# duocadのverbは1箇所でしか宣言しない

**選択**: verbのメタ情報を `duocad/catalog.py` の `COMMANDS` 1箇所に集め、
`server.py` の4つの表(`PREVIEWABLE_VERBS` / `SKETCH_COMMANDS` / `FEATURE_COMMANDS` /
`FILE_COMMANDS`)と `mcp_server.py` の29ツールの定義・ハンドラ表を**そこからの導出**にした。

**理由**: v1.1以降のロードマップが move_face / revolve / shell / primitive / boolean / hole
というverb追加の繰り返しであり、多重登録のままだと同期作業と食い違いバグがverb数だけ増える。
実際、改修前は `fillet` の定義がサーバ実装コードだけで8箇所にあり、`extrude` は
`body_id` の有無が表ごとに食い違ったまま放置されていた(4表の同期を守るテストは無かった)。

**線の引き方**: frontend(`web/src/commands/*.ts`)だけは別に持つ。TS側が持つのは
**引数の集め方**(どのクリックを待つか・入力欄のid・ボタンのid)で、宣言には無い情報だから。
名前と分類(`category`)が一致することだけを `tests/test_ui_command_wiring.py` が機械で見る。

**同時に消した順序依存**: UIの送信経路は `isSketchVerb` / `isFeatureVerb` / `isFileVerb` を
順に当てて「どれにも当たらなければ `extrude_face`」だった —— 分類を書き忘れたverbが
**黙って別のverbとして送られる**形の失敗で、`CommandDef.category` 1つの参照に置き換えた。

**判断が要った点**: `extrude` の `body_id`(どのBodyへ足すか)はWSのコマンドには無く、
MCPとプレビューだけが受け取る。これは書き漏れではなく正しい姿と実測で確認した
(UIが送るのは `{distance, mode}` だけ)。`body_id` は対象集合ではなく名前なので、
宣言では「WSには出さない引数」1つとして表せる。

コミット: duocad `8c15339`。
