---
name: line2-raycast-ignores-line-threshold
description: three.jsの太線Line2/LineSegments2のraycastはraycaster.params.Line.thresholdを見ず、当たり半径はmaterial.linewidthになる
created: 2026-09-11
updated: 2026-09-11
---

# Line2 の当たり判定は `params.Line.threshold` を見ない

three.js の太線(`Line2` / `LineSegments2` / `LineGeometry`。0.185.1で実測)は、
`raycaster.params.Line.threshold` を**参照しない**。当たり半径は `LineMaterial.linewidth`
(=描いている線の太さ[px])で決まる。素の `THREE.Line` とは別の実装なので、
「同じ Raycaster に同じ threshold を渡している」つもりでも効かない。

duocad で実際にこれを踏んでいた —— スケッチ線の当たり半径を `EDGE_PICK_PIXELS`(6px) の
つもりで `params.Line.threshold` に入れていたが、実際に効いていたのは
`SKETCH_LINE_WIDTH_PX`(1.5px)で、**線から2px外すともう掴めなかった**。
1年近く誰も気づかなかったのは、実機smokeが線の**真上**しかホバーしていなかったため。

併せて: `LineSegmentsGeometry` の `position` 属性は**太線を描くための箱のテンプレート**(8頂点)
であって折れ線ではない。折れ線の点列を後から読みたいなら、作るときに別途持っておくこと。
