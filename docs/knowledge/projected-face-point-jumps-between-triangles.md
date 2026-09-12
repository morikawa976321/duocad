---
name: projected-face-point-jumps-between-triangles
description: duocadのprojectFaceは「最初にピックできた三角形の重心」を返すので、視点や画角を変えると同じ面でも数十px跳ぶ。視点比較の測定点にはprojectPoint(face.centroid)を使う(2026-08-28にacceptance.mjsのFOV検査を実際にこれで直した)
created: 2026-08-28
updated: 2026-08-28
---

duocadの `window.__duocad.projectFace(bodyId, faceIndex)` は、その面の三角形を順に走査して
**最初に「本当にそこをクリックすればその面が取れる」画素**を返す実装である。つまり返るのは
面の代表点ではなく**たまたま条件を満たした三角形の重心**で、カメラを動かすと勝ち残る三角形が
入れ替わり、**同じ面なのに画面座標が数十px跳ぶ**。

2026-08-28に実測(画角を45→60に変えた前後、同一の面):

| 測り方 | 中心から211pxの面の移動量 |
|---|---|
| `projectFace`(最初にピックできた三角形) | 27.3 px |
| 面の重心(`face.centroid`)をそのまま射影 | 3.5 px |

したがって**視点・画角の違いを比べる検査の測定点に `projectFace` を使ってはいけない**。
比べたいのが「どこに写るか」なら、面の重心のような**固定した世界座標**を射影する
(`window.__duocad.projectPoint(face.centroid)`。`face.centroid` は `getGeometry()` の
`FaceGeometry.centroid`)。`projectFace` が要るのは「本当にそこを押せるか」を見るとき
(＝実際にクリックする手)だけである。

## 予告どおり実際に赤くなり、projectPointへ差し替えて解消した(2026-08-28)

タスク「STEP/STLの入出力をツールバーから左サイドバーのファイルパネルへ移す」でツールバーの
ボタンが3つ減り、ビューポートの高さが変わった。それだけで `acceptance.mjs` の
「画角45→60度で中心付近の面は6px以内」検査が、中心最寄りの面が入れ替わったことで
9.6px移動へ変わり、3回連続で再現性のある赤になった。コアの距離補正コード
(`viewport.ts` の `fitDistance`)は無変更で、原因は測定側だった。

`web/scripts/acceptance.mjs` の `faceScreenPoints()` を `projectPoint(face.centroid)` へ
差し替えて解消。副産物として、比較に使える共通面が8〜12面から59面へ増え、測定が
統計的にも頑丈になった。

検出力は温存されている: `fitDistance` を一時的に画角非依存(`camera.fov` を無視して常に45度で
計算)へ壊して再ビルドし、修正後の検査が正しく赤になる(exit 1・「収める距離が画角に追随して
いない」)ことを確認してから元に戻した。
