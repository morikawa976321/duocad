# duocad コード調査: 対応が必要な項目

`docs/CODE_SURVEY.md`(一次調査)→ `docs/CODE_SURVEY_EVAL.md`(妥当性評価)→ `docs/CODE_SURVEY_CONCLUSION.md`(実装を自分で確認した最終結論)という3段階の調査の結果、**実際にコード変更が要ると結論した項目だけ**を抜き出したものです。判定の詳しい根拠(なぜ他の項目は対応不要と判断したか等)は `CODE_SURVEY_CONCLUSION.md` を参照してください。

---

## 1. 今すぐ直した方がいいバグ

### 1.1 Bodyを削除してもキャッシュが残り続ける

- **場所**: `duocad/server.py` の `Session.delta()`(L620–641)
- **何が起きているか**: `delta()` は生きているBodyだけを見て差分を返すので、Bodyが削除されても `_geometry_cache` / `_edge_cache` / `_vertex_cache` / `_volume_cache` からそのBodyのエントリが消えない。文書を開き直したとき(`_replace_document`, L1258–1263)は全部まとめて `.clear()` されるので、そこでは問題ない。
- **影響**: 1つの文書を開いたまま形状の作成・削除(試行錯誤)を繰り返すセッションで、使われないキャッシュがメモリ上に線形に溜まり続ける。
- **対応案**: Bodyを削除する処理(該当verbの実装箇所)で、削除したBody IDに対応するキャッシュ4種を明示的に pop する。

### 1.2 AIループの進捗通知が失敗しても誰にも分からない

- **場所**: `duocad/agent/loop.py` の `AgentLoop._notify`(L546–553)
- **何が起きているか**: 進捗コールバック呼び出しが `except Exception: pass` で丸ごと握りつぶされている。ログ出力も一切ない。
- **影響**: UI側の進捗表示処理にバグがあっても、AIループ側は何事もなかったかのように進み続ける。原因不明の「進捗が更新されない」不具合の調査が困難になる。
- **対応案**: 最低限 `logging.warning("progress subscriber failed", exc_info=True)` のようなログを1行足す。

### 1.3 「保存済み」判定に使う `applied_count` がAI経由とUI経由で食い違う

- **場所**:
  - `duocad/mcp_server.py` の `DocumentTools.describe_document`(L273): `"applied_count": len(document.log.entries)`
  - `duocad/server.py` の `Session._applied_count`(L279で初期化、L895の `_after_logged_verb()` で加算): `state_meta()` が返す値
- **何が起きているか**: `undo()` は操作ログのエントリ数を減らすが、`Session._applied_count` は減らさない。そのため同じ文書に対して MCP(AI)経由で見る `applied_count` と WebSocket(UI)経由で見る `applied_count` が、undo操作後にずれる。
- **影響**: `web/src/App.svelte` の「保存済み」表示(`saved` の導出ロジック、L203–208)は `applied_count` の一致を前提にしている。AI操作とUndoを絡めた操作順序によっては、この前提が崩れて保存状態の表示を誤る可能性がある。
- **対応案**: 2つの `applied_count` の定義をどちらかに統一する。素直なのは `describe_document` 側も `Session._applied_count` を参照するように揃えること(あるいは逆に、Session側も log.entries の長さを都度計算する方式に揃える)。

### 1.4 複数クライアントが同時に同じ文書を編集すると操作の順序が乱れうる(**2026-08-27クローズ**)

> **【2026-08-27】対応案2を選択してクローズ。** 本人判断で「現時点では単一ブラウザ前提」と
> 決めたので、排他制御は入れない。制約は `duocad/server.py` の `Server` クラスdocstringと
> `README.md` §5「前提にしないこと」に明記済み。以下は当時の調査内容の記録。

- **場所**: `duocad/server.py` の `handle()`(L1648–1665)、`_apply()`(L1910–1921)
- **何が起きているか**: 個々のverb適用自体は同期処理でawaitを挟まないため壊れないが、あるWS接続が `await self._push(...)` で待っている間に、別のWS接続の操作が先に処理されうる。単一スレッド(イベントループ)内でのコマンド単位のインターリーブであり、形状データが破損するような重大な競合ではない。
- **影響**: 複数ブラウザタブ・複数ユーザーが同じ文書を同時に開いて操作する場合、操作ログや選択状態が意図と異なる順序で反映される可能性がある。1ブラウザだけで使う想定なら実害はない。
- **対応案(どちらか一方でよい)**:
  1. 複数クライアントの同時編集を今後もサポートする方針なら、Session全体を守る排他制御を入れる。
  2. サポートしない方針なら、「単一ブラウザでの利用を前提とする」という制約をコードコメントか設計ドキュメントに明記して、この項目をクローズする。

---

## 2. 直っているのに回帰テストが無いバグ(再発を検知できない)

以下3件は実装上は既に修正済みだが、それを保証するテスト関数が存在しない(モジュールdocstringに箇条書きで触れられているだけ)。将来のリファクタで静かに再発する可能性がある。

| 何のバグか | 実装済みの修正 | テストを追加すべき場所 |
|---|---|---|
| 参照STEPを読み込んだ直後に `join` すると最初の板が作れず `ValueError` になっていた | `duocad/import_editable.py`(L81–86)で、参照インポート直後に editable solid を追加してactiveにする | `tests/test_m2_regressions.py` |
| `set_parameter` を1回打つだけで作業対象(active body)が別のBodyへ黙って移っていた | `duocad/parameters.py` の `rebuild`(L210–213)が `saved_active` を復元 | `tests/test_m3_regressions.py` |
| 作り直し(`rebuild`)が途中で失敗すると、操作ログは無傷なのにBody/Sketchが0個という不整合な文書が残っていた | `duocad/parameters.py` の `rebuild`(L175–206)が失敗時にbodies/sketches/parameters/activeを復元 | `tests/test_m3_regressions.py` |

3件とも「参照STEPを移動しただけ」「パラメータを1回変えただけ」のような単純な操作で再発しうるため、優先度は高め。特に3番目は今のコードだと参照STEPのパスがずれるだけで再現しうるので最優先で追加を推奨。

---

## 3. リファクタリング(優先順位順)

いずれも「動作するが保守しづらい」ものであり、緊急のバグではない。着手する場合の順番の目安:

1. **`duocad/server.py` の分割**(現状2170行、物理行ベースでリポジトリ最大)。`Session` / `ToolsOnLoop` / `WebHandler` / `Server`(AI/エージェント配線含む)が同一ファイルに同居している。MCPの文書APIが `Session` に依存しているため、まずここを分けるのが後続の分割の前提になる。
2. **verb追加手順の一本化**。現状、verbを1つ足すには `duocad/verbs/` 本体、`Session` のラッパ、`DocumentTools` のラッパ、`catalog.py` の `COMMANDS` エントリ、UI側の引数収集の**5箇所**を手で揃える必要がある(catalog.py 冒頭docstringに明記済み)。README にも「汎用ディスパッチャが要る(未着手)」とある。
3. **`duocad/kernel.py` の分割**(1642行)。STEP/STL入出力・テセレーション・モデリング・スケッチ平面・形状種別テーブルが同居している。
4. **`Server._dispatch` の表駆動化**。`ws="special"` な特殊分岐が15個手書きの `elif` として残っている。

   > **【2026-08-27】この項目のうち命名の非対称は解消済み(残りは表駆動化のみ)。** `extrude` verbが `Session.extrude_regions` を、`extrude_face` verbが `Session.extrude` を呼ぶという逆転があったので、`Session.extrude`(領域) / `Session.extrude_face`(面)へ改名してverb名と揃えた。`catalog.py` の `session=` 2件・`_dispatch` の `extrude_face` 分岐・MCPの `DocumentTools` ラッパ・呼んでいたテスト7ファイルも同時に直してある(テスト526件緑)。`kernel.extrude_regions` は幾何側の関数名で逆転していないため触っていない。
5. **`web/src/session.ts` の `Applied` 型の手動同期**。バックエンドが返すverbの種類ごとにTypeScriptの判別可能ユニオン型を手で書き足しており、コード生成の仕組みは無い。verb追加のたびに型定義が漏れるリスクがある(優先度は上記4件より低い — 図形種別のテーブルは既に `tests/test_entity_kind_tables.py` が3表とも突合しているため、そちらは対応不要)。
