# docs/ の索引

入口は [../README.md](../README.md)（文書の地図・設計原則・起動手順）。ここは `docs/` 配下の一覧。
**いまの挙動の正はコードとテスト**で、文書は「なぜそうしたか」と実測を持つ。同じことを2箇所に書かず、
正を1つ決めて他は参照にする（正を宣言してもコピーは追随しない —— 見つけたらコピーを消す）。
文書はコードと同じリポジトリに置く（誰が直せるかで置き場を決める）。

| 文書 | 何が書いてあるか |
|---|---|
| [MANUAL.md](MANUAL.md) | 使い方（制作用の安定版と開発用の使い分け） |
| [DESIGN_DECISIONS.md](DESIGN_DECISIONS.md) | コア・全体の設計判断と実測（層の分け方・外してはいけない3つ・技術選定の根拠・verb 粒度・図形の表現・保存・性能・テスト方針・方向） |
| [AI_INTEGRATION.md](AI_INTEGRATION.md) | アプリ内 AI・MCP・ローカル LLM（方向と未決・実測・「止まる」規律の実装・落とし穴） |
| [LOCAL_LLM_QWEN38.md](LOCAL_LLM_QWEN38.md) | Qwen3.8-27B を dGPU 無しのラップトップで動かしたアプリ内 AI の実測（思考の設定・依頼文・失敗の型・ばらつき対策・スロット保存が効かない理由・測定の運用） |
| [UI.md](UI.md) | 画面と操作の判断（方針・パネル配置・コマンド層・スケッチ入力・見た目） |
| [VIEWPORT.md](VIEWPORT.md) | 3D ビューポート（深度の規約・カメラ・ピック・グリッド・可変解像度と面カリング・ビューキューブ・断面） |
| [SMOKE_TESTING.md](SMOKE_TESTING.md) | 実機検査（puppeteer）の書き方: 座標・待機・選択・fixture・環境 |
| [WEB_PITFALLS.md](WEB_PITFALLS.md) | three.js / Svelte 5 / DOM / CSS / ブラウザの落とし穴 |
| [QA_LESSONS.md](QA_LESSONS.md) | 検証設計の教訓（緑を根拠にしない・変異検査の読み方・検査の設計） |
| [DEV_NOTES.md](DEV_NOTES.md) | 開発環境と運用（venv・行末・一括編集の道具・減量・測定記録の書き方・タスクの切り方） |
| [STABLE.md](STABLE.md) | 安定版と開発版の分離（配置・ポート・導入・画面からの更新・巻き戻し・制作データの扱い） |
| [TOPOPT.md](TOPOPT.md) | 形状最適化の正（実測・二値探索の方式と契約・設計と画面・受入検査の意味） |
| [TOPOPT_ACCEPTANCE.md](TOPOPT_ACCEPTANCE.md) | 最終形状の合格条件と最終処理の検査（平滑化前の受入検査・平滑化後の自己交差の規則・メッシュ集合演算の採用根拠） |
| [TOPOPT_MIN_WIDTH.md](TOPOPT_MIN_WIDTH.md) | 最小幅の探索規則の追加分（くびれ判定・芯の隣接）と完成形の最小幅・強度の診断 |
| [TOPOPT_THICKNESS.md](TOPOPT_THICKNESS.md) | 厚み1〜4層での安全率のずれと最小幅の保証の実測（節 A〜O） |
| [TOPOPT_TARGET_FACTOR.md](TOPOPT_TARGET_FACTOR.md) | 探索に渡す目標安全率 = 実際の目標 × 2.4 の決定・根拠・実測 |
| [TOPOPT_PRESERVE.md](TOPOPT_PRESERVE.md) | 探索が保持セルを空にしない契約と、その前後の実測（hanger） |
| [TOPOPT_IDLE_MATERIAL.md](TOPOPT_IDLE_MATERIAL.md) | 「仕事をしていない材料」が残る問題の原因の実測・対策案の採否・決定 |
| [TOPOPT_STRESS_GATE_DILUTION.md](TOPOPT_STRESS_GATE_DILUTION.md) | 母数に依らない応力の合否判定（採用した判定・実測・却下した案） |
| [topopt_idle_validation/10_SYNTHESIS.md](topopt_idle_validation/10_SYNTHESIS.md) | 不要材料の改善案の検証の統合報告（各検証は付録 A〜G に畳んである） |
| [topopt_idle_validation/00_COMPACT_MODELS.md](topopt_idle_validation/00_COMPACT_MODELS.md) | 不要材料検証の小型題材2つの定義・認定条件 |
| [TOPOPT_SPEED.md](TOPOPT_SPEED.md) | 形状探索の時間の内訳と速度に関する決定（前計算・warm start・解法の使い分け） |
| [TOPOPT_GPU.md](TOPOPT_GPU.md) | CG を GPU で解く経路（有効にする手順・CPU に戻る条件・ビット一致の範囲） |
| [TOPOPT_RECORDING.md](TOPOPT_RECORDING.md) | 最適化の途中記録（毎回自動で作る診断記録の中身） |
| [CAD_LIBRARY_PITFALLS.md](CAD_LIBRARY_PITFALLS.md) | build123d / OCCT / STEP / `.venv` / MCP サーバの落とし穴（コード側コメントの索引つき） |
| [TOPOPT_GAP_CAUSALITY.md](TOPOPT_GAP_CAUSALITY.md) | 穴口の微細隙間とメッシュ欠損の因果調査（境界交差・最小再現・OCCT内部検査）と、案A（境界交差の検出・共有辺再分割）の設計・実装・実測 |
| [TOPOPT_STOP_RULE.md](TOPOPT_STOP_RULE.md) | 段階4の打ち切り（issue #22）の判断材料: 終盤の力学の実測（issue の実行・hanger の回し直し・合成題材）と、候補規則を記録済みの軌道へ後付けした試算表。決定は無い |

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
