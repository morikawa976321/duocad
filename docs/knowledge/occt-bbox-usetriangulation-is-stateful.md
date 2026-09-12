---
name: occt-bbox-usetriangulation-is-stateful
description: 落とし穴(2026-08-31実測) — OCCTのBRepBndLib.Add_sをuseTriangulation=Trueで呼ぶと、形状に貼り付いたままの前回の三角形分割を使うため、同じ形状オブジェクトへ2回目に呼ぶと違うbboxが返る。BRepMesh自体はdeviation固定なら決定的で、非決定性の源は分割器ではなくbboxの取り方だった
created: 2026-08-31
updated: 2026-08-31
---

# OCCTのbboxは「前回の三角形分割」を見るので状態を持つ

`BRepBndLib.Add_s(shape, box, useTriangulation)` の第3引数を `True` にすると、
**形状に既に付いている三角形分割があればそれから** bbox を計算する。OCCT は
`BRepMesh_IncrementalMesh` の結果を形状に貼り付けたままにするので、同じ形状オブジェクトに
2回目の処理をかけると「1回目=素の幾何のbbox / 2回目以降=前回の三角形分割のbbox」と
**値が変わる**。

実測(duocad `probe_topopt/voxelize.py` stage1、2026-08-31):

- `Cylinder(10,30)`: `useTriangulation=True` の bbox が 1回目 ±10.0000001 → 2回目以降 ±10.0073。
- bbox から要素サイズ→三角形分割の細かさを決める作りだったため、実物STEP
  (`shelf_attachment_7.step`・231面)で三角形数が呼ぶたびに 62,264 → 62,168 → 62,138 と動いた。

**`BRepMesh_IncrementalMesh` 自体は deviation を固定すれば決定的**(同じ引数で4回呼んで
三角形数248/248/248/248、STEPでも62,050×3)。**非決定性の源は分割器ではなく bbox の取り方**
だった。決定性が要るなら `useTriangulation=False`(素の幾何から計算する)を使う。
実物STEP 231面で0.003秒なので、速度を理由に `True` を選ぶ必要はない。

検出できたのは、受入検査を「**同じ形状オブジェクトを使い回して、別解像度を1回挟んでから**
同じ解像度をやり直す」形に書いたため。「2回続けて呼ぶ」だけの決定性検査では素通りする
(1回目と2回目の差ではなく、曲面のある形でしか露見しない差だった)。
