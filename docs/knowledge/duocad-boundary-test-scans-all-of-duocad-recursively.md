---
name: duocad-boundary-test-scans-all-of-duocad-recursively
description: duocadの tests/test_boundaries.py は duocad/ 配下の全.pyを再帰走査して OCP/build123d の直接importを禁じるので、幾何を直に触るコードを duocad/ の下へ移すと必ず赤くなる
created: 2026-09-05
updated: 2026-09-05
---

# duocad の境界検査は `duocad/` 配下を再帰走査する(サブパッケージで逃げられない)

`tests/test_boundaries.py` が固定している規律:

- **`OCP.*` を import してよいのは `duocad/kernel.py` だけ**
- **`build123d` を import してよいのは `duocad/query.py` だけ**
- 判定は **`DUOCAD_DIR.rglob("*.py")`** で、動的import(`importlib.import_module` /
  `__import__`)も AST で検出する。**免除はパス一致**であってファイル名一致ではない
  (`duocad/verbs/kernel.py` を作って抜けられないように、という明示の設計)。

**帰結: 幾何を直接触るコードを `duocad/` の下へ移すと、移した瞬間にこの2本が赤になる。**
2026-09-05 に `probe_topopt/` を `duocad/topopt/` へ移設したとき7ファイルが違反になった
(`topopt/voxelize.py` が OCP、`topopt/{voxelize,anchor,design_space}.py` と受入検査4本が
build123d)。

対処として採ったのは2つ:

1. **受入検査は `duocad/` の外へ置く**(リポジトリ直下 `topopt_checks/`)。検査は合成
   ソリッドを build123d で組むのが仕事なので、`duocad/` の中に置くと免除が11本ぶん要り、
   境界の設計意図そのものを壊す。`tests/` が `duocad/` の外にあるのと同じ理屈。
2. **製品側の3ファイルは名指しで免除**(ディレクトリ単位では免除しない)。免除一覧の中身を
   別のテストが `assert` で固定してあるので、増やすには2箇所直すことになる ——
   その手間が「境界を1つ緩めた」という判断の記録になる。

**新しいサブパッケージを `duocad/` に足すときは、幾何アクセスの経路を先に決めること。**

関連: [[topopt-code-moved-into-duocad-core]]
