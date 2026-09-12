---
name: build123d-ships-svg-parsers
description: 事実(2026-08-26実測) — build123d 0.11.1は推移依存でsvgelements・svgpathtools・ocpsvg・svgwriteを連れてくる。build123dを入れたPython環境ではSVGパーサとSVG→OCCT形状変換が追加installゼロで既に使える
created: 2026-08-26
updated: 2026-08-26
---

# build123dはSVGパーサを同梱で連れてくる

`build123d==0.11.1` を入れると、pyproject に書いていないのに以下が venv へ入る（推移依存）:

- **svgelements 1.9.6** — SVGのパーサ。全要素を Move / Line / Arc / CubicBezier / QuadraticBezier / Close へ正規化し、`transform` も解決済みの座標で返す。`Arc` は `rx` `ry` `center` `theta`（開始角・度）`delta`（掃引角・度）を持つので、真円の弧（rx==ry）かどうかを判定して円弧として扱える
- **svgpathtools 1.7.2** — パス演算
- **ocpsvg 0.6.0** — SVG↔OCCT形状の変換（`import_svg_document` / `wires_from_svg_path` / `faces_from_svg_path` / 逆向きの `edge_to_svg_path` 等）
- **svgwrite 1.4.3** — SVG書き出し

build123d 本体も `import_svg` / `import_svg_as_buildline_code` / `ExportSVG` を公開している。

**効き所**: build123dを使うCADプロジェクトでSVGを読み書きしたくなったとき、パーサの自作も依存の追加も要らない。duocadでのSVG取り込み検討（[[duocad-svg-sketch-import-probe]]）はこれが前提で、追加依存ゼロになった。
