---
name: duocad-code-survey-decisions
description: duocadコード調査(2026-08-27、CODE_SURVEY→EVAL→CONCLUSION→ACTIONSの3段階)から積んだもの・クローズしたもの・積まないものの確定 — バグは1.1〜1.3のみ着手、1.4は単一ブラウザ前提でクローズ、リファクタ5項目は3.1と3.5だけ着手。調査文書4件は削除しコミットba292ecにだけ実体が残る
created: 2026-08-27
updated: 2026-08-28
---

# duocadコード調査(2026-08-27)からの対応の確定

duocadの3段階のコード調査(`docs/CODE_SURVEY.md` → `CODE_SURVEY_EVAL.md` → `CODE_SURVEY_CONCLUSION.md`、
まとめが `docs/CODE_SURVEY_ACTIONS.md`)の結果を受けた本人の選択。理由の明言は無い。

## 1章 バグ修正 —「まずはバグ修正の1〜3をtaskboxに積む」(2026-08-27)

- **積んだ**: 1.1 消えたBodyのキャッシュ4種が溜まり続ける ＋ 1.2 AIループの進捗通知の失敗が
  無音(`20260827-2300`。**1.2は1.1へ統合した** —— 実質1〜2行で、タスク1件ぶんの手続きに対して
  小さすぎるという本人判断) / 1.3 `applied_count` がMCP経路とWS経路で食い違う(`-2302`)
- **クローズ**: 1.4 複数クライアント同時編集の順序 → 単一ブラウザ前提として決着
  ([[duocad-assumes-a-single-browser]])
- 2群目の回帰テスト3件は2026-08-27時点で**別セッションが着手済み**
  (`tests/test_m2_regressions.py` / `test_m3_regressions.py` が編集中)

## 3章 リファクタ5項目 — 着手は3.1と3.5だけ(2026-08-28確定)

- **3.1 `server.py` の分割** → **積む**(taskbox `20260827-2330` / `-2331` の2段構え。
  [[duocad-server-split-keeps-a-reexport-entry]])
- **3.2 汎用ディスパッチャ(verb追加手順の一本化)** → **積まない**
- **3.3 `kernel.py` の分割** → **積まない**
- **3.4 `_dispatch` の表駆動化** → **積まない**。ただし3.4のうち命名の非対称
  (`extrude` / `extrude_face` の逆転)だけは2026-08-27に別途解消済み
- **3.5 TS側 `Applied` 型の同期** → **積む**(taskbox `20260827-2347`)。ただし**生成ではなく
  突合の機械検査**として([[duocad-catalog-declares-inputs-only]])

## 調査文書の扱い

調査ドキュメント4件は `duocad/docs/` から削除し、**コミット `ba292ec` にだけ実体を残した**
(`git show ba292ec:docs/CODE_SURVEY_ACTIONS.md`)。作業ツリーに未追跡の調査資料を置き続けない、
という扱い。

同じ調査の運用知見は [[duocad-survey-findings-are-solid-but-remedies-are-not]]（所見は正しくても
対応案は実在しない場所を指しうる）。
