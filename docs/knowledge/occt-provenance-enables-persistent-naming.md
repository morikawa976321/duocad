---
name: occt-provenance-enables-persistent-naming
description: 事実(2026-08-07実測) — OCCTの由来マップ(Modified/Generated/BRepTools_History)はOCPから直接叩け、添字を壊すフィレットでも旧面→新面を1件で正しく追跡できた。ただしbuild123dはこれを捨てている(参照0件)ため履歴を持つなら操作層はOCP直叩きになる
created: 2026-08-07
updated: 2026-08-07
---

# OCCTの由来マップで永続命名は作れる（build123dは捨てている）

[[build123d-face-index-breaks-on-fillet]] で「添字は壊れる」ことを実測した続き。
**壊した当の操作（縦エッジへのフィレット）**で、OCCTの由来マップが追跡できるかを試した。

```
基準形状の右側面      : 添字 i=5 / area 800
フィレット後          : 添字 i=9 / area 680     ← 添字はずれる
mk.Modified(旧面)     : 戻り 1件 → 新添字 i=9 / area 680  ← 正しく追跡できた
IsDeleted(旧面)       : False
```

- **`BRepTools_History` / `BRepBuilderAPI_MakeShape.Modified()` / `.Generated()` / `.IsDeleted()` は
  OCP（生のOCCTバインディング）から直接叩ける。** 永続命名（トポロジカルネーミング問題の本解）の
  中核はこの環境で**約20行で動いた**。
- **build123d はこれを捨てている** — パッケージ内に `Modified` / `Generated` / `BRepTools_History` への
  参照が**0件**。高レベルAPIは形状だけ返して由来を落とす。
- **帰結**: 履歴（パラメトリックな後戻り編集）を持つなら、**操作層は build123d ではなく OCP を
  直接叩く形になる**。これが履歴実装コストの本体であり、「1操作あたりの恒常税」として効く。
- **後付けは高いが、予防は安い**: 操作の戻り値に由来情報のスロットを空で用意しておけば
  （1操作1行）、後から操作ごとに順次L4化できる。一斉移行は不要。

**「FreeCADが10年かけた」は後付けの話**であって、由来マップを最初から通す新規実装のコストとは別物。
この点で以前の悲観（2026-08-07の初稿）は行き過ぎだった。

関連: [[cad-migration-scope-shifted-to-app]] / [[ocp-vscode-selection-channel-discovery]]
