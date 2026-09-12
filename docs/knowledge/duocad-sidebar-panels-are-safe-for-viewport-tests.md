---
name: duocad-sidebar-panels-are-safe-for-viewport-tests
description: 運用知見(2026-08-22) — duocadの右サイドバー(`.sidebar`)は幅固定+overflow-y:autoなので、パネルを増やしても`.viewport`の寸法(=カメラのアスペクト)は変わらない。実ブラウザ検査の決め打ち座標を壊す心配なく足せる唯一の場所
created: 2026-08-22
updated: 2026-08-22
---

# duocadの右サイドバーへのパネル追加は、決め打ち座標の実ブラウザ検査を壊さない

[[ui-restyle-breaks-hardcoded-pick-coordinates]] の教訓(サイドバー追加でビューポートが
狭まり、決め打ちのモデル座標クリックが画面外になる)を受けて、`duocad-save-folder-and-picker`
タスクで新しい保存/開くパネル(`FilesPanel.svelte`)をどこへ置けば安全か調べた結果:

**`web/src/styles.css` の `.sidebar` は `width: var(--sidebar-w)`(固定300px)・
`flex: 0 0 auto`・`overflow-y: auto`。** 中に積むパネル(`AiPanel`/`SelectionPanel`/
`ParametersPanel`/`SketchPanel`/新設の`FilesPanel`)がどれだけ増減しても、サイドバー自身は
内部スクロールで吸収するだけで**幅は変わらない**。`.viewport` は `flex: 1 1 auto` で
残り幅を埋めるので、サイドバーの中身がどれだけ伸びても`.viewport`の寸法(=カメラの
アスペクト比)には一切触らない。`App.svelte` の該当コメントに明記されている。

**危険なのはサイドバー自体の幅を変える変更・ヘッダ/ツールバーの高さを変える変更・
新しいサイドパネルを`.main`直下に横並びで足す変更**(`PartsPanel`のような固定幅の
兄弟要素)であって、**既存の`.sidebar`の中に縦積みでパネルを1つ足すだけなら
実ブラウザ検査(`smoke*.mjs`)への影響はゼロ**(実測: `FilesPanel.svelte`追加後、
`smoke.mjs`/`smoke-mcp.mjs`/`smoke-toast.mjs`/`smoke-files.mjs`は無風で緑のまま)。

今後duocadにUIを足すとき、「サイドバーに1枚積む」で足りる機能は、レイアウト起因の
実ブラウザ検査破壊を心配しなくてよい。

関連: [[ui-restyle-breaks-hardcoded-pick-coordinates]] / [[duocad-save-location-fixed-folder]]
