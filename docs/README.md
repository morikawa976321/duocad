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
| [VIEWPORT.md](VIEWPORT.md) | 3D ビューポート（深度の規約・カメラ・ピック・グリッド・可変解像度と面カリング・ビューキューブ） |
| [SMOKE_TESTING.md](SMOKE_TESTING.md) | 実機検査（puppeteer）の書き方: 座標・待機・選択・fixture・環境 |
| [WEB_PITFALLS.md](WEB_PITFALLS.md) | three.js / Svelte 5 / DOM / CSS / ブラウザの落とし穴 |
| [QA_LESSONS.md](QA_LESSONS.md) | 検証設計の教訓（緑を根拠にしない・変異検査の読み方・検査の設計） |
| [DEV_NOTES.md](DEV_NOTES.md) | 開発環境と運用（venv・行末・一括編集の道具・減量・測定記録の書き方・タスクの切り方） |
| [TOPOPT.md](TOPOPT.md) ほか `TOPOPT_*.md` | 形状最適化（実測・設計と画面・受入検査・探索の時間短縮） |
| [CAD_LIBRARY_PITFALLS.md](CAD_LIBRARY_PITFALLS.md) | build123d / OCCT / STEP / `.venv` / MCP サーバの落とし穴（コード側コメントの索引つき） |
| [TOPOPT_PRE_SMOOTHING_CHECKS.md](TOPOPT_PRE_SMOOTHING_CHECKS.md) | 島除去後・平滑化前の6条件と探索履歴の検査 |
| [PLAN_BINARY_STRESS_SEARCH.md](PLAN_BINARY_STRESS_SEARCH.md) | 二値モデル・応力制約による探索の現行仕様（§1〜§5）と、段階7・段階4置き換えの実測（§10〜§12） |
| [FIX_BINARY_SEARCH_INITIALIZATION.md](FIX_BINARY_SEARCH_INITIALIZATION.md) | hanger初期探索失敗への修正指示と実施結果（境界条件の確定・入口検査・候補メモリ管理。§9に実測） |
