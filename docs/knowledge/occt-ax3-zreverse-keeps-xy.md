---
name: occt-ax3-zreverse-keeps-xy
description: OCCTのgp_Ax3.ZReverse()は主方向だけ反転してX/Yを据え置く（左手系になる）ため、平面姿勢をorigin/normal/xdirの3つだけで保存すると反転面でv軸が符号反転する
created: 2026-08-30
updated: 2026-08-30
---

# gp_Ax3 の姿勢を数値で保存するときは v 軸も保存する

OCCT(OCP 7.9)で実測（2026-08-30）:

- `gp_Ax3(location, direction, xdirection)` のコンストラクタは**必ず右手系**を作る
  （`YDirection = Direction ^ XDirection`、`Direct() == True`）。
- `gp_Ax3.ZReverse()` は**主方向だけを反転し、XDirection と YDirection を据え置く**
  （結果は左手系 `Direct() == False`）。`XReverse` / `YReverse` も同様に1軸だけを反転する。

OCCTは平面の面が `TopAbs_REVERSED` のとき法線を裏返すのに `ZReverse()` を使うので、
**面の姿勢を origin / normal / xdir の3ベクトルだけで永続化して組み直すと、反転面では v 軸だけが
符号反転する**（＝スケッチ図形が平面内で鏡像になる。例外は出ず、見た目でも気づきにくい）。

対処: **v も保存し、復元時に `YReverse()` で左手系へ戻す**（`gp_Ax3(loc, normal, u)` を作った後、
`YDirection · v < 0` なら `YReverse()`）。復元後に4ベクトルを保存値と突き合わせて一致を検査すると、
直交していない枠や非正規の枠を黙って通さずに済む。

押し出しで作った20mm直方体は6面中5面が反転面だったので、**「たまたま反転面を踏まない」ことは
期待できない**。
