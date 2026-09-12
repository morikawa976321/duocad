---
name: tracked-scratch-artifact-clobbered-by-acceptance-run
description: 落とし穴(2026-09-10実測・duocad) — duocad/topopt/output/design_space_reference.stl は build_reference の既定STL出力先なのにgit追跡下にあり、受入検査を1本走らせるだけで題材ごとの内容へ黙って上書きされる(並行セッションがコミットした2.4MBが検査1回で108KBの別物に)。検査後の git status に自分が編集していない成果物が出ていたら自分のrunが書いたものなので戻し、コミットに含めない
created: 2026-09-10
updated: 2026-09-11
---

# git追跡下の既定出力先は、検査を走らせるだけで動く(2026-09-10実測)

`duocad/topopt/output/design_space_reference.stl` は `design_space.build_reference` の**既定のSTL出力先**で、かつ**git追跡下にある**(直前に別セッションが `chore: design_space_reference.stl を更新` としてコミットしていた)。

案5削除タスクで `check_optimize_wiring.py` / `check_safety_loop.py` を回しただけで、HEADの2,419,684バイト(hanger 2.4mm由来)が108,084バイト(検査題材のbracket由来)へ置き換わった。**検査は失敗していない — 黙って上書きされる。**

- **受入検査を回したあとの `git status` に、自分が編集していない成果物が出ていないか見る。** 出ていたら自分のrunが書いたものなので `git checkout -- <path>` で戻す(自分が壊した状態を元に戻すのであって、他人の未コミット変更を消すのとは別)。**コミットに含めない。**
- 参照を作る検査・スクリプトを新しく書くときは `stl_path=` を必ず渡す。
- 一般化: **「既定の出力先を持つ関数」+「その出力先がgit追跡下」の組み合わせは、実行するだけでリポジトリの状態が動く。** [[run-topopt-default-stl-path-collides-across-repeated-calls]]は同一セッション内の衝突の話だったが、追跡下にあるとセッションをまたいで成果物が消える。
