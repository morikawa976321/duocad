---
name: occt-add-bbox-overestimates-even-an-untrimmed-torus
description: 実測(2026-09-07・OCCT) — BRepBndLib::Add はトーラス面の bbox をトリムの有無に関わらず外す。一方 BRepPrimAPI の部分円柱・部分球は AddOptimal と厳密に一致する
created: 2026-09-07
updated: 2026-09-07
---

`BRepBndLib::Add` が実寸より大きな箱を返す件は「面のトリムを見ないから」と説明されている
（duocad `docs/TOPOPT.md`）が、**トリムしていない全周トーラスでも外す**。2026-09-07 の実測
（`useTriangulation=False` / `SetGap(0.0)`、比較対象は三角形分割の節点から作った箱）:

| 形状 | `Add` の辺 | `AddOptimal` の辺 | 三角形分割 |
|---|---|---|---|
| 全周トーラス R100 r10 | 238.13 x 238.13 x 20.0 | 220.0 x 220.0 x 20.0 | 同左 |
| トーラス片 R100 r10 掃引 pi/6 | 41.12 x 59.53 x 20.0 | 32.06 x 55.00 x 20.0 | 同左 |
| トーラス片 掃引 pi/12 | 24.02 x 28.72 x 20.0 | 23.07 x 28.47 x 20.0 | 同左 |

**外れ幅は 9.06mm でほぼ一定**（= r=10 の面を R=100 の円に沿わせたときの、極の周りの
ふくらみ）。掃引角を変えても倍率だけが変わる。

**逆に、`BRepPrimAPI_MakeCylinder(r, h, angle)` と `BRepPrimAPI_MakeSphere(r, angle)` の
部分形状は `Add` と `AddOptimal` が厳密に一致した**（実測。pi/2・pi/6 とも差 0.000mm）。
「曲面をトリムすれば必ずずれる」ではないので、**bbox の取り方を試す題材にはトーラスを使う**。
実物のSTEPで部分円柱がずれるのは、その面が BRepPrimAPI 由来ではないため。

三角形分割の箱は真の寸法を**超えない**（節点は必ず真の面の上にある）。その差は指定した
deviation よりずっと小さい — 0.2mm 指定で実測 0.012mm。実寸の独立した基準として使える。
