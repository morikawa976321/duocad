# CODE_SURVEY 妥当性評価

```yaml
eval_date: 2026-08-27
source: docs/CODE_SURVEY.md
source_date: 2026-08-27
method: static_reaudit
code_survey_md: unmodified
pytest:
  survey: "521 passed, 2 skipped"
  collect_now: 523
  full_rerun: not_executed
  skip_sources_observed:
    - tests/test_agent_llama_server.py  # skipif x2 on 1 test (llama-server / STEP)
    - tests/test_files_ui.py            # runtime skip (symlink)
verdict_enum: [confirm, confirm_nuance, overstated, stale, inaccurate, missing_guard]
```

凡例: `confirm`=現行コードと一致 / `confirm_nuance`=事実は正しいが程度・引用が粗い / `overstated`=過大 / `stale`=実装は直ったが記載が古い / `inaccurate`=事実誤認 / `missing_guard`=修正はあるが主張の回帰テストが無い

---

## meta

| field | survey | eval | verdict |
|-------|--------|------|---------|
| pytest | 521 passed, 2 skipped | collect 523 (=521+2) | confirm_nuance |
| method | static + regression review + targeted audit | 再監査は静的。全件pytestは未再走 | confirm_nuance |

`pytest` 数値は collect と算術一致。本評価ではフルスイートを再実行していない。

---

## bugs.active

| id | survey_sev | verdict | loc_cited | loc_now | evidence |
|----|------------|---------|-----------|---------|----------|
| B001 | medium | confirm | server.py L620–641 | `Session.delta` L620–641。cache clear は `_replace_document` L1259–1262 のみ | Body削除後も `_geometry_cache`/`_edge_cache`/`_vertex_cache`/`_volume_cache` を pop しない。`delta()` は生存 Body だけ列挙。クライアントは `viewport.prune()` (`App.svelte` L363) |
| B002 | low | confirm_nuance | L1264–1267 | L1264–1268 | `_revision_counter` 保持はコメントどおり意図的。dead key 蓄積は事実。バグというより版衝突回避の代償 |
| B003 | high / suspected | confirm_nuance | handle L1664–1665, `_send_locks` L1557–1561 | handle L1664–1665, `_send_locks` L1557–1562, `_apply` L1910–1921 | 接続間に Session mutex 無し。`_apply` は同期で1 verbは原子的。インターリーブは `await _push` / `_commit_before_verb` の待ち点での**コマンド粒度**。不変条件は `Session` ではなく `ToolsOnLoop` docstring (L203)。典型1ブラウザでは high は過大になりうる。`suspected` は妥当 |
| B004 | low | confirm | App.svelte L227, L350 | L227, L350 | `myClientOps.add` のみ。`delete`/`clear` 無し |
| B005 | low | confirm_nuance | L151–155, L589–590, L638–639 | 同。clear は L261–262, L293–295 | セッション中は `${bodyId}:${revision}` を積み続ける。切断・一部エラーでは clear する（調査は未記載） |
| B006 | medium | confirm | loop.py L546–553 | L546–553 | `except Exception: pass`。購読者失敗はログ無し |
| B008 | low | confirm | App.svelte L192–207 | L181–208 (`saved` derived L203–208) | docstring の非対称と実装一致 |

### B001 補足

`_sketch_cache` は `sketches_state()` L433–436 で死んだ sketch_id を落とす。Body 系キャッシュだけ未掃除。

---

## bugs.latent

| id | survey_sev | verdict | evidence |
|----|------------|---------|----------|
| B007 | medium | confirm | `speculative.py` L13–18 が SNAPSHOTS 非復元を明記。`set_parameter` は `previewable` 既定 False (`catalog.py` L583–600)。`previewable_verbs()` = `extrude`,`extrude_face`,`fillet`,`chamfer` のみ |

---

## bugs.fixed_guarded

| id | claimed_test | test_fn | impl | verdict |
|----|--------------|---------|------|---------|
| B-F01 | test_m2_regressions.py | `test_sketch_on_a_face_can_be_extruded_and_replayed` | `kernel._ax3_from_face` が `gp_Ax3` 組み直し + `ZReverse` (L898–904) | confirm |
| B-F02 | test_m2_regressions.py | `test_extrude_rejects_regions_from_two_different_sketches` | 混在押し出しは ValueError | confirm |
| B-F03 | test_m2_regressions.py | `test_broken_numbers_never_enter_the_sketch` | NaN/0/負を作成時拒否 | confirm |
| B-F04 | test_m2_regressions.py | **無し**（モジュールdocstring項目4のみ） | `import_step` が参照の直後に editable solid を `add_body` し active にする (`import_editable.py` L81–86)。join 直後の回帰テストは他ファイルにも無し | missing_guard |
| B-F05 | test_m3_regressions.py | **無し**（モジュールdocstring項目1のみ） | `parameters.rebuild` L210–213 が `saved_active` を戻す | missing_guard |
| B-F06 | test_m3_regressions.py | **無し**（モジュールdocstring項目2のみ） | `rebuild` L175–206 が失敗時 bodies/sketches/parameters/active を復元 | missing_guard |
| B-F07 | test_m3_regressions.py | `test_set_parameter_rejects_values_that_add_circle_would_reject` | 作成時と同系の値検査 | confirm |
| B-F08 | test_m3_regressions.py | `test_replaying_a_foreign_log_fails_loudly_instead_of_emptying_the_document` | 外ログ `replay` は ValueError | confirm |
| B-F09 | test_fillet.py | `test_radius_10_5_is_reported_as_done_but_is_actually_broken` | r=10.5 は ValueError かつ `"BRepCheck"` を含む | confirm |

`test_m3_regressions.py` にあるのは B-F07 / B-F08 と `test_set_parameter_rejects_pattern_counts_that_are_not_positive_integers`（表外）。B-F05/B-F06 は**実装済み・契約テスト無し**。`fixed_guarded` の定義（「回帰テストで保護済み」）に対し B-F04/05/06 は不成立。

---

## refactor_high

| id | pri | verdict | loc_survey | loc_now_phys | evidence |
|----|-----|---------|------------|--------------|----------|
| R001 | high | confirm | server.py 1610 | 2170 | `Session`/`ToolsOnLoop`/`WebHandler`/`Server`(+AI) が同一モジュール。MCP の文書 API は `Session` |
| R002 | high | confirm | kernel.py 1384 | 1642 | OCP import 境界は維持。STEP/STL IO・tessellate・modeling・sketch平面・由来マップが同居。`io_step.py` は文書I/Oで kernel の OCP IO は残存 |
| R003 | high | confirm | — | — | catalog 冒頭が Session+DocumentTools ラッパを手順2–3で要求。README L555–557「汎用ディスパッチャが要る（未着手）」現行一致 |
| R004 | high | confirm_nuance | special 17件 | **Command 15件** | `ws="special"` 文字列は17（うちコメント2）。実体15: set_visible, modify/commit/clear_selection, sketch_create, extrude_face, import_svg, new/open_document, undo, redo, get_state, preview, get_vertices, get_edges。`_dispatch` は elif 手書き + 表駆動。`extrude`(session=`extrude_regions`) vs `extrude_face`(session=`extrude`) の非対称は残る。`ai_*` は catalog 外 |
| R005 | high | confirm_nuance | session.ts 658 | phys 810 | `Applied` union は TS 手同期のまま。`test_ui_command_wiring` はコマンド名。**図形種別は不正確**: `test_entity_kind_tables.py` は sketch/kernel/**entityKinds.ts の3表キー一致**を既に検査（調査は「Python 2つだけ」） |
| R006 | medium | confirm | viewport.ts 1702 | 2073 | `createViewport` クロージャに Body描画・ピック・スケッチ・領域・プレビュー。公開は `Viewport` + `createViewport` |
| R007 | medium | confirm | App.svelte 1180 | 1254 | 辺/頂点取り寄せ・保存状態・ピック翻訳・`runCommand` 同居。提案の `documentState.ts` 等は未抽出 |
| R008 | medium | confirm_nuance | — | — | スケッチ entity 組み立ての重複は `DocumentTools.describe_document` (L239–247, `parameters` キー) vs `Session._sketch_state` (L480–493, `refs` キー)。**`measure` は選択計測であり entity 組み立てでは無い**。追加差: `describe_document.applied_count=len(log.entries)` vs `state_meta.applied_count=Session._applied_count`（undo で前者が動き後者が動かない） |
| R009 | medium | confirm | — | — | `ToolsOnLoop`・プロバイダ・`run_in_executor`・進捗放送が `Server` (概ね L1544–2092) |
| R010 | medium | confirm | mcp_server.py 1145 | 1302 | `ToolError`/`DocumentTools`/`McpServer`/`error_envelope`/stdio+HTTP CLI (`main` L1266+) 同居 |

---

## file_metrics

調査の `lines` は UTF-8 物理行でも非空行でもなく、Windows PowerShell `Get-Content | Measure-Object -Line`（既定エンコーディング・空行除外）に一致する。日本語コメントの多いファイルは過小。

| path | survey | phys_utf8 | nonempty_utf8 | ps_measure_line | note |
|------|--------|-----------|---------------|-----------------|------|
| web/src/viewport.ts | 1702 | 2073 | 1956 | 1702 | 調査は「largest」。非空行では largest。物理行では server.py が上 |
| duocad/server.py | 1610 | 2170 | 1919 | 1610 | 物理行最大 |
| duocad/kernel.py | 1384 | 1642 | 1384 | 1384 | nonempty 一致 |
| web/src/App.svelte | 1180 | 1254 | 1180 | 1180 | nonempty 一致 |
| duocad/mcp_server.py | 1145 | 1302 | 1145 | 1145 | nonempty 一致 |
| duocad/catalog.py | 926 | 977 | 925 | 926 | ±1 |

順位主張（保守コストの大きいファイル）は妥当。絶対行数は比較時に定義を固定すること。

---

## priority_matrix

| list | survey_order | verdict |
|------|--------------|---------|
| bugs_fix_first | B003, B001, B006 | confirm_nuance |
| refactor_first | R001, R003, R002, R004, R005 | confirm |

- B003 を先頭にするのはマルチクライアントを前提にするなら妥当。単一 UI なら B001（OCCT shape 保持）の方が実害が先。
- R001→R003/R009 の依存は現行配置と一致。
- R005 の「図形種別3表が無検査」は既に偽。残課題は `Applied` 応答フィールドの FE/BE 同期。

---

## counts

```yaml
bugs_active: {survey: 7, confirm: 4, confirm_nuance: 3}
bugs_latent: {survey: 1, confirm: 1}
fixed_guarded: {survey: 9, confirm: 6, missing_guard: 3}  # B-F04 B-F05 B-F06
refactor: {survey: 10, confirm: 7, confirm_nuance: 3}
file_metrics_rank_ok: true
file_metrics_line_def: powershell_measure_object_line
```

---

## deltas_vs_survey

1. `ws="special"` は **15 Command**（調査の17は文字列ヒット）。
2. 図形種別3表は `test_entity_kind_tables.py` が既に固定。R005 の検査欠落は stale。
3. B-F04/05/06 は実装または構造変化で再発しにくいが、主張ファイルにテスト関数が無い。
4. 物理行では `server.py`(2170) > `viewport.ts`(2073)。
5. AI/UI の `applied_count` 意味が異なる（R008 の方が実害の芯）。
