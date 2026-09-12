---
name: build123d-mesher-does-mesh-to-brep
description: 事実(2026-08-29実測) — build123d 0.11.1のMesherクラスはSTL/3MFの読み書きに加え、読んだ三角メッシュをfaceted B-repのSolidへ変換して返す。三角形→Face→Sewing→Shellの自前実装は要らない
created: 2026-08-29
updated: 2026-08-29
---

# build123dのMesherがメッシュ→faceted B-repをやる

`build123d.Mesher` は STL/3MF の入出力クラスだが、**`read()` が返すのは点群やメッシュではなくOCCTの形状**である。duocadの.venv（build123d 0.11.1）で実測:

```python
from build123d import Box, Mesher
m = Mesher(); m.add_shape(Box(10,10,10)); m.write(p)     # 684 bytes
s = Mesher().read(p)[0]
# type=Solid / faces=12 / volume=1000.0
```

内部で三角形→平面Face→`BRepBuilderAPI_Sewing`→Shell→(manifoldなら)Solid をやっているので、**この経路を自前で書く必要は無い**。

ただしこれは**faceted B-rep**であり、面の数は三角形の数と等しくなる（上の箱は同一平面の三角形が併合されて12面になっているが、有機形状ではそうならない）。10万三角形のメッシュを通せば10万面のソリッドになり、フィレット等のB-rep編集は事実上不可能。STEP交換用途に限る。編集可能なB-repへの変換路が無いことは [[no-oss-mesh-to-editable-brep]]。

`is_valid` はメソッドではなく**プロパティ**（`s.is_valid()` は `TypeError: 'bool' object is not callable`）。

既出の [[build123d-ships-svg-parsers]] と同じで、build123dは「入れた時点で付いてくる機能」が広い。
