---
name: build123d-face-center-is-not-the-area-centroid
description: 落とし穴(2026-09-09) — build123d の Face.center() の既定は CenterOf.GEOMETRY で、曲面では面パッチから大きく離れる。面積重心が要るときは CenterOf.MASS を明示する
created: 2026-09-09
updated: 2026-09-09
---

`Face.center()` を引数なしで呼ぶと **`CenterOf.GEOMETRY`** になる。平面では面積重心と
一致するが、**曲面では一致しない**。duocad の hanger のトーラス面(面積 2783mm^2)での実測:

| 呼び方 | 値 |
|---|---|
| `center()` / `center(CenterOf.GEOMETRY)` | `[0, -212.862, 60.0]` |
| **`center(CenterOf.MASS)`** | `[0, -200.0, 60.0]` |
| `BRepGProp.SurfaceProperties_s` の `CentreOfMass` | `[0, -200.0, 60.0]`(MASS と一致) |

**差は 12.9mm** で、これは面の代表寸法(sqrt(面積)=52.8mm)に対する比であって、
**解像度やメッシュの細かさには依らない**。

**「面の上のどこか」として使うと事故になる。** duocad では、面アンカーの節点重心との距離を
`3 x 要素サイズ` と比べる門がこれを使っていたため、要素サイズを小さくするほど許容だけが
縮んで**必ず落ちる**状態だった([[anchor-centroid-gate-false-positive-on-curved-condition-faces]])。
`CenterOf.MASS` に変えたら距離は 12.807mm → 0.055mm になった。

同じ罠は OCCT を直接叩く側にもある: **面の「中心」を返す API と「重心」を返す API は別物**。
重心が要るなら `BRepGProp.SurfaceProperties_s(...).CentreOfMass()`(duocad の
`kernel._face_props` がこれ)。
