---
name: duocad-bezier-kind-without-verb
description: 判断(2026-08-26) — duocadのベジエは「人もAIも直接は使えない図形種別」として実装する。取り込み(import_svg)だけが作り、add_bezierというverbは作らない
created: 2026-08-26
updated: 2026-08-26
---

# ベジエは「verbを持たない図形種別」として入れる

duocadへSVG取り込みを足すにあたり、曲線の扱いで3つの案を出したところ、本人は
**「人間もAIも使えない種別として実装して」** を選んだ。

- `bezier` は図形種別としては一人前（領域検出・押し出し・反復・保存・undo・描画が効く）
- ただし **`add_bezier` というverbは作らない**。カタログにも `oplog` にも登録しない
- ベジエを作れるのは `import_svg` だけ

提示した他の2案は「MCPにだけ開ける（AIは制御点を数値で打てる）」と
「ハンドルをドラッグするUIを作る」。

**関連**: この判断の材料になった実測は [[bezier-entity-kind-beats-flattening]]、
取り込み経路そのものの実測は [[duocad-svg-sketch-import-probe]]。
