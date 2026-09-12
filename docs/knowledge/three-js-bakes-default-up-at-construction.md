---
name: three-js-bakes-default-up-at-construction
description: three.jsのOrbitControlsとHemisphereLightは生成時のupを焼き込むので、camera.upを後から変えても効かない(Z-up化のときに踏む)
created: 2026-08-27
updated: 2026-08-27
---

three.js を Z-up で使うとき、`camera.up.set(0, 0, 1)` を**後から**呼ぶ形にすると、2か所が
静かに Y-up のまま取り残される。**`THREE.Object3D.DEFAULT_UP.set(0, 0, 1)` を、カメラ・
コントロール・ライトを1つも `new` する前に実行する**のが正しい直し方。three 0.185.1 の
実物(`node_modules`)で確認した機序:

- **`OrbitControls`** —— **コンストラクタ**で
  `this._quat = new Quaternion().setFromUnitVectors( object.up, new Vector3(0,1,0) )` を計算し、
  `update()` で再計算しない。生成後に `camera.up` を変えても**オービットの極は変わらない**。
  見た目(画面の上方向)だけ直って回転の軸が旧いまま、という分かりにくい壊れ方をする。
- **`HemisphereLight`** —— コンストラクタで `this.position.copy( Object3D.DEFAULT_UP )` を実行し、
  **その position が空(sky)の向きになる**。後から up を変えても照明の天地は横倒しのまま。
  逆に言うと `DEFAULT_UP` を先に書き換えておけば追加のコードは要らない。

`DirectionalLight` を `camera.add(light)` でカメラの子にしている場合、その `position` は
カメラのローカル座標なので影響を受けない(触らなくてよい)。

**派生する注意**: 平面スケッチのために一時的に `camera.up` を倒す実装をしている場合、
`OrbitControls._quat` は更新されないので**そのモード中だけオービットの極がずれる**。
`_quat` は私有API(アンダースコア)なので、直すならバージョン更新で壊れる書き方になることを
承知の上でやる。duocadで実際にこの状態になっている([[duocad-viewport-switches-to-z-up]])。
