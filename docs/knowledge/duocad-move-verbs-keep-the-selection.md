---
name: duocad-move-verbs-keep-the-selection
description: 判断の顛末(2026-08-30提案→2026-08-31却下) — move_sketch_entities/move_bodyだけresets_selection=Falseにする案を本人が非として却下し、17verb全て(移動系も含め)Trueへ統一済み
created: 2026-08-30
updated: 2026-09-03
---

# move verbも結局リセットする(提案は却下・統一契約が勝った)

## 結論(2026-09-03時点)

`move_sketch_entities` / `move_body` を含め、形を変える全17verbが `resets_selection=True` で統一されている。**「移動系だけ例外にする」案は本人が却下した**(commit `bc309d3`、2026-08-31、「本人が非として却下」)。以下は却下されるまでの経緯の記録。

## 経緯

2026-08-30、`move_sketch_entities` / `move_body` を新設した際、実装者判断でこの2本だけ
`catalog.Command.resets_selection=False` にした(他は従来どおり True)。理由(実装者の判断であって本人の明言ではない):

- リセット契約の根拠は「面・辺・領域の**添字**が形状変更で壊れる」ことだが、平行移動では
  壊れない。`move_body` は `TopLoc_Location` の差し替えなので面の添字と法線の対応が不変、
  `move_sketch_entities` の参照はそもそも添字ではなく `entity_id` である。
- 主用途が「少しずつ動かして位置を詰める」なので、1回ごとに選び直させると使い物にならない。

この例外が是か非かは「本人裁定待ち」としていたが、2026-08-31に**非**と裁定され、
`catalog.py` の2行がTrueへ戻された(commit `bc309d3` 「move_sketch_entities/move_bodyを
選択リセットの統一契約に揃える」)。

関連: [[duocad-selection-resets-on-shape-change]](統一契約そのもの)
