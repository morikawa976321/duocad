---
name: marching-cubes-cell-center-half-offset
description: 事実(2026-08-30・QAが発見、実測で確認) — per-cell(要素中心)の値をscikit-imageのmarching_cubesへ渡すとき、配列の添字iをそのままi*spacingの位置として使うと面が全軸半セルずれる。正しいオフセットは(pad_width-0.5)*spacingで、pad_width*spacingでは0.5要素分大きすぎる
created: 2026-08-30
updated: 2026-08-30
---

# セル中心の値をmarching cubesへ渡すときは半セルのオフセットが要る

FEM/トポロジー最適化の密度場のように、**配列の各要素が「セルの中心」の値**(`rho[ex]` は
要素 `ex` の中心の値)を持つ場合、`skimage.measure.marching_cubes` にそのまま渡すと
面の位置が**全軸一律に半セルずれる**。

`marching_cubes` は配列の添字 `i` を、そのまま `i * spacing` の物理位置とみなして頂点を
置く(つまり添字を「格子点(コーナー)の値」として扱う)。ゼロパディング(`np.pad(rho, pad_width)`)
した配列では、パッド済み添字 `i` は元のセル `ex = i - pad_width` に対応するが、
**そのセルの物理的な位置はコーナーではなく中心** `(ex + 0.5) * spacing` である。
これを踏まえずに `offset = pad_width * spacing` を引くと、頂点座標は
`(ex) * spacing`(コーナー扱い)になってしまい、**正しい中心規約 `(ex+0.5)*spacing` から
半セル(`0.5*spacing`)だけずれる**。正しいオフセットは `(pad_width - 0.5) * spacing`。

duocad `probe_topopt` で実測(合成密度場、既知の位置に直方体ブロックを1つ置いた場合):

| オフセット | バウンディングボックス |
|---|---|
| `pad_width * spacing`(誤り) | 期待値から全軸 `-0.5*spacing` ずれる |
| `(pad_width - 0.5) * spacing`(正しい) | 期待値と厳密に一致(誤差は浮動小数点精度のみ) |

**このバグは体積・水密性・法線分布など「並進不変」な検査では一切検出できない**
(平行移動しても体積や辺の共有数は変わらないため)。検出するには、**既知の位置に置いた
合成密度場でバウンディングボックスの絶対位置を見る検査**が要る。

**副作用として、辺上の内分比を見る検査(marching cubesが二値化されていない連続値を
受け取っているかの検証。関連 [[topopt-probe-includes-face-anchoring]] とは別件)も
巻き添えを食う**: 内分比は「2軸が整数格子点・1軸だけが端点間の線形補間値」という前提で
判定するが、オフセット後の座標(半整数ずれた基準)でこの判定をすると軸の見分けが
丸ごと反転し、常に0.5という誤った結果になる。**この種の検査はオフセット適用前
(marching cubes直後の生座標、整数格子点のまま)で行う必要がある。**

関連: [[marching-cubes-winding-is-inward-for-stl-occt]](同じ実装で見つかった、巻き順に
関する別の落とし穴)
