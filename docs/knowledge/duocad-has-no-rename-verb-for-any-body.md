---
name: duocad-has-no-rename-verb-for-any-body
description: duocadにはボディの改名verbが1つも無い(B-repも含めて全種別)。名前は作るときに決まり、後から変える口はMCPにもWSにもコアにも無い
created: 2026-09-05
updated: 2026-09-05
---

# duocad にボディの改名verbは無い(2026-09-05時点)

`grep -rn "rename\|body.name = " duocad/` の当たりは3件だけで、どれも改名の口ではない
(`speculative._restore` の巻き戻し / `add_mesh.py` の生成時 / `mcp_server` の別クラスの属性)。

つまり **`Body.name` は作った瞬間に決まり、以後変えられない**:

- STEP読み込み … `<元ファイル名の拡張子抜き>_1` … `_N`(`io_step.solid_names`)
- 押し出しで作った新規ボディ … `"solid"` 固定
- `add_mesh_body` … 引数の `name`(既定 `"mesh"`)

## なぜ記録するか

タスクの完了条件に「削除・表示切替・**名前変更**はできる」と書かれることがあるが、
**改名はメッシュ固有の欠落ではなく、duocad全体に無い機能**である。種別を1つ足す作業の中で
「メッシュだけ改名できない」と誤診して局所的な口を作ると、**種別ごとに違う改名手段**が
生えることになる。足すなら全種別共通の `rename_body` verb として別に判断すること
(`catalog.py` に1エントリ + `Session` + `DocumentTools` + MCPツール)。
