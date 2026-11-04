# 報告: 面の削除・面の編集の高速化(担当 `faceedit`、ブランチ `cloud/perf-faceedit`)

基準は develop の `4ef67570`(`git archive` で書き出したコードを別フォルダから走らせた)。測った機械はクラウドの Linux
(4 論理コア・メモリ 15 GB)、Python 3.14.6、OCP 7.9.3。本人の機械(14 論理コア)とはコア数が違う。RemoveFeatures と
BRepCheck は並列で、範囲の検査は直列なので、本人の機械では範囲の検査の割合がここより大きいはず(未確認)。

入れたコミット(どちらも `duocad/kernel_face_edit.py` だけを変え、テストを足した。1つずつ採否を決められる):

| コミット | 内容 | 形・番号・ログ |
|---|---|---|
| `8ee2907` | 残した面の範囲の検査で、面の分類の探索器と周期の寄せ方を面ごとに1回だけ作る | 変えない(分類の答えが同じ) |
| `d0838c2` | 関門で積分した結果の体積を子から返し、親が積分し直さない | 変えない(体積の値も1ビット同じ) |

三角形の並べ方を変える変更は無い(配信のバイト数も前後で同じ)。

## 1. 測った内訳(前)

### 1.1 ベンチ(`--part big`、穴1つの面の削除。前の 3 回)

`tools/bench_all_ops.py --docs part --part big --only _faces`。ms。

| 項目 | 1回目 | 2回目 | 3回目 | 中央値 |
|---|---:|---:|---:|---:|
| ゴースト(preview) | 15,263 | 14,499 | 14,772 | 14,772 |
| 確定(confirm) | 36 | 32 | 22 | 32 |
| 確定後の配信(push) | 52 | 50 | 32 | 50 |
| MCP の確定(mcp) | 8,045 | 8,840 | 7,722 | 8,045 |
| その後の配信(mcp_push) | 7,187 | 7,805 | 7,097 | 7,187 |
| undo / redo | 5 / 6 | 16 / 13 | 6 / 5 | 6 / 6 |

- 同じ部品の「offset_faces 側面 0.3」(面 398)は前後とも MakeOffset が空を返して断る。ベンチは断ったケースの時間を残さないので、
  断りの時間は下の 1.6 と 2 章の同じプロセスの比較で測った。
- `--part small` は2件とも前後で同じ文で断る(穴が真ん中のリブにかかっていて「kept face 56 lost part of its original region」、
  側面は MakeOffset が空)。時間はベンチに残らない。
- ゴーストと配信の大半は親の三角形分割(ベンチはカメラの段で細かい解像度にしてあるので約 7 秒)で、この担当の範囲の外。

### 1.2 子の計算の内訳(`kernel_face_edit.compute` を同じプロセスで cProfile。穴 395 の削除 8.31 秒)

| 部分 | 秒 | 割合 |
|---|---:|---:|
| `BOPAlgo_RemoveFeatures.Perform` | 3.27 | 39% |
| 残した面の範囲の検査 `_region_kept`(455 面) | 3.93 | 47% |
|  うち格子 169 点の分類 `_interior_points` | 2.29 | |
|  うち標本 49 点の照合 `_point_on_face`(周期への寄せ方 0.50 を含む) | 1.57 | |
| 構造の関門 `_structural_gate` | 1.03 | 12% |
|  うち BRepCheck(既に並列) / 体積 / 面積 | 0.83 / 0.10 / 0.08 | |
| そのほか(由来の表・曲面の照合・張り替え) | 約 0.08 | 1% |

**なぜ 455 面も検査するか**: RemoveFeatures は feature ごとに残す面を全部 `BOPAlgo_MakerVolume` に渡して形全体を作り直す
(docs/FACE_EDIT.md「削除の時間をどこまで縮められるか」)。その結果、残す 484 面のうち 455 面が履歴で Modified になる
(453 面は曲面が元と同じ物で、辺だけが作り直されている)。触られていない面は 29 だけ。検査の時間は、穴の多い上下の平面
(辺 448・388)が 0.83 + 0.71 秒、残りの穴の円筒 383 面が合わせて 2.06 秒(1面約 5 ms)。

**1回あたりの費用**(µs、`micro.py`):

| 呼び出し | 穴の円筒(辺 4) | 穴の多い平面(辺 448) |
|---|---:|---:|
| `BRepClass_FaceClassifier(face, uv, 1e-7)` | 35.6 | 1,114 |
| `BRepTools.UVBounds_s(face)` | 1.4 | 250 |
| `BRepAdaptor_Surface(face)` | 2.1 | 251 |
| 周期への寄せ方(UV の範囲と adaptor を作り直す) | 4.0 | 543 |
| `GeomAPI_ProjectPointOnSurf` の作成 / 使い回しの Perform | 2.2 / 0.6 | 3.5 / 0.7 |

OCCT 7.9.3 の `BRepClass_FaceClassifier::Perform(face, …)` は点ごとに `BRepClass_FaceExplorer` を作り、平面・円筒(曲面の範囲が無限)
では `BRepTools::UVBounds` で面の範囲を測り直す。多い平面ではこれが分類の約2割。

### 1.3 親の側(穴 395 の削除、既定の解像度。`prof_parent2.py`)

- MCP の確定 7.3 秒は、ほぼ全部が子の返事待ち。
- 配信 2.3 秒は、結果の 455 面の三角形分割(`IMeshTools.DiscretizeFaces` 1.73 秒)と体積の積分 0.06 秒。子が返す「入力のまま残った面」
  (`kernel.identical_faces`、TShape の同一性)は 29 面だけなので、455 面を測り直す。
- 三角形分割の大半は、本当に変わった上下の平面2枚(穴が1つ消えた)。厳しい条件で幾何が元のままと分かる面 78 を除いて測っても
  2.14 → 2.00 秒にしかならない(`replica.py`)。
- ゴースト 10.4 秒 = 子 8.2 秒 + 三角形分割 2.1 秒 + 体積 0.06 秒。

### 1.4 連なりの探索・代案探し

| 形 | `blend_chain`(全部の面) | `deletion_candidates`(1組) | 子へ渡す符号化 / 復号 |
|---|---|---|---|
| big(485 面) | 最大 28 ms・平均 1.4 ms | 平均 1.5 ms | 5.7 ms / 8.0 ms |
| 縁を丸めた穴の板(54 面) | 最大 26 ms・平均 0.8 ms | 平均 0.3 ms | |
| B-spline の角の箱(18 面) | 最大 7 ms・平均 2.5 ms | 平均 2.4 ms | |

代案探し(`wider_deletion`)は断られたときだけ走り、候補ごとに子で削除をもう一度計算する(1件 5 秒・合わせて 10 秒まで)。
候補の計算は上の削除と同じ内訳なので、範囲の検査の短縮がそのまま効く。成功する削除では、連なりと代案は待ちの原因ではない。

### 1.5 答え(調べること 1)

成功する削除の待ちは RemoveFeatures 本体が約4割、前後の検査が約6割(範囲の検査が大半、BRepCheck が1割)。連なりの探索は
1クリック数 ms で、代案探しは断られたときだけ。親のゴースト・配信は子の計算と、作り直された面の三角形分割で半々。

### 1.6 反例の形(この担当用に作った形。`make_shapes.py`)

- `big.brep`: `--part big` の部品。穴 395 の削除(成功)と、縦の丸めに接する側面 1 の 0.3 のオフセット(丸めを外す削除を通ってから
  MakeOffset が空を返して断る。16.3 秒: 丸めを外す削除 9.0 秒(うち範囲の検査 4.4)、MakeOffset 6.1 秒、BRepCheck 2回 1.9 秒)
- `holes_rim.brep`: 60×40×8 の板に穴 6×4、穴の上の縁を r0.8 で丸めた形(周期面のトーラスと円筒)。[2, 30](丸めと穴)・[2](丸めだけ)
- `corners.brep`: 縦4辺 r5・上の縁一周 r2 で、角の区間を B-spline にした箱(`tests/test_delete_faces_b_spline_corners.py` と同じ作り)。
  [3, 6, 9](連なり、成功)と [3](RemoveFeatures が警告で断る)
- `small.brep`: `--part small` の部品。穴 35(範囲の検査で断る)

## 2. 入れた変更

### 2.1 `8ee2907` 範囲の検査の分類の探索器と周期の寄せ方を面ごとに1回だけ作る

**何を**: `_region_kept` の中で、像の面ごとに曲面・周期への寄せ方・`BRepClass_FaceExplorer` を1回だけ作り、49 点の照合で使い回す
(`_point_on_face(face)` が照合の関数を返す)。`_interior_points` の格子 169 点と、細い面の代わりの `_points_from_mesh` も同じ探索器で
分類する。周期への寄せ方は `_into_face_period` を `_face_period(face)`(寄せる関数を返す)に替えた。

**なぜ形を変えないと言えるか**:

- OCCT 7.9.3 の `BRepClass_FaceClassifier::Perform(face, P, tol, false, 0.1)` は `BRepClass_FaceExplorer aFex(face)` を作って
  `BRepClass_FClassifier::Perform(aFex, P, tol)` を呼ぶだけ。探索器に点をまたいで残るのは、遅れて1回だけ求める面の UV の範囲
  (`ComputeFaceBounds`、面だけで決まる)。辺の番号と探りの位置は `Segment` が点ごとに初めに戻し、wire・辺の走査と頂点の表は
  `InitWires`・`InitEdges` が作り直す。`SetMaxTolerance(0.1)`・`SetUseBndBox(false)` は探索器の既定値と同じ。
  (`BRepClass_FaceExplorer.cxx`・`BRepClass_FaceClassifier.cxx`・`TopClass_FaceClassifier.gxx` を V7_9_3 のタグで読んだ)
- 周期への寄せ方は同じ式・同じ値(`UVBounds_s` の u1・v1 と adaptor の周期)で、点ごとに求めていたものを先に求めるだけ。
- 突き合わせ:
  - 変更前の写しと、4つの形(616 面。平面・円筒・トーラス・B-spline・押し出し、細い面の三角形の代わり)の全部の面で、格子の点
    (数 1・9・49)、三角形の代わりの点、自分と隣の面への照合(内側・UV の範囲の外・辺の上と辺から 1e-8〜1e-3 ずらした点)を
    **913 万 5,160 回比べて食い違い 0**(`equiv_classify.py`)。
  - 面の削除・オフセットのテスト 9 本を、変更後の関数が呼ばれるたびに変更前の関数も呼んで比べる仕掛け(子プロセスにも入る
    `sitecustomize.py`)で走らせ、食い違い 0(格子 259 回・照合 9,541 回・三角形の代わり 3 回まで数えた。子は止めるときに
    数え残しがある)。
  - 下の 7 ケースで、結果の形の BinTools のバイト列の SHA-1・由来の表・断りの文が前後で同じ。
- テスト `tests/test_face_edit_classifier.py`: 使い回す分類が、点ごとに作る分類と同じ答えを、順に呼んでも逆順に呼んでも返す
  (穴 15・縁の丸めの板の 36 面、内 2,507・境界 2,472・外 2,077 点)。周期面で一周ずれた角度を面の範囲へ戻す。

**前後の値**(中央値。同じプロセスで変更前と変更後を交互に 3 回。秒):

範囲の検査だけ(`ab_region.py`。1回の削除で検査する面の全部を、変更前と変更後の `_region_kept` に通す):

| 形・面 | 検査する面 | 前 | 後 | 答え |
|---|---:|---:|---:|---|
| big [395] | 455 | 3.688 | **2.617**(−29%) | 同じ |
| holes_rim [2, 30] | 52 | 0.291 | **0.215**(−26%) | 同じ |
| corners [3, 6, 9] | 14 | 0.181 | **0.153**(−15%) | 同じ |

子の計算全体(`ab_compute.py`。`compute` を交互に):

| 形・操作 | 結果 | 前 | 後 |
|---|---|---:|---:|
| big 削除 [395] | 通る(484 面) | 7.927 | **7.127** |
| big オフセット [1] 0.3 | 断る(MakeOffset が空) | 14.314 | **13.411** |
| holes_rim 削除 [2, 30] | 通る | 0.529 | **0.344** |
| holes_rim 削除 [2] | 通る | 0.259 | 0.213 |
| small 削除 [35] | 断る(範囲の検査) | 0.591 | 0.479 |
| corners 削除 [3, 6, 9] | 通る | 0.578 | 0.597(揺れの範囲) |
| corners 削除 [3] | 断る(検査の前に RemoveFeatures の警告) | 0.037 | 0.046(この経路は変えていない。揺れ) |

### 2.2 `d0838c2` 関門で積分した結果の体積を子から返す

**何を**: `compute` の戻り値に `volume_after`(構造の関門で積分した結果の体積。子では形の同一性で覚えた値を返すだけで積分し直さない)
を載せる。`kernel_worker.run` は op によらず `volume_after` を戻った形の体積として覚える(フィレット・面取りと同じ口)ので、
親のゴースト(`query.volume`)と配信(`view._body_volume`)が積分し直さない。

**なぜ形を変えないと言えるか**: 形・由来には触らない。体積の値は、BinTools の往復が幾何と面の並びを変えないので、親で積分し直した値と
1ビットも違わない(big・holes_rim・corners の削除の結果で確かめた)。テスト `tests/test_face_edit_volume.py` が、削除とオフセットの
結果の体積を親が積分せずに知り、積分し直した値と等しいことを固定する(このコミットを外すと落ちることを確かめた)。

**前後の値**: 親の積分 big 63 ms・holes_rim 10 ms・corners 4 ms(5回の中央値)が 0 になる。下の親の側の測定で、ゴーストと配信の
体積の積分は前 54〜68 ms(6 回とも)→ 後 0。親の積分は GIL を握るので、その間ほかの要求も待つ。

### 2.3 合わせた効果

ベンチ(`--part big`、前後を交互に 3 回ずつ。ms):

| 項目 | 前 1回目 | 前 中央値 | 後 1回目 | 後 中央値 | 前の値 | 後の値 |
|---|---:|---:|---:|---:|---|---|
| preview | 15,263 | 14,772 | 15,757 | 15,276 | 15263, 14499, 14772 | 15757, 15276, 14434 |
| confirm | 36 | 32 | 30 | 23 | 36, 32, 22 | 30, 23, 22 |
| push | 52 | 50 | 32 | 33 | 52, 50, 32 | 32, 33, 33 |
| **mcp** | 8,045 | **8,045** | 6,841 | **6,841**(−15%) | 8045, 8840, 7722 | 6841, 6633, 7538 |
| mcp_push | 7,187 | 7,187 | 7,460 | 7,460 | 7187, 7805, 7097 | 7460, 7597, 7401 |
| undo | 5 | 6 | 5 | 5 | 5, 16, 6 | 5, 5, 5 |
| redo | 6 | 6 | 6 | 6 | 6, 13, 5 | 6, 6, 12 |
| preview_bytes | 3,240,436 | 3,240,432 | 3,240,432 | 3,240,432 | | |
| mcp_bytes / push_bytes | 3,222,016 / 3,221,084 | | 3,222,016 / 3,221,084 | | | |

- ゴーストと MCP の後の配信は、ベンチでは縮まらなかった(前後の幅が重なる)。どちらも親の三角形分割が約 7 秒を占め、その揺れ(±0.4 秒)が
  子の短縮(約 0.8 秒)より大きい。三角形分割と配信のバイト数は前後で同じ。
- 三角形分割を既定の解像度にして親の側を分けて測ると(`prof_parent2.py`、交互に 3 回。ms):

| 項目 | 前 | 後 | 前の値 | 後の値 |
|---|---:|---:|---|---|
| ゴースト | 10,447 | **9,447** | 10447, 10298, 11572 | 9804, 9447, 8652 |
|  うち子の待ち | 8,192 | **6,852** | 8192, 7934, 9125 | 6888, 6852, 6321 |
|  うち三角形分割 | 2,266 | 2,546 | 2145, 2266, 2307 | 2864, 2546, 2288 |
|  うち体積 | 62 | **0** | 62, 54, 62 | 0, 0, 0 |
| MCP の確定 | 7,278 | **6,671** | 8257, 7027, 7278 | 7072, 6671, 6138 |
| その後の配信 | 2,324 | 2,607 | 2738, 2297, 2324 | 2607, 2667, 2141 |
|  うち三角形分割 | 2,216 | 2,558 | 2625, 2202, 2216 | 2558, 2623, 2100 |
|  うち体積 | 57 | **0** | 68, 54, 57 | 0, 0, 0 |

- 冷えた状態と温まった状態: ベンチの各回は別のプロセスで、MCP の確定がそのプロセスの最初の面の編集(1回目の値は表の「1回目」)。
  1回目と2回目以降で差の向きは変わらない(MCP は 3 回とも後が速い)。
- メモリ: 測っていない。足したのは検査の間だけ面ごとに持つ探索器と周期の値で、呼び出しをまたいで覚えるものは無い。

## 3. 入れなかった案

| 案 | 測った値 | 入れなかった理由 |
|---|---|---|
| 分類を多角形の分類(`BRepTopAdaptor_FClass2d`)に替える | 内側の格子で 5〜9 倍速い(big の 81,965 点で 1.62 → 0.33 秒、食い違い 0)。辺の近くと範囲の外の点では食い違う: holes_rim 10,584 点中 1,104(今が外・多角形が内 816 など)、big 30,456 点中 990、corners 3,720 点中 32 | 判定が変わる(検査を緩める・変える)。この枝の低リスクの枠の外 |
| 「写しの面」は範囲の検査を省く(RemoveFeatures が Modified と言うが、曲面が同じ物で、辺の2D曲線・範囲・許容差・向きが元と同じ面) | big で 455 面中 78 面が当たり、検査 0.41 秒ぶん | 射影が見つからない・周期の境目といった、今は断る場合を通すことになりうる(検査を緩める) |
| 写しの面の三角形を借りる(子が返す「入力のまま残った面」に足す) | big の三角形分割 2.14 → 2.00 秒 | 効果が小さい(本当に変わった穴の多い平面2枚が大半)。`kernel_worker.py`・`view.py` の担当(C3)で、面の同一性の決めごとにも関わる |
| 射影 `GeomAPI_ProjectPointOnSurf` を面ごとに作って点ごとに `Perform` | big で 2.2 → 0.6 µs × 2.2 万回 ≒ 35 ms | 効果が測れる大きさでない。B-spline の面では大きいかもしれないが実データで測っていない。作り方で許容差の既定(`Precision::Confusion` と `PConfusion`)が変わるので、同じ答えと言うには許容差を合わせる確認が要る |
| 近傍だけ切り出して消す(docs/FACE_EDIT.md の試作) | 試作で丸め8枚 4.8 → 1.5 秒 | 由来の表と面の並びを作り直すことになり、番号を変えうる |
| 範囲の検査を並列にする | — | OCP の呼び出しは GIL を握るのでスレッドでは並ばない。プロセスは本人の Windows に fork が無く、立ち上げ約 1.6 秒と形の受け渡しが要る |
| 検査の順を変えて早く断る | — | 断りの文(原因)が変わる。探索・検査の順を変えるものは枠の外 |
| RemoveFeatures の設定(OBB・あいまいさ) | docs/FACE_EDIT.md で揺れの範囲・内部の MakerVolume に渡らない | 試し直していない |

範囲の外で気づいたこと(変えていない):

- `kernel_worker.py` の冒頭の説明は「フィレット・面取りは "volume_before"・"volume_after" も」と書いている。2.2 で面の編集も
  `volume_after` を返すので、取り込むときに説明を直すとよい(このファイルは担当の外)。
- 削除・オフセットの後の配信の大半は、RemoveFeatures が辺を作り直した面の三角形分割で、とくに穴の多い平面(C1 の範囲)。

## 4. テスト

- 全件: `PATH=/opt/node24/bin:$PATH uv run pytest -q -p no:cacheprovider`(Node 24.22.1。`npm ci --prefix web` の後)→
  **2,793 通過・5 失敗・5 skip(198 秒)**。5 件は `kernel_face_edit.py` を基準(4ef67570)の中身に戻しても同じ5件が同じく落ちる
  (この Linux の環境によるもの。どれも面の編集を呼ばない):
  - `tests/test_channel.py` の3件: 自己更新は Windows だけなので 501 を返し、テストは 409 を待つ
  - `tests/test_web_scripts.py::test_start_server_stops_the_python_server_when_the_start_fails`: `.venv/Scripts/python.exe`(Windows の
    venv の場所)を探して見つからない(STATUS.md に同じ1件の記録がある)
  - `tests/test_loft.py::test_a_smooth_loft_that_pinches_between_sections_is_rejected`: 断面の間で潰れるロフトを断らない。基準でも
    同じで、原因は調べていない(ロフトは担当の外)
- 足したテスト: `tests/test_face_edit_classifier.py`(2件)、`tests/test_face_edit_volume.py`(1件。`d0838c2` を外すと落ちる)。
- 面の削除・オフセットのテスト 9 本を、変更前後の関数を突き合わせる仕掛けで走らせて食い違い 0(236 通過。`npm ci` の前だったので
  `test_protocol.py::test_the_web_code_type_checks_against_the_declaration` だけ TypeScript が無くて落ちた。変更とは関係ない)。

## 5. 親に確かめてほしいこと

- 保存済み 206 文書の再生の指紋(`tools/replay_fingerprint.py`)が変更前と一致すること。合成の形では分類の答えも結果のバイト列も同じだったが、
  本人の実データ(Fusion の STEP の B-spline の丸め・継ぎ面)では試していない。
- QR1 のカバー・別部品で、丸めらしい面の1クリック(docs/FACE_EDIT.md「入れたもの」の 52・92 クリック)の通る・断る・代案が前と同じこと
  (カバー 17・別部品 30)と、そのときの時間。
- 本人の機械(Windows・14 コア)で、`OCP.BRepClass.BRepClass_FaceExplorer` が読み込めること(同じ OCP 7.9.3 なら出ているはず)と、
  削除のゴースト・MCP の確定の前後。RemoveFeatures と BRepCheck はコアが多いほど縮むので、範囲の検査の割合はここより大きいはず。
- ブラウザでの体感(ゴースト・Enter)は確かめていない。

## 再現

```bash
git fetch origin develop cloud/perf-faceedit
uv sync --frozen
npm ci --prefix web
uv run python tools/bench_all_ops.py --docs part --part big --only _faces --out /tmp/after.json
```

前の値は `git archive 4ef67570 duocad tools | tar -x -C <別フォルダ>` で書き出し、**そのフォルダを作業フォルダにして**
`uv run --project <このリポジトリ> python tools/bench_all_ops.py …` で走らせた(子プロセスは作業フォルダの `duocad` を読む)。

使った台本(セッションの使い捨て。リポジトリには置いていない。要点だけ):

- `make_shapes.py`: 上の反例の形を BRep に書き出す(`bench_all_ops.build_part`、OCP の箱・円柱・フィレット、`kernel_face_edit._as_bspline`)
- `ab_compute.py <brep> <verb> <faces> [distance] --rounds 3`: 変更前の `kernel_face_edit.py` を `importlib` で別名
  (`duocad.kernel_face_edit_base`)に読み込み、同じプロセスで `compute` を交互に走らせて、時間・結果の BinTools の SHA-1・由来を比べる
- `ab_region.py <brep> <faces>`: 変更後で1回削除して `_region_kept` に渡る (面, 像) を控え、前後の `_region_kept` を交互に測る
- `equiv_classify.py <brep>...`: 前後の `_interior_points`(数 1・9・49)・`_points_from_mesh`・`_point_on_face` を全部の面で突き合わせる
- `dualrun/sitecustomize.py`: 環境変数があるとき `duocad.kernel_face_edit` の読み込み直後に3つの関数を「前後を両方呼んで比べる」ものに
  差し替える(`PYTHONPATH` で子プロセスにも入る)
- `prof_parent2.py mcp|preview`: `Session` で部品を作り、穴の削除の親の側を cProfile で子の待ち・三角形分割・体積に分ける

### 生の値

`ab_compute.py`(秒):

```json
{"brep": "small.brep", "verb": "delete_faces", "faces": [35], "old_s": [0.531, 0.601, 0.591], "new_s": [0.479, 0.48, 0.451], "same_result": true, "outcome": "refused"}
{"brep": "corners.brep", "verb": "delete_faces", "faces": [3, 6, 9], "old_s": [0.54, 0.582, 0.578], "new_s": [0.531, 0.65, 0.597], "same_result": true, "outcome": "ok", "sha1": "d64fc87d6be1746d534ae4bb6a5d1cc80782d17b"}
{"brep": "corners.brep", "verb": "delete_faces", "faces": [3], "old_s": [0.053, 0.037, 0.036], "new_s": [0.046, 0.042, 0.046], "same_result": true, "outcome": "refused"}
{"brep": "holes_rim.brep", "verb": "delete_faces", "faces": [2, 30], "old_s": [0.554, 0.529, 0.427], "new_s": [0.337, 0.344, 0.4], "same_result": true, "outcome": "ok", "sha1": "419cf2001da9db538f327cc63b2b117afaf5efa1"}
{"brep": "holes_rim.brep", "verb": "delete_faces", "faces": [2], "old_s": [0.26, 0.259, 0.237], "new_s": [0.239, 0.209, 0.213], "same_result": true, "outcome": "ok", "sha1": "82508191fba940e97ff447131580e9d0a1f72cd5"}
{"brep": "big.brep", "verb": "delete_faces", "faces": [395], "old_s": [7.879, 8.108, 7.927], "new_s": [7.315, 7.127, 6.904], "same_result": true, "outcome": "ok", "sha1": "ddb9beb6876063fe75f031f82e32da10f9d79146"}
{"brep": "big.brep", "verb": "offset_faces", "faces": [1], "distance": 0.3, "old_s": [13.889, 14.314, 14.732], "new_s": [13.151, 13.411, 14.152], "same_result": true, "outcome": "refused"}
```

`ab_region.py`(秒):

```json
{"brep": "big.brep", "faces": "395", "checked_faces": 455, "old_s": [3.283, 3.688, 3.74], "new_s": [2.617, 3.054, 2.511], "same_answers": true}
{"brep": "holes_rim.brep", "faces": "2,30", "checked_faces": 52, "old_s": [0.252, 0.291, 0.303], "new_s": [0.194, 0.215, 0.242], "same_answers": true}
{"brep": "corners.brep", "faces": "3,6,9", "checked_faces": 14, "old_s": [0.205, 0.15, 0.181], "new_s": [0.153, 0.141, 0.174], "same_answers": true}
```

`prof_parent2.py`(ms):

```json
{"n": 1, "repo": "base", "apply": 8257, "push": 2738, "child_wait": 8228, "tessellate": 2625, "volume": 68}
{"n": 1, "repo": "new", "apply": 7072, "push": 2607, "child_wait": 7050, "tessellate": 2558, "volume": 0}
{"n": 1, "repo": "base", "preview": 10447, "child_wait": 8192, "tessellate": 2145, "volume": 62}
{"n": 1, "repo": "new", "preview": 9804, "child_wait": 6888, "tessellate": 2864, "volume": 0}
{"n": 2, "repo": "base", "apply": 7027, "push": 2297, "child_wait": 7007, "tessellate": 2202, "volume": 54}
{"n": 2, "repo": "new", "apply": 6671, "push": 2667, "child_wait": 6645, "tessellate": 2623, "volume": 0}
{"n": 2, "repo": "base", "preview": 10298, "child_wait": 7934, "tessellate": 2266, "volume": 54}
{"n": 2, "repo": "new", "preview": 9447, "child_wait": 6852, "tessellate": 2546, "volume": 0}
{"n": 3, "repo": "base", "apply": 7278, "push": 2324, "child_wait": 7258, "tessellate": 2216, "volume": 57}
{"n": 3, "repo": "new", "apply": 6138, "push": 2141, "child_wait": 6118, "tessellate": 2100, "volume": 0}
{"n": 3, "repo": "base", "preview": 11572, "child_wait": 9125, "tessellate": 2307, "volume": 62}
{"n": 3, "repo": "new", "preview": 8652, "child_wait": 6321, "tessellate": 2288, "volume": 0}
```

cProfile の上位(変更後、同じ削除。7.07 秒): `BOPAlgo.Perform` 3.33、`_region_kept` 2.69(うち OCCT の分類そのもの 2.06)、
`_structural_gate` 0.99(BRepCheck 0.86)。範囲の検査に残っているのは OCCT の分類そのもので、同じ答えのまま削れる所はもう小さい。
