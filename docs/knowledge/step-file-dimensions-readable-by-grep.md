---
name: step-file-dimensions-readable-by-grep
description: 運用知見(2026-09-03) — AP214のSTEPはテキストなので、grep/awkだけで寸法・穴径・ザグリ深さ・軸位置まで読める。CADを開く必要がない
created: 2026-09-03
updated: 2026-09-03
---

2026-09-03、[[hook-triangle-screw-not-limited-by-bending]] のレビューで、CADを一切開かずに
STEPから設計寸法を全部取り出せた。手順を残す。ファイルはISO-10303-21のテキストで、
1行1エンティティ（`#123=TYPE(...);`）なのでgrep/awkで十分扱える。

## 手順

1. **単位** — `grep -o "SI_UNIT([^)]*)"`。`.MILLI.,.METRE.` ならmm。
2. **バウンディングボックス** — `CARTESIAN_POINT` を全部拾ってmin/max。
   注意: 名前付きの点（`CARTESIAN_POINT('Origin',(...))`）があるので
   `CARTESIAN_POINT('[^']*',([^)]*))` で拾わないと取りこぼす。最初 `''` 決め打ちで
   935点中121点しか取れなかった。
3. **本当の頂点だけ** — `VERTEX_POINT('',#N)` を解決すると、B-splineの制御点が混ざらない
   実形状の角だけが得られる。側面（x=一定）の頂点だけ抜けば2D断面プロファイルが復元できる。
   ただし稜線Rのぶん x=0 / x=R / x=幅-R に分かれるので、Rの接線位置（例 x=1.0）を選ぶ。
4. **穴径・フィレット半径・ザグリ** — 半径がエンティティに直書きされている。
   - `CYLINDRICAL_SURFACE('',#axis,R)` → 穴径・稜線R（同じRが多数なら稜線Rのフィレット）
   - `CONICAL_SURFACE('',#axis,R,半角rad)` → **皿ザグリ**。0.7854 = 45° = 90°皿
   - `TOROIDAL_SURFACE('',#axis,大,小)` → フィレット。大 = 母体R ± 小 で対応関係が読める
5. **穴の位置と向き** — 上記の `#axis` は `AXIS2_PLACEMENT_3D('',#loc,#axis,#ref)`。
   `#loc`→CARTESIAN_POINT、`#axis`→DIRECTION を引けば中心座標と軸ベクトルが出る。
6. **ザグリ深さ** — 円錐面と円筒面の境界頂点のz（軸方向座標）差で読む。
   例: 皿の大端 r=4.8 が y=0、貫通穴 r=2.6 の始まりが y=2.2 → 深さ2.2、残り肉 = 板厚 - 2.2。

## 限界

面の接続関係（EDGE_CURVE / ADVANCED_FACE）まで追えば形状は完全に復元できるが、
B-splineサーフェスが混ざると手作業では割に合わない。断面が押し出しでない場合
（幅方向にもR形状がある等）は、断面の代表寸法までで止めて、残りは目視や本人への確認に回すのが速い。
