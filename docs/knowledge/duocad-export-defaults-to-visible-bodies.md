---
name: duocad-export-defaults-to-visible-bodies
description: duocadのSTEP/STL書き出しは「表示中のボディのみ」に固定し、対象選択の引数を持たせないと決めた（2026-08-26）
created: 2026-08-26
updated: 2026-08-26
---

duocadのSTEP/STL書き出しについて、**対象は常に「そのとき表示中(`visible=True`)のボディのみ」とし、どのボディを書き出すかを選ぶ引数(`body_ids`)を持たせない**と決めた（2026-08-26）。人もAIも「ファイルに含めたいものを表示し、それ以外を非表示にしてから書き出す」という一本道になる。`path` と `deviation` は引数のまま残す（対象選択ではないため）。

決定前の挙動は「`kind="solid"` のボディ全部（1つも無ければ形を持つ全ボディ）」で、`Body.visible` を一切見ていなかった。したがって `set_visible(false)` で隠したソリッドもファイルに入っていた。

`Body.visible` は操作ログに載らず保存もされない（開き直すと全ボディが可視で始まる）ため、この決定により**書き出し結果は文書だけでは決まらずセッションの見た目に依存する**ことになるが、それを承知のうえでの選択である。

関連: [[duocad-oplog-embeds-imported-step]]
