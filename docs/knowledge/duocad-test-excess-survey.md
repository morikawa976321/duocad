---
name: duocad-test-excess-survey
description: 調査結果(2026-08-20) — duocad全632テストの過剰洗い出し。明確な過剰は8本のみ(スイートの1%強)で、維持コストの主因はテスト本数でなくフィクスチャ設計(不要な実STEP読込)と140秒のllama実機canaryに集中。test_fillet.py:249に常にTrueの無効assertも発見
created: 2026-08-20
updated: 2026-08-20
---

# duocadテスト過剰調査の結果（2026-08-20）

sonnetサブエージェント6本で全35ファイル・632テスト（実測180.6秒・全緑）を精査した結論。
下記「明確に過剰」8項目（9関数）はユーザーが同日削除を決定し、実施済み（duocad `e15dc84`、622件全緑）。
包含が厳密でなかった2箇所（選択イベント合計のみ検査／切削後の体積未固定）は残す側を強化して補った。

## 明確に過剰（検出力の増分ほぼゼロ）8本

1. `test_fillet.py:231` `test_failures_do_not_follow_a_simple_rule_about_the_radius` — 他テストで立証済みの事実を最重量演算（全辺フィレット×4）で再実行。**L249の `assert len(doc.log.entries) == len(doc.log.entries)` は同値比較で常にTrue（無効assert・実質バグ）**
2. `test_pattern_circular.py:199` — L180の `len(angles)==COUNT` に完全包含
3. `test_mcp.py:199` — `test_mcp_verbs.py:360` と同名・ほぼ同一実装の完全重複（しかも重いSHELFフィクスチャ上）
4. `test_mcp_verbs.py:323` — テスト自身の定数の同語反復（L297の完全一致テストが通れば数学的に自動成立）
5. `test_oplog.py:54` — 同ファイルL64の真部分集合
6. `test_extrude.py:127` — L302の面種完全一致チェックに包含
7. `test_selection.py:580` — L592の1手目と完全重複
8. `test_mcp.py:301/368` — test_mcp_verbs側の構造化コード検査と同一シナリオを、壊れやすい文言部分一致で重複検査

## 維持コストの本丸はテスト本数でなく構造3点

- **フィクスチャ過大**: test_mcp.py（プロトコル7本が実STEP読込に相乗り）、test_sketch_ui.py（12本中9本は平面しか使わないのに私物STEPに全員依存＝ファイル無しで全滅skip）、test_extrude_face.py（関数スコープで同一STEPを約19回再パース）、test_visibility.py（KeyError検査等にまで17ソリッド分解）。テストを消さずフィクスチャ分割/スコープ変更で解決する性質
- **実行時間の78%が1本**: `test_agent_llama_server.py`（llama-server起動中のみ実行、140.3秒）。skip設計自体は理想的なcanaryパターン
- **リポジトリ外依存**: tests/models.py が `C:\Users\tm976\mf\3d-models` の私物STEPを参照。このマシン以外で確実に落ちる

## 逆に手薄と判明した箇所

- `test_server_selection_reset.py`: resets_selection宣言13verbに対し実挙動テストは7〜8種のみ（add_line/add_rectangle/add_arcが個別テスト無し）
- `set_parameter` の値検証テストが `test_m3_regressions.py` にしか無い（test_parameters.py側に無い＝回帰テストの皮を被った唯一の契約テスト）

## 全体評価

意図的な層またぎ二重検査（[[duocad-intentional-duplications]]）はどの担当も正しく機能中と確認。撤回済み契約の固定は0件（2026-08-20の選択全リセット・引数統一は全テストに正しく反映済み）。逐語スキーマ固定（ARGUMENTS表）はcatalog.py一本化リファクタを無傷で通過した実績があり価値が実証されている。スイートは総じて高規律で、削減余地は632本中の1%台。
