---
name: mirroring-an-arc-swaps-its-endpoints
description: 実測事実(2026-09-06・duocad _mirror_arc) — 中心+半径+start_deg/end_deg(反時計回り掃引)で持つ円弧を直線に対して鏡映するとき、両角度を独立に映すと掃引の符号が反転して表現規約と矛盾する。始点と終点を入れ替えてから映す(new_start=2*axis-end, new_end=2*axis-start)。回転は角度を足すだけでよく、この非対称が見落としやすい
created: 2026-09-06
updated: 2026-09-06
---

# 円弧の鏡映は始点・終点を入れ替えてから角度を映す

「中心+半径+`start_deg`/`end_deg`(反時計回りの掃引)」で円弧を持つ表現で、軸(点A・単位方向u)に対する鏡映を実装するとき、**回転と同じ発想で `start_deg`/`end_deg` をそれぞれ独立に鏡映すると掃引方向が壊れる**。

鏡映は向きを反転する変換(行列式が負)。角度 φ を、角度 θ の直線に対して鏡映すると `2θ - φ`。これを両方へ適用すると `new_end - new_start = -(end - start)` になり、「startからendへCCWに掃引する」規約と矛盾するデータになる(見た目には正しい位置に見えても符号が食い違う)。

**正しい式**: 始点と終点を**入れ替えてから**映す。

```
axis_angle = atan2(u.y, u.x)
new_start_deg = 2*axis_angle - end_deg
new_end_deg   = 2*axis_angle - start_deg
```

これで `new_end - new_start == end_deg - start_deg`。中心点は通常の点の鏡映式(`t=dot(Q-A,u); foot=A+t*u; mirrored=2*foot-Q`)でよい。回転(向きを保つ変換)は `start_deg += angle` / `end_deg += angle` でよく、この非対称(回転は不要・鏡映は入れ替えが要る)が実装時に見落としやすい。

duocad `duocad/sketch.py::_mirror_arc` で実装、`tests/test_mirror_verbs.py::test_mirroring_an_arc_keeps_its_sweep_and_flips_its_position` で確認済み。関連: [[duocad-arc-sweep-is-ccw-in-kernel-but-signed-in-the-ghost]]
