---
name: window-escape-preventdefault-kills-dialog-esc-close
description: 落とし穴(2026-09-04・duocadで実測) — 全域のkeydownハンドラがEscapeにpreventDefaultを掛けていると、native <dialog> のEscで閉じるが黙って死ぬ。モーダルでもそうなる
created: 2026-09-04
updated: 2026-09-04
---

native `<dialog>` の「Escで閉じる」は**keydownの既定動作(close request)**として実装されている。
したがって、**どこかの全域ハンドラが Escape に `preventDefault()` を掛けていると、ダイアログは
Escで閉じなくなる**。`showModal()` で開いたモーダルでも同じで、モーダルだから守られるということは無い。

duocadでの実例: `CommandHost.svelte` が `svelte:window onkeydown` で
`if (event.key === 'Escape') { event.preventDefault(); abort() }`(構え中のコマンドの中断)を
持っている。このため**設定ダイアログは新設時からEscで閉じたことが一度も無かった**にもかかわらず、
`SettingsDialog.svelte` の冒頭コメントは「`showModal()` を使うので Esc がただで付く」と書いていた。
2026-09-04に非モーダル化([[duocad-settings-dialog-goes-non-modal]])したとき、Escを自前で拾う
ハンドラを足して初めて実際に閉じるようになった。

**気づきにくい理由**: 仕様上は付いてくる機能なので、実機で押して確かめる動機が湧かない。
コメントに「ただで付く」と書いてあると、その記述自体がさらに検証を遠ざける。
**ネイティブの既定動作に頼る機能は、全域のキーハンドラがある画面では実際に押して確かめること。**

**直し方の注意**: 自前のEscハンドラで `preventDefault()` / `stopPropagation()` を掛け返さない。
duocadでは同じEscを中断にも使っており、モーダルだった頃も両方が起きていた —— 片方を止めると、
その修正が変えるつもりの無い挙動まで変わる。
