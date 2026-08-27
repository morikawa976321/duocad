# duocad コード調査報告

```yaml
survey_date: 2026-08-27
scope: full_repository
pytest: "521 passed, 2 skipped"
method: static_analysis + regression_test_review + targeted_code_audit
```

---

## bugs

### active

| id | severity | location | summary | evidence | confidence |
|----|----------|----------|---------|----------|------------|
| B001 | medium | `duocad/server.py` (`Session._geometry_cache`, `_edge_cache`, `_vertex_cache`, `_volume_cache`) | Body 削除(undo 等)後もサーバ側テセレーション・辺・頂点・体積キャッシュが `body_id` キーで残る。`_replace_document` 時のみ全クリア。 | `delta()` は削除 Body を通知するがキャッシュ掃除はしない(L620–641)。クライアントは `viewport.prune()` で描画を掃除するがサーバは dead エントリを保持。 | confirmed |
| B002 | low | `duocad/server.py` (`Session._revision_counter`) | 文書差し替え時 `_revision_state` はクリアするが `_revision_counter` は意図的に保持。削除済み `body_id` のカウンタエントリが蓄積。 | L1264–1267 コメント「`_revision_counter` は消さない」 | confirmed |
| B003 | high | `duocad/server.py` (`Server._dispatch`, `Session`) | 複数 WebSocket 接続が `await _push()` 中に交互に `_apply()` すると Session の操作ログ・選択がインターリーブ可能。MCP/AI は `ToolsOnLoop` でイベントループへ直列化されるが WS 経路に Session 全体のロックはない。 | `handle()` は接続ごと直列(L1664–1665)だが接続間は並行。`_send_locks` は配信直列化のみ(L1557–1561)。Session docstring「イベントループのスレッドだけが触る」前提と矛盾。 | suspected |
| B004 | low | `web/src/App.svelte` (`myClientOps`) | 操作ごとに `myClientOps.add(opCounter)` するが削除しない。長時間セッションで Set が肥大化。 | L227, L350 — add のみ delete なし | confirmed |
| B005 | low | `web/src/App.svelte` (`edgesAsked`, `verticesAsked`) | `${bodyId}:${revision}` キーを Set に追加し続けるが古いキーを削除しない。同一セッション内で蓄積。 | L151–155, L589–590, L638–639 | confirmed |
| B006 | medium | `duocad/agent/loop.py` (`AgentLoop._notify`) | AI 進捗コールバックの例外を `except Exception: pass` で握りつぶし。配信失敗・UI バグがログ・警告に出ない。 | L546–553 | confirmed |
| B008 | low | `web/src/App.svelte` (`saved` derived) | 保存後に verb → undo で保存時点の形状に戻しても `applied_count` が増えたまま「未保存」と表示。逆(未保存なのに保存済み)は起きない既知の非対称。 | L192–207 docstring | confirmed |

### latent

| id | severity | location | summary | evidence | confidence |
|----|----------|----------|---------|----------|------------|
| B007 | medium | `duocad/speculative.py`, `duocad/catalog.py` (`set_parameter`) | `speculative` は `SNAPSHOTS` を戻さない。`set_parameter` を `previewable=True` にすると中断プレビューが実 undo で誤形状を復元しうる。現状 `set_parameter.previewable=False` で未発火。 | `speculative.py` L13–18, `catalog.py` `previewable` 既定 False | confirmed |

### fixed_guarded

回帰テストで保護済み。現行コードは修正済みだが契約テストが触れない経路で再発しうる。

| id | regression_test | summary |
|----|-----------------|---------|
| B-F01 | `tests/test_m2_regressions.py` | 面貼りスケッチで `gp_Ax3`/`ZReverse` 誤用 → `regions()` 常時例外 |
| B-F02 | `tests/test_m2_regressions.py` | 別スケッチ領域混在押し出し → 体積0退化ソリッドを黙って生成 |
| B-F03 | `tests/test_m2_regressions.py` | NaN/0/負半径がスケッチ・JSON に永続化、正常領域まで消滅 |
| B-F04 | `tests/test_m2_regressions.py` | 参照 STEP 直後の `join` が ValueError |
| B-F05 | `tests/test_m3_regressions.py` | `set_parameter` → `rebuild` で active body 選択が捨てられ編集先が別ボディへ移動 |
| B-F06 | `tests/test_m3_regressions.py` | `rebuild` 途中失敗で bodies/sketches=0 の不整合文書 |
| B-F07 | `tests/test_m3_regressions.py` | `set_parameter` が `add_circle` と異なる値検証（NaN 等の素通り） |
| B-F08 | `tests/test_m3_regressions.py` | 外ログ `replay` が黙って空文書化 |
| B-F09 | `tests/test_fillet.py` | r=10.5 が `IsDone` 通過だが `BRepCheck` 偽 — 現行は拒否 |

---

## refactor_high

| id | priority | location | lines | summary | impact | suggested_direction |
|----|----------|----------|-------|---------|--------|---------------------|
| R001 | high | `duocad/server.py` | 1610 | `Session`(文書・キャッシュ・verb ラッパ)、`Server`(WS)、`WebHandler`(HTTP/静的)、`ToolsOnLoop`、アプリ内 AI が同居。MCP の唯一の文書 API(`Session`) がアプリ層モジュールに埋め込み。 | maintainability, architecture | `session.py` / `ws_server.py` / `http_static.py` / `agent/ws_bridge.py` へ分割 |
| R002 | high | `duocad/kernel.py` | 1384 | OCP import 境界は守られているが STEP/STL IO・テセレーション・モデリング・スケッチ平面・由来マップが単一モジュールに集中。 | maintainability, correctness | `kernel_io.py` / `kernel_mesh.py` / `kernel_modeling.py` 等へ責務分割、`kernel.py` は re-export ハブ |
| R003 | high | `duocad/server.py` + `duocad/mcp_server.py` (`DocumentTools`) | — | verb 追加ごとに `Session.*` と `DocumentTools.*` の薄いラッパペアが必要。README L557「汎用ディスパッチャが要る（未着手）」。 | maintainability, correctness | `catalog.COMMANDS` から `invoke_verb(name, args)` を導出。MCP は封筒生成のみ残す |
| R004 | high | `duocad/server.py` (`Server._dispatch`) + `duocad/catalog.py` | — | catalog `ws="special"` 17件と手書き `elif` 分岐が並存。宣言から dispatch ロジックは導出されていない。 | maintainability, correctness | catalog に `dispatch_kind` を追加しハンドラ表駆動。`extrude` vs `extrude_regions` 命名不对称も統一 |
| R005 | high | `web/src/session.ts` + `entityKinds.ts` + Python 2表 | 658 | `Applied` union・図形種別が FE/BE 手同期。`test_ui_command_wiring` はコマンド名のみ検査。図形種別は Python 2表 + TS 1表の 3箇所（`test_entity_kind_tables` は Python 2つだけ）。 | correctness, maintainability | catalog に `response_fields` 追加→TS 生成 or スキーマ検証。図形種別は JSON 単一宣言から生成 |
| R006 | medium | `web/src/viewport.ts` | 1702 | three.js ラッパが単一クロージャに Body 描画・ピック・スケッチ・領域・プレビューを集約。プロジェクト最大ファイル。 | maintainability | 内部を非 export モジュール分割、公開は `createViewport` インタフェースのみ |
| R007 | medium | `web/src/App.svelte` | 1180 | 組み立て専用から拡大。辺/頂点取り寄せ・保存状態・ピック翻訳・`runCommand` が同居。 | maintainability | `documentState.ts` / `selectionBridge.ts` / `geometryFetch.ts` へ抽出 |
| R008 | medium | `duocad/mcp_server.py` + `duocad/server.py` | — | `describe_document` / `measure` / `state_meta` でスケッチ entity 組み立てが別実装。AI 見る文書と UI 見る文書の微妙な差リスク。 | maintainability, correctness | `Session.document_snapshot()` を共有投影関数として集約 |
| R009 | medium | `duocad/server.py` (AI ブロック) + `duocad/agent/` | — | `ToolsOnLoop`・プロバイダ設定・`run_in_executor`・進捗ブロードキャストが `Server` に集中。 | maintainability | `agent/ws_bridge.py` へ移し `Server` は委譲のみ |
| R010 | medium | `duocad/mcp_server.py` | 1145 | JSON-RPC・`McpServer.handle`・CLI・`DocumentTools`・`error_envelope` が同居。 | maintainability | `document_tools.py` 分離、`mcp_server.py` はプロトコル層のみ |

---

## file_metrics

| path | lines | note |
|------|-------|------|
| `web/src/viewport.ts` | 1702 | largest |
| `duocad/server.py` | 1610 | Session+Server+HTTP+AI |
| `duocad/kernel.py` | 1384 | OCP boundary |
| `web/src/App.svelte` | 1180 | UI orchestration |
| `duocad/mcp_server.py` | 1145 | MCP+DocumentTools |
| `duocad/catalog.py` | 926 | COMMANDS SSOT |

---

## priority_matrix

```yaml
bugs_fix_first:
  - B003  # multi-client Session race
  - B001  # cache lifecycle on body delete
  - B006  # silent progress callback failures

refactor_first:
  - R001  # server.py split (enables R003/R009)
  - R003  # catalog-driven verb dispatch
  - R002  # kernel.py split
  - R004  # _dispatch table-driven
  - R005  # FE/BE contract automation
```
