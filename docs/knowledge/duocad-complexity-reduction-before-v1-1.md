---
name: duocad-complexity-reduction-before-v1-1
description: 判断(2026-08-20) — duocadはv1.1のverb追加に入る前に複雑性削減5件(WS引数統一・verb登録一本化・選択リセット・kind表化・小粒削除)を先に実施と本人が確定。実行順はB→A→C→E→D。見送り3件も確定
created: 2026-08-20
updated: 2026-08-20
---

# duocad: v1.1の前に複雑性削減5件を先行実施

2026-08-20、本人が確定。v1.1以降のverb追加(move_face / revolve / shell / primitive / boolean / hole /
拘束ソルバ等)に入る前に、以下5件をこの順(B→A→C→E→D)で実施する:

1. **B — WS引数の選択統一**([[duocad-target-set-taken-only-from-selection]])
2. **A — verb登録の一本化(1 verb = 1宣言)**: server.pyの4レジストリ+mcp_server.pyのスキーマ394行と
   handlers辞書を単一宣言からの導出に置換。frontendの3判定関数と暗黙フォールバックも `CommandDef.category` へ
   ([[duocad-verb-single-declaration]])
3. **C — 形状変更時の選択全リセット**([[duocad-selection-resets-on-shape-change]])
4. **E — 小粒削除4件**: `Operation.selection` 廃止(実質未使用)・`_body_meta`/`_body_summary` 統一・
   `bodyGeometry`(コア状態の第3のコピー)削除・`Applied` 型のdiscriminated union化
5. **D — Entity.kind分岐のテーブル化**: 処理ごとのkind分岐(コア31箇所4ファイル+TS3ファイル)を
   図形ごとの表へ反転(import境界のためPython側は純幾何表+OCP表の2表)

**判断の背景(2026-08-19〜20のsonnetサブエージェント3本+docs抽出の調査)**: verb 1つの追加はコア4ファイル
9箇所の同期、filletの定義はサーバ実装だけで8箇所、extrudeの引数は2レジストリで既に食い違い(同期テスト無し)。
ロードマップはverb追加の繰り返しなので、多重登録の解消が最も効くという評価を本人が採用した。

**あわせて見送りを確定**(AIの提案を本人が採用): エラー封筒のWS/MCP統一(WS側の構造化コードに消費者が
おらず過剰仕様)、import_step 3verb体制の統合(参照のみ読み込みは意図的非対称の可能性・利用実態を見てから)、
provenanceスロット型統一・`_REBUILDING` 除去・HistoryPanel summarize移設(効果小・ついでがあれば)。
