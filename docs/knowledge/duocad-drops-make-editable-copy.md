---
name: duocad-drops-make-editable-copy
description: 判断(2026-08-26) — duocadのverb make_editable_copy を削除する。あわせて import_step 側に「読んだら編集可能になる」機能を持たせ、STEP読み込みを1本に統合する
created: 2026-08-26
updated: 2026-09-04
---

**判断（本人・選択のみ。理由の明言なし）**: duocadの verb **`make_editable_copy` を削除する**。
このverbが何かを確認した直後に「不要。削除する」と決めた。

同じやりとりで決まった付随事項:

- **`import_step` 側に、読み込んだSTEPを編集可能にする機能を持たせる**（本人の言葉）。
  参照ボディだけを足す `import_step` は、`make_editable_copy` が消えると
  「読んでも編集する手段が無い」行き止まりになるため。
- **失われる能力**: ボディの複製。`make_editable_copy` は参照ボディ起点なら「編集可能にする」、
  編集用ボディ起点なら「複製」として働く同一verbで、後者の代替は作らない。

実装は taskbox `20260826-2135-duocad-drop-make-editable-copy` へ積んだ（2026-08-26）。
そこでのAI側の解釈: 機能を足すと現 `import_step_editable` と同一物になるので、
**生き残る名前を `import_step` にして `import_step_editable` を削除する**
（本人が名指ししたのが `import_step` だったことによる）。

このverbの来歴（削除の判断とは別の、記録としての事実）:
2026-08-10のM1（コミット `2f6bbfa`）で**実装計画の7 verbに無い追加**として入った。
`import_step` が作るボディが `kind="reference"` で編集できず、M1の完了条件に届かなかったため。
2026-08-15の [[duocad-startup-only-capability-is-a-defect]] で `import_step_editable` が入って
主役から降り、catalog の description 自身が「通常は要らない」と書く状態のまま残っていた。

関連: [[duocad-verb-single-declaration]]（verbメタ情報の正は catalog.py 1箇所）、
[[cad-verb-granularity-rule]]（ログ1行・undo1回・MCP1呼び出しが一致する単位）

続報: 2026-09-03に参照ボディ(`kind="reference"`)の概念自体を撤去した([[duocad-reference-body-removal-decided]])。
