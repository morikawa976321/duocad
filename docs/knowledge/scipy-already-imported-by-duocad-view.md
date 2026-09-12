---
name: scipy-already-imported-by-duocad-view
description: duocad は `import duocad.session` の時点で既に scipy を読み込む(view.py 経由)。import費用の退行検査に scipy の有無を使うと常に True になって検出力ゼロになる
created: 2026-09-05
updated: 2026-09-05
---

duocad は「`duocad/topopt/*` の import は関数の中に置く」(pytest 875件が毎回
scipy / skimage の起動費用を払わないため)という規律を持つ。その退行を検査しようとして
`import duocad; import duocad.session` の後に `scipy` が `sys.modules` に居ないことを
assert したところ、**このタスクの変更とは無関係に居た**。

実測(2026-09-05): `import duocad` だけなら numpy も scipy も来ないが、
**`import duocad.view` で numpy と scipy が来る**(このタスクの前から)。

**この規律の退行を検出したいなら、見るのは `skimage` と `duocad.topopt` である。**
scipy は既に別の理由で入っているので指標にならない。同型の注意として、
「重いものが来ていないこと」の検査は**まず現状を実測してから**書かないと、
最初から真にならない条件か、最初から偽の条件を固定してしまう。
