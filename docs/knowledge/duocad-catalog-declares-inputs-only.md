---
name: duocad-catalog-declares-inputs-only
description: duocadのcatalog.pyはverbの入力(params)しか宣言しておらず出力の宣言が無いので、TS側のApplied型をcatalogから生成することはできない
created: 2026-08-27
updated: 2026-08-27
---

`duocad/catalog.py` の `COMMANDS` は「verbのメタ情報の唯一の宣言」だが、**宣言しているのは
入力だけ**である。1エントリが持つのは name / category / description / params / session /
ws / mcp / previewable / changes_document / resets_selection / uses_selection / note で、
**verbが何を返すかの宣言は無い**。

そのため `web/src/session.ts` の `Applied` 型(verbごとの応答ペイロード)を catalog から
生成する道は**今は存在しない**。応答の形の事実上の正は `Session` のラッパが組み立てる
戻り値dict(Pythonコード)であり、宣言ではない。生成を導入するなら**先にcatalogへ出力の
宣言を足す**という設計判断が要る。

**これは踏みやすい罠である。** `docs/CODE_SURVEY_ACTIONS.md` の3.5が「コード生成の仕組みは
無い」と書いているため、「catalogから生成すればよい」と即断しやすい(2026-08-27に実際に
一度そう結論して、catalog.pyを読み直して訂正した)。

代わりに採れる手は**突合の機械検査**で、`tests/test_entity_kind_tables.py`(3つの表の
キー集合の一致をソースの静的読み取りで見る)がこのリポジトリの既存の作法。
`tests/test_ui_command_wiring.py` の冒頭docstringも、TS側を宣言から生成しない理由を
別の層について明文化している。

