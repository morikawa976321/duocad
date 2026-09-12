---
name: duocad-viewport-switches-to-z-up
description: duocadのビューポートをZ-up右手系(X=右/Y=奥/Z=上)へ揃える決定。カーネルはZ-upなのにビューアがthree.js既定のY-upのままだった
created: 2026-08-27
updated: 2026-08-28
---

2026-08-27、duocadの3Dビューポートを **X=右 / Y=奥 / Z=上の右手系**(Fusion 360・Blender・Curaと同じ
CADの標準)に直すことを本人が決定した。**2026-08-28に実装済み**(`THREE.Object3D.DEFAULT_UP` をモジュール読み込み時に `(0,0,1)` へ、
既定の視点を front-top-right へ)。

## 発端になった事実

Fusion 360 / Blender / Cura の座標系(3つとも Z-up 右手系。Curaは内部のシーングラフだけY-up)を
確認した流れで、duocadのビューポートを調べたところ**カーネルとビューアで上方向が食い違っていた**:

- カーネル(OCCT/build123d)は **Z-up** —— `_BASE_PLANE_AXES` の `"XY"` の法線は `(0,0,1)`、
  「上面」の判定も `normal.Z > 0.9`。
- ビューア(three.js)は **Y-up** —— `camera.up` を設定しておらず `Object3D.DEFAULT_UP` の
  書き換えも無い。three.js既定のままだった。
- OCCTの座標は成分の入れ替えも回転もなく three.js へそのまま渡っており、**手系は右手系のまま**。
  食い違っているのは**上方向だけ**。
- 結果、画面で床に見えていたのは XZ 平面で、基準地面である XY 平面は壁のように立っていた。
  XY にスケッチして +Z へ押し出すと、上ではなく手前左へ伸びる状態だった。

直すのは**カメラのupだけ**で、ジオメトリ側に回転や軸入替を入れる話ではない。実装上の落とし穴は
[[three-js-bakes-default-up-at-construction]]。UIをFusionに寄せる方針は
[[duocad-ui-moved-to-fusion-layout]] と一貫している。
