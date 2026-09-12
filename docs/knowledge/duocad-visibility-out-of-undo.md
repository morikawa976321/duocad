---
name: duocad-visibility-out-of-undo
description: 判断(2026-08-26) — duocadのモデル表示/非表示をCtrl+Z・Ctrl+Yの対象外にした。保存にも残さないと本人が選択
created: 2026-08-26
updated: 2026-08-26
---

duocadの `set_visible`(モデルの表示・非表示)を**undo / redo の対象外**にすると本人が指示した。

派生の選択肢「操作ログから外すと保存ファイルにも可視性が残らなくなる(開き直すと全ボディ表示)」
について、**保存にも残さない**を本人が選択(理由の明言は無し)。選択と同じ「セッション状態」の
扱いになった。

実装は verb をやめて `duocad/verbs/visibility.py` を廃止し、実体を `duocad/selection.py` へ移した
(可視性は選択機構の一部という既存原則の延長)。関連: [[hidden-bodies-stay-uneditable]]、
[[duocad-selection-resets-on-shape-change]]。
