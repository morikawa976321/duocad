---
name: occt-stl-relative-deflection-ignores-deviation
description: 事実(2026-08-10実測) — OCCTのBRepMesh_IncrementalMeshをisRelative=Trueで呼ぶと、線形偏差を0.2→0.05→0.01mmと1/20にしてもSTLが1バイトも変わらない(112084Bのまま)。build123dのexport_stlはisRelative=Trueなので、偏差を効かせたいなら流用せずisRelative=Falseで自分で呼ぶ
created: 2026-08-10
updated: 2026-08-10
---

# OCCTのSTL出力は相対偏差モードだと偏差指定が効かない

`cadquery-ocp-novtk==7.9.3.1.1`（import名は `OCP`）/ `build123d==0.11.1` の環境で、
`shelf_attachment_GD.step`（56面）を対象に実測した。形状は毎回読み直し、
`BRepTools.Clean_s()` で既存の三角形分割を捨ててから測っている。

| deviation (mm) | `isRelative=False`（絶対mm） | `isRelative=True`（相対） |
|---|---|---|
| 0.2 | 112084 B | 112084 B |
| 0.05 | 143884 B | 112084 B |
| 0.01 | 302684 B | 112084 B |

相対モードでは**偏差を1/20にしてもファイルが1バイトも変わらない**。値が無視されているのと
区別がつかない挙動で、`IsDone()` は `True` を返す（＝黙って効いていない）。

## 何に効くか

`build123d` の `export_stl`（`exporters3d.py`）は内部で
`BRepMesh_IncrementalMesh(shape, tolerance, True, angular_tolerance, True)` と
**第3引数 `isRelative=True`** で呼んでいる。「偏差を設定可能にする」ことを要件にしている
CADツールでこれをそのまま流用すると、**偏差のスライダを動かしても出力が変わらない**という
形で壊れる。しかも例外もエラーも出ないので、出力サイズを測るまで気づかない。

## How to apply

STL出力の偏差をユーザーに露出するなら、`BRepMesh_IncrementalMesh(shape, deviation, False, 0.5,
False)` のように **`isRelative=False`（絶対mm）で自分で呼ぶ**。build123dのエクスポータは
使わない。効いていることの確認は「偏差を下げたらファイルサイズが増える」の単調性で足りる
（絶対サイズは形状ごとに違うので契約にしない）。

関連: [[ocp-stepcontrol-reader-segfault-on-failed-readfile]] /
[[step-roundtrip-volume-not-bitexact]]
