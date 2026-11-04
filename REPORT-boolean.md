# REPORT-boolean — ブーリアンと分割(枝 `cloud/perf-boolean`)

- 基準: develop `4ef67570`。変更は3コミット(採否を1つずつ決められる):
  - `881fad3` 統合の本番が試行と同じ中身なら BRepCheck を繰り返さない(`brep_operations.unify_same_domain`)
  - `121063f` 同じとき、試行で積分した体積を引き継ぐ(同)
  - `b0b79f9` ブーリアンを並列で解く(W3。`brep_operations.build_boolean`)。**入れるかは親が決める**ので別のコミット
- 機械: クラウドのコンテナ(論理 CPU 4、メモリ 15 GB、Linux)。Python 3.14.6(uv)、OCP 7.9.3。前後は同じ機械で交互に測った。
- 変えたファイル: `duocad/brep_operations.py` と、足したテスト `tests/test_unify_same_domain.py`・`tests/test_parallel_boolean.py` だけ。
  `verbs/boolean_body.py`・`verbs/split_body.py` は変えていない(下の 1. のとおり、そこに削れる仕事が無かった)。

要点:
- **ブーリアン(引く)と分割のゴースト・確定後の配信は、ほぼ全部が結果の面分割**(穴 384 の板で、ブーリアンのゴースト 3.6 秒のうち
  2.8 秒、分割のゴースト 6.0 秒のうち 5.6 秒)。この枝のファイルには無い(C1・C3)。OCCT のブーリアン自体は 0.13〜0.46 秒。
- 統合(和・押し出しの結合・フィレットの吸収で掛かる)は BRepCheck を2回走らせていた。1回にして、押し出しの結合の MCP の確定が
  **2.10 → 1.40 秒**(領域・ボディを貫く)、**0.96 → 0.61 秒**(面)。ゴーストも 1.2 秒・0.2〜0.5 秒縮む。
- 並列のブーリアンは、テストの 1,391 回と合成の 960 組で**直列とバイト列まで同じ**。**ねじは 4.06 → 1.85 秒**(合成の小さい部品)。
  穴の多い板の切り取り・分割・押し出しには効かない(OCCT の時間の6割が並列にならない立体の組み立て)。
- 版の間で、結果の形(BinTools の SHA-1)・面と辺の数・体積・三角形の数・由来は全部同じ(8 操作 × 2 部品)。

---

## 1. 測った内訳(前)

### 1-1. ブーリアン(引く)。`--part big`(穴 384・485 面の板を、横に貫く棒で切る。トンネルが穴 約 160 個と交わる)

ゴースト 3.66 秒の cProfile(`harness.py --only "boolean_body cut" --profile preview`):

| どこ | 秒 | 持ち主 |
|---|---|---|
| 結果の面分割 `kernel.tessellate_reusing` | 2.83 | C1・C3 |
| ├ OCCT の面分割 `IMeshTools.DiscretizeFaces`(新しい面 344 枚。トンネルの上下の面が穴 約 160 個の平面) | 2.05 | C1 |
| ├ 三角形の取り出し `_face_triangles` / 面のモデル `_Model.__init__` / 境界の検査 | 0.28 / 0.23 / 0.07 | C1 |
| ブーリアン `kernel._boolean_bodies` | 0.83 | — |
| ├ OCCT `BRepAlgoAPI.Build` | 0.455 | この枝(`build_boolean`) |
| ├ 妥当性 `brep_checks._require_valid`(並列の BRepCheck) | 0.29 | `kernel.py`(範囲外) |
| ├ 体積 `_checked_volume` | 0.063 | `kernel.py`(範囲外) |
| └ 由来(`target_successors`・面の添字表) | 約 0.01 | — |
| 文書の写し `speculative.fork` | 0.034 | C3 |

- MCP の確定 0.83〜0.86 秒はブーリアンそのもの(上の 0.83 秒)。その後の配信 2.6〜2.9 秒は、トンネルの面の面分割(上と同じ)。
- **OCCT の `Build` 0.4〜0.5 秒の中身**(gdb で 40 回止めて数えた): 立体の組み立て `BRepAlgoAPI_BuilderAlgo::BuildResult` が 25 回
  (うち `BOPAlgo_BuilderSolid::PerformAreas` 11 回)、交差 `IntersectShapes`(並列にできる段)が 14 回。並列にしても速くならない
  理由(1-4)。

### 1-2. 分割(YZ 平面)。`--part big`

ゴースト 5.97 秒の cProfile:

| どこ | 秒 |
|---|---|
| 2つの塊の面分割(`DiscretizeFaces` が 4.60。上面・底面が半分ずつに割れ、穴 約 190 個の平面が4枚新しくなる) | 5.57 |
| ├ 新しいボディの塊(何も借りない `kernel.tessellate`) | 2.98 |
| └ 元のボディを引き継ぐ塊(残った面を借りる `tessellate_reusing`) | 2.59 |
| 分割 `kernel_split._split_by_plane` | 0.41 |
| ├ OCCT `BRepAlgoAPI_Splitter.Build` | 0.126 |
| ├ 塊ごとの BRepCheck(2回) | 0.217 |
| └ 体積・重心 | 0.05 |

`verbs/split_body.py` は `kernel_split.split_by_plane`(結果を覚える `kernel.remembered`)を呼ぶだけで、プレビューと確定は同じ結果を
使う。この枝のファイルに削れる所は無かった(3. の案 C・D)。

### 1-3. 統合(`brep_operations.unify_same_domain`。和・押し出しの結合/new・フィレットの吸収で掛かる)

押し出し(面・結合 0.5)の MCP の確定 0.92 秒(`--part big`)の cProfile:

| どこ | 秒 |
|---|---|
| `unify_same_domain` | 0.714 |
| ├ BRepCheck 2回(試行の結果と、本番の結果) | 0.568(1回 0.28) |
| ├ `ShapeUpgrade_UnifySameDomain.Build` 2回(試行・本番) | 0.086 |
| ├ 体積の積分(試行の結果) | 0.045 |
| └ 写し `BRepBuilderAPI_Copy` ほか | 0.016 |
| OCCT のブーリアン(和) | 0.067 |
| 結果の体積の積分(呼び手。統合した形) | 0.045 |
| 自動保存ほか | 0.03 |

本番の結果の BRepCheck は「試行では通ったのに本番で壊れた」を捕まえる関門で、統合は同じ入力に同じ処理なので、本番が試行と同じ
中身なら答えは決まっている。これを削った(2. の `881fad3`)。

### 1-4. 和(カーネルだけ。板と棒、統合は面が減らず採らない)

`kernel._boolean_bodies(板, [棒], "union")` 1.05 秒: `Build` 0.42、BRepCheck 0.42、体積 2 回 0.11、統合の試行 0.07(面が減らないので
写しと統合の `Build` だけ)。

### 1-5. 並列(W3)の効き方

- 穴 384 の板の切り取り: `SetRunParallel(True)` で CPU 時間は 2 倍(532 → 1,055 ms)になるが、経過時間は変わらない(545 → 495〜541 ms)。
  上の gdb の数え方どおり、並列にできる交差の段が全体の 3〜4 割しかない。
- 曲面の多い交差(ねじ・フィレットのはみ出しの吸収): テストの中の呼び出しで、直列 145 秒 → 並列 91 秒(テスト 4 本を同時に走らせた
  状態なので、絶対値は目安)。`test_fillet_overflow.py` の分が 84.9 → 49.0 秒、`test_thread.py` の分が 46.3 → 29.7 秒。

---

## 2. 入れた変更

前後の値は `harness.py`(`tools/bench_all_ops.py` の `build_part`・`run_case` をそのまま使い、カメラの段と丸めの候補探しだけ省いたもの。
付録)で、版を交互に3回ずつ。**冷えた回** = 新しいプロセスで最初の1回(部品を作る途中で OCCT は温まっている)、**温まった回** =
同じプロセスの2回目(`kernel._RESULTS` を空にして、同じ形の結果の使い回しには当てない)。中央値、ms。生の値は付録 A。

### 2-1. `881fad3` 統合の本番が試行と同じ中身なら BRepCheck を繰り返さない

- 何を: `unify_same_domain` の最後で、本番の結果と試行の結果を **写し同士の BinTools のバイト列**(三角形なし)で比べ
  (`_same_content`)、同じなら試行の BRepCheck の答えを使う。違えば今までどおり本番を BRepCheck して、壊れていれば同じ例外で断る。
- なぜ写し同士か: 生のバイト列は試行と本番で**必ず違う**(全件のテストで統合を採った 71 回すべて)。ブーリアンの結果は辺どうしで
  pcurve を共有していて、`BRepBuilderAPI_Copy` がそれを分けるので、幾何の表の並びと pcurve の番号がずれる(値は同じ)。写し同士なら
  71 回すべて同じだった。比較の費用は 485 面で 0.05 秒(BRepCheck は 0.28 秒)。
- 効果(同じ cProfile、`881fad3` だけの版): `unify_same_domain` 0.714 → 0.519 秒、BRepCheck 2 回 → 1 回、比較 `_same_content` 0.055 秒。
- 形を変えないと言える根拠:
  - 本番の統合はそのまま(結果の形・履歴・由来は元と同じ物)。省くのは答えが決まっている検査だけ。
  - 比べて同じだった回で、省いた検査を実際に走らせると全部妥当: 全件のテスト 71/71、合成の形 69/69。
  - 合成の形の 15 回(5 形 × 3 回)は、写し同士でも違った。違いは**辺の連続性の記録(BRep の `CN` 行)だけ**で、本番が持ち、試行が
    持たない。この回は今までどおり BRepCheck に回り(全部妥当)、比較の分 4〜13 ms が余分にかかる。角を丸めた板で起きたので、
    丸めの多い本人の部品では検査に回る割合が上がりうる(5. の確認 2)。
  - 版の間の指紋(BinTools の SHA-1・面/辺の数・体積・三角形の数・由来)が全部同じ(付録 B)。

### 2-2. `121063f` 同じとき、試行で積分した体積を引き継ぐ

- 何を: 2-1 で同じと分かったときだけ、試行の体積を `brep_geometry.note_volume` で統合した形に覚えさせる。呼び手(ブーリアン・押し出し)が
  結果の体積を求めるときに積分し直さない。
- 効果: 積分 1 回。押し出し(面・結合 0.5、`--part big`)の MCP の確定の cProfile で、`881fad3` だけの版は体積の積分 3 回 0.167 秒、
  `121063f` を足した版は 2 回 0.102 秒(1 回 0.05〜0.065 秒が消える。経過時間の揺れより小さいので、時間の表では 2-1 と分けていない)。
- 根拠: 引き継いだ値は、積分し直した値とビット単位で一致(全件のテスト 71/71、合成の形 69/69。違う中身の回は引き継がない)。
  テスト `test_an_adopted_unification_hands_over_the_volume_it_integrated`。

### 2-1・2-2 の前後(`--part big`。base → 2つのコミット)

| 操作 | 段 | 冷えた回 | 温まった回 |
|---|---|---|---|
| 押し出し 領域 join(ボディを貫く) | MCP の確定 | 2,103 → **1,395** | 2,358 → **1,342** |
| 〃 | ゴースト | 9,973 → **8,808** | 10,550 → **9,329** |
| 押し出し 面 join 0.5 | MCP の確定 | 964 → **611** | 984 → **671** |
| 〃 | ゴースト | 6,947 → 6,767 | 7,119 → **6,571** |
| ブーリアン 引く(統合しない) | MCP の確定 / ゴースト | 863 → 928 / 3,611 → 3,636 | 889 → 944 / 3,765 → 3,711 |
| 分割(統合しない) | MCP の確定 / ゴースト | 523 → 497 / 6,401 → 6,648 | 543 → 564 / 6,596 → 6,299 |
| ブーリアン 和(側面に接する箱。面が減らず統合しない) | MCP の確定 | 518 → 519 | 559 → 566 |

- 統合を採らない操作(引く・分割・面の減らない和)は揺れの範囲(±1 割)。
- 確定後の配信・Enter・undo/redo は変わらない(面分割と配りものは触っていない)。
- `--part small`(59 面): 押し出し 領域 join の MCP の確定 85 → 66(冷えた回)、79 → 65(温まった回)。他は揺れの範囲。
- 公式の物差し(`tools/bench_all_ops.py --part big --only "extrude 面 join 0.5"`、1 回ずつ。base → `b0b79f9`): MCP の確定 922 → 572、
  ゴースト 6,992 → 6,593(付録 C)。

### 2-3. `b0b79f9` ブーリアンを並列で解く(W3。採否は親)

- 何を: `build_boolean` に `SetRunParallel(True)`。押し出し・ねじ・フィレットのはみ出しの吸収(`kernel_overflow`・`kernel_tight`)・
  ブーリアンの verb・`fuse_all` が通る。分割(`kernel_split` の `BRepAlgoAPI_Splitter`)・面の編集・干渉は通らない(範囲外。干渉と
  面の編集は既に並列)。
- 突き合わせ(結果の BinTools のバイト列・面と辺の `Modified`/`Generated`/`IsDeleted` の添字・成否):
  - **全件のテストの中で呼ばれた `build_boolean` 1,391 回**(287 テスト。切る 569・共通 418・足す 404。子プロセスの呼び出しも含む):
    入力の写しの上で直列1回・並列2回解き直して、**全部一致**、並列の2回も一致。
  - **合成の 960 組**(穴 0〜250 個を乱数で開けた板。角を丸めたもの・ポケット付きを含む。道具は箱・傾けた箱・円柱・横の円柱・球・
    円錐・トーラス・角を丸めた箱・トンネルを 1〜3 個。和・引く・共通。`kernel._boolean_bodies` の経路で統合まで):
    成否・例外の文・バイト列・由来・統合したか・体積が**全部一致**。OCCT のスレッド数 4 で 480 組、2 と 16 で 240 組ずつ
    (スレッドの数で仕事の分け方が変わるので、この機械の 4 より少ない 2 と多い 16 も見た)。
  - 一致しなかった組: **無し**。
- 前後(合成の部品。base の統合の2コミットの上で → +並列):

| 操作 | 段 | 冷えた回 | 温まった回 |
|---|---|---|---|
| ねじ(穴に刻む trapezoid30)`--part small` | MCP の確定 | 4,244 → **1,849** | 3,975 → **1,986** |
| 〃 | ゴースト | 4,234 → **2,190** | 4,494 → **2,137** |
| ブーリアン 引く `--part big` | MCP の確定 / ゴースト | 928 → 926 / 3,636 → 3,493 | 944 → 952 / 3,711 → 3,655 |
| 押し出し 領域 join `--part big` | MCP の確定 | 1,395 → 1,357 | 1,342 → 1,316 |
| 分割 `--part big` | MCP の確定 | 497 → 505 | 564 → 495 |

- 公式の物差し(`--part small --only thread`、1 回ずつ。base → `b0b79f9`): ゴースト 4,128 → 2,085、MCP の確定 4,297 → 2,024、
  redo 4,101 → 1,815(付録 C)。
- 穴の多い板では揺れの範囲(遅くもならない)。
- メモリ: 公式の物差しの最大常駐メモリが、ねじ(`--part small`)で 632 → 778 MB(1 回ずつ)。穴の多い板(`--part big`)では
  5.5〜5.75 GB で変わらない(カメラの段の三角形が大半)。
- 判断の材料: decisions.md の W3 の保留は、得が 30〜50 ms で既存ログの面の並びを壊す危険に見合わない、という判断だった。並びは
  上の範囲で壊れず、ねじでは得が秒の単位(上の表)、フィレットのはみ出しの吸収もテストの中の呼び出しで 4 割ほど縮む(1-5。
  テストを同時に走らせた状態の値)。テスト `tests/test_parallel_boolean.py`(穴あき板・丸めた箱 × 切る・足す・
  共通の 9 通りで、並列と直列のバイト列と面の由来を比べる)は、OCCT を上げたときに並びが変わったら落ちる見張り。

---

## 3. 入れなかった案

| 案 | 測った値 | 入れなかった理由 |
|---|---|---|
| A. 本番と試行を**生の**バイト列で比べる | 全件のテストの 71 回すべて違う(pcurve の共有) | 効かない。写し同士で比べる形にした(2-1) |
| B. 試行の結果をそのまま最終の形にして、本番の統合をやめる(写しの履歴で由来を引く) | 統合の `Build` 1 回(485 面で 0.04 秒)と比較が消える | 残った面の TShape が入力と別物になり、面の同一性で三角形を借りる仕組み(`view._borrow_faces`)が全部外れる。配信が丸ごとの面分割になる |
| C. 分割で、新しいボディの塊にも元のボディの三角形を貸す(プレビュー) | 1-2 の内訳で、借りない塊 2.98 秒・借りる塊 2.59 秒(左右ほぼ対称) → 約 0.4 秒 | `session.preview_job` は同じ id のボディにしか貸さない。新しいボディ(分割の片側)にも、元のボディの写しとの `kept_pairs` を引けば借りられる。C3 の持ち物なので案だけ |
| D. 分割・引く・共通の後の BRepCheck を減らす | 引く 0.29 秒・分割 0.22 秒(485 面) | `kernel.py`・`kernel_split.py` は範囲外。壊れた形を通さない最後の関門で、面の一部だけ検査するのは検査の意味が変わる |
| E. `SetUseOBB(True)`・`SetCheckInverted(False)`・`SetGlue` | 測っていない | 結果の並びを変えうる設定で、並列のような突き合わせをしていない。decisions.md でも `SetUseOBB` はねじで 8% |
| F. 複数の道具の `fuse_all` を1回の多入力ブーリアンに | — | decisions.md のとおり面の並びが変わりうる(順に足す結果と同じかを示せていない) |
| G. 統合で「面が減らない」を、統合する前に安く見分ける | 減らない統合が 257 回中 186 回。費用は写しと統合で 485 面 0.07 秒 | 統合してみないと分からない。安い予測は外れたときに統合しない(形が変わる)ので入れない |

---

## 4. テスト

| コマンド | 結果 |
|---|---|
| `uv run pytest -q -p no:cacheprovider`(基準 4ef67570、Node 22.23) | 2,756 passed / 39 failed / 5 skipped。失敗のうち 34 は Node が古い(.ts を読めない・`web/node_modules` が無い) |
| 同(基準、Node 24.22 を入れて `npm ci --prefix web` の後) | **2,790 passed / 5 failed** / 5 skipped |
| 同(最後の `b0b79f9`、Node 24.22) | **2,804 passed / 5 failed** / 5 skipped(足したテスト 14 件が通る) |
| 足したテストを基準のコードで | `test_unify_same_domain.py` の 3 件が落ちる(検査が2回・`_same_content` が無い)ことを確かめた |
| 関係する verb のテスト(押し出し・ブーリアン・分割・ねじ・フィレット・面取り・面の編集・統合ほか 21〜23 ファイル)をコミットごとに | 536 / 540 / 549 passed |

基準でも最後でも落ちる 5 件(この枝の変更と関係ない。環境):
- `test_loft.py::test_a_smooth_loft_that_pinches_between_sections_is_rejected`(`DID NOT RAISE ValueError`)
- `test_channel.py` の 3 件(`501 == 409`。git の更新の経路)
- `test_web_scripts.py::test_start_server_stops_the_python_server_when_the_start_fails`(`.venv/Scripts/python.exe` が無い。Windows の前提)

Node の評価器(`tests/eval_*.mjs`)は Node 24.22.2(スクラッチに入れた)で走らせた。

---

## 5. 親に確かめてほしいこと

1. **再生の指紋**(`tools/replay_fingerprint.py`。保存済み 206 文書)を、`881fad3`+`121063f` と、`b0b79f9` を足した版で。
   統合の2コミットは形を変えない作りだが、並列は「合成とテストで同じ」でしかない。本人の文書の押し出し・ねじ・フィレットの吸収の
   ブーリアンが直列と同じかは、文書群でしか分からない。
2. **QR1 で、統合を採った回のうち `_same_content` が同じになる割合**。丸めのある部品で辺の連続性の記録だけが違う回(2-1)が多いと、
   比較の分(485 面で 0.04〜0.055 秒)が余分になり、効果が小さい。`brep_operations._same_content` に数えを仕込めば分かる。
3. **QR1 の押し出し(結合)とブーリアン(和)の確定・ゴースト**の前後。この機械では穴の多い合成の部品でしか測っていない。
   引くブーリアン(QR1 のゴースト 0.94 秒)は統合しないので、2-1・2-2 では変わらない見込み。
4. **並列の速さを本人の機械で**(論理 CPU が多い)。ねじ・フィレットのはみ出しの吸収・引くブーリアンのゴースト。プレビューの
   ワーカーとループ側の面分割が同時に OCCT の並列を使うときの取り合いもこの機械では見ていない。
5. **並列のメモリ**: ねじで最大常駐メモリが 632 → 778 MB(この機械、1 回ずつ)。OCCT のスレッドごとの作業領域と見ているが、
   確かめていない。論理 CPU の多い本人の機械では増え方が違いうる。

---

## 付録 A. 前後の生の値(`harness.py`。版を交互に 3 回)

base = 4ef67570、統合 = 121063f(2-1・2-2)、+並列 = b0b79f9。`confirm`・`push` は Enter と確定後の配信。
配信の大きさ(バイト)は同じ版の中でも 4 バイト刻みで揺れる(フレームの中の番号の桁)ので、形の一致は付録 B の指紋で見る。

### big  (中央値 ms、3 回。左から base → 統合の2コミット → +並列)

#### 冷えた回(プロセスで最初)

| 操作 | 段 | base | 統合 | +並列 | 生の値 base / 統合 / +並列 |
|---|---|---|---|---|---|
| boolean_body cut | preview | 3611 | 3636 | 3493 | 3611,3766,3442 / 3352,3636,3810 / 3283,3589,3493 |
| boolean_body cut | confirm | 22 | 21 | 21 | 22,40,20 / 21,31,20 / 23,20,21 |
| boolean_body cut | push | 34 | 33 | 36 | 34,48,30 / 33,36,32 / 32,36,43 |
| boolean_body cut | mcp | 863 | 928 | 926 | 863,850,891 / 890,962,928 / 881,926,953 |
| boolean_body cut | mcp_push | 2640 | 2635 | 2626 | 2698,2640,2629 / 2538,2822,2635 / 2524,2626,2650 |
| boolean_body cut | undo | 5 | 5 | 5 | 5,5,5 / 5,5,5 / 5,5,6 |
| boolean_body cut | redo | 6 | 5 | 6 | 6,6,5 / 5,5,7 / 6,5,7 |
| boolean_body union 側面に接する箱 | preview | 485 | 483 | 522 | 561,485,481 / 439,483,521 / 503,544,522 |
| boolean_body union 側面に接する箱 | confirm | 25 | 22 | 21 | 33,23,25 / 20,25,22 / 20,21,21 |
| boolean_body union 側面に接する箱 | push | 46 | 35 | 33 | 46,38,52 / 31,46,35 / 30,33,33 |
| boolean_body union 側面に接する箱 | mcp | 518 | 519 | 562 | 515,610,518 / 519,536,513 / 480,574,562 |
| boolean_body union 側面に接する箱 | mcp_push | 57 | 61 | 54 | 59,51,57 / 54,75,61 / 57,54,53 |
| boolean_body union 側面に接する箱 | undo | 5 | 6 | 5 | 5,5,6 / 5,6,6 / 6,5,5 |
| boolean_body union 側面に接する箱 | redo | 6 | 6 | 6 | 6,5,6 / 5,6,6 / 6,5,6 |
| extrude 面 join 0.5 | preview | 6947 | 6767 | 6286 | 6984,6947,6694 / 6305,6887,6767 / 6286,6278,6608 |
| extrude 面 join 0.5 | confirm | 24 | 23 | 28 | 27,21,24 / 23,24,20 / 28,23,29 |
| extrude 面 join 0.5 | push | 28 | 28 | 27 | 25,29,28 / 32,28,27 / 27,27,34 |
| extrude 面 join 0.5 | mcp | 964 | 611 | 573 | 964,996,923 / 611,647,592 / 542,573,634 |
| extrude 面 join 0.5 | mcp_push | 6055 | 5945 | 5731 | 7099,5855,6055 / 5592,6150,5945 / 5698,5731,6045 |
| extrude 面 join 0.5 | undo | 5 | 5 | 6 | 5,5,5 / 5,5,6 / 5,8,6 |
| extrude 面 join 0.5 | redo | 5 | 7 | 5 | 5,5,6 / 7,6,7 / 5,7,5 |
| extrude 領域 join(ボディを貫く) | preview | 9973 | 8808 | 8875 | 9973,9847,9999 / 8808,9879,8793 / 8664,8875,9079 |
| extrude 領域 join(ボディを貫く) | confirm | 22 | 22 | 22 | 22,23,20 / 21,22,22 / 24,21,22 |
| extrude 領域 join(ボディを貫く) | push | 28 | 27 | 28 | 28,29,24 / 24,28,27 / 28,26,30 |
| extrude 領域 join(ボディを貫く) | mcp | 2103 | 1395 | 1357 | 2581,2103,2026 / 1274,1395,1542 / 1182,1357,1497 |
| extrude 領域 join(ボディを貫く) | mcp_push | 7741 | 8169 | 7774 | 7741,7550,7749 / 7555,8416,8169 / 7293,8290,7774 |
| extrude 領域 join(ボディを貫く) | undo | 5 | 7 | 5 | 5,5,6 / 6,8,7 / 6,5,5 |
| extrude 領域 join(ボディを貫く) | redo | 6 | 7 | 6 | 5,8,6 / 7,7,6 / 6,6,7 |
| split_body YZ | preview | 6401 | 6648 | 5889 | 6669,6401,6347 / 5846,6753,6648 / 5675,5889,6335 |
| split_body YZ | confirm | 23 | 21 | 20 | 20,23,24 / 18,26,21 / 48,18,20 |
| split_body YZ | push | 44 | 46 | 48 | 42,51,44 / 37,46,47 / 41,58,48 |
| split_body YZ | mcp | 523 | 497 | 505 | 476,523,529 / 491,543,497 / 476,505,525 |
| split_body YZ | mcp_push | 5862 | 5804 | 5436 | 5862,6037,5453 / 5094,6183,5804 / 5294,5571,5436 |
| split_body YZ | undo | 5 | 6 | 6 | 5,5,7 / 5,6,6 / 6,5,7 |
| split_body YZ | redo | 6 | 6 | 5 | 6,8,5 / 5,6,6 / 7,5,5 |

#### 温まった回(同じプロセスの2回目)

| 操作 | 段 | base | 統合 | +並列 | 生の値 base / 統合 / +並列 |
|---|---|---|---|---|---|
| boolean_body cut | preview | 3765 | 3711 | 3655 | 3801,3765,3488 / 3408,3816,3711 / 3359,3655,4462 |
| boolean_body cut | confirm | 23 | 24 | 23 | 27,23,21 / 24,41,24 / 20,23,27 |
| boolean_body cut | push | 35 | 36 | 34 | 38,35,33 / 36,52,34 / 30,34,36 |
| boolean_body cut | mcp | 889 | 944 | 952 | 853,889,972 / 923,944,964 / 921,952,985 |
| boolean_body cut | mcp_push | 2884 | 2824 | 2905 | 2742,2945,2884 / 2625,3166,2824 / 2859,2905,3267 |
| boolean_body cut | undo | 5 | 5 | 6 | 7,5,5 / 5,6,5 / 6,6,7 |
| boolean_body cut | redo | 6 | 6 | 6 | 5,6,6 / 6,6,6 / 6,6,7 |
| boolean_body union 側面に接する箱 | preview | 551 | 499 | 500 | 551,517,553 / 499,486,536 / 464,500,571 |
| boolean_body union 側面に接する箱 | confirm | 21 | 23 | 20 | 21,27,21 / 22,26,23 / 20,20,26 |
| boolean_body union 側面に接する箱 | push | 36 | 38 | 35 | 36,37,34 / 38,38,35 / 35,30,38 |
| boolean_body union 側面に接する箱 | mcp | 559 | 566 | 518 | 559,585,540 / 568,545,566 / 518,506,544 |
| boolean_body union 側面に接する箱 | mcp_push | 63 | 65 | 60 | 63,64,57 / 69,65,60 / 56,60,63 |
| boolean_body union 側面に接する箱 | undo | 5 | 5 | 5 | 5,6,5 / 8,5,5 / 5,5,6 |
| boolean_body union 側面に接する箱 | redo | 6 | 5 | 6 | 6,6,5 / 5,5,6 / 6,6,6 |
| extrude 面 join 0.5 | preview | 7119 | 6571 | 6654 | 6872,7977,7119 / 6127,6870,6571 / 6380,6654,7814 |
| extrude 面 join 0.5 | confirm | 32 | 28 | 35 | 32,30,32 / 28,30,28 / 26,44,35 |
| extrude 面 join 0.5 | push | 24 | 24 | 23 | 23,26,24 / 22,24,26 / 21,26,23 |
| extrude 面 join 0.5 | mcp | 984 | 671 | 640 | 930,1005,984 / 603,671,687 / 605,640,786 |
| extrude 面 join 0.5 | mcp_push | 6597 | 5933 | 6182 | 5856,6597,6669 / 5654,6197,5933 / 5776,6182,6493 |
| extrude 面 join 0.5 | undo | 5 | 6 | 6 | 5,5,9 / 6,6,5 / 6,12,6 |
| extrude 面 join 0.5 | redo | 6 | 6 | 6 | 5,6,6 / 6,6,5 / 6,6,6 |
| extrude 領域 join(ボディを貫く) | preview | 10550 | 9329 | 9262 | 10406,10798,10550 / 8700,9556,9329 / 9262,9004,9967 |
| extrude 領域 join(ボディを貫く) | confirm | 19 | 21 | 20 | 18,19,24 / 21,24,17 / 20,19,21 |
| extrude 領域 join(ボディを貫く) | push | 30 | 29 | 28 | 30,28,31 / 28,30,29 / 27,28,32 |
| extrude 領域 join(ボディを貫く) | mcp | 2358 | 1342 | 1316 | 2403,2299,2358 / 1324,1342,1460 / 1310,1398,1316 |
| extrude 領域 join(ボディを貫く) | mcp_push | 8314 | 7919 | 7947 | 8509,8314,7954 / 7593,8430,7919 / 7820,7947,8165 |
| extrude 領域 join(ボディを貫く) | undo | 5 | 6 | 8 | 5,6,5 / 6,6,6 / 13,6,8 |
| extrude 領域 join(ボディを貫く) | redo | 6 | 7 | 6 | 6,7,6 / 8,6,7 / 6,7,6 |
| split_body YZ | preview | 6596 | 6299 | 6056 | 6174,7254,6596 / 5793,6899,6299 / 6056,6040,7394 |
| split_body YZ | confirm | 22 | 24 | 23 | 22,22,44 / 35,24,24 / 22,23,27 |
| split_body YZ | push | 40 | 44 | 37 | 34,40,68 / 53,44,35 / 37,36,44 |
| split_body YZ | mcp | 543 | 564 | 495 | 525,550,543 / 558,572,564 / 448,495,526 |
| split_body YZ | mcp_push | 5702 | 5410 | 5429 | 5268,6290,5702 / 5393,6271,5410 / 5142,5429,6246 |
| split_body YZ | undo | 6 | 6 | 6 | 6,6,8 / 6,6,6 / 6,5,6 |
| split_body YZ | redo | 6 | 6 | 6 | 5,6,8 / 6,7,6 / 6,5,6 |

失敗: なし
配信の大きさ(バイト)が版の間で同じ: False
  boolean_body cut mcp_bytes: [2126004, 2126012]
  boolean_body cut preview_bytes: [4608672, 4608676]
  boolean_body cut push_bytes: [2125080]
  boolean_body union 側面に接する箱 mcp_bytes: [48020, 48024]
  boolean_body union 側面に接する箱 preview_bytes: [3257424, 3257428]
  boolean_body union 側面に接する箱 push_bytes: [52324, 52328, 52332, 52336]
  extrude 面 join 0.5 mcp_bytes: [733096, 733100, 733104]
  extrude 面 join 0.5 preview_bytes: [3255908, 3255912]
  extrude 面 join 0.5 push_bytes: [732172]
  extrude 領域 join(ボディを貫く) mcp_bytes: [1527128, 1527132, 1527136]
  extrude 領域 join(ボディを貫く) preview_bytes: [3244108, 3244112]
  extrude 領域 join(ボディを貫く) push_bytes: [1527648, 1527656]
  split_body YZ mcp_bytes: [1527664, 1527668, 1527672]
  split_body YZ preview_bytes: [3257700, 3257704, 3257708]
  split_body YZ push_bytes: [2407172]

### small  (中央値 ms、3 回。左から base → 統合の2コミット → +並列)

#### 冷えた回(プロセスで最初)

| 操作 | 段 | base | 統合 | +並列 | 生の値 base / 統合 / +並列 |
|---|---|---|---|---|---|
| boolean_body cut | preview | 153 | 147 | 172 | 164,153,149 / 147,139,151 / 155,172,226 |
| boolean_body cut | confirm | 3 | 3 | 5 | 3,3,3 / 3,3,3 / 3,5,6 |
| boolean_body cut | push | 3 | 3 | 3 | 3,4,3 / 3,2,3 / 3,3,6 |
| boolean_body cut | mcp | 76 | 71 | 74 | 82,70,76 / 71,80,69 / 68,74,76 |
| boolean_body cut | mcp_push | 74 | 80 | 82 | 77,68,74 / 82,80,80 / 78,82,93 |
| boolean_body cut | undo | 1 | 2 | 2 | 1,1,1 / 2,2,1 / 2,2,2 |
| boolean_body cut | redo | 1 | 1 | 1 | 2,1,1 / 1,1,2 / 1,1,2 |
| boolean_body union 側面に接する箱 | preview | 49 | 46 | 54 | 46,49,59 / 52,46,46 / 50,54,89 |
| boolean_body union 側面に接する箱 | confirm | 3 | 3 | 3 | 3,3,5 / 3,3,3 / 3,3,3 |
| boolean_body union 側面に接する箱 | push | 9 | 8 | 9 | 9,9,12 / 9,8,8 / 17,8,9 |
| boolean_body union 側面に接する箱 | mcp | 43 | 45 | 51 | 40,43,55 / 62,44,45 / 46,51,80 |
| boolean_body union 側面に接する箱 | mcp_push | 13 | 12 | 13 | 13,11,15 / 16,11,12 / 11,13,24 |
| boolean_body union 側面に接する箱 | undo | 1 | 1 | 1 | 1,1,1 / 2,1,1 / 1,1,2 |
| boolean_body union 側面に接する箱 | redo | 1 | 1 | 1 | 10,1,1 / 2,1,1 / 1,1,2 |
| extrude 面 join 0.5 | preview | 154 | 147 | 136 | 148,175,154 / 147,126,167 / 138,133,136 |
| extrude 面 join 0.5 | confirm | 3 | 3 | 5 | 3,3,2 / 3,2,3 / 5,3,5 |
| extrude 面 join 0.5 | push | 2 | 2 | 2 | 2,2,2 / 2,2,2 / 4,2,2 |
| extrude 面 join 0.5 | mcp | 67 | 54 | 56 | 67,90,66 / 67,54,52 / 56,56,61 |
| extrude 面 join 0.5 | mcp_push | 88 | 97 | 74 | 97,88,81 / 83,110,97 / 74,71,90 |
| extrude 面 join 0.5 | undo | 2 | 2 | 1 | 2,2,2 / 2,1,2 / 1,1,2 |
| extrude 面 join 0.5 | redo | 2 | 2 | 2 | 2,1,2 / 2,1,2 / 3,1,2 |
| extrude 領域 join(ボディを貫く) | preview | 175 | 159 | 199 | 175,193,165 / 167,159,159 / 210,144,199 |
| extrude 領域 join(ボディを貫く) | confirm | 5 | 4 | 4 | 4,5,7 / 4,4,4 / 4,4,6 |
| extrude 領域 join(ボディを貫く) | push | 10 | 6 | 6 | 7,10,12 / 7,6,6 / 6,6,8 |
| extrude 領域 join(ボディを貫く) | mcp | 85 | 66 | 69 | 85,81,86 / 70,57,66 / 82,69,66 |
| extrude 領域 join(ボディを貫く) | mcp_push | 113 | 99 | 112 | 163,113,92 / 107,99,98 / 126,112,109 |
| extrude 領域 join(ボディを貫く) | undo | 2 | 1 | 1 | 2,2,2 / 1,1,1 / 2,1,1 |
| extrude 領域 join(ボディを貫く) | redo | 2 | 2 | 2 | 3,2,2 / 2,2,2 / 2,1,2 |
| split_body YZ | preview | 149 | 142 | 155 | 149,144,155 / 137,148,142 / 177,145,155 |
| split_body YZ | confirm | 3 | 3 | 3 | 3,4,3 / 4,3,3 / 3,3,4 |
| split_body YZ | push | 3 | 3 | 3 | 4,3,3 / 3,4,3 / 3,3,3 |
| split_body YZ | mcp | 50 | 48 | 52 | 50,42,65 / 48,44,50 / 52,41,62 |
| split_body YZ | mcp_push | 93 | 92 | 101 | 93,91,112 / 92,86,98 / 103,90,101 |
| split_body YZ | undo | 2 | 2 | 2 | 2,2,1 / 2,2,1 / 2,1,2 |
| split_body YZ | redo | 1 | 1 | 2 | 1,1,2 / 2,1,1 / 2,2,2 |
| thread 穴 trapezoid30 | preview | 4106 | 4234 | 2190 | 4361,3898,4106 / 4661,4002,4234 / 2090,2257,2190 |
| thread 穴 trapezoid30 | confirm | 3 | 3 | 3 | 3,3,3 / 3,3,5 / 3,3,5 |
| thread 穴 trapezoid30 | push | 3 | 3 | 3 | 3,3,3 / 3,3,5 / 3,3,4 |
| thread 穴 trapezoid30 | mcp | 4055 | 4244 | 1849 | 4310,3975,4055 / 4245,4074,4244 / 1997,1836,1849 |
| thread 穴 trapezoid30 | mcp_push | 233 | 247 | 207 | 233,205,235 / 247,244,271 / 223,207,204 |
| thread 穴 trapezoid30 | undo | 2 | 2 | 1 | 2,2,2 / 2,2,2 / 2,1,1 |
| thread 穴 trapezoid30 | redo | 4081 | 4043 | 1967 | 4195,4012,4081 / 4043,3916,4462 / 1967,1981,1714 |

#### 温まった回(同じプロセスの2回目)

| 操作 | 段 | base | 統合 | +並列 | 生の値 base / 統合 / +並列 |
|---|---|---|---|---|---|
| boolean_body cut | preview | 146 | 157 | 151 | 129,146,174 / 157,133,185 / 134,151,152 |
| boolean_body cut | confirm | 3 | 3 | 3 | 3,4,3 / 3,3,3 / 3,3,4 |
| boolean_body cut | push | 3 | 4 | 3 | 3,4,3 / 5,4,3 / 3,6,3 |
| boolean_body cut | mcp | 90 | 79 | 72 | 91,69,90 / 79,107,70 / 70,72,77 |
| boolean_body cut | mcp_push | 90 | 90 | 79 | 90,74,98 / 90,99,90 / 68,79,109 |
| boolean_body cut | undo | 1 | 2 | 2 | 1,1,2 / 2,1,2 / 2,1,2 |
| boolean_body cut | redo | 1 | 1 | 1 | 1,1,2 / 2,1,1 / 2,1,1 |
| boolean_body union 側面に接する箱 | preview | 44 | 48 | 61 | 44,50,44 / 50,48,40 / 47,62,61 |
| boolean_body union 側面に接する箱 | confirm | 3 | 3 | 3 | 3,3,3 / 3,3,3 / 3,3,3 |
| boolean_body union 側面に接する箱 | push | 10 | 9 | 8 | 8,13,10 / 10,9,8 / 8,10,8 |
| boolean_body union 側面に接する箱 | mcp | 45 | 56 | 52 | 43,60,45 / 56,62,46 / 45,52,52 |
| boolean_body union 側面に接する箱 | mcp_push | 12 | 11 | 13 | 12,12,12 / 12,11,11 / 12,13,22 |
| boolean_body union 側面に接する箱 | undo | 1 | 1 | 1 | 1,1,1 / 1,1,1 / 1,1,1 |
| boolean_body union 側面に接する箱 | redo | 1 | 1 | 2 | 1,1,1 / 1,2,1 / 1,2,2 |
| extrude 面 join 0.5 | preview | 154 | 152 | 155 | 154,149,159 / 154,148,152 / 135,178,155 |
| extrude 面 join 0.5 | confirm | 4 | 3 | 4 | 3,4,4 / 4,3,3 / 4,3,4 |
| extrude 面 join 0.5 | push | 2 | 2 | 2 | 2,3,2 / 3,2,2 / 2,2,3 |
| extrude 面 join 0.5 | mcp | 67 | 67 | 58 | 70,66,67 / 67,87,53 / 58,52,59 |
| extrude 面 join 0.5 | mcp_push | 90 | 87 | 86 | 99,84,90 / 75,98,87 / 72,86,101 |
| extrude 面 join 0.5 | undo | 2 | 2 | 2 | 2,1,2 / 1,2,2 / 2,1,2 |
| extrude 面 join 0.5 | redo | 1 | 2 | 2 | 1,1,1 / 4,2,2 / 1,2,2 |
| extrude 領域 join(ボディを貫く) | preview | 181 | 174 | 155 | 189,161,181 / 174,161,182 / 145,200,155 |
| extrude 領域 join(ボディを貫く) | confirm | 3 | 3 | 3 | 3,4,3 / 4,3,3 / 3,3,3 |
| extrude 領域 join(ボディを貫く) | push | 5 | 5 | 4 | 5,7,5 / 6,5,5 / 4,4,7 |
| extrude 領域 join(ボディを貫く) | mcp | 79 | 65 | 71 | 72,79,93 / 83,65,55 / 71,61,73 |
| extrude 領域 join(ボディを貫く) | mcp_push | 100 | 113 | 99 | 111,91,100 / 113,104,125 / 90,99,142 |
| extrude 領域 join(ボディを貫く) | undo | 2 | 2 | 2 | 1,2,2 / 1,2,2 / 2,1,2 |
| extrude 領域 join(ボディを貫く) | redo | 2 | 2 | 2 | 2,2,2 / 2,2,2 / 2,2,4 |
| split_body YZ | preview | 164 | 162 | 157 | 164,145,165 / 181,133,162 / 140,157,157 |
| split_body YZ | confirm | 4 | 3 | 4 | 5,4,4 / 3,3,3 / 4,4,4 |
| split_body YZ | push | 5 | 3 | 4 | 6,4,5 / 6,3,3 / 4,5,4 |
| split_body YZ | mcp | 48 | 48 | 46 | 48,45,49 / 48,44,49 / 46,46,50 |
| split_body YZ | mcp_push | 112 | 106 | 90 | 115,87,112 / 109,83,106 / 89,90,112 |
| split_body YZ | undo | 2 | 1 | 1 | 2,2,1 / 2,1,1 / 1,1,1 |
| split_body YZ | redo | 2 | 2 | 2 | 2,1,2 / 2,2,2 / 2,1,2 |
| thread 穴 trapezoid30 | preview | 4365 | 4494 | 2137 | 4365,3962,4820 / 4494,4130,4494 / 2054,2314,2137 |
| thread 穴 trapezoid30 | confirm | 3 | 3 | 4 | 3,3,3 / 4,3,3 / 3,4,4 |
| thread 穴 trapezoid30 | push | 3 | 3 | 3 | 3,3,3 / 3,3,3 / 3,3,3 |
| thread 穴 trapezoid30 | mcp | 4043 | 3975 | 1986 | 4222,4043,3967 / 4214,3975,3742 / 1985,1986,2166 |
| thread 穴 trapezoid30 | mcp_push | 248 | 263 | 241 | 287,231,248 / 288,215,263 / 265,231,241 |
| thread 穴 trapezoid30 | undo | 2 | 2 | 2 | 2,2,1 / 2,1,2 / 2,2,2 |
| thread 穴 trapezoid30 | redo | 3967 | 4202 | 1913 | 4106,3967,3897 / 4202,3647,4247 / 1754,1913,2066 |

失敗: なし
配信の大きさ(バイト)が版の間で同じ: False
  boolean_body cut mcp_bytes: [164988]
  boolean_body cut preview_bytes: [326640, 326644, 326648]
  boolean_body cut push_bytes: [164056]
  boolean_body union 側面に接する箱 mcp_bytes: [10092, 10096]
  boolean_body union 側面に接する箱 preview_bytes: [225236]
  boolean_body union 側面に接する箱 push_bytes: [14376, 14384, 14388]
  extrude 面 join 0.5 mcp_bytes: [53664, 53668]
  extrude 面 join 0.5 preview_bytes: [223708, 223712]
  extrude 面 join 0.5 push_bytes: [52736]
  extrude 領域 join(ボディを貫く) mcp_bytes: [105580, 105584]
  extrude 領域 join(ボディを貫く) preview_bytes: [225604, 225608]
  extrude 領域 join(ボディを貫く) push_bytes: [106076, 106084]
  split_body YZ mcp_bytes: [112036, 112040, 112044]
  split_body YZ preview_bytes: [228980, 228984]
  split_body YZ push_bytes: [170860]
  thread 穴 trapezoid30 mcp_bytes: [243332, 243336]
  thread 穴 trapezoid30 preview_bytes: [358656, 358660]
  thread 穴 trapezoid30 push_bytes: [242400]

## 付録 B. 版の間の指紋(`fingerprint.py`。MCP の順に掛けた結果)

ボディごとに BinTools(三角形なし)の SHA-1・面/辺の数・体積(`repr`)・画面の粗さ(対角/1200)の三角形の数、と操作の由来(新しい
ボディの id は並びの番号に置き換え)。3 版で全部同じなら「同じ」。

| 部品 | 操作 | 3 版で同じ | 結果(面, 三角形, 体積) |
|---|---|---|---|
| small | (操作の前の部品) | 同じ | 59, 8676, 80975.30420955905 |
| small | extrude 領域 join(ボディを貫く) | 同じ | 65, 8700, 81455.30420955544; 6, 12, 12600.0 |
| small | extrude 領域 cut(貫通) | 同じ | 64, 8704, 80627.30420955832; 6, 12, 12600.0 |
| small | extrude 面 join 0.5 | 同じ | 59, 8676, 81140.30420955902; 6, 12, 12600.0 |
| small | extrude 側面 join 1 | 同じ | 59, 8676, 82075.30420955904; 6, 12, 12600.0 |
| small | thread 穴 trapezoid30 | 同じ | 70, 14194, 80983.95329819819; 6, 12, 12600.0 |
| small | split_body YZ | 同じ | 37, 4388, 40487.652104804925; 6, 12, 12600.0; 37, 4348, 40487.65210480326 |
| small | boolean_body cut | 同じ | 81, 12776, 71193.18022932648 |
| small | boolean_body union 側面に接する箱 | 同じ | 64, 8692, 93775.30420955972; 6, 12, 12600.0 |
| big | (操作の前の部品) | 同じ | 485, 131028, 71624.41650800995 |
| big | extrude 領域 join(ボディを貫く) | 同じ | 493, 130464, 72216.43595971144; 6, 12, 12600.0 |
| big | extrude 領域 cut(貫通) | 同じ | 494, 130444, 71388.43595971077; 6, 12, 12600.0 |
| big | extrude 面 join 0.5 | 同じ | 485, 131028, 71789.41650801008; 6, 12, 12600.0 |
| big | extrude 側面 join 1 | 同じ | 485, 131028, 72724.41650801015; 6, 12, 12600.0 |
| big | thread 穴 trapezoid30 | 同じ | 495, 137222, 71623.66594898314; 6, 12, 12600.0 |
| big | split_body YZ | 同じ | 250, 65564, 35812.208254029945; 6, 12, 12600.0; 250, 65524, 35812.208254028206 |
| big | boolean_body cut | 同じ | 657, 185960, 63200.93485543832 |
| big | boolean_body union 側面に接する箱 | 同じ | 490, 131044, 84424.4165080102; 6, 12, 12600.0 |

## 付録 C. 公式の物差し(`tools/bench_all_ops.py`。版ごとに 1 回、最大常駐メモリつき)

`harness.py` との違いは、カメラの段と丸めの候補探しを踏むこと(面の三角形のキャッシュの状態が違う)。3 回の中央値は付録 A。

| 物差し | 版 | 操作の行 | 最大常駐メモリ(このプロセス。子プロセスは含まない) |
|---|---|---|---|
| boolean_big | base | `part  boolean_body cut                   preview=3547 confirm=23 push=29 mcp=790 mcp_push=2968 undo=6 undo_push=23 redo=6 redo_push=33` | 5750 MB |
| boolean_big | b0b79f9 | `part  boolean_body cut                   preview=3566 confirm=27 push=35 mcp=795 mcp_push=2797 undo=5 undo_push=21 redo=5 redo_push=30` | 5531 MB |
| extface_big | base | `part  extrude 面 join 0.5                 preview=6992 confirm=29 push=22 mcp=922 mcp_push=6305 undo=8 undo_push=28 redo=7 redo_push=22` | 5695 MB |
| extface_big | b0b79f9 | `part  extrude 面 join 0.5                 preview=6593 confirm=24 push=24 mcp=572 mcp_push=6099 undo=6 undo_push=25 redo=6 redo_push=21` | 5696 MB |
| split_big | base | `part  split_body YZ                      preview=6359 confirm=25 push=53 mcp=676 mcp_push=5672 undo=6 undo_push=31 redo=6 redo_push=27` | 5496 MB |
| split_big | b0b79f9 | `part  split_body YZ                      preview=6699 confirm=25 push=49 mcp=626 mcp_push=5897 undo=6 undo_push=29 redo=5 redo_push=26` | 5502 MB |
| thread_small | base | `part  thread 穴 trapezoid30               preview=4128 confirm=5 push=3 mcp=4297 mcp_push=228 undo=2 undo_push=2 redo=4101 redo_push=185` | 632 MB |
| thread_small | b0b79f9 | `part  thread 穴 trapezoid30               preview=2085 confirm=4 push=3 mcp=2024 mcp_push=244 undo=3 undo_push=2 redo=1815 redo_push=221` | 778 MB |

## 付録 D. 突き合わせの集計

- 全件のテストの中の `build_boolean`(`sitecustomize.py` を `PYTHONPATH` に置き `PARCHECK_OUT` を設定。子プロセスも含む): 1,391 回・287 テスト。
  直列と並列(2 回)でバイト列・面と辺の履歴・成否が全部一致、並列の 2 回も一致。種類: 切る 569・共通 418・足す 404。
- 同じ走らせ方で `UNIFYCHECK_OUT` を設定した統合: 257 回。面が減らず採らない 186、採った 71。採った 71 回すべてで、生のバイト列は違い、
  写し同士は同じ、本番は妥当、体積はビット単位で同じ。
- 合成の 960 組(`corpus.py`。種 1〜4 はスレッド 4、種 5・6 は 16、種 7・8 は 2): 一致 960、並列の決定性 960。成功 957、失敗 3(直列・並列とも
  同じ例外 `intersect left nothing solid`)。和 237・引く 503・共通 220、道具 2〜3 個 387 組、板の面は最大 231。
- 合成の統合(種 1〜4): 354 回。採った 84、写し同士が同じ 69、違う 15(辺の連続性の記録だけ。本番は全部妥当、体積は全部同じ)。

## 付録 E. 再現のコマンド

スクリプトは付録 F。`S` はスクリプトと出力を置く作業フォルダ、`base` などは `git archive <commit> | tar -x -C $S/<版>` で書き出した版。

```bash
uv sync --frozen
```

```bash
# 前後の時間(版を交互に 3 回。--repeat 2 で冷えた回と温まった回)
for v in base v_uni v_par; do (cd $S/$v && uv run --project <repo> python $S/harness.py --repo $S/$v --part big --only "boolean_body cut,boolean_body union,split_body YZ,extrude 面 join 0.5,extrude 領域 join" --repeat 2 --out $S/ab/big_${v}_r1.json); done
python3 $S/ab_table.py big
```

```bash
# 内訳(1 ケース・1 段だけ cProfile)
uv run python $S/harness.py --repo . --part big --only "boolean_body cut" --profile preview --out /tmp/x.json
```

```bash
# 全件のテストの中のブーリアンを直列と並列で突き合わせる(と、統合の検査の記録)
PYTHONPATH=$S/plugin PARCHECK_OUT=$S/parcheck UNIFYCHECK_OUT=$S/unifycheck uv run pytest -q -p no:cacheprovider
```

```bash
# 合成の形(種・件数・出力。POOL で OCCT のスレッド数)
POOL=16 uv run python $S/corpus.py $S/base 5 120 $S/corpus/pool16_seed5.jsonl
```

```bash
# 版の間の指紋
uv run python $S/fingerprint.py --repo $S/base --part big --out $S/fp/big_base.json
```

```bash
# 公式の物差し(最大常駐メモリつき)
cd $S/base && uv run --project <repo> python $S/ru.py tools/bench_all_ops.py --docs part --part big --only boolean_body --out $S/ruler/boolean_big_base.json
```

## 付録 F. スクリプト

<details><summary><code>harness.py</code> — 前後の時間(`bench_all_ops` の部品と `run_case` をそのまま使う)</summary>

```python
"""bench_all_ops の部品と run_case を使い、カメラの段を省いてブーリアン・分割だけを測る(A/B 用)。

uv run python harness.py --repo <checkout> --part big --only boolean_body --out x.json [--camera] [--profile phase]
"""
import argparse
import cProfile
import importlib.util
import json
import pstats
import sys
import tempfile
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--repo", required=True)
ap.add_argument("--part", default="big")
ap.add_argument("--only", default="")
ap.add_argument("--out", required=True)
ap.add_argument("--camera", action="store_true", help="bench_all_ops と同じカメラの段も踏む")
ap.add_argument("--profile", default="", help="preview / mcp / mcp_push / push / confirm")
ap.add_argument("--repeat", type=int, default=1)
args = ap.parse_args()

repo = Path(args.repo).resolve()
sys.path.insert(0, str(repo))
spec = importlib.util.spec_from_file_location("bench_all_ops", repo / "tools" / "bench_all_ops.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)

from duocad import kernel  # noqa: E402
from duocad.session import Session  # noqa: E402
from duocad import brep_topology as T  # noqa: E402

PROF = None
if args.profile:
    PROF = cProfile.Profile()
    original_ms = bench.ms
    counter = {"n": 0}
    # run_case の ms 呼び出しの順: mcp, (push: Wire.push 内), undo, ..., preview, confirm
    # Wire.push は内部で ms を使うので、名前で包むのは難しい。ms の呼び出し順で数える。

tmp = Path(tempfile.mkdtemp(prefix="hb-"))
s = Session(kernel.DEFAULT_DEVIATION, models_dir=tmp)
t0 = time.perf_counter()
body_id = bench.build_part(s, **bench.PARTS[args.part])
build_ms = (time.perf_counter() - t0) * 1000
s.apply("sketch_create", {"base_plane": "XZ"})
sid = bench.last_sketch(s)
s.apply("add_rectangle", {"sketch_id": sid, "corner": [-70, 2], "width": 140, "height": 3})
s.leave_sketch()
bench.select(s, [{"sketch": sid, "index": 1}])
s.apply("extrude", {"distance": 30, "mode": "new"})
tool = s.document.bodies[-1].id
s.clear_selection()
link = bench.Wire(s)
first = link.push()
if args.camera:
    bench.camera_and_selection("part", s, link, body_id)
else:
    diag = s.geometry(body_id)["bbox"]["diagonal"]
    s.set_view_resolution(diag / 1200.0, diag)
    link.push()

if "fillet" not in args.only and "chamfer" not in args.only and args.only:
    bench.blendable = lambda *a, **k: []  # 丸めの候補探し(重い)は分割・ブーリアンの段に要らない
shape_before = bench.body_of(s, body_id).shape
info = {"faces": T.face_count(shape_before), "build_ms": build_ms, "first_push": first[0]}

if PROF is not None:
    names = []

    def ms_named(fn):
        names.append(None)
        return original_ms(fn)

def flush_union_setup():
    # 板の +x の側面に、上下の面を揃えて接する箱(和で上面・底面・側面が同一平面になり、統合が採られる)
    s.apply("sketch_create", {"base_plane": "XY"})
    fsid = bench.last_sketch(s)
    s.apply("add_rectangle", {"sketch_id": fsid, "corner": [60, -40], "width": 20, "height": 80})
    s.leave_sketch()
    bench.select(s, [{"sketch": fsid, "index": 1}])
    s.apply("extrude", {"distance": 8, "mode": "new"})
    flush = s.document.bodies[-1].id
    top = max((f for f in s.geometry(body_id)["faces"] if f["kind"] == "plane"), key=lambda f: f["centroid"][2])
    bench.select(s, [bench.body_ref(flush)])
    return {"target_ref": bench.face_ref(body_id, top["index"]), "mode": "union"}


cases = bench.cases_for(s, body_id, tool) + [("boolean_body union 側面に接する箱", "boolean_body", flush_union_setup)]
wanted = [w for w in args.only.split(",") if w]
selected = [c for c in cases if not wanted or any(w in c[0] or w == c[1] for w in wanted)]
runs = [(rep, c) for rep in range(args.repeat) for c in selected]
for rep, (name, verb, setup) in runs:
    if rep:
        kernel._RESULTS.clear()  # 温まった回: 同じ形の結果の使い回しに当てない(OCCT とキャッシュの他は温まったまま)
    before_rows = len(bench.ROWS)
    if PROF is not None:
        # 目的の段だけ cProfile を有効にする
        order = {"mcp": 0, "mcp_push": 1, "undo": 2, "redo": 4, "preview": 7, "confirm": 8, "push": 9}
        calls = {"n": 0}

        def ms_profiled(fn, _calls=calls):
            idx = _calls["n"]
            _calls["n"] += 1
            want = order.get(args.profile)
            if want is not None and idx == want:
                PROF.enable()
            t = time.perf_counter()
            try:
                r = fn()
            finally:
                el = (time.perf_counter() - t) * 1000
                if want is not None and idx == want:
                    PROF.disable()
            return el, r
        bench.ms = ms_profiled
        bench.PROFILE = None
    bench.run_case("part", s, link, name, verb, setup)
    for row in bench.ROWS[before_rows:]:
        row["rep"] = rep

out = {"info": info, "rows": bench.ROWS}
Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1))
if PROF is not None:
    st = pstats.Stats(PROF)
    st.sort_stats("cumulative").print_stats(40)
    st.sort_stats("tottime").print_stats(25)
```

</details>

<details><summary><code>ab_table.py</code> — 付録 A の表</summary>

```python
"""ab/*.json(harness の出力)から、版・操作・段ごとの中央値の表を作る(冷えた回 rep0 と温まった回 rep1 を分ける)。"""
import glob
import json
import statistics
import sys
from collections import defaultdict

S = "/tmp/claude-0/-home-user-duocad/331b933f-cc3d-558b-95e9-1cafb35ad450/scratchpad"
part = sys.argv[1]
versions = ["base", "v_uni", "v_par"]
keys = ["preview", "confirm", "push", "mcp", "mcp_push", "undo", "redo"]
values = defaultdict(list)
sizes = defaultdict(set)
errors = defaultdict(int)
for v in versions:
    for f in sorted(glob.glob(f"{S}/ab/{part}_{v}_r*.json")):
        for row in json.load(open(f))["rows"]:
            if "rep" not in row:
                continue
            if "error" in row:
                errors[(v, row["case"], row["rep"])] += 1
                continue
            for k in keys:
                if k in row:
                    values[(v, row["case"], row["rep"], k)].append(row[k])
            for k in ("preview_bytes", "push_bytes", "mcp_bytes"):
                if k in row:
                    sizes[(row["case"], k, v)].add(row[k])
cases = sorted({c for (_, c, _, _) in values})
print(f"## {part}  (中央値 ms、3 回。左から base → 統合の2コミット → +並列)")
for rep, label in ((0, "冷えた回(プロセスで最初)"), (1, "温まった回(同じプロセスの2回目)")):
    print(f"\n### {label}\n")
    print("| 操作 | 段 | base | 統合 | +並列 | 生の値 base / 統合 / +並列 |")
    print("|---|---|---|---|---|---|")
    for c in cases:
        for k in keys:
            row = [values.get((v, c, rep, k), []) for v in versions]
            if not all(row):
                continue
            med = [statistics.median(x) for x in row]
            raw = " / ".join(",".join(f"{y:.0f}" for y in x) for x in row)
            print(f"| {c} | {k} | {med[0]:.0f} | {med[1]:.0f} | {med[2]:.0f} | {raw} |")
print("\n失敗:", dict(errors) or "なし")
same = all(len(sizes[(c, k, v)] | sizes[(c, k, versions[0])]) == len(sizes[(c, k, versions[0])]) for (c, k, v) in sizes)
print("配信の大きさ(バイト)が版の間で同じ:", same)
for (c, k, v), s in sorted(sizes.items()):
    if v == "base":
        print(f"  {c} {k}: {sorted(s)}")
```

</details>

<details><summary><code>fingerprint.py</code> — 付録 B の指紋</summary>

```python
"""各版で同じ操作を MCP の順(apply)に掛け、結果の形の指紋を JSON に書く(版の間で突き合わせる)。

uv run python fingerprint.py --repo <checkout> --part big --out x.json
指紋: ボディごとの BinTools(三角形なし)の SHA-1・面/辺/頂点の数・体積(repr)・操作の由来(JSON)・画面の粗さの三角形の数
"""
import argparse
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--repo", required=True)
ap.add_argument("--part", default="big")
ap.add_argument("--out", required=True)
args = ap.parse_args()
repo = Path(args.repo).resolve()
sys.path.insert(0, str(repo))
spec = importlib.util.spec_from_file_location("bench_all_ops", repo / "tools" / "bench_all_ops.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)
bench.blendable = lambda *a, **k: []

from OCP.BinTools import BinTools, BinTools_FormatVersion  # noqa: E402
from duocad import kernel  # noqa: E402
from duocad import brep_topology as T, brep_geometry as G  # noqa: E402
from duocad.session import Session  # noqa: E402


def body_print(s, body):
    stream = io.BytesIO()
    BinTools.Write_s(body.shape, stream, False, False, BinTools_FormatVersion.BinTools_FormatVersion_CURRENT)
    mesh = kernel.tessellate(body.shape, s.view.view_deviation, s.view.view_angular_deflection)
    return {
        "sha1": hashlib.sha1(stream.getvalue()).hexdigest(),
        "faces": T.face_count(body.shape),
        "edges": T.edge_count(body.shape),
        "volume": repr(G._integrated_volume_props(body.shape).Mass()),
        "triangles": sum(len(f["indices"]) // 3 for f in mesh["faces"]),
    }


s = Session(kernel.DEFAULT_DEVIATION, models_dir=Path(tempfile.mkdtemp()))
body_id = bench.build_part(s, **bench.PARTS[args.part])
s.apply("sketch_create", {"base_plane": "XZ"})
sid = bench.last_sketch(s)
s.apply("add_rectangle", {"sketch_id": sid, "corner": [-70, 2], "width": 140, "height": 3})
s.leave_sketch()
bench.select(s, [{"sketch": sid, "index": 1}])
s.apply("extrude", {"distance": 30, "mode": "new"})
tool = s.document.bodies[-1].id
s.clear_selection()
diag = s.geometry(body_id)["bbox"]["diagonal"]
s.set_view_resolution(diag / 1200.0, diag)


def flush_union_setup():
    s.apply("sketch_create", {"base_plane": "XY"})
    fsid = bench.last_sketch(s)
    s.apply("add_rectangle", {"sketch_id": fsid, "corner": [60, -40], "width": 20, "height": 80})
    s.leave_sketch()
    bench.select(s, [{"sketch": fsid, "index": 1}])
    s.apply("extrude", {"distance": 8, "mode": "new"})
    flush = s.document.bodies[-1].id
    top = max((f for f in s.geometry(body_id)["faces"] if f["kind"] == "plane"), key=lambda f: f["centroid"][2])
    bench.select(s, [bench.body_ref(flush)])
    return {"target_ref": bench.face_ref(body_id, top["index"]), "mode": "union"}


wanted = ["boolean_body cut", "split_body YZ", "extrude 面 join 0.5", "extrude 領域 join", "extrude 側面 join", "extrude 領域 cut", "thread 穴"]
cases = [c for c in bench.cases_for(s, body_id, tool) if any(w in c[0] for w in wanted)]
cases.append(("boolean_body union 側面に接する箱", "boolean_body", flush_union_setup))
out = {"base_body": body_print(s, bench.body_of(s, body_id)), "cases": {}}
for name, verb, setup in cases:
    before = len(s.document.log.entries)
    try:
        arguments = setup()
        s.apply(verb, arguments)
        operation = s.document.log.entries[-1]
        text = json.dumps(getattr(operation, "provenance", None), sort_keys=True, default=str)
        for position, b in enumerate(s.document.bodies):  # 新しいボディの id は乱数なので並びの番号に置き換える
            text = text.replace(str(b.id), f"B{position}")
        out["cases"][name] = {
            "provenance": text,
            "bodies": [body_print(s, b) for b in s.document.bodies if b.shape is not None],
        }
    except Exception as error:  # noqa: BLE001
        out["cases"][name] = {"error": f"{type(error).__name__}: {error}"}
    while len(s.document.log.entries) > before:
        s.undo()
    s.clear_selection()
    print(name, "error" if "error" in out["cases"][name] else "ok", flush=True)
Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1))
```

</details>

<details><summary><code>plugin/sitecustomize.py</code> — 全件のテストの中の突き合わせ(直列/並列・統合の検査)</summary>

```python
"""pytest の全件の中で呼ばれるブーリアン(`brep_operations.build_boolean`)を1回ずつ、入力の写しの上で直列と並列で
解き直し、結果のバイト列(BinTools・三角形なし)と面・辺の由来の添字が同じかを記録する。本物の呼び出しは元のまま直列。

    PYTHONPATH=<this dir> uv run pytest -q -p no:cacheprovider -p parcheck_plugin
    記録: $PARCHECK_OUT/parcheck-<pid>.jsonl
"""
import io
import json
import os
import time


_OUT = os.environ.get("PARCHECK_OUT", "/tmp")


def _install(brep_operations):
    from OCP.BinTools import BinTools, BinTools_FormatVersion
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape, TopTools_ListOfShape

    original = brep_operations.build_boolean
    log = open(os.path.join(_OUT, f"parcheck-{os.getpid()}.jsonl"), "a", encoding="utf-8")
    state = {"test": ""}

    def blob(shape):
        if shape is None or shape.IsNull():
            return b""
        buf = io.BytesIO()
        BinTools.Write_s(shape, buf, False, False, BinTools_FormatVersion.BinTools_FormatVersion_CURRENT)
        return buf.getvalue()

    def imap(shape, kind):
        m = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, kind, m)
        return m

    def history(builder, inputs, result):
        out = []
        for kind in (TopAbs_ShapeEnum.TopAbs_FACE, TopAbs_ShapeEnum.TopAbs_EDGE):
            rmap = imap(result, kind)
            for shape in inputs:
                m = imap(shape, kind)
                for i in range(1, m.Extent() + 1):
                    sub = m.FindKey(i)
                    mods = builder.Modified(sub)
                    pos = []
                    it = mods.cbegin() if hasattr(mods, "cbegin") else None
                    for s in mods:
                        pos.append(rmap.FindIndex(s))
                    gens = [rmap.FindIndex(s) for s in builder.Generated(sub)]
                    out.append((int(kind), i, pos, gens, bool(builder.IsDeleted(sub))))
        return out

    def run(kind, first, second, fuzzy, parallel):
        builder = {"fuse": BRepAlgoAPI_Fuse, "cut": BRepAlgoAPI_Cut, "common": BRepAlgoAPI_Common}[kind]()
        a, t = TopTools_ListOfShape(), TopTools_ListOfShape()
        a.Append(first)
        t.Append(second)
        builder.SetArguments(a)
        builder.SetTools(t)
        if fuzzy:
            builder.SetFuzzyValue(fuzzy)
        builder.SetRunParallel(parallel)
        t0 = time.perf_counter()
        builder.Build()
        elapsed = time.perf_counter() - t0
        if not builder.IsDone():
            return {"done": False, "ms": elapsed * 1000}
        result = builder.Shape()
        return {
            "done": True,
            "ms": elapsed * 1000,
            "blob": blob(result),
            "history": history(builder, (first, second), result),
            "errors": bool(getattr(builder, "HasErrors", lambda: False)()),
            "warnings": bool(getattr(builder, "HasWarnings", lambda: False)()),
        }

    def checked(kind, first, second, fuzzy=0.0):
        try:
            copies = [(BRepBuilderAPI_Copy(first, True, False).Shape(), BRepBuilderAPI_Copy(second, True, False).Shape()) for _ in range(3)]
            serial = run(kind, *copies[0], fuzzy, False)
            par1 = run(kind, *copies[1], fuzzy, True)
            par2 = run(kind, *copies[2], fuzzy, True)
            record = {
                "test": os.environ.get("PYTEST_CURRENT_TEST", ""),
                "kind": kind,
                "fuzzy": fuzzy,
                "faces_in": [imap(first, TopAbs_ShapeEnum.TopAbs_FACE).Extent(), imap(second, TopAbs_ShapeEnum.TopAbs_FACE).Extent()],
                "serial_ms": serial["ms"],
                "parallel_ms": [par1["ms"], par2["ms"]],
                "done": [serial["done"], par1["done"], par2["done"]],
            }
            keys = ("blob", "history", "errors", "warnings")
            record["same"] = all(serial.get(k) == p.get(k) for p in (par1, par2) for k in keys)
            record["par_det"] = all(par1.get(k) == par2.get(k) for k in keys)
            if not record["same"]:
                record["diff"] = [k for k in keys if serial.get(k) != par1.get(k) or serial.get(k) != par2.get(k)]
            log.write(json.dumps(record) + "\n")
            log.flush()
        except Exception as error:  # noqa: BLE001
            log.write(json.dumps({"test": os.environ.get("PYTEST_CURRENT_TEST", ""), "kind": kind, "error": repr(error)}) + "\n")
            log.flush()
        return original(kind, first, second, fuzzy)

    brep_operations.build_boolean = checked
    return state



import importlib.abc
import importlib.util
import sys


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != "duocad.brep_operations":
            return None
        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(name)
        finally:
            sys.meta_path.insert(0, self)
        loader = spec.loader
        exec_module = loader.exec_module

        def patched(module):
            exec_module(module)
            if os.environ.get("PARCHECK_OUT"):
                _install(module)
            if os.environ.get("UNIFYCHECK_OUT"):
                _install_unify(module)
        loader.exec_module = patched
        return spec




def _install_unify(brep_operations):
    """`unify_same_domain` を、同じ手順に計測と「本番の結果が試行の結果とバイト列で同じか・本番の妥当性」の記録を足したものに替える。"""
    from OCP.BinTools import BinTools, BinTools_FormatVersion
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

    from duocad import brep_checks as C, brep_geometry as G, brep_topology as T

    log = open(os.path.join(os.environ["UNIFYCHECK_OUT"], f"unify-{os.getpid()}.jsonl"), "a", encoding="utf-8")

    def blob(shape):
        buf = io.BytesIO()
        BinTools.Write_s(shape, buf, False, False, BinTools_FormatVersion.BinTools_FormatVersion_CURRENT)
        return buf.getvalue()

    def unify_same_domain(shape, volume_before):
        rec = {"test": os.environ.get("PYTEST_CURRENT_TEST", "")}
        clock = time.perf_counter
        t = clock()
        trial = ShapeUpgrade_UnifySameDomain(BRepBuilderAPI_Copy(shape).Shape(), True, True, False)
        trial.Build()
        candidate = trial.Shape()
        rec["trial_ms"] = (clock() - t) * 1000
        try:
            if candidate is None or T.is_null(candidate):
                rec["stop"] = "null"
                return shape, None, False
            rec["faces"] = [T._face_map(shape).Extent(), T._face_map(candidate).Extent()]
            if rec["faces"][1] >= rec["faces"][0]:
                rec["stop"] = "no_reduction"
                return shape, None, False
            t = clock()
            vol = G.volume(candidate)
            rec["volume_ms"] = (clock() - t) * 1000
            if abs(vol - volume_before) > 1e-6 * max(1.0, abs(volume_before)):
                rec["stop"] = "volume"
                return shape, None, False
            t = clock()
            ok = C.is_valid(candidate)
            rec["check_trial_ms"] = (clock() - t) * 1000
            if not ok:
                rec["stop"] = "invalid_trial"
                return shape, None, False
            t = clock()
            unify = ShapeUpgrade_UnifySameDomain(shape, True, True, False)
            unify.Build()
            G.forget_volume(shape)
            unified = unify.Shape()
            rec["real_ms"] = (clock() - t) * 1000
            t = clock()
            same = (not T.is_null(unified)) and blob(unified) == blob(candidate)
            rec["blob_ms"] = (clock() - t) * 1000
            t = clock()
            norm = (not T.is_null(unified)) and blob(BRepBuilderAPI_Copy(unified, True, False).Shape()) == blob(BRepBuilderAPI_Copy(candidate, True, False).Shape())
            rec["norm_ms"] = (clock() - t) * 1000
            rec["same_norm"] = norm
            if not norm and os.environ.get("UNIFYDUMP"):
                from OCP.BRepTools import BRepTools
                n = len(os.listdir(os.environ["UNIFYDUMP"]))
                BRepTools.Write_s(BRepBuilderAPI_Copy(unified, True, False).Shape(), os.path.join(os.environ["UNIFYDUMP"], f"{n:03d}_real.brep"))
                BRepTools.Write_s(BRepBuilderAPI_Copy(candidate, True, False).Shape(), os.path.join(os.environ["UNIFYDUMP"], f"{n:03d}_trial.brep"))
            t = clock()
            valid = unified is not None and not T.is_null(unified) and C.is_valid(unified)
            rec["check_real_ms"] = (clock() - t) * 1000
            rec["same_bytes"] = same
            rec["real_valid"] = valid
            if not T.is_null(unified):
                fresh = G._integrated_volume_props(unified).Mass()
                rec["same_volume"] = fresh == vol
                rec["volume_rel"] = abs(fresh - vol) / max(1.0, abs(vol))
            rec["stop"] = "adopted"
            if not valid:
                raise ValueError("ShapeUpgrade_UnifySameDomain が試行と異なる結果を返した")
            return unified, unify.History(), True
        finally:
            log.write(json.dumps(rec) + "\n")
            log.flush()

    brep_operations.unify_same_domain = unify_same_domain


if os.environ.get("PARCHECK_OUT") or os.environ.get("UNIFYCHECK_OUT"):
    sys.meta_path.insert(0, _Finder())
```

</details>

<details><summary><code>corpus.py</code> — 合成の形の突き合わせ</summary>

```python
"""合成の形の多数で、ブーリアンの直列と並列の結果を突き合わせる。

穴の数・位置・大きさを乱数で変えた板(角を丸めたもの・ポケット付きも)と、それを貫く・かすめる・内に収まる道具
(箱・傾けた箱・円柱・球・円錐・トーラス・角を丸めた箱、1〜3個)を、union / cut / intersect で `kernel._boolean_bodies`
(verb と同じ経路。統合を含む)に掛ける。並列は `BOPAlgo_Options.SetParallelMode_s`(BRepAlgoAPI の既定値になる大域の値)で切り替える。
入力は毎回 BRepBuilderAPI_Copy の写し(BOP は入力の許容差を書き換えうるので、前の回の影響を受けないように)。

比べるもの: 成否と例外の文・結果の BinTools のバイト列(三角形なし)・由来(face_map / deleted / new_faces)・統合したか・体積(完全一致)。
並列は2回解いて、2回の間の一致(決定性)も見る。

    uv run python corpus.py <repo> <seed> <count> <out.jsonl>
"""
import io
import json
import math
import random
import sys
import time

sys.path.insert(0, sys.argv[1])

from OCP.BinTools import BinTools, BinTools_FormatVersion  # noqa: E402
from OCP.BOPAlgo import BOPAlgo_Options  # noqa: E402
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy, BRepBuilderAPI_Transform  # noqa: E402
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet  # noqa: E402
from OCP.BRepPrimAPI import (  # noqa: E402
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCone,
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakeSphere,
    BRepPrimAPI_MakeTorus,
)
from OCP.TopAbs import TopAbs_ShapeEnum  # noqa: E402
from OCP.TopExp import TopExp_Explorer  # noqa: E402
from OCP.TopoDS import TopoDS  # noqa: E402
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec  # noqa: E402

from duocad import brep_operations, kernel  # noqa: E402
from duocad import brep_geometry as G  # noqa: E402


def blob(shape):
    buf = io.BytesIO()
    BinTools.Write_s(shape, buf, False, False, BinTools_FormatVersion.BinTools_FormatVersion_CURRENT)
    return buf.getvalue()


def moved(shape, rx=0.0, ry=0.0, rz=0.0, dx=0.0, dy=0.0, dz=0.0):
    trsf = gp_Trsf()
    for axis, angle in ((gp_Dir(1, 0, 0), rx), (gp_Dir(0, 1, 0), ry), (gp_Dir(0, 0, 1), rz)):
        if angle:
            r = gp_Trsf()
            r.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), axis), angle)
            trsf = r.Multiplied(trsf)
    t = gp_Trsf()
    t.SetTranslation(gp_Vec(dx, dy, dz))
    trsf = t.Multiplied(trsf)
    return BRepBuilderAPI_Transform(shape, trsf, True).Shape()


def filleted_vertical(box, r, height):
    maker = BRepFilletAPI_MakeFillet(box)
    explorer = TopExp_Explorer(box, TopAbs_ShapeEnum.TopAbs_EDGE)
    seen = 0
    while explorer.More():
        edge = TopoDS.Edge_s(explorer.Current())
        from OCP.BRepAdaptor import BRepAdaptor_Curve
        adaptor = BRepAdaptor_Curve(edge)
        p, q = adaptor.Value(adaptor.FirstParameter()), adaptor.Value(adaptor.LastParameter())
        if abs(p.X() - q.X()) < 1e-9 and abs(p.Y() - q.Y()) < 1e-9 and abs(abs(p.Z() - q.Z()) - height) < 1e-9:
            maker.Add(r, edge)
            seen += 1
        explorer.Next()
    maker.Build()
    return maker.Shape() if maker.IsDone() else box


def plate(rng):
    w, d, t = rng.uniform(40, 140), rng.uniform(30, 100), rng.uniform(3, 12)
    shape = BRepPrimAPI_MakeBox(gp_Pnt(-w / 2, -d / 2, 0), w, d, t).Shape()
    if rng.random() < 0.4:
        shape = filleted_vertical(shape, rng.uniform(0.5, min(w, d) / 6), t)
    holes = []
    count = rng.choice([0, 3, 8, 20, 40, 80, 150]) if rng.random() < 0.9 else rng.randint(1, 250)
    for _ in range(count):
        r = rng.uniform(0.4, 4.0)
        x, y = rng.uniform(-w / 2 + r + 0.3, w / 2 - r - 0.3), rng.uniform(-d / 2 + r + 0.3, d / 2 - r - 0.3)
        if any((x - hx) ** 2 + (y - hy) ** 2 < (r + hr + 0.2) ** 2 for hx, hy, hr in holes):
            continue
        holes.append((x, y, r))
    through = rng.random() < 0.8
    cylinders = [
        BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, -1 if through else t * rng.uniform(0.2, 0.7)), gp_Dir(0, 0, 1)), r, t + 2).Shape()
        for x, y, r in holes
    ]
    if cylinders:
        tool = brep_operations.fuse_all(cylinders, "holes") if len(cylinders) < 40 else _compound_fuse(cylinders)
        cut = brep_operations.build_boolean("cut", shape, tool)
        if cut.IsDone():
            shape = cut.Shape()
    if rng.random() < 0.3:  # ポケット
        pw, pd = rng.uniform(5, w / 3), rng.uniform(5, d / 3)
        pocket = BRepPrimAPI_MakeBox(gp_Pnt(rng.uniform(-w / 2, w / 2 - pw), rng.uniform(-d / 2, d / 2 - pd), t * 0.5), pw, pd, t).Shape()
        cut = brep_operations.build_boolean("cut", shape, pocket)
        if cut.IsDone():
            shape = cut.Shape()
    return shape, (w, d, t, len(holes))


def _compound_fuse(shapes):
    # 穴が多いときは、離れた円柱を compound にして1回で切る(fuse を何十回も重ねない)
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound
    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    for shape in shapes:
        builder.Add(compound, shape)
    return compound


def tool(rng, w, d, t):
    kind = rng.choice(["box", "box", "tilted_box", "cylinder", "cylinder_h", "sphere", "cone", "torus", "round_box", "tunnel"])
    cx, cy = rng.uniform(-w / 2, w / 2), rng.uniform(-d / 2, d / 2)
    if kind in ("box", "tilted_box", "round_box"):
        a, b, c = rng.uniform(2, w), rng.uniform(2, d), rng.uniform(1, 3 * t)
        z = rng.uniform(-c, t)
        shape = BRepPrimAPI_MakeBox(gp_Pnt(cx - a / 2, cy - b / 2, z), a, b, c).Shape()
        if kind == "round_box":
            shape = filleted_vertical(shape, rng.uniform(0.2, min(a, b) / 4), c)
        if kind == "tilted_box":
            shape = moved(shape, rx=rng.uniform(-0.6, 0.6), ry=rng.uniform(-0.6, 0.6), rz=rng.uniform(0, math.pi))
    elif kind == "tunnel":
        a, h = rng.uniform(1, d / 2), rng.uniform(0.5, t * 0.8)
        shape = BRepPrimAPI_MakeBox(gp_Pnt(-w, cy - a / 2, rng.uniform(0.1, t - h - 0.05)), 2 * w, a, h).Shape()
    elif kind == "cylinder":
        r = rng.uniform(0.5, min(w, d) / 3)
        shape = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(cx, cy, rng.uniform(-t, t)), gp_Dir(0, 0, 1)), r, rng.uniform(1, 3 * t)).Shape()
    elif kind == "cylinder_h":
        r = rng.uniform(0.3, t)
        shape = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(-w, cy, rng.uniform(0, t)), gp_Dir(1, 0, 0)), r, 2 * w).Shape()
        shape = moved(shape, rz=rng.uniform(-0.5, 0.5))
    elif kind == "sphere":
        shape = BRepPrimAPI_MakeSphere(gp_Pnt(cx, cy, rng.uniform(-t, 2 * t)), rng.uniform(1, min(w, d) / 3)).Shape()
    elif kind == "cone":
        r1 = rng.uniform(0.5, min(w, d) / 3)
        shape = BRepPrimAPI_MakeCone(gp_Ax2(gp_Pnt(cx, cy, rng.uniform(-t, t / 2)), gp_Dir(0, 0, 1)), r1, rng.uniform(0, r1), rng.uniform(1, 3 * t)).Shape()
    else:
        r1 = rng.uniform(2, min(w, d) / 3)
        shape = BRepPrimAPI_MakeTorus(gp_Ax2(gp_Pnt(cx, cy, rng.uniform(0, t)), gp_Dir(0, 0, 1)), r1, rng.uniform(0.3, min(r1 * 0.8, t))).Shape()
    return kind, shape


BOP_MS = [0.0]
_build_boolean = brep_operations.build_boolean


def _timed_build_boolean(*args, **kwargs):
    t0 = time.perf_counter()
    try:
        return _build_boolean(*args, **kwargs)
    finally:
        BOP_MS[0] += (time.perf_counter() - t0) * 1000


def solve(target, tools, mode, parallel):
    BOPAlgo_Options.SetParallelMode_s(parallel)
    a = BRepBuilderAPI_Copy(target, True, False).Shape()
    b = [BRepBuilderAPI_Copy(s, True, False).Shape() for s in tools]
    t0 = time.perf_counter()
    BOP_MS[0] = 0.0
    brep_operations.build_boolean = _timed_build_boolean
    try:
        result = kernel._boolean_bodies(a, b, mode)
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "error": f"{type(error).__name__}: {error}", "ms": (time.perf_counter() - t0) * 1000, "bop_ms": BOP_MS[0]}
    finally:
        BOPAlgo_Options.SetParallelMode_s(False)
        brep_operations.build_boolean = _build_boolean
    elapsed = (time.perf_counter() - t0) * 1000
    shape = result["shape"]
    return {
        "ok": True,
        "ms": elapsed,
        "bop_ms": BOP_MS[0],
        "blob": blob(shape),
        "provenance": json.dumps(result["provenance"], sort_keys=True),
        "unified": result["unified"],
        "volume": result["volume_after"],
        "faces": len(json.loads(json.dumps(result["provenance"]["face_map"]))),
    }


def main():
    import os
    if os.environ.get("POOL"):
        from OCP.OSD import OSD_ThreadPool
        print("pool threads", OSD_ThreadPool.DefaultPool_s(int(os.environ["POOL"])).NbThreads(), flush=True)
    seed, count, out = int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    rng = random.Random(seed)
    with open(out, "a", encoding="utf-8") as log:
        for case in range(count):
            target, (w, d, t, holes) = plate(rng)
            tools = [tool(rng, w, d, t) for _ in range(rng.choice([1, 1, 1, 2, 3]))]
            mode = rng.choice(["union", "cut", "cut", "intersect"])
            serial = solve(target, [s for _, s in tools], mode, False)
            par1 = solve(target, [s for _, s in tools], mode, True)
            par2 = solve(target, [s for _, s in tools], mode, True)
            keys = ("ok", "error", "blob", "provenance", "unified", "volume")
            same = all(serial.get(k) == p.get(k) for p in (par1, par2) for k in keys)
            record = {
                "seed": seed, "case": case, "mode": mode, "holes": holes, "tools": [k for k, _ in tools],
                "plate_faces": G.__name__ and __import__("duocad.brep_topology", fromlist=["x"]).face_count(target),
                "ok": serial["ok"], "error": serial.get("error"), "unified": serial.get("unified"),
                "serial_ms": serial["ms"], "parallel_ms": [par1["ms"], par2["ms"]],
                "serial_bop_ms": serial["bop_ms"], "parallel_bop_ms": [par1["bop_ms"], par2["bop_ms"]],
                "same": same, "par_det": all(par1.get(k) == par2.get(k) for k in keys),
            }
            if not same:
                record["diff"] = [k for k in keys if serial.get(k) != par1.get(k) or serial.get(k) != par2.get(k)]
            log.write(json.dumps(record, ensure_ascii=False) + "\n")
            log.flush()
            print(case, mode, holes, record["tools"], "ok" if serial["ok"] else serial["error"][:60], "same" if same else f"DIFF {record.get('diff')}",
                  f"bop {serial['bop_ms']:.0f}/{par1['bop_ms']:.0f}/{par2['bop_ms']:.0f} ms", flush=True)


if __name__ == "__main__":
    main()
```

</details>

<details><summary><code>ru.py</code> — 最大常駐メモリ</summary>

```python
"""スクリプトを runpy で走らせ、終わりに最大常駐メモリ(MB)を出す。 python ru.py <script> <args...>"""
import resource, runpy, sys
script = sys.argv[1]
sys.argv = sys.argv[1:]
try:
    runpy.run_path(script, run_name="__main__")
finally:
    print(f"maxrss_mb self={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} children={resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024:.0f}", flush=True)
```

</details>

