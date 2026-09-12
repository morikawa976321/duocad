---
name: marching-cubes-winding-is-inward-for-stl-occt
description: 事実(2026-08-30実測) — skimage.measure.marching_cubesが返す三角形の巻き順は、外向き法線規約(v1-v0)x(v2-v0)に対して逆向き。是正はmarching_cubesを呼んだ直後(生成側)で行う
created: 2026-08-30
updated: 2026-09-04
---

# marching_cubesの巻き順はSTL/OCCTの規約と逆

`skimage.measure.marching_cubes` が返す `(verts, faces)` をそのままバイナリSTLへ書き出すと、
`build123d.Mesher().read()` で読み戻したSolidの体積が**符号だけ反転する**
(duocad `probe_topopt` で実測: 期待+3022.04が-3022.04で返ってきた)。

原因は巻き順。発散定理による符号付き体積 `sum(v0・(v1×v2))/6` を検証用の単純な立方体
(4x4x4のバイナリ場、中心2x2x2を1、level=0.5)で計算すると、`marching_cubes` が返す
そのままの順序では **-5.667**、列を `faces[:, [0, 2, 1]]` へ入れ替えると **+5.667** になる。
STL/OCCTは `(v1-v0)×(v2-v0)` が外向き法線になる巻き順(頂点を外から見て反時計回り)を
期待するので、`marching_cubes` の出力はこれと逆。

**是正は `marching_cubes` を呼んだ直後(生成側)で `faces[:, [0, 2, 1]]` に入れ替える。**
巻き順に依存しない処理は確かに多い(体積計算は発散定理の絶対値、頂点隣接行列は無向、
水密性検査は辺の共有数、法線の内積で測る肉厚は全体反転で不変)が、**依存する消費者は
STL書き出しだけではない** —— メッシュを描くクライアントは巻き順から法線を導く
(three.js の `computeVertexNormals`、既定の `side: FrontSide` による裏面間引き)。
書き出し直前で直す形は消費者が1つのうちは正しく、増えた瞬間に静かに壊れる
([[winding-fix-at-export-only-leaks-to-other-consumers]] が実例)。

向きが正しいことの検査は閾値を要らない: 閉曲面の向きは「全部外向き」か「全部内向き」の
2通りしかないので、**(a)同じ有向辺が2度現れない**(共有辺を2枚の三角形が逆向きにたどる
＝向きが全面で一貫)と**(b)符号付き体積が正**の2つで確定する。(a)だけでは全体反転を、
(b)だけでは一部反転の打ち消し合いを見逃す。

**注意(2026-09-04)**: 生成側で是正する2026-09-02の修正(`mesh.py`・`check_mesh.py` の検査9)は未コミットのまま消え、2026-09-04時点の `mesh.py` の巻き順は内向きに戻っている(実測。経緯は [[uncommitted-work-is-unsafe-while-unattended-agents-run]])。方針は変わらないが、コードにはまだ入っていない。

関連: [[build123d-mesher-does-mesh-to-brep]]
