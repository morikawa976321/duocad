# 報告: ねじ(`thread`)— 担当 `thread`

- ブランチ `cloud/perf-thread`(基準 develop `4ef67570`)。書いたのは `duocad/kernel_thread.py` と、この枝のテスト2本
  (`tests/test_thread_reuse.py`・`tests/test_thread_volume_gate.py`)だけ。`verbs/thread.py` は変えていない。
- 機械: クラウドの Linux、4 コア、メモリ 15 GB、Python 3.14.6(uv)、OCP 7.9.3。本人の機械より遅い(`--part big` のゴーストが
  ブリーフの 7.5 秒に対してここで 11〜12 秒)。基準と変更後を交互に3回ずつ測り、中央値を使った。値は ±1〜2 割揺れる。

## 要約

`tools/bench_all_ops.py --docs part --only thread`、中央値(ms):

| | `--part big` 前 | 後 | `--part small` 前 | 後 | 効いたコミット |
|---|---|---|---|---|---|
| redo | 3,801 | **6** | 4,216 | **1** | de51f7d |
| redo の後の配信 | 236 | **24** | 201 | **3** | de51f7d |
| MCP の確定 | 4,534 | **3,627** | 4,260 | 3,994(揺れの範囲) | 89abcde |
| ゴースト | 12,468 | **11,244** | 4,258 | 4,209(揺れの範囲) | 89abcde |
| 確定(Enter) / 確定後の配信 / undo | 22 / 25 / 7 | 23 / 27 / 6 | 4 / 3 / 1 | 5 / 4 / 1 | — |

- **redo の 2.2 秒の理由**: 画面・MCP はピッチ(と多くは隙間)を空欄のまま `kernel_thread.thread_face` に渡し、ログには解決後の数値を残す。
  redo・再生はその数値で呼ぶので `kernel.remembered` の鍵 `(面, 山形, 隙間, ピッチ)` が `(…, None, None)` と食い違い、undo で戻した
  同じ形への redo が毎回ねじを計算し直していた(ほかの verb は確定とログで同じ引数を渡すので当たる)。→ 同じ結果を解決後の数値の鍵でも
  覚えた(de51f7d)。
- **ブーリアン2回の外**(`--part big` の MCP の確定で約 2.3 秒): 体積の Eps 付きの積分 3 回 0.85 秒(前の形 0.3・道具 0.06・結果 0.47)、
  結果の体積(既定の積分)0.55 秒、結果の BRepCheck 0.35 秒、入口の面取り 0.42 秒(うち面取りの結果の BRepCheck 0.35 秒)、
  ねじ部を面から組む 0.07 秒、ほか 0.03 秒。→ 体積の門の積分を、前後の片方にしか無い面だけにした(89abcde。0.76 → 0.16 秒)。
- **一番大きい残りは「引く」ブーリアン**(雌: 道具のらせんの面と埋めた本体)。小さい部品で 3.4 秒、大きい部品で 1.7 秒で、部品の面の数
  よりらせんの面との交わりで決まる。OCCT の並列モード(`SetRunParallel`)でねじ全体が 4 コアでも 30〜50% 縮み、7 通りの形で結果の
  指紋(面・辺の並び)も一致したが、ブーリアンの共通の組み立て(枝 `boolean`)と W3 の判断にかかわるので入れずに案にした(3 章の 1)。
- **ゴーストの残りの大半は三角形分割**: 入口の面取りで縁が変わる上下の板 2 面(穴 384)を測り直す(この機械で 1 面 1.7〜2.0 秒)。
  らせんの面は 1 面 20〜45 ms。面の分割は C1 の担当。
- 形・面と辺の番号・保存形式・操作ログ・三角形の並べ方は変えていない(4 章)。

## 1. 測った内訳(前)

### 1.1 全体(`bench_all_ops.py`、基準 4ef67570、生の値)

`--part big`(ms):

| 回 | preview | confirm | push | mcp | mcp_push | undo | undo_push | redo | redo_push |
|---|---|---|---|---|---|---|---|---|---|
| r1 | 12,598 | 20 | 25 | 4,641 | 7,835 | 6 | 37 | 3,974 | 285 |
| r2 | 12,468 | 26 | 26 | 4,534 | 7,843 | 7 | 31 | 3,801 | 236 |
| r3 | 11,286 | 22 | 25 | 4,293 | 7,580 | 7 | 38 | 3,749 | 228 |

`--part small`(ms):

| 回 | preview | confirm | push | mcp | mcp_push | undo | undo_push | redo | redo_push |
|---|---|---|---|---|---|---|---|---|---|
| r1 | 4,513 | 4 | 4 | 4,458 | 250 | 1 | 2 | 4,264 | 200 |
| r2 | 3,983 | 4 | 3 | 4,189 | 211 | 1 | 2 | 3,851 | 201 |
| r3 | 4,258 | 4 | 3 | 4,260 | 230 | 2 | 2 | 4,216 | 251 |

- redo は MCP の確定とほぼ同じ時間 = 丸ごと計算し直している。成功・失敗: 全回成功(error なし)。
- 配信の大きさ(big): ゴースト 3,405,484 B、確定後 1,668,720 B、MCP の後 1,669,644 B、redo の後 1,669,648 B。最大常駐メモリ(ベンチの
  プロセス全体。カメラのズームを含む): big 5.6〜5.9 GB、small 0.64 GB。前後で変わらない。

### 1.2 cProfile(基準、`--part big`、1 ケース)

MCP の確定 4.16 秒:

| 何 | 秒 |
|---|---|
| `BRepAlgoAPI` の `Build`(2 回: 埋める・引く) | 1.82 |
| `BRepGProp.VolumeProperties_s`(Eps 付き 3 回 + 既定 1 回) | 0.89 |
| `brep_checks._analyzer_valid`(2 回: 面取りの結果・最後の結果) | 0.72 |
| `brep_geometry._integrated_volume_props`(結果の体積。既定の積分) | 0.58 |
| `kernel.chamfer_edges`(入口の面取り。上の検査 1 回を含む) | 0.43 |
| `threaded_segment`(ねじ部を面から組む) | 0.07 |
| `describe_target` / `_provenance` | 0.02 / 0.01 |

ゴースト 12.35 秒 = `tessellate_reusing` 8.56(うち `IMeshTools.DiscretizeFaces` 7.80、借りられない 13 面)+ ねじの計算 3.79。

### 1.3 段ごと(カーネル単体。BREP に書き出した形で `_thread_face` を3回、2回目以降は温まった状態)

雌: small(穴 D6、P1.5)・big(穴 D2.475、P1.2)、反例(ねじに合わせて作った形): pin(穴 384 の板に立てた軸 D6×12、雄)・
deep(厚さ 40 の板の D8 の貫通穴、雌・長い)。基準の2回目の値(ms):

| 形 | 全体 | 引く/足す(らせん) | 埋める/切る | 端の common | Eps 積分(門) | 結果の体積 | BRepCheck | 面取り |
|---|---|---|---|---|---|---|---|---|
| small | 4,223 | 3,529 | 32 | — | 26 + 56 + 113 | 355 | 18 + 15 | 27 |
| big | 3,866 | 1,558 | 164 | — | 310 + 52 + 456 | 500 | 311 + 349 | 378 |
| pin | 6,045 | 2,053 | 115 + 155 | 1,114 | 524 + 1 + 309 + 717 | 643 | 269 | — |
| deep | 3,266 | 2,253 | 10 | — | 1 + 208 + 276 | 373 | 1 + 5 | 3 |

- 「引く」の時間は部品の面の数で決まらない(485 面の big より 59 面の small が遅い。small のほうが径が大きくピッチが粗い)。
  道具のらせんの B スプライン面(何周も巻いた (3,1) 次の面 5 枚)と、埋めた本体の円板・円錐の交わりが重い。
- 面の分割(big、全体表示の偏差 0.12): ねじで新しくなる 13 面のうち、板 2 面が 2,034 / 1,674 ms、らせんの面 20〜43 ms、ほか 4〜7 ms。

## 2. 入れた変更

### de51f7d perf(thread): 空欄の隙間・ピッチで刻んだねじの結果を、解決後の数値の鍵でも覚える

- 何を: `thread_face` が求めた結果を、空欄の鍵 `(面, 山形, None, None)` に加えて解決後の数値の鍵 `(面, 山形, 隙間, ピッチ)` でも
  `kernel.remembered` に覚える。
- なぜ: 1.1 のとおり redo が毎回計算し直していた。
- 前後(中央値): redo big 3,801 → **6** ms、small 4,216 → **1** ms。redo の後の配信 big 236 → **24** ms(確定と同じ形の物に戻るので、
  確定のときに測った三角形をそのまま使う。配信の大きさは確定後と同じ 1,668,720 B)。
- 形を変えないと言える根拠: `_thread_face` は空欄を `resolve` で既定に埋めてから先は解決後の数値しか使わない(山形・面・形は同じ)。
  `tests/test_thread_reuse.py` が、空欄で計算したねじと解決後の数値で計算し直したねじの由来・数値・面の数・体積が一致することと、
  undo → redo で計算が 1 回だけ・確定と同じ形の物に戻ることを固定する。プレビューの写しの上で覚えた鍵は `kernel.adopt_results` が
  ほかの鍵と同じく元の形へ付け替える。覚える数は 1 件増える(`_RESULTS_LIMIT` 8 の中。形は同じ物を指すのでメモリは増えない)。

### 89abcde perf(thread): 体積の門の増減を、前後の片方にしか無い面だけの積分で求める

- 何を: 体積の門(増減が期待値とねじ部の体積の 1% 以内)の「増減」を、前の形と結果の形を Eps 付きで丸ごと積分した差から、
  前後の片方にしか無い面(TShape・位置・向きで比べる)だけの積分に替えた(`_volume_change`)。基準点は前後とも結果の形の頂点の平均
  (OCCT 7.9 の `VolumeProperties_s` が丸ごと積分するときの点 `roughBaryCenter`)。面ごとの寄与は `BRepGProp_Vinert` で求め、素の
  足し算で足す。
- なぜ: 集合演算が触れなかった面(big では 495 面中 484 面。穴 384 の板 2 面は 1 面 0.1 秒かかる)を前後で2回積分していた。
- 前後(段ごとの中央値。門の積分の時間 / ねじ全体):

  | 形 | 門の積分 前 → 後 | ねじ全体(2回目以降) 前 → 後 |
  |---|---|---|
  | big | 760 → **161** | 4,033 → **3,412** |
  | pin(雄・反例) | 1,085 → **632** | 5,956 → **5,468** |
  | small | 133 → 87 | 4,099 → 3,758(揺れの範囲) |
  | deep(長い穴・反例) | 256 → 226 | 3,453 → 3,432 |
  | 長い軸 D6×L60(雄。門の積分だけ5回) | 2,490 → 2,435 | — |

  `bench_all_ops` の MCP の確定は big 4,534 → 3,627(差 0.9 秒のうち門の短縮が 0.6 秒、残りは揺れ)、ゴースト 12,468 → 11,244。
- 形を変えないと言える根拠: 変えたのは門の検査の値の求め方だけで、集合演算・形・由来には触れない。7 通りの形(small の穴 2 つ・big・pin・
  deep・20mm 立方体の穴・D6 の軸)で結果の指紋(添字順の面の種類・面積・重心・向き、辺の長さ、体積、由来、警告)が変更前とバイト単位で一致。
- 門の値が変わらないと言える根拠: 片方にしか無い面の組は閉じているので、前後を同じ基準点で測れば増減は基準点に依らない。面ごとに足した値は
  `VolumeProperties_s` の丸ごとの値とビット単位で一致する(テストで固定)。丸ごとの差との違いは 8 通りの形で 1e-8 mm³ 未満
  (最大は small の 3.1e-9、長い軸 D6×L60 で 0、D10×L60 で 1.4e-12)、門の許容は 0.3 mm³ 以上なので、通す・断るは変わらない。
- 境界の無い面(`NbChildren() == 0`)が片方にしか無い面に混じったら、丸ごとの差に戻す(OCCT の Eps 付きの積分はその面に直前の面の
  境界を使い回すので、面ごとに測ると値が食い違う)。ねじの結果には現れないが、値を変えないための逃げ道。
- 試して捨てた作り: 片方にしか無い面を 1 つの compound にまとめて積分する。基準点が変わってらせんの面の適応積分の収束が変わり、
  長い軸で 1.8 → 2.4 秒と遅くなり、増減も丸ごとの差から 8.5e-3 mm³ ずれた。

## 3. 入れなかった案

| # | 案 | 測った値 | 入れなかった理由 |
|---|---|---|---|
| 1 | ねじのブーリアンを並列モード(`SetRunParallel(True)`)で走らせる | 下の表。ねじ全体 30〜50% 減 | ブーリアンの共通の組み立て(`brep_operations.build_boolean`、枝 `boolean`)の変更で、W3(並列ブーリアン)は面の並びの危険で保留の判断がある。並びの一致は 7 通りでしか見ていない |
| 2 | `SetUseOBB(True)` | 引く: small 3.0 → 3.07 秒・big 1.8 → 1.8 秒(変わらず)、埋める: big 164 → 139 ms | 効果がほぼ無い(前回の 8% と同じ傾向) |
| 3 | `SetCheckInverted(False)` | 埋める: big 164 → 107 ms、引く: 変わらず | 0.06 秒。反転した立体を渡したときの意味が変わる。共通の組み立て |
| 4 | `SetNonDestructive(True)` | 変わらず | 効果なし |
| 5 | 入口の面取りの結果の BRepCheck を省く(最後の結果だけ検査する) | big で 0.35 秒 | `kernel.chamfer_edges` の中(範囲外)。省くと、面取りが壊れても最後の形が妥当なら通るようになり、断る範囲が変わる |
| 6 | 結果の体積(既定の積分)を求めない | big で 0.5〜0.6 秒 | 画面の配りもの(`query.volume`)が必ず使い、形の同一性で覚えて使い回すので、求める場所が移るだけ |
| 7 | ねじ部の体積(門の許容と期待値)と門の増減で、らせんの面を2回積分しているのを1回にする | 50〜500 ms(pin で大) | 1回にするには許容(ねじ部の体積の 1%)を別の値から求めることになり、門の値が変わる |
| 8 | 板の面の分割を速くする(ゴーストの 8.5 秒の大半) | 1 面 1.7〜2.0 秒 | 面の分割は C1(`kernel_meshing.py`)の担当。入口の面取りで板の縁が変わるのは形そのものなので、ねじの側からは避けられない |
| 9 | 局所だけ切り出して足し戻す | — | 前回の判断どおり、面の並びを変えうる |
| 10 | `describe_target` で辺の祖先の表を両端で2回作るのを1回にする | 約 5 ms | 効果が小さい |

案 1 の測定(カーネル単体、2回目以降の中央値、ms。`build_boolean` に `SetRunParallel(True)` を足した写しで測った):

| 形 | ねじ全体 今 → 並列 | ブーリアンの合計 今 → 並列 | 指紋 |
|---|---|---|---|
| small | 3,744 → **1,862** | 3,280 → 1,320 | 一致 |
| big | 3,546 → **2,525** | 1,896 → 899 | 一致 |
| pin | 5,469 → **3,810** | 3,355 → 1,558 | 一致 |
| deep | 3,377 → **2,297** | 2,446 → 1,354 | 一致 |

7 通りの形(2 章と同じ)で、並列の結果の指紋が基準とバイト単位で一致した。コアの多い本人の機械ではもっと縮むはず。ねじだけに限って入れるなら、
`build_boolean` に引数を足して `kernel_thread` の呼び出し 8 か所(雌の埋める・引く、雄の端の common・空きの処理 3 回・切る・足す)だけで渡す形が狭い:

```diff
 def build_boolean(kind: str, first: TopoDS_Shape, second: TopoDS_Shape, fuzzy: float = 0.0, parallel: bool = False):
     ...
     if fuzzy:
         builder.SetFuzzyValue(fuzzy)
+    if parallel:
+        builder.SetRunParallel(True)
     builder.Build()
```

確かめ方の案: 本人の文書群のうちねじを含むものを、並列あり・なしで `tools/replay_fingerprint.py` にかけて一致を見る。OCCT の並列モードは
交わりの組ごとの計算を並べて、結果は添字の順に集めるので並びは変わらない作りだが、保証の文書は確認していない。

## 4. テスト

- 全件: `uv run pytest -q -p no:cacheprovider`(Node 24.22.2 を入れ、`npm ci --prefix web` の後)→ **2,796 passed, 5 failed, 5 skipped**。
  落ちた 5 件は、同じチェックアウトで `duocad/` を基準 4ef67570 に戻しても同じように落ちる(環境由来):
  - `tests/test_channel.py` の3件(期待 409 に対して 501)
  - `tests/test_web_scripts.py::test_start_server_stops_the_python_server_when_the_start_fails`(Windows の `.venv/Scripts/python.exe` を探す)
  - `tests/test_loft.py::test_a_smooth_loft_that_pinches_between_sections_is_rejected`(DID NOT RAISE。ねじと無関係)
- 基準の全件(Node 22・`web/node_modules` 無し・git でない書き出し)は 2,755 passed / 40 failed で、40 件は Node 22 が .ts を読めない・
  `three` が無い・git でないことによるもの。Node 24 では `.ts` の評価器(`test_ts_evaluators.py`・`test_ghost_agreement.py` ほか)は全部通った。
- ねじまわり: `tests/test_thread.py`・`test_boundaries.py`・`test_thread_sim_study.py`・新しい2本 → 96 passed。
- コミットを1つずつ基準に載せて: de51f7d だけで `test_thread_reuse.py` + `test_thread.py` 37 passed、89abcde だけ(基準へ cherry-pick)で
  `test_thread_volume_gate.py` + `test_thread.py` 37 passed。どちらも単独で採れる。
- 足したテスト(redo のテストは変更前のコードで 2 件落ちることを確かめた):
  - `tests/test_thread_reuse.py`: 空欄の隙間で刻んだねじ(雌・雄)の undo → redo が計算し直さず確定と同じ形の物に戻る(変更前は 2 件落ちる)/
    空欄と解決後の数値で計算したねじが同じ。
  - `tests/test_thread_volume_gate.py`: 雌・雄で門の増減が丸ごとの差と 1e-6 で合い、面ごとに足した値が丸ごとの値とビット単位で一致 /
    箱から円柱を引いた差・共有する面の無い形どうし・同じ形どうし。
- 結果の指紋(7 通り、基準・変更後・並列の案): 一致。ゴーストの中身(small、ベンチと同じ順): 面ごとの三角形・体積・面の数が一致
  (`boundary` の診断に入る段ごとの秒だけが違い、配信の大きさが 4 バイト揺れるのはそのため)。三角形の並べ方を変える変更は無い。
- 走らせられなかったもの: 本人の実データ(QR1 の STEP)、保存済みの文書群、実機のブラウザ(このクラウドに無い)。

## 5. 親に確かめてほしいこと

1. 本人の部品(QR1)で、ねじ(穴に刻む・軸に刻む)のゴースト・MCP の確定・redo の前後。合成の部品の改善率(門の積分は面の多い板で効く)は
   そのまま当てはまらない。redo は ほぼ 0 になるはず。
2. ねじを含む保存済みの文書の再生の指紋(`tools/replay_fingerprint.py`)が変わらないこと。変えたのは門の値の求め方と覚える鍵だけで、
   形は変わらない想定。門の値は丸ごとの差と 1e-8 mm³ 未満の違いなので、通る・断るが変わるのは門の許容ちょうどの形だけのはず。
3. Windows の OCP で `tests/test_thread_volume_gate.py` の「面ごとに足した値 == 丸ごとの値」(ビット単位の一致)が通ること。
   落ちたら OCCT の積分の基準点の決め方がこの版と違う(門の値は合っていても、遅くなる形がありうる)。
4. 実機のブラウザで、ねじを刻む → undo → redo の画面が確定と同じ形になること(redo は確定と同じ形の物を使い、三角形も確定のときのもの)。
5. 案 1(並列ブーリアン)を採るかどうか。採るならねじを含む文書群の再生の指紋で並びの一致を確かめてから。

## 再現

```bash
git fetch origin cloud/perf-thread develop
```

```bash
git checkout cloud/perf-thread && uv sync --frozen
```

全体(基準は `git archive 4ef67570 duocad tools | tar -x -C <基準の置き場>` で書き出し、その `tools/bench_all_ops.py` で同じ引数):

```bash
uv run python tools/bench_all_ops.py --docs part --part big --only thread --out /tmp/thread_big.json
```

```bash
uv run python tools/bench_all_ops.py --docs part --part small --only thread --out /tmp/thread_small.json
```

テスト:

```bash
uv run pytest -q -p no:cacheprovider tests/test_thread.py tests/test_thread_reuse.py tests/test_thread_volume_gate.py
```

段ごとの時間は付録の `stages.py`(BREP に書き出した形を読み、`_thread_face` の中の関数を時計で包む)。形は `build_part` の部品を
`BRepTools.Write_s` で書き出し、穴は `faces_of_kind(.., "cylinder_full")` の最後(small は面 35、big は面 395)。pin は big に
`BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 1.5, 8), Z), 3, 12)` を足したもの(面 1 が軸)、deep は 40mm 立方体から D8 の円柱を
引いたもの(面 7 が穴)。

## 付録: stages.py

```python
"""kernel_thread._thread_face の段ごとの時間。usage: stages.py <repo> <brep> <face> [repeat] [profile]"""
import sys, time, functools
from pathlib import Path
repo = Path(sys.argv[1]); sys.path.insert(0, str(repo))
from OCP.BRepTools import BRepTools
from OCP.BRep import BRep_Builder
from OCP.TopoDS import TopoDS_Shape
from duocad import kernel, kernel_thread, brep_operations, brep_geometry, brep_checks
shape = TopoDS_Shape(); BRepTools.Read_s(shape, sys.argv[2], BRep_Builder())
face = int(sys.argv[3]); repeat = int(sys.argv[4]) if len(sys.argv) > 4 else 3
PROFILE = sys.argv[5] if len(sys.argv) > 5 else "trapezoid30"
log = []
def timed(mod, name):
    orig = getattr(mod, name)
    @functools.wraps(orig)
    def wrapper(*a, **k):
        t0 = time.perf_counter()
        try:
            return orig(*a, **k)
        finally:
            log.append((name, (time.perf_counter() - t0) * 1000, a[0] if name == "build_boolean" else ""))
    setattr(mod, name, wrapper)
for mod, name in ((brep_operations, "build_boolean"), (brep_geometry, "_thread_volume_eps"), (brep_geometry, "volume"),
                  (brep_checks, "_require_valid"), (kernel, "chamfer_edges"), (kernel_thread, "threaded_segment"),
                  (kernel_thread, "describe_target"), (kernel_thread, "_provenance")):
    timed(mod, name)
if hasattr(kernel_thread, "_volume_change"):
    timed(kernel_thread, "_volume_change")
brep_geometry.volume(shape)  # ボディに入った形は体積を覚えている(ベンチと同じ)
for i in range(repeat):
    with kernel._RESULTS_LOCK:
        kernel._RESULTS.clear()
    log.clear()
    t0 = time.perf_counter()
    r = kernel_thread._thread_face(shape, face, PROFILE, None, None)
    total = (time.perf_counter() - t0) * 1000
    agg = {}
    for name, t, kind in log:
        agg.setdefault(f"{name}:{kind}" if kind else name, []).append(round(t))
    print(f"rep{i} total={total:.0f}  " + "  ".join(f"{k}={v}" for k, v in agg.items()), flush=True)
```
