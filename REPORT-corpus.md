# REPORT-corpus — 再生の指紋を補う合成の文書群

担当 `corpus`。ブランチ `cloud/perf-corpus`(基準 develop 4ef67570)。足したのは新しい道具 `tools/synthetic_corpus.py` と
そのテスト `tests/test_synthetic_corpus.py` だけで、既存のファイルは1行も変えていない(`git diff --stat 4ef67570 HEAD` は
この2つとこの報告だけ)。測ったのはクラウドの機械(4 コア・15 GB・Linux・Python 3.14.6・`uv sync --frozen` の `.venv`)。

要点:

- 300 本の文書を種から作れる(登録されている全 42 verb を使い、全文書に添字の連鎖がある)。同じ種で3回作り、どの2回の組でも
  295〜298 本がバイトまで同じで、違った 4 本の違いは `delete_faces` の由来(`provenance.face_map`)の並びだけ(引数と形は同じ)。
- 1操作ずつの指紋(`steps`)は基準で2回再生して 300 本・5,742 操作の全部が一致し、再生の最後の状態は生成時(確定の経路)の状態と
  300 / 300 一致した。三角形の指紋(`--triangles`)も2回で全部一致した。
- 陽性対照: 「多数の領域の柱を1回の多入力ブーリアンで結合する」(decisions.md で不採用にした案)を写しに入れると、62 本中 34 本で
  面・辺・頂点の並びが変わり(数と体積は同じ)、16 本で後の添字の操作が別の面に掛かって落ちた。道具は最初に変わった押し出しの操作を指す。
- **基準のコードに非決定が2つ見つかった**(5章の A・B。どちらも担当の外のコードなので直していない)。
  A: 並列の `BRepCheck_Analyzer` が同じ形を 4〜5.5% の割合で「不正」と判定し、文書の再生がたまに失敗する。
  B: 画面へ配る三角形分割が載ったボディに `extrude` の join を掛けると、OCCT のブーリアンがボディを消して足した柱だけを返す
  (MCP の確定で起きる。開き直すと正しい形に戻るので、作業中の形とファイルが食い違う)。

## 0. 親が前後で比べる手順

文書群は**1回だけ作ってフォルダごと持ち回す**。作り直すと、生成の時間の上限(丸め・面の編集は 20 秒、この道具の予算も 20 秒)に近い
操作が機械の速さで通ったり断られたりして、別の文書になりうる(この機械では、2本並べて走らせても同じ文書ができた)。

```bash
# 1. 文書群を作る(300 本。この機械で 19 分)
.venv/Scripts/python.exe -X utf8 tools/synthetic_corpus.py generate --out <群> --seed 1 --count 300
```

```bash
# 2. 基準のコードで、1操作ずつの指紋・三角形つきの指紋・最後の状態の指紋を取る(DUOCAD_REPO = 再生に使う duocad のチェックアウト)
DUOCAD_REPO=<基準のチェックアウト> .venv/Scripts/python.exe -X utf8 tools/synthetic_corpus.py steps --out before.json <群>/documents
```

```bash
DUOCAD_REPO=<基準のチェックアウト> .venv/Scripts/python.exe -X utf8 tools/synthetic_corpus.py steps --triangles --out before_tri.json <群>/documents
```

```bash
<基準のチェックアウト>/.venv/Scripts/python.exe -X utf8 <基準のチェックアウト>/tools/replay_fingerprint.py --out before_final.json <群>/documents
```

```bash
# 3. 変更後のコードで同じものを取り(after.json / after_tri.json / after_final.json)、比べる
.venv/Scripts/python.exe -X utf8 tools/synthetic_corpus.py compare before.json after.json
```

```bash
.venv/Scripts/python.exe -X utf8 tools/synthetic_corpus.py compare before_tri.json after_tri.json
```

```bash
.venv/Scripts/python.exe -X utf8 tools/replay_fingerprint.py --compare before_final.json after_final.json
```

- `compare` の終了コード: 0 = 一致、1 = 形・添字が違う(入れない)、2 = 形・添字は全部同じで三角形だけが違う(親が判断する)。
- 出力の `DIFFER <文書>: seq <n> <verb> | body <id> faces,edges counts [...]->[...] volume ...` は最初に形・添字が違った操作。
  最初に違う面・辺の添字と値まで出すには、その文書だけ `steps --detail` で取り直して比べる(1文書あたり数十倍の大きさになる)。
- `compare` は verb ごとの再生の時間の前後も出す(同じ機械で取った2つのときだけ意味がある)。
- `DUOCAD_REPO` は、この道具の無い基準のコミットをこの枝の道具で測るため。4ef67570 を `git archive` で書き出したフォルダで、
  5 本の再生がこの枝と一致することを確かめた(`duocad` は `DUOCAD_REPO` の物が読まれる。editable の `.pth` より前に入れる)。
- 内訳 `<群>/manifest.json`: 文書ごとの土台・verb の数・添字の参照の数・連鎖の数・断られた操作(種類と例文)・3 秒以上の操作・
  生成時の最後の状態の要約。`steps` は再生の最後の状態がこの要約と一致するかを出す(違えば確定と再生で形が割れている)。
- Windows では最大常駐量を測らない(`resource` が無い。値は null)。

### 指紋が見るもの

| | replay_fingerprint | synthetic_corpus steps |
|---|---|---|
| いつ | 最後の状態だけ | **各操作の直後**(最初に違った操作が分かる) |
| 面(添字順) | 種別・面積・重心 | 同じ + 外向きの法線 |
| 辺(添字順) | 種別・長さ | 種別・長さ + **始点・中点・終点**(同じ長さの辺の入れ替わりを拾う) |
| 頂点 | 数 | **添字順の座標** |
| 体積 | 丸めた値(アプリの体積のキャッシュを通る) | 丸めた値(OCP で積分し直す) |
| スケッチ | — | 添字順の領域(面積・重心)と数、名前 |
| 文書 | — | ボディの並び・アクティブ・名前・寸法の値・形状最適化の条件(面の添字を持つ) |
| 三角形 | — | `--triangles` で、各操作の直後に `DocumentView.delta` が配る面ごとの節点と並び |

面の性質はアプリの `kernel._face_traits` のキャッシュを通さずに測る(キャッシュの誤りも拾うため)。値は 4 桁(法線 3 桁・体積 3 桁)で
丸めてから要約する。丸めの境目をまたぐ微小な差(並列化で 1e-12 程度ずれる)が出ると、形が同じでも DIFFER になりうる。そのときは
`--detail` で値を見て判断する。

### 文書の作り方

`Session.apply` の経路(画面・MCP と同じ)で組み、対象の面・辺・頂点は形をその場で測って乱数で添字を選ぶ。ID は生成の間だけ
`uuid4` を種から振る関数に差し替える。土台は 11 種(板・穴の多い板(最大 12×8)・重なる図形の多数の領域・円柱とボス/穴・回転体・
ロフト・B-rep 本文の立体(`add_solid_body`)と三角形のボディ・STEP の取り込み・SVG の取り込み・鏡映して結合した対称な板・
板の上面の柱 6〜60 本を1回で結合)。続きの操作は 4〜10 回で、多くは直前の操作が変えたボディに重ねる: 丸め・面取り・面に貼った
スケッチからの押し出し/切り取り・面の押し出し・`extrude_face`(再生専用の旧 verb)・面のオフセット・シェル・面の削除・ねじ・分割・
鏡映・移動と回転(辺・頂点で軸を指す)・ブーリアン(ツール 1〜2 個、面で指す)・投影・包む領域・形状最適化の条件・改名・削除・
寸法の値の変更(`set_parameter`。ログを頭から作り直す)。連鎖が1つもできなかった文書だけ、最後に同じボディへ連鎖を足す。

## 1. 測った内訳(前)

基準 4ef67570(= この枝の `duocad/`)で、作った 300 本を測った。値は1回ずつ(生成は3回、再生は各2回走らせ、時間は ±1 割の範囲で揃った)。

| | 時間 | 備考 |
|---|---:|---|
| 生成 300 本(単独) | 1,139 秒(19.0 分) | 1本の中央値 0.67 秒・p90 7.7 秒・最大 69.5 秒。最大常駐量 1,040 MB |
| 生成 300 本(2本並べて) | 1,212 秒 / 1,197 秒 | |
| `steps`(2本並べて) | 415 秒 / 409 秒 | 再生 348 秒 + 指紋 67 秒。1本の中央値 0.31 秒・p90 3.6 秒・最大 17.9 秒(ねじ)。最大常駐量 779 MB。出力 3.3 MB |
| `steps --triangles`(2本並べて) | 454 秒 / 454 秒 | 最大常駐量 878 MB。出力 3.5 MB |
| `replay_fingerprint.py`(2本並べて) | 388 秒 / 390 秒 | 開く時間の合計 363 秒 / 377 秒 |
| 文書群の大きさ | 6.8 MB | 5,742 操作。1本 5〜39 操作(中央値 18) |

verb ごと(生成は `Session.apply` の時間で、断られた試行と予算で打ち切った試行を含む。`set_parameter` と `extrude_face` は
`apply` を通さないので生成の時間を測っていない):

| verb | 文書数 | 操作数 | 生成 s | 再生 s | 再生(--triangles) s |
|---|---:|---:|---:|---:|---:|
| thread | 47 | 51 | 154.3 | 131.0 | 127.2 |
| extrude | 295 | 1166 | 78.9 | 73.7 | 69.8 |
| fillet | 160 | 219 | 61.5 | 54.1 | 52.4 |
| boolean_body | 188 | 258 | 20.5 | 19.2 | 18.4 |
| set_parameter | 50 | 53 | — | 16.7 | 16.0 |
| shell_body | 41 | 42 | 171.8 | 10.8 | 11.3 |
| delete_faces | 54 | 59 | 263.3 | 9.5 | 10.3 |
| split_body | 94 | 119 | 9.0 | 7.2 | 6.7 |
| chamfer | 142 | 185 | 12.1 | 6.4 | 6.2 |
| loft | 26 | 26 | 39.4 | 5.4 | 4.9 |
| offset_faces | 75 | 89 | 196.7 | 4.9 | 4.5 |
| extrude_face | 58 | 59 | — | 4.8 | 4.5 |
| project_to_sketch | 56 | 63 | 3.2 | 3.0 | 3.0 |
| revolve | 27 | 27 | 0.6 | 0.5 | 0.6 |
| import_step | 27 | 27 | 0.3 | 0.3 | 0.3 |
| mirror_body | 122 | 153 | 0.5 | 0.2 | 0.1 |
| project_outline | 43 | 46 | 26.8 | 0.1 | 0.1 |
| move_body | 128 | 164 | 0.5 | 0.1 | 0.1 |
| sketch_create / _offset | 286 / 233 | 651 / 537 | 1.2 / 1.1 | 0.1 / 0.0 | 0.1 / 0.1 |
| add_circle / add_rectangle | 255 / 266 | 672 / 549 | 1.1 / 0.9 | 0.0 | 0.0 |
| pattern_rectangular / _circular | 55 / 12 | 55 / 12 | 0.1 / 0.0 | 0.1 / 0.0 | 0.1 / 0.0 |
| envelope_regions | 10 | 10 | 1.0 | 0.0 | 0.0 |
| add_ellipse / add_arc / add_line / add_point | 19 / 13 / 43 / 24 | 37 / 13 / 43 / 24 | 0.0〜0.1 | 0.0 | 0.0 |
| mirror_sketch_entities / move_sketch_entities | 20 / 12 | 20 / 12 | 0.0 | 0.0 | 0.0 |
| add_solid_body / add_mesh_body / import_svg | 27 / 12 / 27 | 27 / 12 / 27 | 0.0〜0.2 | 0.0 | 0.0 |
| set_preserve_bodies / set_exclude_bodies / set_fixtures / set_loads / clear_loads | 43 / 5 / 43 / 34 / 14 | 48 / 5 / 48 / 36 / 15 | 0.0〜0.1 | 0.0 | 0.0 |
| rename / delete / delete_items | 48 / 11 / 20 | 52 / 11 / 20 | 0.0〜0.1 | 0.0 | 0.0 |

- 添字の参照は 1,625 個(1本の中央値 5)、連鎖(添字で指した結果をまた添字で指した回数)は 894 回(1本の最小 1・中央値 3)。
- 生成で断られた操作(種類ごとの数。例文は `manifest.json` の `refusal_examples`。`over_budget` は予算を超えた verb をその文書で
  見送った回数): 面の削除 339 + 期限切れ 1 + 見送り 7、面のオフセット 266 + 見送り 20、シェル 184 + 見送り 12、分割 183(平面がボディに接するだけ)、面取り 161、丸め 101、ねじ 65(軸が短い・
  平面を指した)、包む領域 47(分かれる)、押し出し 25(切った結果が不正)、寸法の値の変更 15(作り直しで後の操作が断られて元へ戻した)。
- 生成の時間の多くは、断られる面の削除・オフセット・シェルと、予算(20 秒)で打ち切った試行。再生では打ち切った試行は文書に無いので
  シェルは 10.8 秒、面の削除は 9.5 秒しかかからない。再生の時間はねじ(1回 2.6 秒)・押し出し・丸めが大半。
- 道具そのものの費用(cProfile、20 本・41.4 秒): 指紋 5.7 秒(14%)。うちスケッチの領域 3.3 秒(1,354 回。`region.regions` は覚えた
  結果を返すが、鍵の JSON を毎回組む)、面 1.5 秒、辺 0.4 秒、体積 0.3 秒、頂点 0.07 秒。残りは verb の再生(ねじ 18.7 秒など)。

## 2. 入れた変更

すべて新しいファイルで、製品の経路(`duocad/`)は変えていない。形・面と辺の番号・保存形式・操作ログを変えないと言える根拠は、既存の
ファイルに差分が無いこと(`git diff --stat 4ef67570 HEAD -- duocad tools/replay_fingerprint.py tests/conftest.py docs` は空)。
速さの前後の値は無い(道具を足しただけ)。

| コミット | 何を・なぜ |
|---|---|
| e67ed0f `tools: 再生の指紋を補う合成の文書群の道具を足す(synthetic_corpus)` | `generate` / `steps` / `compare` とテスト 6 件。`replay_fingerprint` が見ない途中の形・同じ長さの辺の入れ替わり・頂点・領域・三角形を、最初に違った操作つきで比べられるようにする |
| af7fb45 `tools(synthetic_corpus): 続きの操作で連鎖ができなかった文書にも必ず連鎖を入れる` | 300 本中 2 本(面1枚のトーラスだけが残った文書など)に連鎖が無かった。連鎖が無いときだけ最後に足すので、他の文書は同じバイト列のまま(3回目の生成で確かめた) |
| b3a64c3 `tools(synthetic_corpus): 最大常駐量は自分のプロセスだけを出す` | `RUSAGE_CHILDREN` は終わった子しか数えず、常駐の子の値が出ない |
| (この報告) | 親は取り込まない |

この道具が読む `duocad` の内部(ほかの枝が名前を変えると、この道具がその枝で動かなくなる。動かなければ `DUOCAD_REPO` 越しの比較が止まる
ので気づける): `brep_topology._face_map` / `_edge_map` / `face_count`、`brep_geometry._face_props` / `_face_normal` /
`classify_face` / `classify_edge` / `edge_length`、`kernel.vertex_positions` / `bounding_box` / `make_compound` / `translate_shape` /
`write_step`、`query.describe_edges`、`region.regions` / `plane_frame`、`kernel_worker.preview_context`、`parameters.set_parameter`、
`verbs.add_solid.add_solid_body`、`oplog.verb_handler` / `OpLog.from_payload`、`Document._active_body_id` / `optimize`、
`DocumentView.delta`、`document.uuid4` / `sketch.uuid4`(生成の間だけ差し替える)。

## 3. 入れなかった案

| 案 | 入れなかった理由 |
|---|---|
| 指紋を許容差で比べる(丸めの境目をまたぐ微小な差を一致とみなす) | 「同じ並びか」を厳密に見るのが目的で、許容差を決める根拠が無い。違ったら `--detail` の値で人が判断する |
| 各操作の直後に面・辺・頂点の行そのものを毎回持つ | 出力が 3.3 MB → 約 100 MB。要約だけを持ち、違った文書だけ `--detail` で取り直す |
| 生成でプレビュー → 採用 → 確定の順に組む | 積まれるログは確定と同じ。生成が倍かかる。採用の経路の確かめは 5章 B の再現で別にした |
| 生成した文書の `delete_faces` の由来の並びを整える | 文書はアプリが書くものそのままにする(並びの非決定は 4章の所見として親に渡す) |
| シェルなど子プロセスの計算を予算で打ち切らずに待つ | 生成した形で 20 秒を超えるシェルがあり、打ち切らなかった1回は 4 分以上返らなかった。文書に入ると親の再生も同じだけ待つ |
| A の修正(並列の妥当性検査をやめる、または「不正」のときだけ直列で測り直す) | `brep_checks.py` は共通の関数で担当の外。5章の案を見て親が決める |
| B の修正(ブーリアンの前に三角形分割の無い写しを使う / join で体積が減ったら断る) | `kernel_extrude.py`・`brep_operations.py` は担当の外(C4・C5 の範囲) |

## 4. テスト

- `uv run pytest -q -p no:cacheprovider tests/test_synthetic_corpus.py` → 6 passed(約 3 秒)。落ちたときの意味は各テストの docstring:
  同じ種から同じ文書ができること・連鎖があること / 作った文書を `replay_fingerprint.py` が開けること / 立方体の別の(同じ長さの)辺を
  丸めたら `compare` が丸めの操作を指すこと / 三角形だけの違いと形の違いを分けること / `--triangles` の有無で形の指紋が変わらないこと
  と、再生の最後の状態が生成時と一致すること / 1操作ずつの再生が `OpLog.from_payload` と同じ並びで読むこと。
- 全件 `uv run pytest -q -p no:cacheprovider` → 2,762 passed・39 failed・5 skipped(207 秒)。落ちた 39 件はこの環境のもので、
  この枝の変更とは関係しない:
  - 35 件は Node が 22.23(23.6 未満で `.ts` を読めない)・`web/node_modules` が無い: `test_ghost_agreement` 21、`test_ts_evaluators` 10、
    `test_adaptive_resolution` 1、`test_protocol` 1(tsc が無い)、`test_web_scripts` 2。Node は入れていない(`tests/eval_*.mjs` は
    走らせられなかった)。
  - 4 件は Node と関係しない: `test_channel` 3(更新用の枝の git の構成。応答 501 が 409 の期待に合わない)と
    `test_loft::test_a_smooth_loft_that_pinches_between_sections_is_rejected` 1(自己交差の ValueError が出ない)。
  - `test_channel` 3・`test_loft` 1・`test_adaptive_resolution` 1 は、4ef67570 を `git archive` で書き出したフォルダ(この枝のファイルが
    無い)でも同じに落ちた。
- 基準での決定性(2回ずつ別プロセス):
  - `steps` 2回: 300 本・5,742 操作で 0 件違う。再生の最後の状態と生成時の状態: 300 / 300 一致。
  - `steps --triangles` 2回: 0 件違う(三角形も一致)。
  - `steps` と `steps --triangles`: 1 件違う(`corpus-1-0008` の seq 29。5章 B。2回とも同じに違うので、決まって起きる)。
  - `replay_fingerprint.py` 2回: 1 件違う(`corpus-1-0237` が1回目だけ開けなかった。5章 A)。
- 生成の再現(同じ種・別プロセス、3回): 1回目と2回目(2本並べて)は 298 / 300 がバイトまで同じ。3回目(単独、連鎖の修正の後)とは、
  連鎖を足した 2 本(0114・0281)を除いて、1回目と 295 / 298、2回目と 295 / 298 が同じ。違った文書(0118・0129・0190・0293)はどれも `delete_faces` の
  `provenance.face_map` で、1つの面が2つ以上に分かれた先の添字の並びが入れ替わっただけ(引数・`numbering`・最後の状態は同じ。並びを
  そろえると同じ)。由来の `face_map` を読み返す箇所は `duocad/` に無く(書くだけ)、再生のたびに作り直されるので形には効かない。
  300 本の中で後継が2つ以上ある由来はブーリアン 118・押し出し 40・ねじ 9・面の削除 5・丸め 4・シェル 1 で、並びが揺れたのは面の削除
  だけ(`BOPAlgo_RemoveFeatures` の履歴 `Modified()` の並び)。
- 陽性対照(道具が並びの変化を拾えるか): 4ef67570 の写しの `brep_operations.fuse_all` を、順に 1 回ずつ結合する代わりに1回の
  多入力の `BRepAlgoAPI_Fuse` にして、3 領域以上の押し出しを持つ 62 本を `steps` で比べた → 34 本 DIFFER。最初に違ったのは全部その
  押し出しで、33 本が面・辺・頂点、1 本が面・辺の並び(数と体積は同じ)。16 本は後の添字の操作(ねじ・面取り・丸め)が別の面に掛かって
  断られた(例: `seq 13 thread: ねじを刻めるのは全周の円筒面だけ(面 22 は plane)`)。`replay_fingerprint.py` も同じ 34 本を DIFFER に
  した(面の並びが変わったので)。再生の時間は 132 → 195 秒に増えた(多入力のほうが遅い)。

## 5. 親に確かめてほしいこと

基準のコードで見つけた非決定(どちらも担当の外なので直していない。再現の文書は親が `generate --seed 1` で同じ物を作れる)。

**A. 並列の妥当性検査が同じ形を時々「不正」と判定する**

- `brep_checks._analyzer_valid` は `BRepCheck_Analyzer(shape, True, True)`(3つ目の引数で並列)。`corpus-1-0237` の seq 19
  (ねじを刻んだ円錐に面取り 0.5、辺 [35, 37])の結果の形を、同じプロセスで 200 回ずつ判定した: 並列は 8・10・11 回「不正」
  (3回の試行)、直列は 0 回。再生では面取りが断られて文書が開けなくなる(6回の再生のうち1回)。生成でも同じ理由で確定が断られうる。
- 同じ形で並列の判定と面取りを続けて回したプロセスが `malloc(): unaligned tcache chunk detected` で落ちたことが1回あった(どちらの
  呼び出しで壊れたかは切り分けていない)。並列の検査がメモリを壊している疑いがある。
- 案: 直列に戻す(733 面のボディで 77 → 153 ms)か、並列で「不正」が出たときだけ直列で測り直す(正しい形の速さは保つが、並列が
  誤って「正しい」と言う場合は防げない。その場合が起きるかは確かめていない)。本人の実データでは、QR1 の STEP と 206 文書の群を同じ
  プロセスで何回か開き直して、断りが揺れるかを見てほしい。

**B. 画面の三角形分割が載ったボディへの join で、ボディが消える**

- `corpus-1-0008` の seq 29(面に貼ったスケッチの 2.9 mm 角を join で 1 mm、相手は 20 面・1,717.9 mm³ のボディ)。開いた直後に
  そのまま確定すると 22 面・1,726.3 mm³。開いて `Session.state()`(接続直後の配信と同じ三角形分割)の後に確定すると、
  **6 面・8.41 mm³(足した柱だけ)** になる。`BRepAlgoAPI_Fuse(相手, 柱)` 自体が柱だけを返し、BRepCheck も通る。
  `_checked_volume` は無変化しか見ないので、体積が 1,717.9 → 8.41 に減った join を断らない。
- 経路ごと: MCP の確定(プレビュー無し)→ 8.41。画面の順(プレビューは三角形分割の無い写しで計算 → 採用 → 確定)→ 1,726.3。
  どちらもファイルを開き直すと 1,726.3。MCP で作業すると、画面に見える形とファイルが食い違う。
- 300 本・5,742 操作のうち `--triangles` の再生で形が変わったのはこの1件だけ。ほかの枝がプレビューや確定の計算を写しから元の形へ
  移すと(三角形の使い回し・プレビューの結果の採用)、画面の順でも起きうるので、C3・C4・C5 の取り込みでは `steps --triangles` の
  前後を必ず比べてほしい。
- 案: (1) ブーリアンに渡す形を三角形分割を持たない写しにする(`brep_operations.copy_shape` は三角形分割を写さない)、
  (2) join で体積が減った・cut で体積が増えた結果を断る。本人の実データで、MCP の押し出しの後にボディが小さくなった例が無いか見てほしい。

**そのほか**

- 合成の文書群は本人の部品の代わりではない(穴の多い板も 12×8 まで)。QR1 の STEP と 206 文書の群での `replay_fingerprint.py` の
  前後の比較は、引き続き親の手元で必要。この道具の `steps` は保存済みの文書にもそのまま使える(`steps --out x.json <文書のフォルダ>`)
  ので、206 文書の群でも1操作ずつ比べられる。
- 生成の再現は、時間の上限に近い操作(この機械で 3 秒以上かかった操作が 65 件、うち断られたか予算で打ち切ったもの 46 件)があるので、
  機械が違うと別の文書になりうる。前後の比較には同じフォルダを使ってほしい。
- `delete_faces` の由来の並び(4章)は形に効かないが、自動保存のファイルが同じ操作でもバイトで変わる。気になるなら後継の列を
  並べ替えて書く(担当の外)。
- Windows の `.venv` で `generate` と `steps` を走らせられるか(この機械は Linux。STEP 書き出しの標準出力を止めるのに `os.dup2` を使う)。

## 再現のコマンドと生の値

```bash
uv run python -X utf8 tools/synthetic_corpus.py generate --out <群> --seed 1 --count 300
```

```bash
uv run python -X utf8 tools/synthetic_corpus.py steps --out s1.json <群>/documents
```

```bash
uv run python -X utf8 tools/synthetic_corpus.py steps --triangles --out t1.json <群>/documents
```

```bash
uv run python -X utf8 tools/synthetic_corpus.py compare s1.json t1.json
```

生成の内訳(3回目、単独)の抜粋:

```json
{"seed": 1, "count": 300, "seconds": 1139.2, "peak_memory_mb": 1039.8,
 "operations": 5742, "index_refs": 1625, "chained": 894, "chained_min": 1,
 "generate_ms": {"median": 667, "p90": 7749, "max": 69515},
 "bases": {"plate": 28, "holey_plate": 28, "regions": 28, "shaft": 27, "revolved": 27, "lofted": 27, "primitive": 27,
           "imported": 27, "svg": 27, "symmetric": 27, "pins": 27},
 "refused": {"boolean_body:ValueError": 7, "chamfer:Refused": 154, "chamfer:ValueError": 7, "delete_faces:Refused": 339,
             "delete_faces:TimedOut": 1, "delete_faces:ValueError": 4, "delete_faces:over_budget": 7,
             "envelope_regions:ValueError": 47, "extrude:ValueError": 25, "fillet:Refused": 100, "fillet:ValueError": 1,
             "loft:ValueError": 2, "loft:over_budget": 1, "offset_faces:Refused": 266, "offset_faces:ValueError": 18,
             "offset_faces:over_budget": 20, "project_outline:ValueError": 1, "set_loads:RoleConflict": 12,
             "set_parameter:Refused": 3, "set_parameter:ValueError": 12, "set_preserve_bodies:ValueError": 2,
             "shell_body:Refused": 184, "shell_body:ValueError": 8, "shell_body:over_budget": 12,
             "split_body:ValueError": 183, "thread:ValueError": 65},
 "slow_ops_over_3s": {"delete_faces": 22, "thread": 13, "offset_faces": 12, "shell_body": 9, "fillet": 5,
                      "project_outline": 2, "loft": 1, "extrude": 1}}
```

比較の結果(基準 4ef67570、同じ文書群):

```text
steps s1 vs s2          : 300 documents (5742 steps): 0 differ in shape/indices, 0 differ only in triangles
steps t1 vs t2          : 300 documents (5742 steps): 0 differ in shape/indices, 0 differ only in triangles
steps s1 vs t1          : 1 differ — corpus-1-0008.json: seq 29 extrude | counts [22, 55, 38]->[6, 12, 8] volume 1726.3->8.41
replay_fingerprint f1/f2: 1 differ — corpus-1-0237.json error: Refused: chamfer produced an invalid shape (BRepCheck, edges [35, 37]) -> None
generated vs replayed   : 300 / 300 match (s1, s2)
control (multi-input fuse_all), 62 docs: 34 differ in shape/indices (replay_fingerprint: 34 differ)
```
