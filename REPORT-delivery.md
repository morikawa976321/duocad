# 報告: 配信の三角形の使い回し(担当 `delivery`、ブランチ `cloud/perf-delivery`)

基準は develop `4ef67570`。前は `git archive 4ef67570` を書き出したコード、後は `a3be451`(下の 4 コミット)を書き出したコードで、
同じクラウドの機械(4 コア・15 GB、Python 3.14.6、OCP 7.9.3)で交互に測った。値は特記が無ければ 3 回の中央値。生の値は 6 章。

## 0. まとめ

`--part big` で、変わっていない面を測り直していた取りこぼしを 4 つ見つけた(1.2 の表)。うち 3 つと、鏡映の小さな数え直し 1 つを 4 コミットにした
(残る 1 つ、複製・鏡映で新しくできたボディの分は 3.1 の案)。**どれも `bench_all_ops.py` の既定では
出ない**: このベンチはカメラの最後が focus なし(全面を細かく)なので、面ごとに測り方が分かれない。本人の画面は常に `focus` を送り
(背を向けた平面・画面外の面を粗く)、そのときにだけ出る。そこで「上から見た画面」(下を向いた平面 73 面を粗く。穴 384 個の底面を含む)を
`focus` に与える計測(以下「上から」)を作って測った。

| 「上から」の操作(`--part big`) | 前 | 後 | 測った面の数(前 → 後) | 効いたコミット |
|---|---|---|---|---|
| ゴースト 2 回目・3 回目(半径を打ち直す) | 8,858 / 8,773 ms | **6,509 / 6,598 ms** | 78 → 5 | ③ |
| ゴースト 1 回目 | 8,729 ms | 8,842 ms | 78 → 78 | (変わらない。3.2 の案) |
| Enter の後の配信 | 100 ms | **29 ms** | 74 → 0 | ④ |
| ゴーストを 3 回出して確定した後の undo の配信 | 10,437 ms | **19 ms** | 485 → 0 | ① |
| MCP の確定(辺1本のフィレット)の後の配信 | 8,694 ms | **6,338 ms** | 62 → 5 | ④ |
| ゴーストを出した後の鏡映・複製の配信 | 8,088 / 7,809 ms | **93 / 68 ms** | 73 → 0 | ③(ゴーストが返した面を借りる) |
| ゴーストを出していない鏡映・複製の配信 | 8,002 / 8,019 ms | 7,774 / 8,000 ms | 73 → 73 | (変わらない。3.1 の案) |

focus の無い `bench_all_ops.py --part big` / `--part small` は、全ケースで前後の差が揺れの範囲(配った大きさは数バイトの差 = serial の桁だけで
同じ面を配っている)。鏡映の配信だけ 118 → 87 ms(②)。MCP の確定後の配信が後で 3〜8 % 遅く見えるケースがあるが、配った面とバイト数は
前と同じで、focus が無いので変えた経路(④の付け替え・③の返却)は動かない。後の 3 回は probe と交互に走らせたので、その揺れと見ている。成功・失敗の件数は前後とも big 63 / 6、small 60 / 9(失敗はどちらもシェルと
面のオフセットの断りで、前後で同じ)。

残りの大半は面の分割そのもの(穴の多い平面 1 枚を細かい側で数秒。担当 `meshing`)と、スケッチの領域の塗り(担当 `regions`)。

## 1. 測った内訳(前)

### 1.1 何を測ったか

- `bench_all_ops.py --docs part --part big`(3 回)と `--part small`(3 回)。
- 配信 1 回の内訳を関数ごとに測る計測(付録 A の `probe_delivery.py`。`state_meta`・`delta`・`compose`・`wire.encode` と、
  `kernel.tessellate_faces`・`transformed_face`・`face_keys`・`face_placements`・`is_closed_shell`・`bounding_box_of_faces`・
  `_borrow_faces`・`sketches_state`・`query.volume` の時間、測った面の数と測り方、配った三角形の数とバイト数)。題材は 3 つ:
  - 「上から」: `focus` = 下を向いていない面(412 面)を細かく、下を向いた平面 73 面を粗く(画面の背面の判定と同じ向きの選び方)
  - 「カメラ」: ベンチの最後と同じ粗さ(`world_per_pixel = 対角/1200`、focus なし)で移動・鏡映・読み込み
  - 既定の粗さ(0.2)で同じもの(1 回だけ。1.3 の数)
- 長い作業でのメモリ(付録 B の `long_session.py`。6.6)。

### 1.2 「上から」での取りこぼし(前)

| 経路 | 時間 | 測った面 | 何が起きていたか |
|---|---|---|---|
| ゴースト(毎回) | 8.5〜10.0 s | 78(細かい側) | 変わった面は 5。残りの 73 は画面が粗く測っている面で、元の形の控えに細かい側が無いので、ゴーストのたびに細かく測り直す(`Session.preview_job` の `lent` は細かい側の面だけ) |
| ゴースト 3 回 → 確定 → undo の配信 | 10.4 s | 485(全面) | ゴーストの結果を覚えさせる(`seed_faces`)たびに、ボディごと 3 つの控えを古い順に捨てる規則で、画面に出ている元の形の控えが追い出される |
| Enter の後の配信 | 100 ms | 74(粗い側) | 面の番号がずれたのに、画面の focus(元の形の番号)を新しい形の番号のまま当てる |
| MCP の確定の後の配信(辺1本のフィレット) | 8.7〜10.2 s | 62(細 33 + 粗 29) | 同上。変わった面は 5 なのに、番号のずれた残った面 57 を別の測り方で測る |
| 鏡映・複製の配信 | 7.5〜9.3 s | 73(細かい側) | 新しいボディは focus に居ない = 全面細かく。元のボディの粗い 73 面の細かい側が無い |

cProfile(前、「上から」の MCP の確定(辺1本のフィレット)の後の配信 1 回、8.74 s): `kernel.tessellate_faces` 8.71 s(組は細かい側と粗い側の 2 つ)の
うち OCCT の `IMeshTools.DiscretizeFaces` が 7.30 s(2 回)、`boundary_check`(境界の交差検査)0.88 s。分割の量そのものが時間で、取りこぼしは
「どの面を分割へ渡すか」(5 面で足りるのに 62 面)。

### 1.3 focus の無い配信の内訳(前)

既定の粗さ(0.2)・focus なし:

| 配信 | 合計 | 内訳 |
|---|---|---|
| 辺1本のフィレット(MCP)の後 | 1,580 ms | 面の分割 1,559(変わった 5 面。穴 384 の上面を含む)、借用 7、閉じた立体か 8、境界箱 5、符号化 2、`state_meta` 4 |
| その undo / redo | 18 / 12 ms | 測らない。変わった 4〜5 面だけ送る |

ベンチの最後の粗さ(0.037 mm)・focus なし:

| 配信 | 合計 | 内訳(ms) |
|---|---|---|
| 移動の後 | 69 | 面の三角形の変換 27、境界箱 11、符号化 14、借用の残り約 12(位置の鍵 2.5・同一性の鍵 1.7 ほか)、`state_meta` 4 |
| 移動の undo / redo | 67〜98 | 同上(undo・redo のたびに新しい `Moved` の形になり、同じ位置へ戻っても全面を変換し直す)。3.3 MB を毎回全部送る |
| 鏡映の後 | 85 | 変換 40、閉じた立体か 8、境界箱 10、符号化 14 |
| 読み込み(`open_document`) | 1,942〜2,153 | ファイル 0.9、ログの解釈 0.3、**再生 1,940**、`view.reset` 0.02 |
| 読み込み直後の配信 | 4,699〜5,414 | **スケッチの領域 2,518**(担当 `regions`)、**面の分割 2,153**(485 面、担当 `meshing`)、符号化 20、閉じた立体か 7、境界箱 5。全ボディの `copy_shape` は 16 ms |

`bench_all_ops.py --part big` の表(6.2)の確定後の配信 5.5〜8.5 s とゴースト 6〜16 s は、ほぼ全部が変わった面(穴の多い上面・底面)の分割。

## 2. 入れた変更

4 コミット。①② は三角形を変えない。③④ は三角形の出どころ・測り方の選び方が変わる(2.5 に三角形を使う箇所の洗い出し)ので、
別のコミットにしてある。どれも形・面と辺の番号・保存形式・操作ログに触らない(`verbs/`・`kernel*.py` を変えていない。変えたのは
`duocad/view.py` と `duocad/session.py` だけ)。

### ① `dad177b` ゴーストを出し直しても、画面に出ている形の面の三角形を追い出さない(`view._face_store`)

- 何を: 控えを追い出すとき、ボディの今の形は残し、配ったことの無い形(プレビューが覚えさせただけ)を先に捨てる。覚える数(3)は同じ。
- なぜ: 1.2 の 10.4 s。取り消してカメラを動かしたときも、画面の形を丸ごと測り直していた。
- 前後: 「上から」のゴースト 3 回 → 確定 → undo の配信 10,437 → **19 ms**。
- 形を変えないと言える根拠: どの控えを残すかだけで、三角形の中身・測り方・面の番号の扱いは同じ。

### ② `d14fe45` 複製・鏡映した形が閉じた立体かを、元の形の控えから引き継ぐ(`view._borrow_faces`)

- 何を: 形全体を剛体で写した複製(`kernel.note_kept_faces` に変換つきで覚えた形で、対が全面)は、元の控えの `closed` を使う。
- なぜ: 写した形は TShape が新しく、`kernel.is_closed_shell` の控えに当たらないので数え直していた(`--part big` で 7〜17 ms、QR1 の 733 面で約 30 ms)。
- 前後: ベンチの鏡映の確定後の配信 118 → 87 ms(MCP)、105 → 86 ms(画面)。複製は 75 → 71 ms(揺れの範囲)。
- 根拠: 剛体の写しは面と辺のつながりが同じ。元の控えが値を持っていないときは今までどおり数える。

### ③ `dd7c08a` ゴーストが細かく測った残りの面を元の形へ返す(`session.preview_job` の `adopt` → `view.return_kept_faces`)

- 何を: ゴーストを採用するとき、結果に残った面のうちそのゴーストが測った面を、元の形のその測り方の三角形として覚えさせる。
  下限に張り付いたズームでは借りないのと同じく返さない。
- なぜ: 1.2 のゴーストのたびの 73 面。
- 前後: 「上から」のゴースト 2 回目・3 回目 8,858 / 8,773 → **6,509 / 6,598 ms**(測る面 78 → 5)。1 回目は変わらない。
  ゴーストの後の鏡映・複製も、返した面を変換して借りるので 8,088 / 7,809 → 93 / 68 ms(ゴーストを出していなければ変わらない)。
- 根拠: 面の番号・形は変わらない。返すのは「同じ面」(`kernel.kept_pairs`: 同じ TShape か子が返した対)の、同じ測り方の三角形。
  **三角形の出どころが変わる**: 2 回目以降のゴーストと、後で画面がその面を細かく求めたとき、その面は 1 回目のゴーストが結果の形の上で
  測った三角形になる(今までは毎回そのときの組で測り直していた)。別々に測った面の境目の扱いは今の借用と同じ
  (decisions.md「変わっていない面の三角形の使い回し」)。テストで、2 回目のゴーストの三角形が確定後の形を丸ごと測った三角形と
  全面一致することを見ている(箱の題材)。

### ④ `a3be451` 操作で面の番号が変わったら、画面の面ごとの測り方を残った面の新しい番号へ移す(`view._body_focus` / `_remap_focus`)

- 何を: `focus` が届いたときの各ボディの形を覚え、今の形が違えば、残った面(同じ面・子の対・undo で子の結果から戻った対・位置だけ違う面)は
  届いたときの指定を引き継ぎ、残っていない面(新しい面・変わった面)は細かい側にする。画面が新しい形で `focus` を送ってくれば、そちらに従う
  (届いたときの形を覚え直す)。
- なぜ: 1.2 の Enter の後の 74 面と、MCP の確定の後の 57 面。
- 前後: 「上から」の MCP の確定(辺1本のフィレット)の後の配信 8,694 → **6,338 ms**(測る面 62 → 5、配る大きさ 1.63 → 0.73 MB)、
  Enter の後の配信 100 → **29 ms**(74 → 0)。
- 根拠: 形・面の番号は変わらない。**測り方の選び方が変わる**: 操作の後、画面が送り直してくるまでに配る三角形は、残った面は前の形と同じ
  測り方、新しい面・変わった面は細かい側(今までは添字の偶然で細かい側か粗い側)。新しい面・変わった面を全部細かく測るので、
  全面が作り直される操作(小さい箱の上面の押し出しで実測)では、今まで偶然粗く測っていた面を細かく測る分だけ重くなりうる。
  画面は形が変わると focus を計算し直し、違えば送ってくる(`web/src/resolutionTracker.ts`)ので、ずれは背景の測り直しで直る。

### 2.5 ③④ で三角形が変わりうる箇所(三角形を使っている所の洗い出し)

配信の三角形(`DocumentView.geometry` の `faces[].positions/indices`)を使うのは**画面だけ**。サーバの計測・干渉・最小幅・STL/STEP は
B-rep か自分で測った三角形を使い、この三角形を読まない(`grep` で `session.geometry` / `view.geometry` の呼び手は `body_state` だけ。
MCP の `get_state` は WebSocket 専用)。画面側:

- 表示: `web/src/bodyMesh.ts`(面ごとの Mesh)、面の境界線 `web/src/edgeSegments.ts`(三角形から求める)
- ゴースト: `web/src/ghostLayer.ts`(ゴーストの三角形・隠れた境界線の破線と輪郭)、`web/src/ghostEdges.ts`
- ピック: `web/src/viewport.ts`(三角形への光線)、矩形選択 `web/src/selectionBand.ts` と遮蔽 `web/src/occlusion.ts`(本物の Mesh の奥行き)
- 断面: `web/src/sectionView.ts`(本物の Mesh)
- 面の選び分け: `web/src/faceCulling.ts`(面ごとの三角形の境界箱と `detail`)
- 配信の省略: `web/src/wire.ts` と `duocad/net/wire.py`(serial が同じなら同じ三角形。③は同じ三角形に同じ serial を付けるので契約は保つ)

③は、どれも「同じ面・同じ測り方の別の回の三角形」に替わるだけ。④は、操作の直後の数百 ms〜数秒(画面が focus を送り直して測り直しが
終わるまで)、残った面の粗さが前の形と同じになり、新しい面が細かくなる。どちらも採否は親の判断。

## 3. 入れなかった案

### 3.1 新しいボディ(複製・鏡映)に元のボディの focus を移す

- 測った値: 「上から」で、ゴーストを出していない鏡映・複製の配信は前 8,002 / 8,019 ms、後 7,774 / 8,000 ms(どちらも 73 面を細かく測る)。focus の無い移動の配信は 60〜70 ms(「カメラ」)。測るのは元のボディが粗く持つ 73 面の細かい側で、
  画面は受け取った直後に新しいボディの focus を送ってくる(その 73 面の多くは粗くてよい)。
- 入れなかった理由: focus に**居ないボディは「全面細かく」**という約束(`resolutionTracker.computeFocus` は全面細かいボディを送らない)なので、
  サーバが勝手に粗い面を混ぜると、画面がそのボディを全面細かいと判断したとき(focus が変わらないので何も送らない)粗いまま残る。
  安全にするには「仮に粗く配り、背景で細かく測り直して配り直す」仕組み(`body_recipe` に仮の印、`ws._push` の後に背景の測り直しを始める、
  失敗したら仮をやめる)が要り、`ws.py`・`refinement.py`・`view.py` にまたがる。低リスクではない。
- 案: 上の仕組みを入れるか、画面が新しいボディの focus を(全面細かくても)必ず送るようにする(`web/` の変更)。

### 3.2 ゴースト 1 回目も、画面が粗く持つ面は粗い側で借りる

- 測った値: ゴースト 1 回目は 8.7 s のまま(78 面)。粗い側で借りれば 5 面になり、2 回目以降と同じ約 6.5 s。
- 入れなかった理由: ゴーストの三角形が変わる(背を向けた面・画面外の面が粗くなる)。ゴーストは隠れた面の境界線を破線で描く
  (`ghostLayer.ts`)ので、穴の縁の破線が粗い多角形になって見える。確定の後の配信とも測り方をそろえる必要があり、④と組で考えることになる。

### 3.3 移動の undo / redo で、同じ位置へ戻った面は変換し直さない

- 測った値: 「カメラ」の移動の undo / redo の配信(前 67〜101 ms、後 61〜87 ms)のうち、全面の変換が 21〜32 ms(前で測った値)。undo は `Moved` の逆を重ねた新しい形になり、
  位置の鎖が違うので同一性(`IsSame`)で引けず、位置の差(数値では単位行列)を掛けて写し直している。
- 入れなかった理由: 「位置の差が厳密に単位か」を見るには `gp_Trsf` を読む必要があり、`view.py` は OCP を触らない約束。`kernel.py` は範囲外。
- 案: `kernel` に「2 つの位置が厳密に同じか」を返す関数を足し、`view._placed_face` で単位なら貸し手の三角形(と serial)をそのまま使う。

### 3.4 面の三角形の変換をまとめて行う

- 測った値: 鏡映・移動の配信で `transformed_face` が 27〜40 ms(485 面 × numpy の小さな配列の往復)。全面を 1 本の配列にして 1 回で
  掛ければ数分の 1 になるはず(未実装・未測定)。
- 入れなかった理由: `kernel.py` の関数で範囲外。

### 3.5 境界箱を面ごとに覚える

- 測った値: 細かい側の配信で `bounding_box_of_faces` が 10〜13 ms(形が変わるたびに全面の節点を走査)。
- 入れなかった理由: 面の三角形(list)の同一性で覚えると、その list を控えが手放した後も持ち続けてメモリが増える。測った時点で
  面の境界箱を `kernel._describe_face` が一緒に返すのが素直で、`kernel.py` の変更になる。

### 3.6 undo / redo で全面を送り直さない

- 測った値: 移動の undo / redo のたびに 3.3 MB を全部送る(「カメラ」)。画面は直前に受け取った面しか持たず(`web/src/wire.ts`)、
  サーバの `wire.Delivered` も同じなので、1 つ前の形の面は省けない。
- 入れなかった理由: `web/` の変更(受け取った面を serial で少し長く覚える)と `wire.py` の契約の変更が組で要る。

### 3.7 MCP の確定の後の配信・読み込み直後の配信をループの外へ

- 測った値: MCP の確定の後の配信は面の分割をループの上で行う(「上から」で 6.3 s、その間クリックが待つ)。読み込みは再生 1.9〜2.2 s と
  最初の配信 4.7〜5.4 s(領域 2.5 s + 分割 2.2〜2.7 s + 符号化 20 ms)がどちらもループの上。形の写し(全ボディの `copy_shape`)は 16 ms。
- 入れなかった理由(依頼どおり調べただけ): 画面は今の形を持っていないので、背景の測り直し(`BackgroundRefinement`)のように「前の三角形を
  出し続ける」ことができない。三角形の無いボディを受け取ったときの画面の扱い(`viewport.prune` は配られていないボディを消す)を変えるか、
  サーバが最初の配信だけを測り終えるまで遅らせ、その間の別の配信がそのボディを測らないようにする必要がある。再生は `Session` を
  ループの上で触る前提で、別スレッドへは出さない。
- 案(読み込み): (1) `open_document` の後、ボディの三角形を背景で測り(形の写しの上で。16 ms)、測り終えるまでの配信はそのボディを
  `deferrable` と同じ扱いで送らない。(2) 画面は「三角形がまだのボディ」を消さずに待つ印を受け取る(`web/` の変更)。スケッチの領域も同じ扱いが要る。

### 3.8 範囲外(他の枝)

- 面の分割そのもの(穴 384 の平面 1 枚が細かい側で数秒): 担当 `meshing`。
- `sketches_state` の領域の塗り(読み込み直後・カメラで 2.5 s): 担当 `regions`。

## 4. テスト

- 足したテスト: `tests/test_delivery_reuse.py`(7 件)。①②③④それぞれ、前のコード(`4ef67570` の書き出し)では落ち、後では通ることを確かめた
  (移動の 1 件は④の「位置だけ違う面」の引き継ぎの番で、前でも通る)。
- 全件: `uv run pytest -q -p no:cacheprovider`(Node 24.11 を PATH の先頭、`npm ci --prefix web` の後)→ **2797 passed, 5 failed, 5 skipped**(172 s)。
  落ちた 5 件は `4ef67570` を同じチェックアウトで走らせても同じに落ちる(この機械の事情):
  - `tests/test_web_scripts.py::test_start_server_stops_the_python_server_when_the_start_fails`(`.venv/Scripts/python.exe` = Windows の venv の場所を探す)
  - `tests/test_channel.py` の 3 件(更新の口が 501 を返す。Windows 前提の更新器)
  - `tests/test_loft.py::test_a_smooth_loft_that_pinches_between_sections_is_rejected`(断られるはずのロフトが通る。この機械の OCCT/環境で前から同じ)
- 最初の全件は `web/node_modules` が無く、Node の評価器 32 件と型検査 1 件が `three` を見つけられずに落ちた。`npm ci --prefix web` の後は通る。
- Node: この機械の Node は 22.23 で、`tests/test_adaptive_resolution.py` の式の突き合わせが最後の桁で落ちた(前のコードでも同じ)。
  Node 24.11 を入れて PATH の先頭に置き、全件を走らせた。

## 5. 親に確かめてほしいこと

- QR1 のブラウザで、focus がある状態の**ゴーストの打ち直し・Enter・undo・MCP の確定・鏡映**(ここでは focus を合成して測っただけ)。
  QR1 は分割が軽いので改善の絶対値は小さいはず。PERFORMANCE.md 6章の「鏡映の後の配信 268 ms」は体積と閉じた立体かの使い回しより前の値の
  ように読める(移動の 76 ms は「ベンチの後に測った」とある)ので、測り直してほしい。
- ③④の三角形の変化が画面で見えないか: ゴーストを打ち直したときの面の境目、操作の直後に新しい面が細かく・残った面が前の粗さで出ること、
  深いズーム(下限に張り付いたとき)。
- 保存済み 206 文書の再生の指紋(`tools/replay_fingerprint.py`): 形・番号には触っていないが、確かめていない。
- 取り込みの重なり: ④は `view.set_view_resolution` の比べる条件の数行を変えた。担当 `regions` が同じ関数の `self._sketch_cache.clear()` の
  あたりを変えると、隣り合った変更で衝突しうる。`sketches_state`・`_sketch_state` には触っていない。

## 6. 生の値と再現

### 6.1 再現

前のコードは `git archive 4ef67570 | tar -x -C <base>`、後は `git archive a3be451 | tar -x -C <after>` で書き出し、同じ環境(`uv sync --frozen`)で走らせた。

```bash
uv run python <base>/tools/bench_all_ops.py --docs part --part big --out out/base_big_1.json
```

```bash
uv run python <after>/tools/bench_all_ops.py --docs part --part big --out out/after_big_1.json
```

```bash
uv run python probe_delivery.py <after> --part big --cases topview,fillet1_preview,mirror,copy --out out/probe_after_top_1.json
```

```bash
uv run python probe_delivery.py <after> --part big --cases topview,fillet1 --out out/probe_after_top_mcp_1.json
```

```bash
uv run python probe_delivery.py <after> --part big --cases topview,mirror,copy --out out/probe_after_topmirror_1.json
```

```bash
uv run python probe_delivery.py <after> --part big --cases move,mirror,open --camera --out out/probe_after_cam_1.json
```

```bash
uv run python long_session.py <after> --rounds 40
```

cProfile は `--profile fillet1`(1 ケースに絞る)。順番: 前の big 1 回 → 前の big 2 回・small 3 回 → (後の probe 3 種 → 前の probe 3 種 → 後の big → 後の small)× 3
(前の probe の 1 回目はその前に単独で)→ ゴーストなしの鏡映(前・後を交互に 3 回)→ 長い作業(前 → 後)。中央値は付録 C の `aggregate.py`。

### 6.2 `bench_all_ops.py --docs part --part big`(3 回の中央値、ms)

| case | key | 前 | 後 |
|---|---|---|---|
| 初回配信 | ms | 2,603 | 2,491 |
| camera 全体表示 配信 | ms | 11,713 | 11,430 |
| camera ズームイン(30面) 配信 | ms | 63,197 | 62,966 |
| camera 回転(別の30面) 配信 | ms | 4,543 | 4,900 |
| camera 全体表示へ戻る 配信 | ms | 2,566 | 2,678 |
| select 面1つ + 配信 | ms | 5.3 | 6.0 |
| clear_selection + 配信 | ms | 4.7 | 5.0 |
| begin_command fillet | ms | 0.1 | 0.1 |
| edges 初回 | ms | 62 | 64 |
| vertices 初回 | ms | 4.7 | 4.9 |
| revolve 領域 new 90° | preview | 40 | 44 |
| revolve 領域 new 90° | confirm | 10 | 11 |
| revolve 領域 new 90° | push | 21 | 17 |
| revolve 領域 new 90° | mcp | 11 | 11 |
| revolve 領域 new 90° | mcp_push | 14 | 15 |
| revolve 領域 new 90° | undo | 4.9 | 5.7 |
| revolve 領域 new 90° | undo_push | 4.8 | 5.4 |
| revolve 領域 new 90° | redo | 9.0 | 10 |
| revolve 領域 new 90° | redo_push | 11 | 11 |
| extrude 領域 new | preview | 34 | 37 |
| extrude 領域 new | confirm | 6.9 | 6.6 |
| extrude 領域 new | push | 14 | 16 |
| extrude 領域 new | mcp | 8.3 | 8.3 |
| extrude 領域 new | mcp_push | 16 | 17 |
| extrude 領域 new | undo | 5.2 | 5.7 |
| extrude 領域 new | undo_push | 6.2 | 6.4 |
| extrude 領域 new | redo | 8.3 | 8.8 |
| extrude 領域 new | redo_push | 12 | 14 |
| extrude 領域 join(ボディを貫く) | preview | 9,969 | 10,373 |
| extrude 領域 join(ボディを貫く) | confirm | 29 | 22 |
| extrude 領域 join(ボディを貫く) | push | 38 | 27 |
| extrude 領域 join(ボディを貫く) | mcp | 2,141 | 2,131 |
| extrude 領域 join(ボディを貫く) | mcp_push | 7,911 | 8,249 |
| extrude 領域 join(ボディを貫く) | undo | 5.7 | 6.0 |
| extrude 領域 join(ボディを貫く) | undo_push | 33 | 33 |
| extrude 領域 join(ボディを貫く) | redo | 6.7 | 6.8 |
| extrude 領域 join(ボディを貫く) | redo_push | 26 | 27 |
| extrude 領域 cut(貫通) | preview | 8,815 | 8,483 |
| extrude 領域 cut(貫通) | confirm | 23 | 24 |
| extrude 領域 cut(貫通) | push | 31 | 28 |
| extrude 領域 cut(貫通) | mcp | 733 | 652 |
| extrude 領域 cut(貫通) | mcp_push | 7,473 | 7,482 |
| extrude 領域 cut(貫通) | undo | 5.2 | 5.6 |
| extrude 領域 cut(貫通) | undo_push | 31 | 35 |
| extrude 領域 cut(貫通) | redo | 6.8 | 6.4 |
| extrude 領域 cut(貫通) | redo_push | 29 | 26 |
| extrude 面 join 0.5 | preview | 6,652 | 7,056 |
| extrude 面 join 0.5 | confirm | 26 | 29 |
| extrude 面 join 0.5 | push | 22 | 22 |
| extrude 面 join 0.5 | mcp | 950 | 995 |
| extrude 面 join 0.5 | mcp_push | 5,877 | 6,060 |
| extrude 面 join 0.5 | undo | 5.8 | 5.7 |
| extrude 面 join 0.5 | undo_push | 26 | 24 |
| extrude 面 join 0.5 | redo | 5.9 | 5.6 |
| extrude 面 join 0.5 | redo_push | 25 | 29 |
| extrude 側面 join 1 | preview | 6,858 | 7,196 |
| extrude 側面 join 1 | confirm | 20 | 23 |
| extrude 側面 join 1 | push | 275 | 308 |
| extrude 側面 join 1 | mcp | 1,035 | 1,094 |
| extrude 側面 join 1 | mcp_push | 6,095 | 6,424 |
| extrude 側面 join 1 | undo | 5.9 | 5.2 |
| extrude 側面 join 1 | undo_push | 25 | 24 |
| extrude 側面 join 1 | redo | 6.2 | 5.3 |
| extrude 側面 join 1 | redo_push | 23 | 24 |
| fillet 辺1本 r=0.5 | preview | 6,306 | 6,870 |
| fillet 辺1本 r=0.5 | confirm | 21 | 19 |
| fillet 辺1本 r=0.5 | push | 23 | 22 |
| fillet 辺1本 r=0.5 | mcp | 446 | 508 |
| fillet 辺1本 r=0.5 | mcp_push | 5,966 | 6,151 |
| fillet 辺1本 r=0.5 | undo | 5.4 | 5.5 |
| fillet 辺1本 r=0.5 | undo_push | 22 | 24 |
| fillet 辺1本 r=0.5 | redo | 5.1 | 7.2 |
| fillet 辺1本 r=0.5 | redo_push | 23 | 29 |
| fillet 辺4本 r=0.5 | preview | 6,554 | 6,523 |
| fillet 辺4本 r=0.5 | confirm | 23 | 20 |
| fillet 辺4本 r=0.5 | push | 27 | 22 |
| fillet 辺4本 r=0.5 | mcp | 740 | 790 |
| fillet 辺4本 r=0.5 | mcp_push | 5,821 | 6,279 |
| fillet 辺4本 r=0.5 | undo | 5.8 | 5.5 |
| fillet 辺4本 r=0.5 | undo_push | 24 | 25 |
| fillet 辺4本 r=0.5 | redo | 5.8 | 5.4 |
| fillet 辺4本 r=0.5 | redo_push | 22 | 20 |
| chamfer 辺1本 0.5 | preview | 6,002 | 6,361 |
| chamfer 辺1本 0.5 | confirm | 19 | 23 |
| chamfer 辺1本 0.5 | push | 22 | 24 |
| chamfer 辺1本 0.5 | mcp | 500 | 498 |
| chamfer 辺1本 0.5 | mcp_push | 5,552 | 5,967 |
| chamfer 辺1本 0.5 | undo | 5.5 | 6.1 |
| chamfer 辺1本 0.5 | undo_push | 21 | 24 |
| chamfer 辺1本 0.5 | redo | 5.0 | 5.6 |
| chamfer 辺1本 0.5 | redo_push | 28 | 27 |
| fillet 穴の縁 r=0.3 | preview | 7,973 | 8,274 |
| fillet 穴の縁 r=0.3 | confirm | 20 | 19 |
| fillet 穴の縁 r=0.3 | push | 24 | 22 |
| fillet 穴の縁 r=0.3 | mcp | 475 | 465 |
| fillet 穴の縁 r=0.3 | mcp_push | 7,462 | 7,833 |
| fillet 穴の縁 r=0.3 | undo | 5.9 | 5.8 |
| fillet 穴の縁 r=0.3 | undo_push | 26 | 24 |
| fillet 穴の縁 r=0.3 | redo | 5.9 | 5.7 |
| fillet 穴の縁 r=0.3 | redo_push | 26 | 22 |
| shell_body 底面を抜く t=0.8 | error | Refused: shell failed (IsDone is false)  | Refused: shell failed (IsDone is false)  |
| offset_faces 側面 0.3 | error | Refused: offset_faces refused (faces [39 | Refused: offset_faces refused (faces [39 |
| delete_faces 穴1つ | preview | 15,827 | 16,342 |
| delete_faces 穴1つ | confirm | 20 | 20 |
| delete_faces 穴1つ | push | 37 | 34 |
| delete_faces 穴1つ | mcp | 7,628 | 7,476 |
| delete_faces 穴1つ | mcp_push | 8,533 | 8,826 |
| delete_faces 穴1つ | undo | 5.7 | 5.7 |
| delete_faces 穴1つ | undo_push | 41 | 43 |
| delete_faces 穴1つ | redo | 6.1 | 5.9 |
| delete_faces 穴1つ | redo_push | 37 | 32 |
| thread 穴 trapezoid30 | preview | 12,241 | 12,224 |
| thread 穴 trapezoid30 | confirm | 19 | 19 |
| thread 穴 trapezoid30 | push | 25 | 25 |
| thread 穴 trapezoid30 | mcp | 4,239 | 4,167 |
| thread 穴 trapezoid30 | mcp_push | 8,198 | 8,317 |
| thread 穴 trapezoid30 | undo | 5.4 | 5.7 |
| thread 穴 trapezoid30 | undo_push | 27 | 29 |
| thread 穴 trapezoid30 | redo | 3,903 | 3,941 |
| thread 穴 trapezoid30 | redo_push | 239 | 258 |
| move_body 平行移動 | confirm | 4.9 | 4.3 |
| move_body 平行移動 | push | 110 | 98 |
| move_body 平行移動 | mcp | 5.8 | 5.7 |
| move_body 平行移動 | mcp_push | 87 | 91 |
| move_body 平行移動 | undo | 5.8 | 5.7 |
| move_body 平行移動 | undo_push | 80 | 79 |
| move_body 平行移動 | redo | 5.5 | 8.0 |
| move_body 平行移動 | redo_push | 118 | 113 |
| move_body 複製 | confirm | 71 | 77 |
| move_body 複製 | push | 80 | 68 |
| move_body 複製 | mcp | 65 | 77 |
| move_body 複製 | mcp_push | 75 | 71 |
| move_body 複製 | undo | 5.2 | 6.3 |
| move_body 複製 | undo_push | 6.7 | 6.9 |
| move_body 複製 | redo | 73 | 72 |
| move_body 複製 | redo_push | 99 | 99 |
| mirror_body YZ | confirm | 73 | 66 |
| mirror_body YZ | push | 105 | 86 |
| mirror_body YZ | mcp | 74 | 71 |
| mirror_body YZ | mcp_push | 118 | 87 |
| mirror_body YZ | undo | 6.8 | 5.1 |
| mirror_body YZ | undo_push | 9.7 | 6.3 |
| mirror_body YZ | redo | 75 | 71 |
| mirror_body YZ | redo_push | 124 | 105 |
| split_body YZ | preview | 6,673 | 6,894 |
| split_body YZ | confirm | 26 | 25 |
| split_body YZ | push | 40 | 41 |
| split_body YZ | mcp | 1,148 | 1,221 |
| split_body YZ | mcp_push | 5,367 | 5,501 |
| split_body YZ | undo | 5.8 | 6.1 |
| split_body YZ | undo_push | 33 | 35 |
| split_body YZ | redo | 5.5 | 6.1 |
| split_body YZ | redo_push | 39 | 45 |
| boolean_body cut | preview | 5,370 | 5,241 |
| boolean_body cut | confirm | 33 | 23 |
| boolean_body cut | push | 40 | 33 |
| boolean_body cut | mcp | 2,509 | 2,822 |
| boolean_body cut | mcp_push | 2,934 | 2,885 |
| boolean_body cut | undo | 5.5 | 5.3 |
| boolean_body cut | undo_push | 299 | 280 |
| boolean_body cut | redo | 6.2 | 5.9 |
| boolean_body cut | redo_push | 33 | 34 |
| delete ボディ | confirm | 4.1 | 4.1 |
| delete ボディ | push | 6.7 | 7.2 |
| delete ボディ | mcp | 5.0 | 5.3 |
| delete ボディ | mcp_push | 39 | 41 |
| delete ボディ | undo | 5.2 | 5.3 |
| delete ボディ | undo_push | 36 | 41 |
| delete ボディ | redo | 5.2 | 5.2 |
| delete ボディ | redo_push | 8.4 | 8.4 |
| add_line(スケッチ内) | confirm | 4.9 | 3.9 |
| add_line(スケッチ内) | push | 6.6 | 5.9 |
| add_line(スケッチ内) | mcp | 6.1 | 5.7 |
| add_line(スケッチ内) | mcp_push | 7.9 | 7.4 |
| add_line(スケッチ内) | undo | 5.3 | 4.7 |
| add_line(スケッチ内) | undo_push | 6.8 | 6.4 |
| add_line(スケッチ内) | redo | 4.7 | 5.0 |
| add_line(スケッチ内) | redo_push | 6.6 | 5.1 |
| add_circle(スケッチ内) | confirm | 7.0 | 3.7 |
| add_circle(スケッチ内) | push | 14 | 7.8 |
| add_circle(スケッチ内) | mcp | 6.3 | 4.8 |
| add_circle(スケッチ内) | mcp_push | 14 | 9.7 |
| add_circle(スケッチ内) | undo | 6.8 | 5.1 |
| add_circle(スケッチ内) | undo_push | 9.6 | 7.5 |
| add_circle(スケッチ内) | redo | 6.2 | 6.4 |
| add_circle(スケッチ内) | redo_push | 9.2 | 11 |
| sketch_create XY | confirm | 4.1 | 4.0 |
| sketch_create XY | push | 8.1 | 6.3 |
| sketch_create XY | mcp | 7.0 | 4.5 |
| sketch_create XY | mcp_push | 9.3 | 5.5 |
| sketch_create XY | undo | 6.4 | 4.4 |
| sketch_create XY | undo_push | 6.4 | 5.8 |
| sketch_create XY | redo | 4.8 | 5.1 |
| sketch_create XY | redo_push | 6.0 | 6.0 |

### 6.3 `bench_all_ops.py --docs part --part small`(3 回の中央値、ms)

| case | key | 前 | 後 |
|---|---|---|---|
| 初回配信 | ms | 73 | 73 |
| camera 全体表示 配信 | ms | 204 | 225 |
| camera ズームイン(30面) 配信 | ms | 957 | 926 |
| camera 回転(別の30面) 配信 | ms | 95 | 90 |
| camera 全体表示へ戻る 配信 | ms | 71 | 71 |
| select 面1つ + 配信 | ms | 0.6 | 0.7 |
| clear_selection + 配信 | ms | 0.4 | 0.3 |
| begin_command fillet | ms | 0.0 | 0.0 |
| edges 初回 | ms | 5.2 | 5.3 |
| vertices 初回 | ms | 0.5 | 0.6 |
| revolve 領域 new 90° | preview | 24 | 18 |
| revolve 領域 new 90° | confirm | 6.0 | 5.3 |
| revolve 領域 new 90° | push | 8.7 | 7.8 |
| revolve 領域 new 90° | mcp | 5.0 | 4.5 |
| revolve 領域 new 90° | mcp_push | 9.1 | 8.1 |
| revolve 領域 new 90° | undo | 1.3 | 1.1 |
| revolve 領域 new 90° | undo_push | 0.6 | 0.4 |
| revolve 領域 new 90° | redo | 5.0 | 5.3 |
| revolve 領域 new 90° | redo_push | 6.2 | 6.0 |
| extrude 領域 new | preview | 9.6 | 9.6 |
| extrude 領域 new | confirm | 2.8 | 2.8 |
| extrude 領域 new | push | 6.6 | 6.5 |
| extrude 領域 new | mcp | 4.1 | 2.8 |
| extrude 領域 new | mcp_push | 7.2 | 6.8 |
| extrude 領域 new | undo | 1.3 | 1.1 |
| extrude 領域 new | undo_push | 0.6 | 0.4 |
| extrude 領域 new | redo | 3.6 | 2.8 |
| extrude 領域 new | redo_push | 5.0 | 4.4 |
| extrude 領域 join(ボディを貫く) | preview | 183 | 177 |
| extrude 領域 join(ボディを貫く) | confirm | 3.5 | 3.1 |
| extrude 領域 join(ボディを貫く) | push | 5.5 | 4.9 |
| extrude 領域 join(ボディを貫く) | mcp | 81 | 82 |
| extrude 領域 join(ボディを貫く) | mcp_push | 94 | 104 |
| extrude 領域 join(ボディを貫く) | undo | 1.5 | 1.7 |
| extrude 領域 join(ボディを貫く) | undo_push | 1.9 | 2.1 |
| extrude 領域 join(ボディを貫く) | redo | 1.8 | 2.0 |
| extrude 領域 join(ボディを貫く) | redo_push | 2.0 | 2.0 |
| extrude 領域 cut(貫通) | preview | 129 | 132 |
| extrude 領域 cut(貫通) | confirm | 3.2 | 3.5 |
| extrude 領域 cut(貫通) | push | 5.9 | 4.4 |
| extrude 領域 cut(貫通) | mcp | 43 | 42 |
| extrude 領域 cut(貫通) | mcp_push | 108 | 99 |
| extrude 領域 cut(貫通) | undo | 1.7 | 1.8 |
| extrude 領域 cut(貫通) | undo_push | 2.4 | 3.4 |
| extrude 領域 cut(貫通) | redo | 2.0 | 2.0 |
| extrude 領域 cut(貫通) | redo_push | 2.2 | 1.9 |
| extrude 面 join 0.5 | preview | 151 | 170 |
| extrude 面 join 0.5 | confirm | 2.7 | 3.0 |
| extrude 面 join 0.5 | push | 1.9 | 2.0 |
| extrude 面 join 0.5 | mcp | 69 | 63 |
| extrude 面 join 0.5 | mcp_push | 90 | 75 |
| extrude 面 join 0.5 | undo | 1.8 | 1.5 |
| extrude 面 join 0.5 | undo_push | 2.1 | 2.0 |
| extrude 面 join 0.5 | redo | 1.6 | 1.5 |
| extrude 面 join 0.5 | redo_push | 2.0 | 1.8 |
| extrude 側面 join 1 | preview | 167 | 141 |
| extrude 側面 join 1 | confirm | 2.7 | 2.6 |
| extrude 側面 join 1 | push | 1.9 | 1.7 |
| extrude 側面 join 1 | mcp | 86 | 87 |
| extrude 側面 join 1 | mcp_push | 97 | 81 |
| extrude 側面 join 1 | undo | 1.6 | 1.5 |
| extrude 側面 join 1 | undo_push | 1.9 | 1.9 |
| extrude 側面 join 1 | redo | 1.5 | 1.5 |
| extrude 側面 join 1 | redo_push | 1.9 | 2.3 |
| fillet 辺1本 r=0.5 | preview | 119 | 110 |
| fillet 辺1本 r=0.5 | confirm | 2.7 | 2.4 |
| fillet 辺1本 r=0.5 | push | 2.0 | 1.9 |
| fillet 辺1本 r=0.5 | mcp | 34 | 37 |
| fillet 辺1本 r=0.5 | mcp_push | 81 | 79 |
| fillet 辺1本 r=0.5 | undo | 1.5 | 1.4 |
| fillet 辺1本 r=0.5 | undo_push | 2.0 | 2.0 |
| fillet 辺1本 r=0.5 | redo | 1.5 | 1.4 |
| fillet 辺1本 r=0.5 | redo_push | 1.7 | 1.7 |
| fillet 辺4本 r=0.5 | preview | 134 | 111 |
| fillet 辺4本 r=0.5 | confirm | 2.9 | 2.5 |
| fillet 辺4本 r=0.5 | push | 2.2 | 1.8 |
| fillet 辺4本 r=0.5 | mcp | 54 | 52 |
| fillet 辺4本 r=0.5 | mcp_push | 85 | 84 |
| fillet 辺4本 r=0.5 | undo | 1.7 | 1.7 |
| fillet 辺4本 r=0.5 | undo_push | 1.9 | 2.0 |
| fillet 辺4本 r=0.5 | redo | 1.6 | 1.4 |
| fillet 辺4本 r=0.5 | redo_push | 2.0 | 1.9 |
| chamfer 辺1本 0.5 | preview | 112 | 112 |
| chamfer 辺1本 0.5 | confirm | 2.6 | 3.1 |
| chamfer 辺1本 0.5 | push | 2.0 | 2.1 |
| chamfer 辺1本 0.5 | mcp | 34 | 32 |
| chamfer 辺1本 0.5 | mcp_push | 88 | 78 |
| chamfer 辺1本 0.5 | undo | 1.9 | 1.6 |
| chamfer 辺1本 0.5 | undo_push | 2.0 | 1.9 |
| chamfer 辺1本 0.5 | redo | 1.5 | 1.4 |
| chamfer 辺1本 0.5 | redo_push | 2.0 | 1.7 |
| fillet 穴の縁 r=0.3 | preview | 115 | 114 |
| fillet 穴の縁 r=0.3 | confirm | 2.6 | 2.6 |
| fillet 穴の縁 r=0.3 | push | 2.1 | 2.1 |
| fillet 穴の縁 r=0.3 | mcp | 33 | 30 |
| fillet 穴の縁 r=0.3 | mcp_push | 92 | 82 |
| fillet 穴の縁 r=0.3 | undo | 1.7 | 1.5 |
| fillet 穴の縁 r=0.3 | undo_push | 1.9 | 1.8 |
| fillet 穴の縁 r=0.3 | redo | 1.5 | 1.3 |
| fillet 穴の縁 r=0.3 | redo_push | 2.3 | 2.0 |
| shell_body 底面を抜く t=0.8 | error | Refused: shell produced an invalid shape | Refused: shell produced an invalid shape |
| offset_faces 側面 0.3 | error | Refused: offset_faces refused (faces [38 | Refused: offset_faces refused (faces [38 |
| delete_faces 穴1つ | error | Refused: delete_faces refused (faces [35 | Refused: delete_faces refused (faces [35 |
| thread 穴 trapezoid30 | preview | 4,439 | 4,252 |
| thread 穴 trapezoid30 | confirm | 4.1 | 3.4 |
| thread 穴 trapezoid30 | push | 4.1 | 2.9 |
| thread 穴 trapezoid30 | mcp | 4,180 | 4,139 |
| thread 穴 trapezoid30 | mcp_push | 224 | 222 |
| thread 穴 trapezoid30 | undo | 1.6 | 1.8 |
| thread 穴 trapezoid30 | undo_push | 2.2 | 2.3 |
| thread 穴 trapezoid30 | redo | 4,284 | 3,966 |
| thread 穴 trapezoid30 | redo_push | 220 | 212 |
| move_body 平行移動 | confirm | 0.9 | 0.7 |
| move_body 平行移動 | push | 7.0 | 6.2 |
| move_body 平行移動 | mcp | 1.5 | 1.5 |
| move_body 平行移動 | mcp_push | 9.1 | 7.3 |
| move_body 平行移動 | undo | 1.5 | 1.5 |
| move_body 平行移動 | undo_push | 7.4 | 6.6 |
| move_body 平行移動 | redo | 1.5 | 1.4 |
| move_body 平行移動 | redo_push | 10 | 8.8 |
| move_body 複製 | confirm | 7.3 | 6.7 |
| move_body 複製 | push | 6.6 | 5.2 |
| move_body 複製 | mcp | 9.1 | 8.6 |
| move_body 複製 | mcp_push | 6.7 | 4.8 |
| move_body 複製 | undo | 1.3 | 1.3 |
| move_body 複製 | undo_push | 0.6 | 0.5 |
| move_body 複製 | redo | 7.9 | 7.3 |
| move_body 複製 | redo_push | 7.4 | 4.9 |
| mirror_body YZ | confirm | 7.6 | 7.1 |
| mirror_body YZ | push | 8.2 | 6.6 |
| mirror_body YZ | mcp | 7.8 | 7.6 |
| mirror_body YZ | mcp_push | 7.7 | 6.0 |
| mirror_body YZ | undo | 1.3 | 1.2 |
| mirror_body YZ | undo_push | 0.7 | 0.6 |
| mirror_body YZ | redo | 8.5 | 7.1 |
| mirror_body YZ | redo_push | 13 | 9.3 |
| split_body YZ | preview | 208 | 203 |
| split_body YZ | confirm | 4.1 | 3.8 |
| split_body YZ | push | 4.2 | 4.1 |
| split_body YZ | mcp | 96 | 95 |
| split_body YZ | mcp_push | 106 | 108 |
| split_body YZ | undo | 1.7 | 1.5 |
| split_body YZ | undo_push | 3.0 | 2.9 |
| split_body YZ | redo | 1.7 | 1.7 |
| split_body YZ | redo_push | 2.7 | 2.8 |
| boolean_body cut | preview | 209 | 216 |
| boolean_body cut | confirm | 3.8 | 3.4 |
| boolean_body cut | push | 2.9 | 3.0 |
| boolean_body cut | mcp | 124 | 125 |
| boolean_body cut | mcp_push | 83 | 86 |
| boolean_body cut | undo | 1.8 | 2.8 |
| boolean_body cut | undo_push | 2.6 | 2.3 |
| boolean_body cut | redo | 1.6 | 1.4 |
| boolean_body cut | redo_push | 2.9 | 4.2 |
| delete ボディ | confirm | 0.8 | 0.8 |
| delete ボディ | push | 0.7 | 0.6 |
| delete ボディ | mcp | 1.4 | 1.4 |
| delete ボディ | mcp_push | 3.1 | 3.1 |
| delete ボディ | undo | 1.4 | 1.1 |
| delete ボディ | undo_push | 3.9 | 3.5 |
| delete ボディ | redo | 1.5 | 1.4 |
| delete ボディ | redo_push | 0.9 | 0.9 |
| add_line(スケッチ内) | confirm | 0.8 | 0.8 |
| add_line(スケッチ内) | push | 0.7 | 0.7 |
| add_line(スケッチ内) | mcp | 1.3 | 1.2 |
| add_line(スケッチ内) | mcp_push | 2.5 | 1.9 |
| add_line(スケッチ内) | undo | 1.4 | 1.2 |
| add_line(スケッチ内) | undo_push | 0.7 | 0.6 |
| add_line(スケッチ内) | redo | 1.4 | 1.2 |
| add_line(スケッチ内) | redo_push | 0.7 | 0.5 |
| add_circle(スケッチ内) | confirm | 0.7 | 0.7 |
| add_circle(スケッチ内) | push | 3.3 | 2.8 |
| add_circle(スケッチ内) | mcp | 1.3 | 1.2 |
| add_circle(スケッチ内) | mcp_push | 6.2 | 5.1 |
| add_circle(スケッチ内) | undo | 1.4 | 1.3 |
| add_circle(スケッチ内) | undo_push | 0.6 | 0.6 |
| add_circle(スケッチ内) | redo | 1.2 | 1.2 |
| add_circle(スケッチ内) | redo_push | 3.2 | 2.9 |
| sketch_create XY | confirm | 0.7 | 0.7 |
| sketch_create XY | push | 0.6 | 0.7 |
| sketch_create XY | mcp | 1.1 | 1.3 |
| sketch_create XY | mcp_push | 0.7 | 0.6 |
| sketch_create XY | undo | 1.5 | 1.1 |
| sketch_create XY | undo_push | 0.5 | 0.5 |
| sketch_create XY | redo | 1.2 | 1.2 |
| sketch_create XY | redo_push | 0.7 | 0.6 |

### 6.4 `probe_delivery.py`(配信の内訳)

#### 「上から」+ ゴースト 3 回 → 確定 → undo、続けて鏡映・複製(3 回の中央値、ms)

| case | 合計 前 | 合計 後 | 測った面 前 | 測った面 後 | 分割 前 | 分割 後 | バイト 前 | バイト 後 |
|---|---|---|---|---|---|---|---|---|
| 初回配信 | 2,455 | 2,836 | 485 | 485 | 2,420 | 2,805 | 2,410,208 | 2,410,208 |
| 初回配信の次(何も変わらない) | 4.5 | 4.5 | 0 | 0 | - | - | 18,676 | 18,676 |
| topview focus (73 面を粗く) 配信 | 13,686 | 14,418 | 485 | 485 | 10,999 | 11,683 | 3,805,196 | 3,805,196 |
| fillet1 preview r=0.6 | 8,729 | 8,842 | 78 | 78 | - | - | - | - |
| fillet1 preview r=0.65 | 8,858 | 6,509 | 78 | 5 | - | - | - | - |
| fillet1 preview r=0.7 | 8,773 | 6,598 | 78 | 5 | - | - | - | - |
| fillet1 confirm push | 100 | 29 | 74 | 0 | 55 | - | 1,680,012 | 733,968 |
| fillet1p undo_push | 10,437 | 19 | 485 | 0 | 10,369 | - | 2,983,800 | 731,340 |
| mirror mcp_push | 8,088 | 93 | 73 | 0 | 7,991 | - | 3,284,188 | 3,283,260 |
| mirror undo_push | 6.1 | 5.9 | 0 | 0 | - | - | 18,708 | 18,708 |
| mirror redo_push | 7,848 | 80 | 73 | 0 | 7,770 | - | 3,284,188 | 3,283,260 |
| mirror undo2_push | 6.0 | 4.7 | 0 | 0 | - | - | 18,708 | 18,708 |
| copy mcp_push | 7,809 | 68 | 73 | 0 | 7,747 | - | 3,284,028 | 3,282,828 |
| copy undo_push | 6.5 | 5.9 | 0 | 0 | - | - | 18,708 | 18,708 |
| maxrss_mb | 2,029 | 1,556 | - | - | - | - | - | - |

#### 「上から」+ MCP の確定(辺1本のフィレット)(3 回の中央値、ms)

| case | 合計 前 | 合計 後 | 測った面 前 | 測った面 後 | 分割 前 | 分割 後 | バイト 前 | バイト 後 |
|---|---|---|---|---|---|---|---|---|
| 初回配信 | 2,589 | 2,624 | 485 | 485 | 2,555 | 2,589 | 2,410,208 | 2,410,208 |
| 初回配信の次(何も変わらない) | 4.2 | 4.4 | 0 | 0 | - | - | 18,676 | 18,676 |
| topview focus (73 面を粗く) 配信 | 13,448 | 13,499 | 485 | 485 | 10,908 | 10,886 | 3,805,196 | 3,805,196 |
| fillet1 mcp_push | 8,694 | 6,338 | 62 | 5 | 8,661 | 6,307 | 1,631,160 | 734,904 |
| fillet1 undo_push | 26 | 18 | 0 | 0 | - | - | 1,382,908 | 731,340 |
| fillet1 redo_push | 24 | 21 | 0 | 0 | - | - | 1,629,432 | 733,980 |
| fillet1 undo2_push | 23 | 19 | 0 | 0 | - | - | 1,382,908 | 731,340 |
| maxrss_mb | 1,519 | 1,090 | - | - | - | - | - | - |

#### 「上から」+ ゴーストなしの鏡映・複製(3 回の中央値、ms)

| case | 合計 前 | 合計 後 | 測った面 前 | 測った面 後 | 分割 前 | 分割 後 | バイト 前 | バイト 後 |
|---|---|---|---|---|---|---|---|---|
| 初回配信 | 2,433 | 2,497 | 485 | 485 | 2,402 | 2,459 | 2,410,208 | 2,410,208 |
| 初回配信の次(何も変わらない) | 4.0 | 4.2 | 0 | 0 | - | - | 18,676 | 18,676 |
| topview focus (73 面を粗く) 配信 | 13,355 | 13,756 | 485 | 485 | 10,838 | 11,180 | 3,805,196 | 3,805,196 |
| mirror mcp_push | 8,002 | 7,774 | 73 | 73 | 7,928 | 7,704 | 3,284,188 | 3,284,188 |
| mirror undo_push | 5.2 | 4.6 | 0 | 0 | - | - | 18,708 | 18,708 |
| mirror redo_push | 7,800 | 8,235 | 73 | 73 | 7,732 | 8,162 | 3,284,188 | 3,284,184 |
| mirror undo2_push | 5.1 | 4.6 | 0 | 0 | - | - | 18,708 | 18,708 |
| copy mcp_push | 8,019 | 8,000 | 73 | 73 | 7,955 | 7,934 | 3,284,028 | 3,284,028 |
| copy undo_push | 5.0 | 4.8 | 0 | 0 | - | - | 18,708 | 18,708 |
| maxrss_mb | 1,224 | 1,171 | - | - | - | - | - | - |

#### 「カメラ」(ベンチの最後の粗さ、focus なし)+ 移動・鏡映・読み込み(3 回の中央値、ms)

| case | 合計 前 | 合計 後 | 測った面 前 | 測った面 後 | 分割 前 | 分割 後 | バイト 前 | バイト 後 |
|---|---|---|---|---|---|---|---|---|
| 初回配信 | 11,238 | 11,562 | 485 | 485 | 8,590 | 8,859 | 4,181,148 | 4,181,148 |
| 初回配信の次(何も変わらない) | 4.7 | 4.7 | 0 | 0 | - | - | 18,676 | 18,676 |
| move mcp_push | 69 | 63 | 0 | 0 | - | - | 3,282,616 | 3,282,616 |
| move undo_push | 77 | 62 | 0 | 0 | - | - | 3,282,512 | 3,282,512 |
| move redo_push | 75 | 69 | 0 | 0 | - | - | 3,282,616 | 3,282,616 |
| move undo2_push | 101 | 87 | 0 | 0 | - | - | 3,282,512 | 3,282,512 |
| mirror mcp_push | 92 | 82 | 0 | 0 | - | - | 3,283,260 | 3,283,260 |
| mirror undo_push | 4.8 | 5.0 | 0 | 0 | - | - | 18,708 | 18,708 |
| mirror redo_push | 97 | 86 | 0 | 0 | - | - | 3,283,260 | 3,283,260 |
| mirror undo2_push | 4.5 | 4.7 | 0 | 0 | - | - | 18,708 | 18,708 |
| open_document | 2,096 | 2,040 | - | - | - | - | - | - |
| open 直後の配信 | 5,026 | 5,352 | 485 | 485 | 2,383 | 2,672 | 2,410,688 | 2,410,688 |
| maxrss_mb | 1,483 | 1,482 | - | - | - | - | - | - |

### 6.5 `probe_delivery.py` の回ごとの値

`probe_delivery.py` 「top」(ms。maxrss_mb は MB、1 回目は記録前)

| case | 前 1 | 前 2 | 前 3 | 後 1 | 後 2 | 後 3 |
|---|---|---|---|---|---|---|
| fillet1 preview r=0.6 | 8,523.5 | 8,729.1 | 9,170.1 | 8,841.8 | 9,028.8 | 8,249.3 |
| fillet1 preview r=0.65 | 9,976.4 | 8,583.7 | 8,858.0 | 6,756.6 | 6,509.3 | 6,332.5 |
| fillet1 preview r=0.7 | 9,337.2 | 8,668.9 | 8,773.0 | 6,967.2 | 6,598.0 | 6,505.4 |
| fillet1 confirm push | 99.8 | 160.8 | 86.6 | 30.8 | 25.8 | 29.1 |
| fillet1p undo_push | 10,437.3 | 10,787.2 | 10,410.7 | 22.5 | 19.4 | 17.9 |
| mirror mcp_push | 9,338.8 | 8,088.1 | 7,520.8 | 101.6 | 93.4 | 82.8 |
| mirror redo_push | 9,177.7 | 7,733.0 | 7,847.6 | 88.4 | 79.9 | 79.6 |
| copy mcp_push | 8,544.2 | 7,808.6 | 7,668.7 | 67.5 | 68.7 | 61.4 |
| maxrss_mb | - | 1,850.5 | 2,207.8 | - | 1,585.3 | 1,526.0 |

`probe_delivery.py` 「top_mcp」(ms。maxrss_mb は MB、1 回目は記録前)

| case | 前 1 | 前 2 | 前 3 | 後 1 | 後 2 | 後 3 |
|---|---|---|---|---|---|---|
| fillet1 mcp_push | 10,159.6 | 8,693.7 | 8,323.8 | 6,195.4 | 6,351.0 | 6,338.4 |
| fillet1 undo_push | 25.6 | 27.4 | 26.0 | 21.0 | 17.7 | 18.3 |
| fillet1 redo_push | 22.1 | 23.7 | 23.6 | 20.5 | 33.2 | 20.6 |
| maxrss_mb | - | 1,523.0 | 1,514.1 | 1,090.3 | 1,118.7 | 1,064.3 |

`probe_delivery.py` 「cam」(ms。maxrss_mb は MB、1 回目は記録前)

| case | 前 1 | 前 2 | 前 3 | 後 1 | 後 2 | 後 3 |
|---|---|---|---|---|---|---|
| move mcp_push | 69.3 | 71.8 | 67.6 | 59.3 | 62.9 | 62.6 |
| move undo_push | 67.2 | 89.2 | 77.2 | 60.7 | 62.5 | 77.2 |
| move redo_push | 67.9 | 84.3 | 74.7 | 61.5 | 68.7 | 75.3 |
| mirror mcp_push | 84.9 | 101.3 | 92.5 | 70.9 | 100.2 | 81.8 |
| mirror redo_push | 79.6 | 97.3 | 101.0 | 70.9 | 87.8 | 85.5 |
| open_document | 1,941.5 | 2,153.3 | 2,096.5 | 1,976.1 | 2,040.0 | 2,051.5 |
| open 直後の配信 | 4,698.6 | 5,414.4 | 5,026.5 | 5,305.3 | 5,352.3 | 5,482.0 |
| maxrss_mb | - | 1,480.4 | 1,485.4 | 1,482.4 | 1,484.3 | 1,481.4 |

`probe_delivery.py` 「topmirror」(ms。maxrss_mb は MB、1 回目は記録前)

| case | 前 1 | 前 2 | 前 3 | 後 1 | 後 2 | 後 3 |
|---|---|---|---|---|---|---|
| mirror mcp_push | 7,774.7 | 8,002.1 | 8,856.5 | 8,538.6 | 7,725.8 | 7,774.5 |
| mirror redo_push | 7,799.9 | 7,632.1 | 8,092.1 | 8,477.5 | 8,234.7 | 7,697.8 |
| copy mcp_push | 7,859.1 | 8,018.9 | 8,231.7 | 7,888.6 | 8,000.5 | 8,124.3 |
| maxrss_mb | 1,229.2 | 1,224.4 | 1,172.6 | 1,177.3 | 1,171.1 | 1,170.4 |

### 6.6 長い作業でのメモリ(`long_session.py`、穴 24 の板、上から見た focus、40 回)

1 回 = 辺を選んで半径を 3 回打ち直したゴースト → 確定 → 配信 → undo → 配信 → カメラの粗さの行き来。10 回ごとに鏡映 → undo。
`cached_numbers` は面の控え(今のボディの形 + 消えたボディの形)が持つ三角形の数の並びの長さの合計(同じ list は 1 回だけ数える)。

前:
```
{"round": 10, "traced_mb": 13.8, "rss_mb": 650.9, "face_stores": 3, "cached_numbers": 346236}
{"round": 20, "traced_mb": 14.0, "rss_mb": 678.2, "face_stores": 3, "cached_numbers": 346218}
{"round": 30, "traced_mb": 12.2, "rss_mb": 688.4, "face_stores": 3, "cached_numbers": 317418}
{"round": 40, "traced_mb": 14.3, "rss_mb": 700.0, "face_stores": 3, "cached_numbers": 346236}
{"seconds": 121.3}
```

後:
```
{"round": 10, "traced_mb": 10.1, "rss_mb": 645.0, "face_stores": 3, "cached_numbers": 274704}
{"round": 20, "traced_mb": 12.1, "rss_mb": 667.1, "face_stores": 3, "cached_numbers": 327696}
{"round": 30, "traced_mb": 11.2, "rss_mb": 679.2, "face_stores": 3, "cached_numbers": 298896}
{"round": 40, "traced_mb": 10.5, "rss_mb": 696.2, "face_stores": 3, "cached_numbers": 274704}
{"seconds": 66.5}
```

控えの数(`face_stores`)・持っている三角形の量・tracemalloc の量のどれも増え続けない(後の方が少し少ない: 測り直した三角形を作らない分)。
40 回にかかった時間は前 121 s、後 67 s。`--part big` の「上から」の計測の最大 RSS も前 1,850〜2,208 MB に対して後 1,526〜1,585 MB。

## 付録 A `probe_delivery.py`

```python
"""配信の内訳を測る(担当 delivery の調査用。リポジトリには入れない)。

    uv run python probe_delivery.py <repo> --part big --cases fillet1,mirror,move,open [--profile <case>]
"""
from __future__ import annotations

import argparse
import cProfile
import json
import pstats
import sys
import tempfile
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("repo", type=Path)
ap.add_argument("--part", default="big")
ap.add_argument("--cases", default="fillet1,mirror,move,open")
ap.add_argument("--profile", default="")
ap.add_argument("--top", type=int, default=35)
ap.add_argument("--out", type=Path, default=None)
ap.add_argument("--camera", action="store_true")
args = ap.parse_args()
sys.path.insert(0, str(args.repo))
sys.path.insert(0, str(args.repo / "tools"))

import bench_all_ops as bench  # noqa: E402
from duocad import kernel, kernel_worker, view as view_module  # noqa: E402
from duocad.net import wire  # noqa: E402
from duocad.session import Session  # noqa: E402
from duocad import brep_topology  # noqa: E402

RESULTS: list = []


class Timer:
    def __init__(self):
        self.parts = {}

    def add(self, name, value):
        self.parts[name] = self.parts.get(name, 0.0) + value


def wrap(obj, name, timer, label):
    original = getattr(obj, name)

    def wrapped(*a, **k):
        t0 = time.perf_counter()
        try:
            return original(*a, **k)
        finally:
            timer.add(label, (time.perf_counter() - t0) * 1000)
    setattr(obj, name, wrapped)
    return original


def push_breakdown(session, link, label, profile=False):
    timer = Timer()
    restore = []
    targets = [
        (kernel, "tessellate_faces", "tessellate_faces"),
        (kernel, "transformed_face", "transformed_face"),
        (kernel, "face_keys", "face_keys"),
        (kernel, "face_placements", "face_placements"),
        (kernel, "is_closed_shell", "is_closed_shell"),
        (kernel, "bounding_box_of_faces", "bbox_of_faces"),
        (view_module.query, "volume", "query.volume"),
        (view_module.DocumentView, "_borrow_faces", "_borrow_faces"),
        (view_module.DocumentView, "sketches_state", "sketches_state"),
        (view_module.DocumentView, "_tessellate_body", "_tessellate_body"),
    ]
    for obj, name, lab in targets:
        restore.append((obj, name, wrap(obj, name, timer, lab)))
    measured_faces = []
    inner = kernel.tessellate_faces
    def counting(shape, groups, **o):
        measured_faces.append({str(k): len(list(v)) for k, v in groups.items()})
        return inner(shape, groups, **o)
    kernel.tessellate_faces = counting
    restore.append((kernel, "tessellate_faces", inner))
    profiler = cProfile.Profile() if profile else None
    try:
        t0 = time.perf_counter()
        if profiler:
            profiler.enable()
        ta = time.perf_counter()
        meta = session.state_meta()
        timer.add("state_meta", (time.perf_counter() - ta) * 1000)
        ta = time.perf_counter()
        delta = session.delta(link.delivered.revisions)
        timer.add("delta", (time.perf_counter() - ta) * 1000)
        ta = time.perf_counter()
        composed = link.delivered.compose(meta, delta["bodies"])
        timer.add("compose", (time.perf_counter() - ta) * 1000)
        ta = time.perf_counter()
        frame = wire.encode({"type": "state", **composed})
        timer.add("encode", (time.perf_counter() - ta) * 1000)
        link.delivered.record(meta, delta["bodies"])
        if profiler:
            profiler.disable()
        total = (time.perf_counter() - t0) * 1000
    finally:
        for obj, name, original in reversed(restore):
            setattr(obj, name, original)
    faces_sent = sum(
        sum(1 for f in (b.get("geometry") or {}).get("faces", []) if "positions" in f)
        for b in composed["delta"]["bodies"]
    )
    triangles = sum(
        sum(len(f.get("indices", [])) // 3 for f in (b.get("geometry") or {}).get("faces", []))
        for b in delta["bodies"]
    )
    row = {"case": label, "total": round(total, 1), "bytes": len(frame), "bodies": len(delta["bodies"]),
           "faces_with_triangles_sent": faces_sent, "triangles": triangles, "measured": measured_faces,
           **{k: round(v, 1) for k, v in sorted(timer.parts.items())}}
    RESULTS.append(row)
    print(json.dumps(row, ensure_ascii=False), flush=True)
    if profiler:
        stats = pstats.Stats(profiler)
        stats.sort_stats("cumulative").print_stats(args.top)
        stats.sort_stats("tottime").print_stats(20)
    return row


def main():
    tmp = Path(tempfile.mkdtemp(prefix="probe-"))
    s = Session(kernel.DEFAULT_DEVIATION, models_dir=tmp)
    t0 = time.perf_counter()
    body_id = bench.build_part(s, **bench.PARTS[args.part])
    print(f"build {(time.perf_counter()-t0)*1000:.0f} ms faces={brep_topology.face_count(s.document.get_body(body_id).shape)}", flush=True)
    link = bench.Wire(s)
    if args.camera:  # ベンチの最後と同じ粗さ(全体表示、focus なし)
        diag0 = s.geometry(body_id)["bbox"]["diagonal"]
        s.set_view_resolution(diag0 / 1200.0, diag0)
    push_breakdown(s, link, "初回配信", profile=args.profile == "first")
    push_breakdown(s, link, "初回配信の次(何も変わらない)")
    cases = args.cases.split(",")
    if "topview" in cases:
        # 上から見た画面の focus: 下を向いた平面(背を向けた面)だけを粗く
        geometry = s.geometry(body_id)
        diag = geometry["bbox"]["diagonal"]
        fine = [f["index"] for f in geometry["faces"] if not (f["kind"] == "plane" and f["normal"][2] < -0.15)]
        coarse_count = len(geometry["faces"]) - len(fine)
        t = time.perf_counter()
        s.set_view_resolution(diag / 1200.0, diag, {body_id: fine})
        push_breakdown(s, link, f"topview focus ({coarse_count} 面を粗く) 配信")
        print(f"  topview focus set+push {(time.perf_counter()-t)*1000:.0f} ms", flush=True)
    shape = s.document.get_body(body_id).shape
    lines = bench.blendable(s, body_id, bench.edges_of_kind(s, body_id, "line", 3.0), 1)
    for case in cases:
        if case == "fillet1":
            bench.select(s, [bench.edge_ref(body_id, lines[0]["index"])])
            t = time.perf_counter(); s.apply("fillet", {"radius": 0.5}); print(f"  mcp fillet {(time.perf_counter()-t)*1000:.0f} ms")
            push_breakdown(s, link, "fillet1 mcp_push", profile=args.profile == case)
            t = time.perf_counter(); s.undo(); print(f"  undo {(time.perf_counter()-t)*1000:.0f} ms")
            push_breakdown(s, link, "fillet1 undo_push")
            t = time.perf_counter(); s.redo(); print(f"  redo {(time.perf_counter()-t)*1000:.0f} ms")
            push_breakdown(s, link, "fillet1 redo_push")
            s.undo(); push_breakdown(s, link, "fillet1 undo2_push")
            s.clear_selection(); link.push()
        elif case == "fillet1_preview":
            bench.select(s, [bench.edge_ref(body_id, lines[0]["index"])])
            for radius in (0.6, 0.65, 0.7):  # 半径を打ち直すたびのゴースト
                fargs = {"radius": radius}
                counted = []
                real = kernel.tessellate_faces
                kernel.tessellate_faces = lambda shape, groups, **o: counted.append(sum(len(list(v)) for v in groups.values())) or real(shape, groups, **o)
                prof = cProfile.Profile() if args.profile == "preview" else None
                t = time.perf_counter()
                if prof:
                    prof.enable()
                job = s.preview_job("fillet", fargs)
                with kernel_worker.preview_context():
                    result = job.run()
                if prof:
                    prof.disable()
                    pstats.Stats(prof).sort_stats("cumulative").print_stats(args.top)
                elapsed = (time.perf_counter()-t)*1000
                kernel.tessellate_faces = real
                job.adopt()
                RESULTS.append({"case": f"fillet1 preview r={radius}", "total": round(elapsed, 1), "faces_measured": counted})
                print(json.dumps(RESULTS[-1], ensure_ascii=False), flush=True)
            t = time.perf_counter(); job.adopt(); s.apply("fillet", fargs); print(f"  confirm {(time.perf_counter()-t)*1000:.0f} ms")
            push_breakdown(s, link, "fillet1 confirm push", profile=args.profile == case)
            s.undo(); push_breakdown(s, link, "fillet1p undo_push")
            s.clear_selection(); link.push()
        elif case == "mirror":
            bench.select(s, [bench.body_ref(body_id)])
            t = time.perf_counter(); s.apply("mirror_body", {"base_plane": "YZ"}); print(f"  mcp mirror {(time.perf_counter()-t)*1000:.0f} ms")
            push_breakdown(s, link, "mirror mcp_push", profile=args.profile == case)
            s.undo(); push_breakdown(s, link, "mirror undo_push")
            s.redo(); push_breakdown(s, link, "mirror redo_push", profile=args.profile == "mirror_redo")
            s.undo(); push_breakdown(s, link, "mirror undo2_push")
            s.clear_selection(); link.push()
        elif case == "copy":
            bench.select(s, [bench.body_ref(body_id)])
            t = time.perf_counter(); s.apply("move_body", {"dx": 200.0, "dy": 0.0, "dz": 0.0, "copy": True}); print(f"  mcp copy {(time.perf_counter()-t)*1000:.0f} ms")
            push_breakdown(s, link, "copy mcp_push", profile=args.profile == case)
            s.undo(); push_breakdown(s, link, "copy undo_push")
            s.clear_selection(); link.push()
        elif case == "move":
            bench.select(s, [bench.body_ref(body_id)])
            t = time.perf_counter(); s.apply("move_body", {"dx": 1.0, "dy": 0.0, "dz": 0.0}); print(f"  mcp move {(time.perf_counter()-t)*1000:.0f} ms")
            push_breakdown(s, link, "move mcp_push", profile=args.profile == case)
            s.undo(); push_breakdown(s, link, "move undo_push")
            s.redo(); push_breakdown(s, link, "move redo_push", profile=args.profile == "move_redo")
            s.undo(); push_breakdown(s, link, "move undo2_push")
            s.clear_selection(); link.push()
        elif case == "open":
            s.save_document("probe.json")
            s2 = Session(kernel.DEFAULT_DEVIATION, models_dir=tmp, opened=False)
            prof = cProfile.Profile() if args.profile == "open" else None
            if prof:
                prof.enable()
            from duocad.oplog import OpLog
            stages = Timer()
            restore = []
            restore.append((s2.store, "read_document_file", wrap(s2.store, "read_document_file", stages, "read_file")))
            restore.append((OpLog, "from_payload", wrap(OpLog, "from_payload", stages, "parse_log")))
            restore.append((OpLog, "replay", wrap(OpLog, "replay", stages, "replay")))
            restore.append((s2.view, "reset", wrap(s2.view, "reset", stages, "view_reset")))
            t = time.perf_counter(); s2.open_document("probe.json"); opened = (time.perf_counter() - t) * 1000
            for obj, name, original in reversed(restore):
                setattr(obj, name, original)
            if prof:
                prof.disable()
                pstats.Stats(prof).sort_stats("cumulative").print_stats(args.top)
            t = time.perf_counter()
            copies = [kernel.copy_shape(b.shape) for b in s2.document.bodies if b.shape is not None]
            copy_ms = (time.perf_counter() - t) * 1000
            print(f"  open_document {opened:.0f} ms {stages.parts} copy_shape(全ボディ) {copy_ms:.0f} ms", flush=True)
            RESULTS.append({"case": "open_document", "total": round(opened, 1), **{k: round(v, 1) for k, v in stages.parts.items()}, "copy_shape": round(copy_ms, 1)})
            link2 = bench.Wire(s2)
            push_breakdown(s2, link2, "open 直後の配信", profile=args.profile == "open_push")
    import resource
    RESULTS.append({"case": "maxrss_mb", "total": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)})
    print(json.dumps(RESULTS[-1]), flush=True)
    if args.out:
        args.out.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=1))


main()
```

## 付録 B `long_session.py`

```python
"""長い作業でのメモリ(担当 delivery の調査用)。画面の focus がある状態で、ゴーストの打ち直し・確定・undo・カメラ・複製を
何十回も繰り返し、面のキャッシュが持つ三角形の量と RSS を比べる。

    uv run python long_session.py <repo> [--rounds 40]
"""
import argparse
import gc
import json
import resource
import sys
import time
import tracemalloc
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("repo", type=Path)
ap.add_argument("--rounds", type=int, default=40)
args = ap.parse_args()
sys.path.insert(0, str(args.repo))

from duocad.server import Session  # noqa: E402


def held_floats(view) -> int:
    seen = set()
    total = 0
    stores = [held for per_body in view._face_cache.values() for held in per_body.values()] + list(view._retired_faces.values())
    for _shape, store in stores:
        for faces in store.by_recipe.values():
            for face in faces.values():
                key = id(face["positions"])
                if key not in seen:
                    seen.add(key)
                    total += len(face["positions"]) + len(face["indices"])
    return total


def main():
    tracemalloc.start()
    s = Session(0.2)
    sid = s.sketch_create("XY")["sketch_id"]
    s.add_rectangle(sid, [0.0, 0.0], 60.0, 40.0)
    for i in range(6):
        for j in range(4):
            s.add_circle(sid, [6.0 + 9.0 * i, 6.0 + 9.0 * j], 4.0)
    s.leave_sketch()
    regions = s.state_meta()["sketches"][-1]["regions"]
    outer = max(regions, key=lambda r: r["area"])
    s.modify_selection([{"sketch": sid, "index": outer["index"]}], [])
    s.extrude(8.0, mode="new")
    s.clear_selection()
    body = s.document.bodies[-1].id
    g = s.geometry(body)
    fine = [f["index"] for f in g["faces"] if not (f["kind"] == "plane" and f["normal"][2] < -0.15)]
    s.set_view_resolution(0.02, 80.0, {body: fine})
    s.geometry(body)
    edges = [e["index"] for e in s.edges(body)["edges"] if e["kind"] == "line"][:6]
    samples = []
    t0 = time.perf_counter()
    for round_ in range(args.rounds):
        edge = edges[round_ % len(edges)]
        s.modify_selection([{"body": body, "kind": "edge", "index": edge}], [])
        for radius in (0.5, 0.6, 0.7):
            job = s.preview_job("fillet", {"radius": radius + 0.001 * round_})
            job.run()
            job.adopt()
        s.fillet(0.7 + 0.001 * round_)
        s.geometry(body)
        s.undo()
        s.geometry(body)
        s.clear_selection()
        # カメラ: 粗さを行き来
        s.set_view_resolution(0.02 * (1.5 if round_ % 2 else 1.0), 80.0, {body: fine})
        s.geometry(body)
        if round_ % 10 == 9:
            s.modify_selection([{"body": body, "kind": "body", "index": None}], [])
            s.apply("mirror_body", {"base_plane": "YZ"})
            s.state_meta()
            s.undo()
            s.state_meta()
            s.clear_selection()
            gc.collect()
            current, _peak = tracemalloc.get_traced_memory()
            samples.append({
                "round": round_ + 1,
                "traced_mb": round(current / 2**20, 1),
                "rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
                "face_stores": sum(len(v) for v in s.view._face_cache.values()) + len(s.view._retired_faces),
                "cached_numbers": held_floats(s.view),
            })
            print(json.dumps(samples[-1]), flush=True)
    print(json.dumps({"seconds": round(time.perf_counter() - t0, 1)}))


main()
```

## 付録 C `aggregate.py`

```python
"""bench_all_ops と probe の JSON を中央値でまとめる(報告用)。

    python aggregate.py bench <base glob> <after glob>
    python aggregate.py probe <base glob> <after glob>
"""
import glob
import json
import statistics
import sys

KEYS = ("preview", "confirm", "push", "mcp", "mcp_push", "undo", "undo_push", "redo", "redo_push", "ms")


def med(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return statistics.median(values) if values else None


def bench(paths):
    table = {}
    for path in sorted(glob.glob(paths)):
        for row in json.load(open(path))["rows"]:
            for key in KEYS:
                if isinstance(row.get(key), (int, float)):
                    table.setdefault(row["case"], {}).setdefault(key, []).append(row[key])
            if row.get("error"):
                table.setdefault(row["case"], {}).setdefault("error", []).append(row["error"][:40])
    return table


def probe(paths):
    table = {}
    for path in sorted(glob.glob(paths)):
        for row in json.load(open(path)):
            entry = table.setdefault(row["case"], {})
            for key, value in row.items():
                if isinstance(value, (int, float)):
                    entry.setdefault(key, []).append(value)
                elif key in ("measured", "faces_measured"):
                    entry.setdefault(key, []).append(value)
    return table


def fmt(value):
    if value is None:
        return "-"
    return f"{value:,.0f}" if abs(value) >= 10 else f"{value:.1f}"


def main():
    kind, base_glob, after_glob = sys.argv[1:4]
    if kind == "bench":
        base, after = bench(base_glob), bench(after_glob)
        print(f"runs: base={len(glob.glob(base_glob))} after={len(glob.glob(after_glob))}")
        print("| case | key | base | after |")
        print("|---|---|---|---|")
        for case in base:
            for key in KEYS + ("error",):
                b = base[case].get(key)
                a = after.get(case, {}).get(key)
                if b is None and a is None:
                    continue
                if key == "error":
                    print(f"| {case} | error | {b and b[0]} | {a and a[0]} |")
                    continue
                print(f"| {case} | {key} | {fmt(med(b or []))} | {fmt(med(a or []))} |")
    else:
        base, after = probe(base_glob), probe(after_glob)
        print(f"runs: base={len(glob.glob(base_glob))} after={len(glob.glob(after_glob))}")
        print("| case | total base | total after | faces measured base | faces measured after | tessellate base | tessellate after | bytes base | bytes after |")
        print("|---|---|---|---|---|---|---|---|---|")
        for case in base:
            b, a = base[case], after.get(case, {})

            def count(entry):
                m = entry.get("measured") or entry.get("faces_measured")
                if not m:
                    return "-"
                first = m[0]
                if isinstance(first, list) and first and isinstance(first[0], dict):
                    return "+".join(str(sum(d.values())) for d in first)
                if isinstance(first, list):
                    return "+".join(str(x) for x in first) or "0"
                return str(first)

            print(f"| {case} | {fmt(med(b.get('total', [])))} | {fmt(med(a.get('total', [])))} | {count(b)} | {count(a)} | "
                  f"{fmt(med(b.get('tessellate_faces', [])))} | {fmt(med(a.get('tessellate_faces', [])))} | "
                  f"{fmt(med(b.get('bytes', [])))} | {fmt(med(a.get('bytes', [])))} |")


main()
```
