---
name: occt-unify-same-domain-corrupts-input
description: ShapeUpgrade_UnifySameDomain.Build() は入力の形状を壊すので「試して駄目なら戻す」が書けない（duocad M1で実測）
created: 2026-08-10
updated: 2026-08-10
---

`ShapeUpgrade_UnifySameDomain`（同一平面の面を統合するOCCTのツール）は、**入力として渡した形状そのものを壊す**。2026-08-10にduocad M1の実装中に実測した:

```
fuse の結果            -> BRepCheck_Analyzer.IsValid() = True
unify.Build() を実行
同じ fuse の結果を測り直す -> IsValid() = False   ← 入力が壊れている
unify の出力            -> IsValid() = False
unify 前に取った BRepBuilderAPI_Copy -> IsValid() = True （無傷）
```

**帰結: 「統合してみて、結果が駄目だったら統合前に戻る」という素直な書き方が成立しない** — 戻り先が既に壊れている。正しい手順は、**コピーに対して試し**、採用条件を満たしたときだけ元の形状に対して本番の統合を行う。本番を元の形状に対して行う必要があるのは、由来（`BRepAlgoAPI_*.Modified()` が返す部分形状）を鍵に新旧の面の添字を張るためで、コピーに統合すると部分形状の同一性が切れて由来の射影が壊れる。

採用条件は3つ（duocadでの判断）: 妥当（`IsValid()`）・体積が変わらない・**面が実際に減る**。3つ目を入れたのは、面が減らない統合は利得ゼロで入力を壊す危険だけが残るため。実測では `shelf_attachment_GD.step` の面36・43を+2mm押し出したとき **57面→57面**（利得なし）で統合後だけ壊れ、20mm立方体の上面+10では **10面→6面**（有効）だった。

この癖に気づかず最終形状に妥当性検査を足すと、**正常な平面の押し出しまで弾かれる**（duocadでテスト8件が同時に赤になった。原因の切り分けに時間を溶かした箇所）。

同じ「OCCTは例外を投げずに黙って失敗する」系: [[ocp-stepcontrol-reader-segfault-on-failed-readfile]] / [[occt-stl-relative-deflection-ignores-deviation]] / [[occt-step-writer-transfer-status-must-be-checked]] / [[occt-curved-face-prism-silently-invalid]]
