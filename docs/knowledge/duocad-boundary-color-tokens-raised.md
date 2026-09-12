---
name: duocad-boundary-color-tokens-raised
description: 判断+実装(2026-08-23) — duocadは区画・境界の視認性が低い(本人判断・境界線強化が第一希望/背景コントラストは第二希望)を受け、--line/EDGE_COLORの明度を上げて解決。borderの無いflex帯内の区切りはposition:absolute疑似要素で追加(ビューポート実寸への影響ゼロ)。第二段は不要と決着
created: 2026-08-23
updated: 2026-09-05
---

# duocadの区画境界の視認性: 判断と対処

## 本人の判断（2026-08-23）

- **アプリ全体で区画・境界の視認性が低い**。特定の箇所を指した指摘ではなく、画面全体の傾向として。
- 直したい順序も明示: **第一希望は境界線（ボーダー）を目立たせること**、**背景色のコントラストを上げるのは「それでも足りなければ」の第二希望**。

着手前の実測値（`web/src/styles.css` / `viewport.ts`）: 区画の地の明度差が `--bg` oklch(17%) ↔ `--panel` oklch(20.5%) で 3.5pt、境界線と地の差が `--line` oklch(30%) ↔ `--panel` で 9.5pt、3Dビューポートの面の境界線 `EDGE_COLOR` 0x20262d はシーン背景 0x161a1f とほぼ同じ暗さ。

## 対処（同日実施・commit `14e3f0b`）

- `--line` を明度30%→40%(oklch)、`EDGE_COLOR`(面の境界線)を `0x20262d` → `0x3f4a56`(`GRID_COLOR` と同程度)へ上げた。実機スクショのピクセル実測で境界線とパネル地のRGB差が約2倍になったことを確認し、**地の明度差を広げる第二段は不要と判断**（`--bg`↔`--panel` の3.5ptは不変）。
- `--line` は区画境界のborderと、input/button/`.tag` の枠を兼ねている。今回もトークンを分割しなかった — 上げると枠も一緒に強くなるが、実機で見て支障が無かったため。分ける理由が明確になったら1トークンまでの追加を検討。**→ 2026-09-05に理由が見つかり、枠は `--line` を使わなくなった**(打ち込む欄は `--input-bg` の凹み、押すものは `--control-bg` の面。線は区画に譲る。[[duocad-ui-font-scale-two-steps]])。
- `PICK_EDGE_COLOR`(選べる辺)・`HIGHLIGHT_COLOR`/`PROPOSED_COLOR`(頂点マーカー色。`smoke-mcp.mjs`が値を機械検査)は変更していない。

## borderの無いflex帯の中の区切り線は、position:absoluteの疑似要素で足す（QA第2ラウンド・commit `0552ff6`）

上記の`--line`変更だけでは、`FilesPanel.svelte`(ファイル入力行→一覧行)と`CommandHost.svelte`(コマンド行→選択肢/パス/数値行)の内部にある行間の区切りは改善していなかった —— **そこにはそもそもborderが1本も無かった**ため。`.toolbar`は`flex:0 0 auto`でビューポート(`.main`)は`flex:1 1 auto`なので、border/paddingで行の高さを増やすと直接ビューポートの実寸を圧迫する(styles.css冒頭の不変条件)。

解決は**`position:absolute`の疑似要素**で、既存のflex gap(4〜6px)の中に`--line`色の1px線を描く方式 —— この要素は通常フローの高さに数えられないため、実寸への影響が理論上ゼロになる(`.viewport`の`getBoundingClientRect()`が変更前後で完全一致することを直接測定して確認済み)。**flex:0 0 autoな帯の中に新しい区切り線を足したいときは、border/paddingではなくposition:absoluteの疑似要素を使う**という、他の場面でも再利用できる手法。
