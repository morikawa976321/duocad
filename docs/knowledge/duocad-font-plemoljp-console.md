---
name: duocad-font-plemoljp-console
description: duocad画面のフォントはPlemolJP Console(OFL)を同梱・固定。ユーザーは今後変わりうる旨を明言
created: 2026-08-23
updated: 2026-08-23
---

duocad(自作CADアプリ)の画面フォントを、OS依存のシステムフォント(Consolas/Cascadia Mono/Meiryo)から **PlemolJP Console**(v3.1.0、SIL OFL 1.1)の同梱・固定に切り替えた(2026-08-23決定)。

比較のため実機のdev serverでCica・HackGen Console・PlemolJP Console・UDEV Gothicの4候補をプレビューした上での選定。ユーザーは決定時に「今後変わるかもしれないが、いったんこれで」と明言しており、確定ではなく暫定固定である。

実装箇所: `duocad/web/src/styles.css` の `@font-face`(`--font` トークン先頭に配置、Consolas/Cascadia Mono/Meiryoはフォールバックとして残置)。フォント実体は `duocad/web/public/fonts/plemoljp/`(woff2、ライセンス文同梱)。
