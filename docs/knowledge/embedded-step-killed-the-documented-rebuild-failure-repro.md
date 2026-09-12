---
name: embedded-step-killed-the-documented-rebuild-failure-repro
description: duocad知見(2026-08-27・実測) — STEP本文をログへ埋め込んだ結果、docstringが書く「参照STEPを移動すればrebuildが失敗する」再現手順が成立しなくなっていた。失敗経路の回帰テストは反復個数を減らして押し出し対象の領域を消す形で作った
created: 2026-08-27
updated: 2026-08-27
---

duocadの `parameters.rebuild`(ログからの作り直し)には「途中で失敗したら bodies/sketches/parameters/active を元へ戻す」後始末がある。その回帰テストを書こうとしたとき、`tests/test_m3_regressions.py` と `docs/CODE_SURVEY_ACTIONS.md` の両方が再現手順として**「参照STEPを移動しただけで起きる」**と書いていた。これは**もう成立しない** —— 2026-08-27の統合で `import_step` がSTEP本文を `args["step"]` に生テキストで取り込むようになり、再生は外部ファイルを一切読まなくなったため、元ファイルを消しても移しても再生は成功する。

**書かれた再現手順は、それを書いた時点の実装に対する記述であって、現在の実装に対する記述ではない。**「壊れ方」は残っていても「壊し方」は別の変更で消えることがある。docstringやレポートの再現手順は、テストを書く前に**その場で一度実際に踏んで**成立を確かめる。

**代わりに使った失敗のさせ方**: 矩形反復(3×2＝6領域)の6番目を押し出しておき、`set_parameter` で `nx` を3→1に落とす。値の検査(`_require_usable_value`)は正の整数なので通り、再生中の `extrude` が「その添字の領域が無い」で ValueError を投げる —— **検査ではなく作り直しの途中で落ちる**という、後始末が要る唯一の状況をログだけで作れる。

関連: [[duocad-oplog-embeds-imported-step]](再生が外部ファイルを読まなくなった統合)、[[test-rewrite-report-omits-lost-coverage]](テストが「緑だが何も見ていない」形になる別事例)
