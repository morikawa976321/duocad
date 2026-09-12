---
name: run-topopt-default-stl-path-collides-across-repeated-calls
description: probe_topoptの落とし穴 — design_space.run_topopt / anchor.run_pipelineはstl_pathを渡さないと共有の既定パス(design_space_case1.stl / bracket.stl)へ書く。二分探索や測定ループのように同じセッションで何度も呼ぶコードは、他の受入検査が「その検査自身の標準実行の成果物」として当てにしているファイルを検証のたびに上書きしてしまう
created: 2026-09-05
updated: 2026-09-05
---

# 繰り返し呼ぶコードは stl_path を必ず明示する

`probe_topopt/design_space.py` の `run_topopt` と `probe_topopt/anchor.py` の
`run_pipeline` は、`stl_path` を渡さないとそれぞれ `output/design_space_case1.stl` /
`output/bracket.stl` へ書く。この既定パスは **`check_design_space.py` [5] と
`check_anchor.py` 自身の標準実行の成果物**として存在する(README「ファイル」節)。

`safety_loop.py`(安全率の外側ループ)の初期実装は、二分探索の候補材料量ごとに
`run_topopt` を呼ぶときに `stl_path` を渡していなかった。テスト実行のたびにこの
共有ファイルが上書きされ、`git status` が `check_design_space.py` の標準実行と
無関係な検証の副産物で汚れる事故を実際に踏んだ(検証中に発見、コミット前に修正)。

## 教訓

- **`run_topopt` / `run_pipeline` を1回のセッションで複数回呼ぶコード
  (二分探索・パラメータスイープ・実測スクリプトなど)は、必ず自分専用の
  `stl_path`(scratch。`.gitignore` 対象)を渡すこと。** 既定パスに任せてよいのは
  「そのファイルの標準実行」を書いている当のコード(=そのファイルの受入検査自身)だけ。
- この構造は他の受入検査ファイルにも共通するはずなので、**次に同種のループ
  (パラメータを振って何度も端から端まで通す系)を書くときは真っ先に確認すること**。
- 対策の型: `safety_loop.py` は `_SEARCH_STL_PATH = output/safety_loop_search.stl` を
  モジュール定数として持ち、`.gitignore` に追加した。「探索の途中経過は scratch、
  採用結果が要るなら呼び出し側がもう一度 `stl_path` を指定して呼び直す」という
  設計にした(`optimize` は決定的なので再実行は同じ結果になる)。

関連: [[uncommitted-work-is-unsafe-while-unattended-agents-run]]（同じタスク領域で未コミットの変更が消えた別の事故）
