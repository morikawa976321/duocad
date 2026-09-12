---
name: duocad-region-projection-fails-on-tiny-triangles
description: 落とし穴+修正(2026-09-11実測) — duocadのviewport.tsのprojectRegionは、領域の三角形が画面上で小さすぎると永遠にnullを返す。smoke-svg.mjsではSVG取り込み後に再フレームされない(lookAtSketchはスケッチ入室時の1回しか自動で効かない)ためこれを踏み、取り込み後の#sketch-look呼び直しで解決。無関係な作業の検証中に発見し、変更を外して再現させて切り分けた
created: 2026-09-11
updated: 2026-09-12
---

`web/src/viewport.ts` の `projectRegion`(領域を画面座標へ射影する。1つの三角形につき4点=重心+3頂点寄せを試すサンプリング方式。[[duocad-sketch-handles-crowd-region-picking]])は、対象の三角形が画面上で小さすぎると**どの三角形のどのサンプル点も当たらず、恒久的にnullを返す**。ズームで三角形を大きくすれば直る(実機で確認: 起動直後のデフォルトズームでは `smoke-svg.mjs` のSVG(60×40mm)の「丘」領域(3領域中、矩形24×16にも円π×49にも一致しない方)が該当し、何十秒ポーリングしても `null` のまま。手動でズームインすると即座に成功する)。

## 発見の経緯(無関係な作業からの巻き込まれ)

長方形以外の交点(円・円弧)対応と線分中点スナップを実装中、`smoke-svg.mjs` が `page.waitForFunction(() => projectRegion(hill.index))` で120秒タイムアウトするようになった。最初は「SVG取り込みで作られる円弧の `start_deg`/`end_deg` が異常値(実測: `start_deg:270,end_deg:0`)になり、新設した `angleWithinArc` の反復加算whileループが停止しない」という仮説を立て、`%` による定数時間の正規化へ書き換えたが、**ハングは直らなかった**。

`git stash push -- <触った5ファイル>` で今回の変更を全部外し、同じ手順を手動でブラウザ実行したところ、**変更前のコードでも同じ領域の projectRegion が同じように恒久的にnullを返す**ことを確認し、無関係な既存不具合と判明した(切り分けの一般則は [[first-red-run-is-not-always-your-own-change]]。bare `git stash` は他セッションの変更を巻き込むのでパス指定にする)。念のための頑丈化(whileループ→%正規化)はしたが、それは今回のハングの原因ではなかった。

なお、この切り分けをブラウザの「computer」ツールで手動操作したとき、ツールの座標系が実CSSピクセルと違うことでもう1つ時間を溶かした → [[computer-tool-screenshot-coordinates-are-scaled]]。

## 根本原因と修正

`lookAtSketch`(`#sketch-look`=「正面から見る」)は**スケッチへ入るときの1回しか自動で呼ばれない**(`App.svelte` のモード遷移effect)。`smoke-svg.mjs` は空のスケッチへ入った直後にSVGを取り込むので、そのときのフレーミングは「まだ何も描いていない」既定倍率のままで、取り込みで図形が10個増えても再フレームされない —— これが丘の領域の三角形が画面上で数pxしかなかった直接の原因([[look-at-sketch-reframe-lags-and-frames-only-what-is-drawn]] の「描いたものにしか合わせない」と同じ性質の別経路)。

`smoke-svg.mjs` の取り込み直後に `page.click('#sketch-look')`(+400ms待ち)を足して `lookAtSketch` を明示的に呼び直すことで解決した(3回連続PASSを確認)。`viewport.ts` の `projectRegion` 自体には手を入れていない —— 「取り込みで内容が増えても自動では再フレームしない」という既存の挙動はそのままに、テスト側が実際のユーザー操作(小さくて見えづらければ「正面から見る」で寄り直す)を模すよう直した。
