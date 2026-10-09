# docs/ の索引

入口は [../README.md](../README.md)（文書の地図・設計原則・起動手順）。ここは `docs/` 配下の一覧。
**いまの挙動の正はコードとテスト**で、文書は「なぜそうしたか」と実測を持つ。同じことを2箇所に書かず、
正を1つ決めて他は参照にする（正を宣言してもコピーは追随しない —— 見つけたらコピーを消す）。
文書はコードと同じリポジトリに置く（誰が直せるかで置き場を決める）。

| 文書 | 何が書いてあるか |
|---|---|
| [INITIAL_PLAN.md](INITIAL_PLAN.md) | 最初期の実装計画（企画の前提・当初スコープ・到達表） |
| [DESIGN_DECISIONS.md](DESIGN_DECISIONS.md) | コア・全体の設計判断と実測（技術選定の根拠・verb 粒度・図形の表現・保存・性能・テスト方針・方向） |
| [AI_INTEGRATION.md](AI_INTEGRATION.md) | アプリ内 AI・MCP・ローカル LLM（方向と未決・実測・「止まる」規律の実装・落とし穴） |
| [UI.md](UI.md) | 画面と操作の判断（方針・パネル配置・コマンド層・スケッチ入力・見た目） |
| [VIEWPORT.md](VIEWPORT.md) | 3D ビューポート（深度の規約・カメラ・ピック・グリッド・可変解像度と面カリング・ビューキューブ・断面） |
| [SMOKE_TESTING.md](SMOKE_TESTING.md) | 実機検査（puppeteer）の書き方: 座標・待機・選択・fixture・環境 |
| [WEB_PITFALLS.md](WEB_PITFALLS.md) | three.js / Svelte 5 / DOM / CSS / ブラウザの落とし穴 |
| [QA_LESSONS.md](QA_LESSONS.md) | 検証設計の教訓（緑を根拠にしない・変異検査の読み方・検査の設計） |
| [DEV_NOTES.md](DEV_NOTES.md) | 開発環境と運用（venv・行末・一括編集の道具・減量・測定記録の書き方・タスクの切り方） |
| [STABLE.md](STABLE.md) | 安定版と開発版の分離（配置・ポート・導入・画面からの更新・巻き戻し・制作データの扱い） |
| [TOPOPT.md](TOPOPT.md) ほか `TOPOPT_*.md` | 形状最適化（実測・設計と画面・受入検査・探索の時間短縮） |
| [TOPOPT_IDLE_SOLUTIONS_2026-09-28.md](TOPOPT_IDLE_SOLUTIONS_2026-09-28.md) | 不要材料の2原因への解決策比較。保存場の追加集計、応力評価・可逆探索・検証計画の提案 |
| [TOPOPT_IDLE_VALIDATION_TASKS_2026-09-28.md](TOPOPT_IDLE_VALIDATION_TASKS_2026-09-28.md) | 第8章の検証を実行するtaskboxの11タスク。小型題材の作成・認定、依存関係、共通条件、成果物、十分性の基準 |
| [CAD_LIBRARY_PITFALLS.md](CAD_LIBRARY_PITFALLS.md) | build123d / OCCT / STEP / `.venv` / MCP サーバの落とし穴（コード側コメントの索引つき） |
| [TOPOPT_PRE_SMOOTHING_CHECKS.md](TOPOPT_PRE_SMOOTHING_CHECKS.md) | 島除去後・平滑化前の6条件と探索履歴の検査 |
| [TOPOPT_MESH_BOOLEAN_STUDY.md](TOPOPT_MESH_BOOLEAN_STUDY.md) | メッシュ集合演算による最終処理の試作評価（合否・保存精度・寸法差。2026-10-06 に製品の最終処理として採用） |
| [TOPOPT_GAP_CAUSALITY.md](TOPOPT_GAP_CAUSALITY.md) | 穴口の微細隙間とメッシュ欠損の因果調査（境界交差・最小再現・OCCT内部検査）と、案A（境界交差の検出・共有辺再分割）の設計・実装・実測 |
| [TOPOPT_STOP_RULE.md](TOPOPT_STOP_RULE.md) | 段階4の打ち切り（issue #22）の判断材料: 終盤の力学の実測（issue の実行・hanger の回し直し・合成題材）と、候補規則を記録済みの軌道へ後付けした試算表。決定は無い |
| [PLAN_BINARY_STRESS_SEARCH.md](PLAN_BINARY_STRESS_SEARCH.md) | 二値モデル・応力制約による探索の現行仕様（§1〜§5）と、段階7・段階4置き換えの実測（§10〜§12） |
| [FIX_BINARY_SEARCH_INITIALIZATION.md](FIX_BINARY_SEARCH_INITIALIZATION.md) | hanger初期探索失敗への修正指示と実施結果（境界条件の確定・入口検査・候補メモリ管理。§9に実測） |

### contracts/ — 作業タスクの契約の分野別蒸留

taskbox 廃止に伴い、完了タスクの「後続への契約」(ソースから読めない不変条件・依存禁止・罠)を分野別に移した文書群。項目末尾の括弧は出典タスクIDと日付。削除済みコードについての契約は外してあり(履歴は git)、現行コードにも当てはまる項目だけを残している。

| 文書 | 何が書いてあるか |
|---|---|
| [contracts/topopt-design-space.md](contracts/topopt-design-space.md) | 形状最適化: 設計空間の導出・連結性・画面検査の罠・bbox・void除去の契約(不変条件・依存禁止・罠。出典はタスクID) |
| [contracts/topopt-volume-stress-safety.md](contracts/topopt-volume-stress-safety.md) | 形状最適化: von Mises 代表応力・縮小検証の契約 |
| [contracts/topopt-solver-optimizers.md](contracts/topopt-solver-optimizers.md) | 形状最適化: 線形解法・CPU並列化と性能測定・反力の契約 |
| [contracts/topopt-pipeline-integration.md](contracts/topopt-pipeline-integration.md) | 形状最適化: パッケージ構成(依存の向き)・検査コードの罠・メッシュ化の Taubin 平滑化の契約 |
| [contracts/topopt-binary-search.md](contracts/topopt-binary-search.md) | 形状最適化: 二値探索の各段(FEM 抽出・受入・初期実行可能解・削減・製品配線)と検査 CLI・docs 整理の契約 |
| [contracts/topopt-measurements-stage4.md](contracts/topopt-measurements-stage4.md) | 形状最適化: 厚み・保持小体・節点所有の計測、peel、段階4の停止規則、中空なしの契約 |
| [contracts/topopt-min-width-peel.md](contracts/topopt-min-width-peel.md) | 形状最適化: 最小幅・芯の連結・射影つき peel の契約 |
| [contracts/topopt-keep-cells.md](contracts/topopt-keep-cells.md) | 形状最適化: 保持セルの exempt / protected の契約 |
| [contracts/topopt-target-factor-warm-start.md](contracts/topopt-target-factor-warm-start.md) | 形状最適化: 探索目標係数・内側目標の契約 |
| [contracts/topopt-solver-pairs.md](contracts/topopt-solver-pairs.md) | 形状最適化: cg と直接解法の対の測定と solver=auto の契約 |
| [contracts/topopt-finalize-smoothing.md](contracts/topopt-finalize-smoothing.md) | 形状最適化: 最終処理・平滑化コーパス・設計要素数上限の契約 |
| [contracts/topopt-idle-validation-setup.md](contracts/topopt-idle-validation-setup.md) | 不要材料検証(題材・プロトコル・参照解・指標)の契約 |
| [contracts/topopt-idle-validation-runs.md](contracts/topopt-idle-validation-runs.md) | 不要材料検証(探索・経路認定・合成・hanger・頑健性・最終形状・総括)の契約 |
| [contracts/ui-selection-viewport.md](contracts/ui-selection-viewport.md) | UI: 選択の鍵・面数カウント・矩形選択・画面ピック・ビューキューブ・FOV・デバッグビューの契約 |
| [contracts/ui-sketch-input.md](contracts/ui-sketch-input.md) | UI: コマンド引数仕様・スケッチ入力・ホバー・ゴースト・グリッド追従の契約 |
| [contracts/ui-optimize-integration.md](contracts/ui-optimize-integration.md) | UI/コア: メッシュ body 種別・最適化バックエンド配線・実行 UI の契約 |
| [contracts/verbs.md](contracts/verbs.md) | verb: move/pattern/mirror/boolean/envelope/extrude とそのプレビューの契約 |
| [contracts/maintenance-docs-comments-tests.md](contracts/maintenance-docs-comments-tests.md) | 保守: 文書・コメント・テストの減量と、doc-code 一致検査・TOPOPT.md 構成の規約 |
