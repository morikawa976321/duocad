---
name: a-check-that-asserts-the-default-has-not-changed-blocks-changing-it
description: 落とし穴(2026-09-05・duocadの受入検査で実発) — DEFAULT_RMIN == PRESET["rmin"]のような「既定を変えていない」assertは、既定を変えるタスクが来た瞬間に必ず落ちる。守りたいのがBだけならB == 定数と書く。同時に、指紋(SHA-256)を採る検査が既定に追随する引数を省略して呼んでおり、既定を動かした瞬間に黙って別物を測っていた。指紋を採る呼び出しは既定に追随しうる引数を全部明示する
created: 2026-09-05
updated: 2026-09-06
---

duocadの受入検査に `DEFAULT_RMIN == PRESET["rmin"]`(「既定を変えていない」)というassertが置いてあり、2026-09-05に既定を実測に基づいて動かしたとき**まずこれが落ちた**。

このassertが本当に守りたかったのは `PRESET["rmin"]`(回帰基準のρ場のSHA-256が乗っている値)のほうで、既定値そのものではない。**「A == B」で守ると、AとBのどちらを動かしても落ちる** —— 守りたいのがBだけなら `B == <定数>` と書く。

同時に踏んだ同型の穴: **指紋(SHA-256)を採る検査が、既定値に追随する引数を省略して呼んでいた。** 「基準はプリセットに乗っているから安全」はプリセット経路にしか当てはまらず、もう1本の指紋は既定を動かした瞬間に黙って別物になる。**指紋を採る呼び出しは、既定に追随しうる引数を全部明示すること。** 同じ機序で比較スクショが再現不能になった件は [[comparison-shots-die-when-the-default-moves]]。

関連: [[bit-exact-baseline-is-retaken-when-the-material-changes]]
