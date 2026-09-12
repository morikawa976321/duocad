---
name: occt-curved-face-prism-silently-invalid
description: 曲面を法線方向へ掃引した押し出しは IsDone 真・体積変化ありのまま壊れた形状を返す（duocad M1で実測）
created: 2026-08-10
updated: 2026-08-10
---

面から直接プリズムを作って押し出す実装（`BRepPrimAPI_MakePrism` ＋ ブーリアン）は、**平面では正確だが曲面では黙って壊れる**。2026-08-10にduocad M1で `shelf_attachment_GD.step`（穴の円筒面22枚）を使って実測:

| 対象 | 距離 | 結果 |
|---|---|---|
| 平面（面36・面積314.159） | +2mm | 体積の増分 628.3185 = **面積×距離と1e-6以内で一致**。妥当 |
| 穴の円筒面（面1） | +1mm | `IsDone()` 真・体積も変化するが **`BRepCheck_Analyzer.IsValid()` が偽** |
| 穴の円筒面（面1） | -1mm | 妥当だが体積が **91018 → 38597** と激減（「有効だが無意味」） |

機序: 全周円筒の「面の中央の法線」は任意の半径方向で、そこへ掃引すると自己交差する／部品の大半を巻き込む立体になる。**エラーにならないので、検査しなければ壊れた形状が文書に残る。**

対処（duocadの判断）: **押し出せる面を平面に限定し、曲面は前提として弾く**。曲面のオフセットは別の操作として後で作る。加えて最終形状に `BRepCheck_Analyzer` の検査を入れる（これは統合の副作用にも効く → [[occt-unify-same-domain-corrupts-input]]）。

一般化: **OCCTでは「成功した」の判定に `IsDone()` だけでは足りない**。少なくとも「結果が空でない」「体積が期待どおり変わった」「`BRepCheck_Analyzer.IsValid()`」の3つを見る。同系の実例は [[ocp-stepcontrol-reader-segfault-on-failed-readfile]] / [[occt-stl-relative-deflection-ignores-deviation]]。
