# duocad — autopilot の進行状況(基本操作の高速化)

autopilot(本人の指示 2026-10-16)で親セッションが進めている作業の現在地。節目ごとに更新する。
判断の記録は [decisions.md](decisions.md)。

## ゴール(本人の指示の原文)

> duocadの基本操作を軽くしたい。コード調査、実測定を行い、高速化の方針を立て、適切な粒度でIssue化しておいて。今回はIssue化までがゴール。なお、ここでの基本操作というのは、ビューポート操作、ゴースト表示、verbの適用からviewport内のソリッド・スケッチへの反映、選択してからそれらがviewport内のソリッド・スケッチへの反映、など。私が挙げた以外にも高速化できそうな点があれば積極的にどうぞ。

ゴールは **GitHub Issue の作成まで**。実装はしない。

## 計画

1. コード経路の追跡(サーバ: `Session.apply` → `_push`、クライアント: `session.ts` → `viewport.ts`)— 済
2. 実測(サーバ単体 `tools/bench_server_ops.py`、ブラウザ `web/scripts/bench-basic-ops.mjs`)— サーバ済・ブラウザ実行中
3. 高速化の方針を立て、astra に反論を取る
4. 構造的な作業の単位で Issue を立てる(1件ずつ直せる不満は issue にしない方針が docs/DESIGN_DECISIONS.md「開発の進め方」にあるので、Issue は「複数ファイルにまたがる・1回の対話で終わらない」単位にする)
5. 実測の表と再現コマンドを docs に残す(`docs/PERFORMANCE.md`)

## どこまで来たか

- サーバ単体の実測が出た(PC ケース 27 部品: ズーム1回の再テセレーション 1.35 秒、フィレットのプレビュー 1.0 秒/回、辺の取り寄せ 0.5〜0.7 秒/部品。小さな文書はサーバ側は数 ms)
- クライアント側の実測を走らせている(箱・PC ケース・長方形 150 個のスケッチ)

## 次にやること

- クライアントの実測結果を読み、ボトルネックを「どの層の何か」で名指しする
- astra に方針を見せて反論を取る
- Issue を立て、`docs/PERFORMANCE.md` に実測と再現コマンドを書く

## 再開の手順

1. `output/bench_basic_ops/*.json` と `*.log` に実測が残っている(git 管理外。無ければ下のコマンドで作り直す)
2. サーバ単体: `.venv/Scripts/python.exe -X utf8 tools/bench_server_ops.py`
3. ブラウザ: `cd web && node scripts/bench-basic-ops.mjs`(PC ケースの STEP は `web/scripts/models.mjs` の `PC_CASE_STEP`)
4. 立てた Issue は `gh issue list --label performance`
