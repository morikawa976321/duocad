---
name: duocad-box-select-cost-is-a-quadratic-dedup
description: 解決済み(2026-09-04・duocad ff503b7/da7d621/49e69b4) — ドラッグ一括選択の遅さの正体は選択集合の重複判定のO(M^2)。27部品2835面の全面選択で合計1016ms→65.5ms、サーバ側857.5ms→1.5ms
created: 2026-09-04
updated: 2026-09-05
---

**解決済み**(2026-09-04。duocad `ff503b7` 重複判定の鍵引き / `da7d621`+`81969de` 面数を
ボディごとに1回 / `49e69b4` 頂点ループのDOM読み追い出し)。同じ条件で測り直した後の値:

| 矩形 | 選択数 | 修正前 | 修正後 |
|---|---|---|---|
| 120px | 315 | 67.0ms | 38.4ms |
| 画面の1/4 | 850 | 125.3ms | 88.9ms |
| 全面 | 2835 | **1015.6ms** | **65.5ms** |

サーバ単体の `modify_selection` は M=2835 で **857.5ms → 1.5ms**。残る数十msは選択件数に
比例しない固定費(WS往復・再描画・計測分解能)。

途中経過: 重複判定を鍵引きにした時点(`ff503b7`)で M=2835 は 47.3ms、その内訳のほぼ全てが `kernel.face_count` × M
(51.1ms。`TopExp.MapShapes_s` を参照1件ごとにやり直す)だった。`_coerce_refs` を新設してボディごとに1回だけ数える形にし
(`_face_map` の呼び出し 2835回→27回)、この残りも潰した。`kernel.py` にキャッシュは入れていない(モジュール関数に寿命の
受け皿が無く、戻り値が可変のOCCTマップで17箇所が使うため)。add/remove の両経路で同じ辞書を共有する必要があった件は
[[single-call-condition-needs-all-internal-invocation-paths-checked]]、fixtureの均一さが変異検査を通した件は
[[uniform-fixtures-hide-cross-body-cache-key-bugs]]。

以下は問題発見時点(修正前)の分析記録として残す。

duocadで「ドラッグで一括選択してからハイライトが出るまで」の内訳を実測した。
モデルは `PC_case_20251122.step`(27部品・2835面)、ビューポート740x748、
headless Chrome(実GPU: RTX 5060 Ti/D3D11)。

**離してからハイライトまでの合計**(実ドラッグ):

- 120px の小さい矩形(315面) = 67ms
- 画面の1/4(850面) = 125ms
- 全面(2835面) = **1016ms**

**内訳(全面2835面のとき)**:

| 場所 | 実測 | 比率 |
|---|---|---|
| サーバ `modify_selection` の重複判定ループ | 約810ms | 80% |
| サーバ `_coerce_ref`(→ `_face_map` の全走査) | 約42ms | 4% |
| クライアント `collectInBox` の頂点ごとの `size()` | 約30ms | 3% |
| クライアントの射影の算術そのもの | 2.3ms | 0.2% |
| WS往復・再描画・計測分解能 | 約130ms | 13% |

**支配項は `duocad/selection.py` の `modify_selection` にある
`for ref in coerced_add: if not any(same_ref(existing, ref) for existing in items)`**。
`items` を線形に舐めるのでM^2になる。サーバ単体の実測は M=850で86.7ms(うち重複判定76.5ms)、
M=2411で629.9ms(うち598.6ms)、M=2835で857.5ms。

**`_face_map` のキャッシュ欠如(`duocad/kernel.py`)は主犯ではない**。面数1006のボディでも
`face_count()` 1回は0.063msで、M=2411でも合計35.9ms。読んだだけの段階では
「同じ全走査をM回繰り返す」が最有力に見えたが、実測すると単価が安く、
**「安い処理をM回」より「M回の中でさらにM回舐める」ほうが桁で効いていた**。

`state_meta()` の固定費はこのモデルでは0.3ms(スケッチが無いため)。ただし図形1001個の
スケッチを開いていると `sketches_state()` が効いて10msになる —— 選択のたびに必ず払う。

関連: [[per-vertex-dom-read-in-a-projection-loop]]、[[duocad-state-meta-recomputes-all-volumes]]
