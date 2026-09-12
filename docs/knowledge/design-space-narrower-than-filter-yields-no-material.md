---
name: design-space-narrower-than-filter-yields-no-material
description: 実測(2026-09-11) — 密度フィルタは塊の外を0として分母に数えるので、塊が筆(rmin)より細い通路だと物理密度が閾値0.5に届かず材料が1つも現れない。塊の連結性を6近傍で見るだけでは不十分
created: 2026-09-11
updated: 2026-09-11
---

duocad の `simp._physical` は

```python
hs    = _filter_conv(np.ones(格子), kernel, r)   # 分母は格子全体
field = _filter_conv(design, kernel, r) / hs
field[void] = 0.0
```

で、**分母が球全体の重み・分子に塊の外が 0 として入る**。したがって**塊の外に囲まれた
セルは、設計変数を1に振り切っても物理密度が1に届かない。**

既定 `DEFAULT_RMIN = 2.5`(要素)で、塊の通路の断面幅ごとに到達できる ρ の上限:

| 通路の幅 | ρ の上限 | メッシュの閾値 0.5 を超えるか |
|---|---|---|
| 1要素 | 0.158 | **超えない** |
| 2要素 | 0.428 | **超えない** |
| 3要素 | 0.829 | 超える |
| 4要素 | 0.915 | 超える |
| 5要素 | 1.000 | 超える |

床の `MIN_RMIN = 1.5` でも1要素は 0.452 で届かない(2要素で 0.711)。

**細い通路は「細い部材」ではなく「何も無い」になる。** そこに繋がっていた保持ソリッドは
浮き島として `mesh.remove_islands` に消える —— 症状は塊が切れている場合と区別がつかない。

## だから塊の連結性は6近傍だけでは足りない

6近傍の連結は**太さ1要素の糸でも成立する**。しかも `corridor_length` は「繋がる最小の
本数」を二分探索するので、**系統的にいちばん細い経路**を持ってくる。

正しい述語は「`erode(塊, rmin)` の上で条件が繋がるか」。**要素を足して幅が減ることはない
ので二分探索の前提はそのまま使える。**

## 罠: 構造要素をフィルタの台と厳密に揃えること

`generate_binary_structure(3,1)` の反復(L1球・半径3)で収縮を測ると「参照材料の2.0倍
取らないと幅が足りない」と出るが、フィルタの台は**ユークリッド球・半径2.5**。揃えて
測り直すと**1.0倍で足りる**に変わる —— **2倍ずれる。** `distance_transform_edt` で書けば
厳密かつ短い:

```python
erode  = distance_transform_edt(mask)  > rmin
dilate = distance_transform_edt(~mask) <= rmin
```

**塊の導出は `rmin` を知らないと幅を保証できない**(いまの `derive` / `derive_from_reference`
は受け取っていない)。渡して記録し、`run_topopt` 側で「塊を作ったときの rmin と、いま回す
rmin が同じか」を検算すること —— 呼び出し側が後から `rmin_mm` を変えると保証が黙って壊れる。

関連: [[fixture-faces-stranded-by-design-space-derivation]]
