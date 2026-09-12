---
name: global-input-width-leaks-into-checkboxes
description: 落とし穴(2026-09-05・duocadで実測) — 素の`input`セレクタに書いた width/text-align/背景は checkbox・radio・range にも当たる。flexの中では行ごとに幅が変わり、隣の列の左端ががたつく
created: 2026-09-05
updated: 2026-09-05
---

CSSで `input { width: 7em; text-align: right; background: ...; border: ... }` のように
**素の要素セレクタで打ち込み欄の作法を書くと、`type=checkbox` / `radio` / `range` にも当たる**。
チェックボックスは `width` を無視しないので7emの箱になり、**flexコンテナの中では
`flex-shrink` の既定値1で潰れる。潰れ方は同じ行の他の要素の長さで決まるので、
同じ列なのに行ごとに幅が変わる**。

duocadのブラウザ(ボディ一覧27行)で実測した値:

| 行 | チェックボックスの幅 | 名前の左端x |
|---|---|---|
| `PC_case_20251122_1`(11面) | 33.4px | 305 |
| `PC_case_20251122_7`(6面) | 39.2px | 311 |
| `PC_case_20251122_8`(181面) | 27.6px | 299 |
| `PC_case_20251122_27`(90面) | 26.5px | 298 |

面数の桁が小さい行ほどチェックボックスが太り、**名前の左端が13px幅で左右にがたつく**。
「一覧が散らかって見える」という見え方の問題として現れるので、原因がCSSの
グローバル規則にあることに気づきにくい。

**打ち消す側が増えたら、規則の置き場所が違う。** duocadでは設定ダイアログが
`#settings-ortho { width: auto }` / `#settings-fov { width: auto }` と個別に打ち消しており、
その打ち消しコメント自体が「素の `input` の width:7em を打ち消す」と書いていた。
2箇所で打ち消していた時点で、グローバル側を型で絞るのが正しかった。

正しい形は、打ち込み欄の作法をグローバルに書くなら**同じ場所で型を除外する**こと:

```css
input[type='checkbox'],
input[type='radio'],
input[type='range'] {
  width: auto;
  flex: 0 0 auto;
  padding: 0;
  background: none;
  border: none;
  accent-color: var(--accent);  /* 指定しないとブラウザ既定の青が出る */
}
```

`accent-color` も同じ話で、**指定しないとダークテーマの中でチェックボックスだけが
ブラウザ既定の青**になる。27個並ぶと配色から外れた青の縦列ができる。

関連: [[duocad-ui-font-scale-two-steps]]
