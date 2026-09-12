---
name: duocad-reference-body-removal-decided
description: 判断(2026-09-03・同日実装済み) — duocadのkind="reference"(読み込みの原本)という概念を撤去し、import_stepは編集用ボディ1つだけを作る形へ変えた
created: 2026-09-03
updated: 2026-09-04
---

**判断(本人・選択のみ)**: duocadの `Body.kind="reference"`(`import_step` がソリッドごとに作る、不可視・編集不可の読み込み原本)という概念を**撤去する**。`import_step` は編集用の `kind="solid"` ボディ1つだけを作る。

**経緯(判断の材料として本人へ提示した事実)**: 参照ボディを唯一「使って」いた機能は `make_editable_copy`(参照から編集用コピーを複製する)verbだったが、これは2026-08-26に [[duocad-drops-make-editable-copy]] で本人が理由の明言なく削除済み。以後、参照ボディは `describe_document(include_references=True)` で「見せるだけ」の存在になり、リセット・差分比較・再複製などの機能は無かった。加えて、新設した `delete` verb([[duocad-delete-verb-contract]])が編集用ボディを消しても対になる参照ボディは不可視ゆえ二度と選べず消せず、**永久に孤児化する**欠陥が見つかったことが撤去判断の直接のきっかけ。

**実装は同日2026-09-03に完了**(duocad commit `603cf3b`、16:46)。判断時点で挙げた撤去対象は `import_step`(`duocad/verbs/import_editable.py`)・`mcp_server.py` の `kind != "reference"` 分岐群・`describe_document` の `include_references`/`reference_count` 引数・関連テスト(`test_io_step.py`/`test_mcp_verbs.py`)。[[duocad-model-files-are-disposable]] によりマイグレーションは不要(旧保存ファイルは捨ててよい方針)。なおこの実装のために積んだtaskboxタスクファイルは、別セッションが先に実装を終えたあと痕跡なく消えた([[taskbox-queued-file-vanished-without-trace]])。

関連: [[duocad-complexity-reduction-before-v1-1]](複雑性削減の路線)
