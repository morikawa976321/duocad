---
name: topopt-code-moved-into-duocad-core
description: 判断(2026-09-04)+実行(2026-09-05) — 形状最適化のコードは probe_topopt から duocad/topopt/ へ「移す」(コピーでも import でもない)。本体がprobeを参照するのではなく、probeが本体を参照する向きにする本人判断。移設は配線の前。受入検査は duocad/ の外の topopt_checks/。probe_topopt は同日(2026-09-05)に削除した — 他ノートが書いている probe_topopt/ のパスの読み替え表もここ
created: 2026-09-04
updated: 2026-09-05
---

# 形状最適化のコードは本体(`duocad/topopt/`)へ移した — 依存の向きを逆にする

**`probe_topopt` は検証用の一時フォルダという位置づけだった**(本人・2026-09-04)。配線タスクに
「コピーは二重管理になるので import する」と書いたが、本人の指摘で撤回した —— **コピーでも参照でもなく
「移す」という第三の道**を見落としていた。二重管理を避けたいのは正しかったが、その答えは本体を実験場に
依存させることではなかった。

## import が壊すもの(本人が挙げた理由3つ)

- **名前と実態がずれる。** `probe_` は「探り」。本体が依存するものがそこに居ると読む人に嘘をつく。
- **壊してよい場所が消える。** probe には画面(`server.py` / `web/index.html`)と実機確認(`shot*.mjs` /
  `screenshots/`)が同居しており、**どれが製品でどれが実験かが場所で表現されている**。本体が依存した瞬間に
  その区別が失われる。
- **配布に実験物が付いてくる。**

移設で依存の向きが **本体 → probe** から **probe → 本体** へ逆になる。向きが逆になるだけで、**実験場が
壊れても本体は無事**という性質が戻る。

## 順序: 移設は配線の前

probe 側の残タスクが全部終わった後、配線が始まる前に置いた。先に移すと残タスクの契約がずれる。後に回すと
「暫定で import して後で直す」が発生し、それは必ず忘れられる。

## 移設後の姿(2026-09-05)

- `duocad/topopt/`(コード9本)/ `topopt_checks/`(受入検査11本・502件。pytestには入れない)/
  `docs/TOPOPT.md`(実測の記録)。
- 受入検査が `duocad/` の外にある理由は [[duocad-boundary-test-scans-all-of-duocad-recursively]]。

## `probe_topopt/` は同じ日(2026-09-05)に削除した — 「実験場として維持」は撤回された

移設の直後は**実験場として維持する**と決めていた(理由は「この場所で実際に成果が出ている」——
メッシュの巻き順・4条件の色分け・進捗の見せ方はすべて probe で先に詰めてから本体の話になった)。
**同じ日にこれを覆した。** probe は `design_space.CASES` の**合成題材しか表示できず**、
実物の部品で起きた問題を切り分けられないことが分かったため
([[debug-views-belong-where-the-real-part-is]])。残っていた画面3つと撮影台を本体へ移し、
`probe_topopt/` は `git rm -r` で消した(duocad `70eb180`。中身は **`03bf794`** までの履歴)。

**他のノートが書いている `probe_topopt/...` のパスは、当時の場所である。** 読み替え表:

| ノートに書いてある場所 | いまの場所 |
|---|---|
| `probe_topopt/{simp,mesh,voxelize,anchor,design_space,stress,safety_loop,bench,perf}.py` | `duocad/topopt/` の同名 |
| `probe_topopt/check_*.py` | `topopt_checks/check_*.py` |
| `probe_topopt/server.py`(画面のサーバ) | `duocad/optimize_run.py` + `duocad/net/ws.py` |
| `probe_topopt/web/index.html`(ビューア) | `web/src/viewport.ts`(URLフラグは `web/src/debugFlags.ts`) |
| `probe_topopt/compare_shots.py` / `.mjs` | `web/scripts/shoot-topopt.mjs` + `shoot_topopt_export.py` |
| `probe_topopt/screenshots/smooth_compare/` | `docs/topopt-smooth-compare/`(16枚 + README) |
| `probe_topopt/shot.mjs` / `shot_anchors.mjs` | `web/scripts/smoke-optimize*.mjs` |

## 付随して決めたこと: CI が無い環境で「既定から外す」は「消す」と同じ

受入検査は通しで140.9秒。`pytest` のマーカーで既定から外す案を検討したが**採らなかった** —— duocad に CI が無い
(`.github/workflows` が存在しない)ので、外した瞬間に誰も回さなくなる。`pytest -q` が緑で返るため、無人の runner は
「緑だ」と報告して先へ進む([[detection-only-notification-ineffective]] と同じ形)。マーカーを使うなら向きを逆にする
(既定は全部走る。速く回したいときだけ外す)。duocad には既に「重いものは `tests/` に置かない」慣習があるので、
**受入検査はスクリプトのまま残す**ことにした。

## ドキュメントは三分割

全部を1か所へ寄せると、コードのそばにある契約と二重になって必ずずれる。

- **契約・不変条件** → コードのそば(docstring)。動かさない
- **実測の記録** → `docs/` の新規ドキュメント(readme の索引表からリンクされている。載せないと読まれない)
- **走らせ方** → `readme.md` の既存の節

関連: [[keep-the-finding-not-the-code-that-found-it]] / [[optimize-result-is-a-mesh-that-cannot-be-edited]] /
[[cantilever-preset-is-the-regression-baseline-not-a-demo]]
