---
name: duocad-svg-sketch-import-probe
description: 実測(2026-08-26) — duocadへSVGをスケッチとして取り込む経路は既存の公開APIだけで端から端まで通った。同時に、図形1個ごとにパラメータd1..が増える設計が大量投入で効くことが判明（小さなSVGで既に309個）
created: 2026-08-26
updated: 2026-08-26
---

# SVG→duocadスケッチは既存APIだけで通る（ただしパラメータが増える）

rect / circle / ellipse（scaleしたgroup内）/ path（L・円弧A・3次ベジエC）を混ぜたSVGで、
`Document` → `sketch_create("XY")` → svgelements で分解 → `add_line` / `add_arc` →
`region.regions()` まで、**duocad本体に1行も足さずに**通ることを実測した。

- 投入: line 6 + arc 5 + ベジエ/楕円弧を折れ線化した線分 65 = **76図形**
- 検出された領域: **4個**（`BRepAlgoAPI_Splitter` は自己交差も入れ子も素通しで扱う）
- 所要: 取り込み 0.06秒 / 領域検出 0.007秒
- 折れ線化の許容偏差 0.05mm、弦からの偏差で再帰分割

**同時に判明した落とし穴 — パラメータの膨張**: `sketch._add_entity` は数値1個ごとに
`parameters.register_value` を呼び、`d1` `d2` … を作る。上記の小さなSVGで既に **309個**。
さらに `register_value` は `while f"d{n}" in doc.parameters` と1から線形に探すので **O(n^2)**、
`ParametersPanel` は `Object.keys(parameters).sort()` を全件並べる。ロゴ1個で数千行のパネルになる。
「全スカラーは名前つきの値」という設計は、人が1つずつ打つ前提では正しいが、
**外部ファイルから一括投入する経路を足すと初めて破れる**。

**形の対応**: duocadのEntityは line / circle / rectangle / arc の4種類しか無くスプラインを持てない。
そのまま乗るのは line・polyline・polygon・回転なしrect・circle・L/H/V/Z・真円のA。
3次/2次ベジエ・楕円弧（rx≠ry）・回転した矩形は折れ線化が要る。fill / stroke / text / image は捨てる。

**決めが要る規約2つ**: SVGはY下向きなので反転しないと上下逆に入り、反転すると円弧の回転方向も
反転するので start/end の入れ替えが要る。もう1つは `viewBox` しか無いSVGのユーザー単位→mmの倍率。
