# CODE_SURVEY 最終結論

```yaml
conclusion_date: 2026-08-27
sources:
  - docs/CODE_SURVEY.md       # 一次調査
  - docs/CODE_SURVEY_EVAL.md  # 二次評価(妥当性再監査)
method: 三次独立検証。5本の並列サブエージェントに個別claimのファクト抽出(行番号・引用のみ、判定はさせない)を行わせ、
  本書の筆者(Claude)が実装を自分で読んだ上で最終判定を下した。B003のみ、争点の核心(ToolsOnLoop.call())を
  サブエージェント経由ではなく自分で直接Grep/Readして確認した。
pytest: フルスイート再実行はしていない(一次調査の521 passed, 2 skippedを踏襲。これ自体は一次・二次とも未再走)。
verdict_enum: [agree_survey, agree_eval, correct_both, needs_action]
```

凡例: `agree_survey`=一次調査の記載を最終的に支持 / `agree_eval`=二次評価の指摘(nuance/overstated等)を最終的に支持 /
`correct_both`=両文書の記載のどちらも部分的に不正確で、本書で訂正 / `needs_action`=判定は一致し、対応が必要

---

## bugs.active

| id | 一次sev | 最終判定 | 結論 |
|----|---------|----------|------|
| B001 | medium | agree_survey / needs_action | `delta()`(L620-641)は生存Bodyだけ列挙し削除済みキャッシュを掃除しない。`_replace_document`(L1258-1263)は全キャッシュを`.clear()`する非対称を確認。**severity medium は妥当。長時間セッションでBody作成・削除を繰り返す運用(パラメトリック試行錯誤)で線形に蓄積するため、修正推奨** |
| B002 | low | agree_eval | `_revision_counter`保持は`_replace_document`直前のコメント(L1264-1267)通り意図的な設計判断(版番号を1へ戻すと「版が同じ=未変更」の誤判定を防ぐため)。dead key蓄積は事実だが、これは「バグ」ではなく明示された設計トレードオフの副作用。対応不要、severity low で妥当 |
| B003 | high/suspected | **correct_both** | `ToolsOnLoop._run_on_loop`(L243-259)を自分で確認: AI実行スレッド・MCP HTTPスレッドからの文書アクセスは`asyncio.run_coroutine_threadsafe`でイベントループへ確実に投げ返しており、**この経路(スレッド跨ぎ)は設計通り安全**。残るリスクは一次調査が指摘した通り「WS接続Aが`await _push()`で待っている間にWS接続Bの`_apply()`が割り込む」という**同一イベントループ内でのコマンド粒度インターリーブ**(二次評価の指摘と一致)。`_apply()`自体は`await`を挟まず同期実行(L1910-1921)なので**verb単体の破損は起きない**。実害は「操作ログ・選択状態が、複数タブ/複数ユーザーが同一文書を同時編集した場合に、想定と異なる順序で見える」という論理的競合に限定される。単一ブラウザ利用が前提なら実害はほぼ無いが、コードは`_send_locks`や複数接続へのbroadcastを前提に書かれており、複数クライアント同時編集は想定使用法に含まれる。**総合してseverityは high→medium相当に下方修正が妥当**(データ破損ではなく状態の見え方の不整合であるため) |
| B004 | low | agree_survey | `myClientOps`(App.svelte)は`.add()`のみで`.delete()`/`.clear()`皆無。確認済み。severity lowで妥当(長時間の1セッションでのみ蓄積、ページリロードで消える) |
| B005 | low | agree_eval | `edgesAsked`/`verticesAsked`はエラー時(L261-262)・切断時(L293-295)にclearされる。一次調査の「削除しない」は不正確で、二次評価の補足が正しい。蓄積は「接続が生きている間に辺/頂点を取得し続けた場合」のみに限定される、より狭い問題。severity lowのまま妥当 |
| B006 | medium | agree_survey | `AgentLoop._notify`(loop.py L546-553)は`except Exception: pass`で購読者側の例外を握りつぶす。ログ出力なし。確認済み、対応推奨(最低限`logging.warning`を追加) |
| B008 | low | agree_survey | `saved`導出(App.svelte L203-208)の`===`比較ロジックとdocstringの非対称説明が完全一致。実装は意図通りで、severity lowの通り実害は「保存済みなのに未保存と出る」という保守的な誤表示のみ |

## bugs.latent

| id | 最終判定 | 結論 |
|----|----------|------|
| B007 | agree_survey | `speculative.py`(L13-18)が明記する通り、`SNAPSHOTS`は`parameters.rebuild`系(ログ再生系)のverbで巻き戻らない。現状`previewable=True`は`extrude`/`extrude_face`/`fillet`/`chamfer`の4つのみで`set_parameter`は`False`(catalog.py)。未発火のlatent bugという評価は妥当。**`previewable_verbs()`に再生系verbを足す際は`speculative.py`も同時に直す」という運用上の警告がコード自身に書かれている点は、将来の実装者への十分な牽制として機能している** |

## bugs.fixed_guarded

| id | 最終判定 | 結論 |
|----|----------|------|
| B-F01, B-F02, B-F03, B-F07, B-F08, B-F09 | agree_eval | 該当テスト関数の実在を確認。`fixed_guarded`の定義を満たす |
| B-F04, B-F05, B-F06 | **agree_eval / needs_action** | 3件とも該当ファイルの`def test_`一覧に対応する関数が存在せず、モジュールdocstringの箇条書き項目としてのみ言及されていることを確認(B-F04: test_m2_regressions.py 項目4、B-F05: test_m3_regressions.py 項目1、B-F06: 同項目2)。実装(`import_editable.py`のactive body設定、`parameters.rebuild`のsaved_active復元・失敗時ロールバック)は現行コードに存在するが、**回帰を検知する契約テストが無い**。`fixed_guarded`(定義:「回帰テストで保護済み」)への分類は誤りであり、`missing_guard`が正しい。**3件ともテスト追加を推奨**(特にB-F06は「参照STEPを移動しただけで起きる」と記載されており再発コストが低い) |

---

## refactor_high

| id | 最終判定 | 結論 |
|----|----------|------|
| R001, R002, R009, R010 | agree_eval | ファイル構成・行数(server.py 2170行/kernel.py 1642行/mcp_server.py 1302行、物理行)を確認。クラス同居・責務混在の指摘は妥当 |
| R003 | agree_eval | catalog.py冒頭docstring(L12-18)にverb追加の5手順(2:Sessionラッパ、3:DocumentToolsラッパ)を確認。README L557に「汎用ディスパッチャが要る（未着手）」を一致する文言で確認 |
| R004 | agree_eval | `ws="special"`は文字列一致では17だが実コマンド数は**15**(catalog.py実測)。`extrude`(catalog)→`session="extrude_regions"`、`extrude_face`(catalog)→`session="extrude"`という命名非対称も確認。一次調査の「17件」は文字列ヒットとコメント2件を区別していない粗さがあり、二次評価の15件が正確 |
| R005 | **correct_both / needs_action(縮小)** | `tests/test_entity_kind_tables.py`を自分で確認: `_ts_entity_kind_table_keys()`(L22-36)が`web/src/entityKinds.ts`を実際に読み込み正規表現でキー集合を抽出し、`test_the_three_entity_kind_tables_agree_on_their_key_set`が sketch/kernel/entityKinds.ts の**3表を実際に突合**している。一次調査の「TS側は無検査」は**stale(既に事実誤認)** — 二次評価の指摘を全面支持。**残る正当な課題はR005のうち「`Applied`ユニオン型がFE/BEで手同期」の部分のみ**(session.tsのApplied型は14種のverb variantを手書き列挙、コード生成なしを確認)。図形種別3表の話は課題として取り下げるべき |
| R006 | agree_survey / correct_eval補足 | viewport.ts(2073行)の`export`は`Viewport`/`createViewport`だけでなく、`FaceGeometry`等11個の型/interfaceも公開している(二次評価が「公開はViewport+createViewportのみ」としたのは不正確)。ただしこれらはBody描画・ピック等のペイロード形状を表す型であり、内部ロジックの分割不足という一次調査の本質的な指摘(単一クロージャへのBody描画/ピック/スケッチ/領域/プレビュー集約)を覆すものではない |
| R007 | agree_survey | App.svelte(1254行)に辺/頂点取得(L582-641)・保存状態(L181-208)・ピック翻訳(L677-770)・`runCommand`(L852-918)が同居することを確認 |
| R008 | **correct_both / needs_action** | 二次評価の指摘通り、`measure`(mcp_server.py L579-634)は選択済み形状の計測(面積・法線・長さ等)であり、スケッチentity組み立てではない — 一次調査がR008の根拠に`measure`を含めたのは不正確。**ただし実質的な重複は残っている**: `describe_document`の`applied_count`は`len(document.log.entries)`(mcp_server.py L273)、`state_meta`(server.py)の`applied_count`は`Session._applied_count`(L279初期化、L895で`undo()`後は減らない)。**undo操作後、AI側(describe_document)とUI側(state_meta)で`applied_count`の値が食い違う**ことを実装コードで確認した。これは二次評価が`deltas_vs_survey`項目5で示唆した通り「R008の実害の芯」であり、単なるリファクタ観点でなく**B008(保存状態表示)と同種の実害が二重に存在するcorrectnessバグ**として扱うべき。R008は`refactor`ではなく`bugs.active`相当に格上げを推奨 |

---

## file_metrics

物理行(`Get-Content`既定/UTF-8改行数)で再計測し、一次調査(PowerShell `Measure-Object -Line`、空行除外)の値と突合:

| path | 一次(PS/空行除外) | 物理行 | 順位変化 |
|------|-------------------|--------|----------|
| `duocad/server.py` | 1610 | **2170** | 物理行では**最大**(viewport.tsを逆転) |
| `web/src/viewport.ts` | 1702 | 2073 | 物理行では2位 |
| `duocad/kernel.py` | 1384 | 1642 | 順位変わらず |
| `duocad/mcp_server.py` | 1145 | 1302 | 順位変わらず |
| `web/src/App.svelte` | 1180 | 1254 | 順位変わらず |
| `duocad/catalog.py` | 926 | 977 | 順位変わらず |

**二次評価の指摘(「物理行ではserver.pyが最大」)を支持**。一次調査の「largest = viewport.ts」は測定方法(空行除外・日本語コメント率の差)に依存した相対的な主張であり、`server.py`は日本語docstring/コメントの割合が高いため空行除外基準で過小に見えていた。**保守コスト評価に使うなら物理行基準の方が実態に近く、R001(server.py分割)の優先度は一次調査の記載以上に高いと判断すべき**。

---

## priority_matrix(最終)

```yaml
bugs_fix_first:
  - B001   # cache lifecycle on body delete — 確認済み、蓄積は実運用で線形増加
  - B006   # silent progress callback failures — 確認済み、ログ追加のみで対応可能
  - R008相当のバグ性課題  # applied_count の AI/UI 不一致(undo後) — 新規に確認、要修正
  - B003   # severity medium に格下げ。複数クライアント同時編集を仕様として維持するなら対応、単一ブラウザ前提を明文化するなら見送り可

test_gap_fix_first:
  - B-F06  # rebuild途中失敗 — 再発コスト最低、テスト追加を最優先
  - B-F05  # set_parameter → rebuild でactive body選択喪失
  - B-F04  # 参照STEP直後のjoin

refactor_first:
  - R001  # server.py分割(物理行で最大と判明、優先度を一次調査より上げる)
  - R003  # catalog駆動dispatch
  - R002  # kernel.py分割
  - R004  # _dispatch表駆動化(15コマンド、命名非対称も同時に解消)
  - R005  # 残課題はApplied型のみに縮小(図形種別3表は既にtest_entity_kind_tablesで保護済みのため取り下げ)
```

---

## 一次・二次調査そのものへの評価

- 一次調査(CODE_SURVEY.md)は実装との事実一致率が高く、B003・R004・R005・R008で細部の粗さ(測定基準・引用範囲・原因帰属)があった程度。全体として実装を裏付けとした調査として信頼できる。
- 二次評価(CODE_SURVEY_EVAL.md)はR005(entity kind tables)・R008(measure誤帰属)・file_metrics(物理行)で一次調査の誤りを的確に検出しており、有効な再監査だった。一方でR006(viewport.tsのexport一覧)は二次評価自身の確認が不足していた(実際は11個の型を追加export)。
- 両文書とも見落としていたのは、**B008とR008が実質同じ種類の欠陥(`applied_count`の意味の不一致による状態表示の誤り)を別の場所で2回起こしている**という横断的な指摘。これは今回の三次検証で新たに確認した。
