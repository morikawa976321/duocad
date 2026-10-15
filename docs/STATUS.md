# duocad — autopilot の進行状況(基本操作の高速化)

autopilot(本人の指示 2026-10-16)で親セッションが進めた作業の現在地。判断の記録は [decisions.md](decisions.md)。

## ゴール(本人の指示の原文)

> duocadの基本操作を軽くしたい。コード調査、実測定を行い、高速化の方針を立て、適切な粒度でIssue化しておいて。今回はIssue化までがゴール。なお、ここでの基本操作というのは、ビューポート操作、ゴースト表示、verbの適用からviewport内のソリッド・スケッチへの反映、選択してからそれらがviewport内のソリッド・スケッチへの反映、など。私が挙げた以外にも高速化できそうな点があれば積極的にどうぞ。

ゴールは **GitHub Issue の作成まで**。実装はしない。

## 結果(2026-10-16 完了)

- 実測と読み取れること: [PERFORMANCE.md](PERFORMANCE.md)
- 物差し: `tools/bench_server_ops.py`(サーバ単体)、`web/scripts/bench-basic-ops.mjs`(ブラウザ)
- Issue(`performance` ラベル): まとめ #165、個別 #166(カメラ操作の丸ごと測り直し)#167(辺の連なり)#168(単一ループの塞がり)#169(プレビューと確定の二重計算)#170(JSON 配信)#171(面ごとの Mesh と常時描画)#172(スケッチの領域検出と作り直し)#173(自動保存)
- astra(Codex 相談役)に粒度・順位・見落としの反論を取り、採用した(decisions.md)

## 本人に残る判断

無し。順位は #165 の表に AI の案として書いた(本人が変えてよい)。着手は本人の「気になった順」(docs/DESIGN_DECISIONS.md「開発の進め方」)。

## 再開の手順(実装に着手するとき)

1. #165 の表で Issue を選ぶ。各 Issue に「現象(実測)・原因(file:line)・方針・物差し・不変条件」が書いてある
2. 着手前に物差しを 1 回走らせて「いま」を取る(`output/` に残す。git 管理外):
   - サーバ単体: `.venv/Scripts/python.exe -X utf8 tools/bench_server_ops.py`
   - ブラウザ: `cd web && node scripts/bench-basic-ops.mjs`(PC ケースの STEP は `web/scripts/models.mjs` の `PC_CASE_STEP`。`--skip A,B,C` で場面を選べる)
3. Issue の「足す物差し」(draw calls・再計算面数・verb 実行回数など)が要るなら、先にベンチへ行を足す
4. 直したら同じ物差しで測り直し、PERFORMANCE.md の表に「後」を足す
