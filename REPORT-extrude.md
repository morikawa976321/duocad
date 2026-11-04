# 報告: 押し出し(担当 `extrude`、ブランチ `cloud/perf-extrude`)

基準は develop の `4ef67570`。後は `178f980`(このブランチの2つ目のコミット)。測った機械はクラウドの 4 コア(Intel Xeon 2.10GHz)・
メモリ 15 GB、Python 3.14.6、OCP 7.9.3.1。前後は同じ機械で交互に測った(基準のコードは `git archive 4ef67570` を一時フォルダへ
書き出して、同じ venv で走らせた)。

要点:

- **入れた変更は2つ**(どちらも `duocad/kernel_extrude.py`)。どちらも形・面と辺の番号・由来・保存形式・操作ログを変えない。
  1. 互いに離れた柱を、ブーリアンを掛けずに「順に fuse した結果と同じ並び」の compound に組む。60 領域の押し出しの確定が
     1,445 → 191 ms、ゴーストが 1,510 → 249 ms。
  2. 複数の面の押し出しで、押した面ごとに全部の面の法線・重心を測り直していたのを1回にする。20 面で行き先探しが 271〜275 → 15〜16 ms。
- 1つの領域・1つの面の押し出し(bench の 5 ケース)は変わらない。そこで残る時間の大半は `brep_operations.unify_same_domain` の中
  (big の join では BRepCheck 2回・統合の Build 2回・体積)・結果の体積・三角形分割で、どれも担当外のファイル。案と測った値を3章に書いた。
- `--part big` の押し出しでは、OCCT の外の後処理のうち最大は **同一平面の統合の中の BRepCheck 2回(region join で 0.90 + 0.86 秒)**。
  統合でできた平面の面だけが、同じ穴 385 個の面でも統合前の 4 倍かかる(写しで辺に pcurve を保存すると統合前と同じ速さに戻る)。

---

## 1. 測った内訳(前)

### 1.1 bench の押し出し(`--part big`、MCP の確定 = OCCT の計算と後処理)

`tools/bench_all_ops.py --docs part --part big --only extrude`(カメラの段を飛ばす包み `bench_wrap.py` は付録)。前の値は2章の表。
MCP の確定(`mcp`)の中を、`_combine_region_solids` と `unify_same_domain` を同じ順に写した段ごとの計測(付録 `stages.py`)と
cProfile で割った。

**領域 join(6×6 の正方形で板を貫く)、MCP の確定 2.4 秒の内訳**(`stages.py big`、3 回の中央値、ms):

| 段 | ms | どこ |
|---|---|---|
| 柱(MakePrism)・柱の体積 | 0.8 | kernel_extrude |
| 相手の体積 `volume(target)` | 54(実際の経路では覚えた値に当たり 0) | brep_geometry |
| ブーリアン(fuse)本体 | 209 | OCCT |
| 結果の体積(無変化の検査) `_checked_volume` | 49 | brep_checks |
| 統合: 試しの写し `BRepBuilderAPI_Copy` | 17 | brep_operations |
| 統合: 試しの `ShapeUpgrade_UnifySameDomain.Build` | 90 | brep_operations |
| 統合: 面の数の比較 | 0.4 | brep_operations |
| 統合: 試しの結果の体積 | 54 | brep_operations |
| **統合: 試しの結果の BRepCheck** | **903** | brep_operations → brep_checks |
| 統合: 本番の `Build` | 97 | brep_operations |
| **統合: 本番の結果の BRepCheck** | **861** | brep_operations → brep_checks |
| 面の表・由来(`target_successors`)・射影 | 3 | brep_history / brep_topology |
| 最後の体積 `volume(final)` | 58 | brep_geometry |

**領域 cut(貫通)、0.6 秒**: ブーリアン 191、BRepCheck(統合しないので1回)298、体積 51 + 49、由来 2(ms)。

**面 join 0.5(リブの上面)、MCP の確定 996 ms**(cProfile): BRepCheck 2回 618、体積の積分 3回 152、統合の Build 2回 94、
ブーリアン 70、行き先の面探し 14、自動保存 15、由来 7。

**`--part small`(59 面)の領域 join、93 ms の内訳**(`stages.py small`、5 回の中央値、ms): ブーリアン 25、BRepCheck 2回 10.8 + 10.3、
体積 4 回 5.9〜6.7 ずつ(相手の体積は実際の経路では覚えた値)、統合の Build 2回 4.9 + 5.3、写し 2.0、由来 0.5。cut は 46 ms
(ブーリアン 23、BRepCheck 9、体積 6.8)。小さい部品では後処理が全体の 7 割を占めるが、絶対値は小さい。

**領域 join のゴースト 4.3 秒**(cProfile、`preview_job.run`): 統合 2,019(うち BRepCheck 2回 1,744)、三角形分割 1,867
(`IMeshTools.DiscretizeFaces` 1,478)、ブーリアン 293、体積 186、残り 50 以下。

### 1.2 BRepCheck が統合の後で遅い理由

面ごとに `BRepCheck_Analyzer(face)` を掛けて測った(`check_faces.py`、`merged_face_why.py`):

| 形 | 一番遅い面 | その面の検査 | 形全体(並列) |
|---|---|---|---|
| 元の板(485 面) | 穴 385 の底面 | 171 ms | 329 ms |
| fuse の結果(500 面) | 穴 382 の底面 | 165 ms | 276 ms |
| **統合の結果(493 面)** | **穴 385 の底面(統合でできた面)** | **705〜846 ms** | **850〜989 ms** |
| cut の結果(494 面) | 穴 382 の底面 | 174 ms | 320 ms |
| 統合の結果に pcurve を足した写し | 同じ面 | **181〜242 ms** | **288〜307 ms** |

写しの平面の面の辺へ `BRepLib.BuildPCurveForEdgeOnPlane` で pcurve(面の上の 2D 曲線)を保存すると(9〜10 ms)、統合でできた面の検査は
687〜705 → 181〜242 ms、形全体は 868〜989 → 288〜307 ms になる(2 回測った。合否はどちらも妥当)。統合でできていない面(穴 314 の上面)は
同じことをしても 200〜217 → 202〜227 ms で変わらないので、遅さは統合でできた面に限られる。平面の pcurve は保存されていなければ呼ばれるたびに射影で作られる
(`BRep_Tool::CurveOnSurface` の注記)ので、統合でできた面の辺はその面の pcurve を保存しておらず、BRepCheck の穴どうしの検査
(穴の数の2乗の組)のたびに作り直していると見られる(保存の有無を OCP から直接は読めず、推定)。

### 1.3 60 領域の柱の結合(反例の形)

長方形 150 個のスケッチ(10×10、ピッチ 12)から 60 領域を選んで `new` で押し出す(`tools/bench_server_ops.py` の B と同じ配置)。
`kernel_extrude.extrude_regions` の 1,474 ms のうち `fuse_all` が 1,355 ms(順に 59 回の fuse。1回あたり 22 ms で、束が太るほど伸びる)、
統合の試し 24 ms(面が減らないので採らない)、BRepCheck 33 ms。

### 1.4 QR1 の約 0.3 秒について(推定。実データはクラウドに無い)

QR1 の押し出し(領域・結合)は、統合が採られるかで経路が分かれる:

- **統合を採らない(面が減らない)**: ブーリアン → 結果の体積 1 回 → 写し → 試しの統合の Build → 面の数の比較で止まる → BRepCheck 1 回
  → 最後の体積は覚えた値(結果がそのまま最後の形)。ゴーストではこの後に新しい面の三角形分割。
- **統合を採る**: 上に加えて、試しの体積・試しの BRepCheck・本番の Build・本番の BRepCheck・最後の体積(big の領域 join と同じ)。

docs の実測(733 面で BRepCheck 77 ms・体積の積分 1 回 0.10〜0.15 秒)を当てると、採らない経路は体積 0.10〜0.15 + BRepCheck 0.08 +
写しと試しの Build + 新しい面の三角形分割で、0.3 秒とほぼ合う。採る経路なら BRepCheck 2 回と体積 3 回だけで 0.45〜0.6 秒になり
0.3 秒を超えるので、QR1 の押し出しは採らない経路と見られる。その場合の残りは「結果の体積 1 回」「BRepCheck 1 回」「捨てる試しの統合
(big で写し 17 + Build 90 ms)」で、どれも担当外の関数の中(3章)。確かめる手順は5章。

---

## 2. 入れた変更

### 2.1 `f6ee12e` 離れた柱はブーリアンを掛けずに、順の fuse と同じ並びの compound に組む

**何を**: `_combine_region_solids`(領域の押し出し・回転体)と `extrude_faces` が柱を束ねる `brep_operations.fuse_all` の前に、
`kernel_extrude._fused_solids` を置いた。柱が全部 SOLID で、どの2つも境界箱が離れているときだけ、ブーリアンを掛けずに
(p[n-1], …, p[2], p[0], p[1]) の並びの compound を組み、fuse の後処理と同じ許容差の補正2つ(`BOPTools_AlgoTools.CorrectTolerances` /
`CorrectShapeTolerances`)を掛ける。それ以外(触れる・重なる・compound の柱 = 穴のあるテーパー)は今までどおり `fuse_all`。
`brep_operations.py` は変えていない。

**なぜこの並びで同じになるか**(OCCT 7.9.3 の原文で確かめた):

- `BOPAlgo_BOP::BuildResult` は引数を型ごとの段で `myShape` へ入れる(SOLID の段が COMPOUND の段より先)。`fuse_all` の2回目以降は
  「束(compound)+ 新しい柱(SOLID)」なので、新しい柱が先・束の中身が後になる。1回目は SOLID 2つなので引数の順。
- FUSE の `BuildRC` は `myShape` を SOLID で辿り、`BuildSolid` は他の立体と面を共有せず像の無い SOLID を「触られていない」として
  そのまま compound に入れる(作り直さない)。
- ブーリアンが組を調べるのは境界箱(`BOPDS_DS::Init` が `BRepBndLib::Add` で面・辺の箱を取り、頂点は許容差ぶん広げ、
  `Precision::Confusion` の半分の余白を足す)が重なる部分形状だけ。こちらは同じ関数で面・辺・頂点の箱を足し、1e-3 mm 広げた箱が
  重ならないことを条件にした(三角形分割を持つ面でも同じ箱になる)。組の候補が無いので、交差の計算も pcurve の書き足し
  (`PaveFiller::Prepare`)も起きない。
- 後処理 `BOPAlgo_Builder::PostTreat` は結果全体に許容差の補正を掛け、入力の形をその場で書き換える。同じ2つを1回掛ける
  (補正は部分形状ごとで、離れた柱は部分形状を共有しないので、毎回掛けても最後に1回でも同じ)。

**形を変えないと言える根拠**:

| 確かめ | 結果 |
|---|---|
| 前のコード(柱を `fuse_all`)と今のコードで、同じ入力を別々に作り直して掛け、結果の **BRep の書き出し**(子の並び・許容差・幾何・pcurve)・由来の JSON・体積を比べる(`e2e_compare.py`、乱数 600 ケース) | **全部一致**。近道を通った呼び出しは 534 回。ケースの内訳は new 149・join(箱)67・cut(箱)64・join(`--part small` の板)68・revolve 75・面の押し出し 53・テーパー 56・頂点の許容差が足りない形 68 |
| 上の最後の種類(頂点を辺の端から 2e-5 mm ずらし許容差 1e-7)で、許容差の補正を外す | 30 ケースの試しに入っていたこの種類 4 ケースが全部食い違う(ほかの 26 ケースは一致)→ 補正が要り、確かめが効いている |
| 前のコードで作って保存した 64 文書(離れた領域の new / join / cut / revolve / テーパー、触れる領域、どれも後に添字で辺を丸め・面を押し・面取り)を、前のコードとこのコミットのコードで再生し `tools/replay_fingerprint.py --compare` | **64 文書、0 件違う**(開く合計 15.9 → 11.5 秒) |
| 並びを素直な順にする・補正を外す、の2つの書き換えで `tests/test_extrude_apart_solids.py` | それぞれ 5 件・2 件落ちる |

**前後の値**(中央値。`regions_session.py`、長方形 150 個から 60 領域、3 回):

| 操作 | 確定(apply) | ゴースト(preview_job.run) | 面の数 |
|---|---|---|---|
| 60 領域 `new` | 1,445 → **191** ms | 1,510 → **249** ms | 360 → 360 |
| 60 領域を板へ `join` | 1,456 → **194** ms | 1,577 → **292** ms | 306 → 306 |
| (2 組目)60 領域 `new` | 1,357 → **151** ms | 1,376 → **299** ms | 360 → 360 |
| (2 組目)60 領域を板へ `join` | 1,364 → **197** ms | 1,545 → **299** ms | 306 → 306 |

1 回目(そのプロセスで最初の押し出し)と 2 回目以降に差は無い(生の値は付録 A.2。例: `new` の確定は前 1,445 / 1,473 / 1,287、
後 191 / 201 / 141)。プロセスの最大 RSS は前後とも約 470 MB(`new` 471 → 470、`join` 477 → 477)。

1つの領域・1つの面(bench の 5 ケース)は近道を通らない(柱が1本)ので変わらない(2章の表)。

### 2.2 `178f980` 面の押し出しの行き先探しで、面の法線と重心を押した面ごとに測り直さない

**何を**: `extrude_faces` の `after_face_indices` は、押した面ごとに結果の全部の面の法線を求め、向きの合う面の重心(面積分)を
測っていた(`_find_translated_face`)。押す面はどれも同じ法線なので、向きの合う面と重心を1回だけ求めて、押した面ごとに近いものを選ぶ
(`_find_translated_faces`)。許容差・同じ距離のときの選び方(添字の小さい方)・NaN の扱い(`<` で外す書き方)は前のまま。

**形を変えないと言える根拠**: 変わるのは由来の `after_face_indices` の求め方だけで、形には触らない。離れた柱の上面を乱数の順に
1〜29 枚選び join / cut / new で押す 80 ケースで、1つ目のコミットのコードと由来の JSON(`after_face_indices` を含む)がバイト単位で一致
(`lids_dump.py`)。押した順のふたを指すことをテストに足した。

**前後の値**(`faces_many.py big <n>`、`--part big` の板に柱を立てた 609 面の形。3 回の中央値を 2 組):

| 押した面 | 行き先探し | `extrude_faces` 全体 |
|---|---|---|
| 20 | 271〜275 → **15〜16** ms | 1,403〜1,496 → **1,165〜1,222** ms |
| 6 | 77〜81 → **12〜15** ms | 1,093〜1,136 → 1,058〜1,081 ms |
| 1 | 約 14 ms(変わらない) | 変わらない |

---

## 3. 入れなかった案

### 3.1 統合の2回目の BRepCheck を省く(`brep_operations.unify_same_domain`、担当外)

統合は写しで試して(a)面が減る(b)体積が同じ(c)BRepCheck が妥当、を確かめてから元の形に本番を掛け、本番の結果にもう一度
BRepCheck を掛ける。本番は同じ入力(添字の数え方も幾何も同じ写し)に同じ決定的な処理を掛けるので、2回目の検査は試しと同じ答えになる
はず。`--part big` で 1 回あたり 0.30〜0.86 秒(領域 join 861 ms、面 join 約 300 ms)。統合が採られるときだけ効く(QR1 の押し出しは
1.4 の推定では採らない経路なので、効かない見込み)。
**入れなかった理由**: `brep_operations.py` は `boolean` の担当。2回目を「面の数と体積が試しと同じか」の安い比べに替える案として渡す
(壊れた本番を通さない、という今の関門の意図は残る)。

### 3.2 試しの体積を本番の結果に引き継ぐ(`unify_same_domain`、担当外)

試しの結果と本番の結果は同じ幾何・同じ面の並びなので、`kernel_extrude` が最後に測る `volume(final)` は試しの体積と同じになる
はず。`brep_geometry.note_volume(unified, 試しの体積)` で 1 回ぶんの積分(big で 58 ms、733 面で 0.10〜0.15 秒)が消える(統合が採られるときだけ)。
**入れなかった理由**: 同上。積分の結果がビット単位で同じかは未確認(同じ関数・同じ並びなので同じと見るが、確かめてから入れる)。

### 3.3 試しの統合の結果に pcurve を足してから BRepCheck を掛ける(`unify_same_domain`、担当外)

1.2 の通り、統合でできた平面の面は(pcurve を保存していないと見られ)BRepCheck が 4 倍遅い。試しの結果は検査が済めば捨てる写しなので、そこへ
`BRepLib.BuildPCurveForEdgeOnPlane` で pcurve を足しても、文書に入る形は変わらない。big の領域 join で試しの検査が 868〜989 → 288〜307 ms
(+9〜10 ms)。3.1 と合わせると、統合の検査 2 回 1.76 秒が約 0.3 秒になる。
**入れなかった理由**: 担当外。加えて、pcurve を保存した形と都度射影する形とで BRepCheck の合否が同じかは、妥当な形でしか
確かめられていない(壊れた形の例がクラウドに無い)。本番の結果に足す案(以後の操作も速くなる)は、形のデータが変わるので
低リスクではない(面分割・後のブーリアンが保存された pcurve を使う)。

### 3.4 触れ合う・重なる柱の結合を1回の多入力 fuse にする

`touching_multi.py 60 3`(10×10 の柱 60 本を格子に並べ、順の fuse と、1つを引数・残り 59 を道具にした1回の fuse を比べる。3 回の中央値):

| 並べ方 | 順の fuse(今) | 1回の多入力 fuse | 面の数 | 面の並び | 面の集まり |
|---|---|---|---|---|---|
| 辺を共有して触れ合う | 617 ms | 135 ms | 152 / 152 | **違う** | 同じ |
| 1 mm ずつ重なる | 2,644 ms | 566 ms | 478 / 478 | **違う** | 同じ |

**入れなかった理由**: 前回保留した理由(面の並びが変わる)が測定でもそのまま残る。触れ合う柱は作り直されるので、並びは
`BOPAlgo_BuilderSolid` の内側で決まり、順の fuse と同じ並びを組み立てる方法が無い。離れた柱だけを近道にしたのは、作り直されない
ことを原文と BRep の書き出しの一致で示せるから。

### 3.5 面が減らないときの試しの統合を省く(`unify_same_domain`、担当外。案だけ)

統合は面が減らないと試しの結果を捨てるが、写しと Build は払う(big の領域 join で 17 + 90 ms、小さい部品で 2 + 5 ms)。面が減るか
を Build の前に安く見分けられれば省けるが、減るか(同じ平面・同じ円筒の隣り合う面があるか)を正しく当てる判定は統合そのものに近く、
外すと統合されない形が残って番号が変わる。測っていない。

### 3.6 そのほか測ってやめたもの

- 領域の面の指紋 `kernel.shapes_digest`(覚えた結果の鍵): 穴 385 の領域で 1.2〜2.0 ms、385 領域まとめて 7 ms。小さい。
- 柱ごとの体積の検査が `brep_geometry` の体積の覚え(64 件)を押し出す: 画面の配信はボディごとに別に覚えている(`view._volume_cache`)
  ので、後の配信は積分し直さない。効果なし。
- 結果の体積(無変化の検査)`_checked_volume` を省く: 「足したのに何も変わっていない」を成功として通さない関門なので残す。
- 1つの領域の押し出しのゴーストの残り(三角形分割 1.9 秒・写し)は `kernel_meshing` / `session` の担当。

### 3.7 ファイルの大きさ

`duocad/kernel_extrude.py` は 1,603 → 1,658 行(1,500 行を超えていた)。切れ目はロフトの節(`loft_regions` 以下、約 530 行で
共有状態に触らない)。切るのはこの担当の範囲を超えるので、`kernel_loft.py` へ出す案として残す。

---

## 4. テスト

| コマンド | 結果 |
|---|---|
| `uv run pytest -q -p no:cacheprovider tests/test_extrude_apart_solids.py` | 9 passed |
| `uv run pytest -q -p no:cacheprovider tests/test_extrude*.py tests/test_coplanar_seams.py tests/test_revolve*.py tests/test_region*.py tests/test_selection.py -n 4` | 222 passed |
| `uv run pytest -q -p no:cacheprovider`(全件、Node 22.23・`web/node_modules` 無し) | 2,765 passed・39 failed・5 skipped。39 件は基準のコード(`kernel_extrude.py` を 4ef67570 に戻す)でも同じ 39 件が落ちる |
| 上の 39 件を Node 24.11.1(一時フォルダに入れた)と `npm ci --prefix web` の後で | 34 件が通り、5 件が落ちる。5 件は基準のコードでも落ちる(下) |

落ちたまま残る 5 件(どれも基準でも落ちる。この変更と関係しない):

- `tests/test_channel.py` 3 件: 更新の口が 409 でなく 501 を返す(クラウドのチェックアウトの枝・リモートの状態)
- `tests/test_web_scripts.py::test_start_server_stops_the_python_server_when_the_start_fails`: Windows の venv の場所
  (`.venv/Scripts/python.exe`)を前提にしている
- `tests/test_loft.py::test_a_smooth_loft_that_pinches_between_sections_is_rejected`: Linux の OCP で、くびれるロフトが断られない
  (ロフトのコードは触っていない。基準でも落ちる)

足したテスト(`tests/test_extrude_apart_solids.py`):

- 離れた柱の近道が、new / join / cut / revolve / 頂点の許容差が足りない形 / 面の押し出しの 6 種で、順の fuse と BRep の書き出し・由来・
  体積まで一致する(近道を通ったことも確かめる)
- 許容差が足りない形が、本当に補正を要する(上のテストが補正を確かめている前提)
- 触れる・重なる・1e-4 mm しか離れていない・compound の柱は近道に入らず、触れる2つは1つの箱(6 面)に繋がる。1e-2 mm 離れていれば入る
- 複数の面を押したとき、`after_face_indices` が選んだ順の面のふたを指す

---

## 5. 親に確かめてほしいこと

1. **保存済み 206 文書の再生の指紋**: 前(4ef67570)と後(178f980)で `tools/replay_fingerprint.py --compare`。クラウドで確かめたのは
   合成の 64 文書だけ。とくに「離れた領域を複数選んだ押し出し・回転体」「同じ平面の離れた面をまとめて押した面の押し出し」を含む文書。
2. **QR1 の押し出しの内訳**: 1.4 の推定(統合を採らない経路で、0.3 秒の大半が結果の体積 1 回・BRepCheck 1 回・捨てる試しの統合・新しい面の
   三角形分割)を、付録の `bench_wrap.py` で cProfile を取って確かめる:
   `DUOCAD_REPO=. .venv/Scripts/python.exe -X utf8 bench_wrap.py --docs step --step <QR1 の STEP> --only "extrude 領域 join" --profile`
   (bench の `--profile` はカメラの段も包むので、カメラを飛ばす包みで取る)。3.1〜3.3 の効果の見積もりもこれで決まる。
3. **STEP 由来の面を複数選んだ面の押し出し**: 許容差の大きい面(STEP の取り込み)で近道に入ったとき、後処理の補正が元のボディの面の
   許容差を書き換える(前のコードの fuse も同じことをする)。合成の試験では一致したが、本人の部品で前後の再生が一致するか。
4. **ブラウザでの見た目**: 60 領域の押し出しのゴーストと確定(三角形の数・配信の大きさは同じ形なので変わらないはずだが、実機で)。
5. **3.1〜3.3・3.5 の採否**: `boolean` の担当(`brep_operations.py`)と重なるので、どちらの枝で入れるかを決めてほしい。

---

## 付録 A. 生の測定値

### A.1 bench(`bench_wrap.py`、カメラの段を飛ばす。ms、前 → 後、3 回の中央値)

#### --part small(ms、3 回の中央値。前 → 後)

| ケース | preview | confirm | push | mcp | mcp_push | undo | redo |
|---|---|---|---|---|---|---|---|
| extrude 領域 new | 8 → 8 | 3 → 4 | 10 → 10 | 3 → 3 | 6 → 6 | 1 → 1 | 3 → 3 |
| extrude 領域 join(ボディを貫く) | 122 → 120 | 3 → 3 | 4 → 4 | 86 → 84 | 44 → 45 | 1 → 2 | 2 → 2 |
| extrude 領域 cut(貫通) | 77 → 73 | 3 → 3 | 4 → 3 | 44 → 43 | 40 → 38 | 2 → 1 | 2 → 2 |
| extrude 面 join 0.5 | 101 → 118 | 3 → 3 | 2 → 1 | 91 → 66 | 29 → 28 | 1 → 1 | 1 → 1 |
| extrude 側面 join 1 | 109 → 105 | 3 → 3 | 1 → 1 | 81 → 90 | 32 → 35 | 1 → 1 | 1 → 1 |

生の値:
```json
{"extrude 領域 new": {"base": {"preview": [8, 8, 9], "confirm": [3, 3, 4], "push": [10, 9, 11], "mcp": [3, 3, 3], "mcp_push": [6, 6, 7], "undo": [1, 1, 2], "redo": [3, 3, 3], "preview_bytes": [2740, 2744, 2748], "mcp_bytes": [7112, 7112, 7112], "push_bytes": [8548, 8548, 8548]}, "new": {"preview": [8, 8, 9], "confirm": [4, 3, 4], "push": [10, 10, 10], "mcp": [3, 3, 3], "mcp_push": [6, 8, 6], "undo": [1, 1, 20], "redo": [3, 3, 22], "preview_bytes": [2744, 2748, 2744], "mcp_bytes": [7112, 7112, 7112], "push_bytes": [8544, 8544, 8548]}}, "extrude 領域 join(ボディを貫く)": {"base": {"preview": [111, 122, 135], "confirm": [3, 3, 5], "push": [4, 4, 5], "mcp": [86, 82, 95], "mcp_push": [43, 44, 46], "undo": [2, 1, 1], "redo": [2, 2, 2], "preview_bytes": [109844, 109844, 109844], "mcp_bytes": [52648, 52648, 52648], "push_bytes": [53144, 53144, 53144]}, "new": {"preview": [135, 120, 114], "confirm": [4, 3, 3], "push": [5, 4, 3], "mcp": [81, 84, 85], "mcp_push": [45, 37, 53], "undo": [2, 1, 2], "redo": [2, 2, 2], "preview_bytes": [109844, 109840, 109844], "mcp_bytes": [52648, 52652, 52648], "push_bytes": [53144, 53144, 53144]}}, "extrude 領域 cut(貫通)": {"base": {"preview": [70, 77, 89], "confirm": [3, 3, 3], "push": [3, 4, 4], "mcp": [43, 44, 57], "mcp_push": [40, 39, 48], "undo": [1, 2, 2], "redo": [2, 2, 2], "preview_bytes": [109688, 109692, 109688], "mcp_bytes": [52484, 52488, 52484], "push_bytes": [52984, 52984, 52984]}, "new": {"preview": [91, 73, 70], "confirm": [3, 3, 3], "push": [3, 3, 3], "mcp": [43, 44, 41], "mcp_push": [52, 38, 37], "undo": [2, 1, 1], "redo": [2, 2, 1], "preview_bytes": [109688, 109688, 109688], "mcp_bytes": [52488, 52488, 52488], "push_bytes": [52984, 52984, 52984]}}, "extrude 面 join 0.5": {"base": {"preview": [100, 101, 110], "confirm": [3, 3, 2], "push": [2, 1, 2], "mcp": [91, 75, 102], "mcp_push": [37, 29, 29], "undo": [3, 1, 1], "redo": [3, 1, 1], "preview_bytes": [107940, 107940, 107940], "mcp_bytes": [28380, 28380, 28380], "push_bytes": [27452, 27452, 27452]}, "new": {"preview": [118, 94, 141], "confirm": [3, 3, 4], "push": [1, 1, 1], "mcp": [72, 66, 66], "mcp_push": [28, 31, 28], "undo": [1, 1, 1], "redo": [2, 1, 1], "preview_bytes": [107940, 107940, 107936], "mcp_bytes": [28380, 28384, 28380], "push_bytes": [27452, 27452, 27452]}}, "extrude 側面 join 1": {"base": {"preview": [107, 109, 109], "confirm": [3, 3, 2], "push": [1, 1, 1], "mcp": [81, 80, 88], "mcp_push": [34, 29, 32], "undo": [2, 1, 1], "redo": [1, 1, 1], "preview_bytes": [107960, 107960, 107960], "mcp_bytes": [28400, 28400, 28400], "push_bytes": [27472, 27472, 27472]}, "new": {"preview": [103, 117, 105], "confirm": [3, 4, 2], "push": [1, 1, 1], "mcp": [78, 90, 100], "mcp_push": [27, 41, 35], "undo": [1, 2, 1], "redo": [1, 1, 3], "preview_bytes": [107960, 107960, 107960], "mcp_bytes": [28404, 28400, 28400], "push_bytes": [27472, 27472, 27472]}}}
```

#### --part big(ms、3 回の中央値。前 → 後)

| ケース | preview | confirm | push | mcp | mcp_push | undo | redo |
|---|---|---|---|---|---|---|---|
| extrude 領域 new | 38 → 36 | 8 → 9 | 13 → 15 | 9 → 8 | 14 → 12 | 6 → 5 | 8 → 8 |
| extrude 領域 join(ボディを貫く) | 4159 → 4110 | 21 → 32 | 18 → 20 | 2419 → 2481 | 1836 → 1823 | 6 → 9 | 6 → 7 |
| extrude 領域 cut(貫通) | 2304 → 2378 | 21 → 21 | 22 → 20 | 687 → 672 | 1709 → 1779 | 6 → 6 | 6 → 7 |
| extrude 面 join 0.5 | 2447 → 2419 | 20 → 21 | 15 → 15 | 1000 → 1047 | 1404 → 1531 | 5 → 7 | 6 → 8 |
| extrude 側面 join 1 | 2463 → 2542 | 19 → 19 | 18 → 14 | 1092 → 1112 | 1345 → 1526 | 5 → 6 | 5 → 5 |

生の値:
```json
{"extrude 領域 new": {"base": {"preview": [38, 33, 46], "confirm": [8, 8, 14], "push": [13, 12, 22], "mcp": [9, 9, 9], "mcp_push": [16, 13, 14], "undo": [6, 6, 6], "redo": [8, 8, 8], "preview_bytes": [2744, 2748, 2744], "mcp_bytes": [23828, 23828, 23824], "push_bytes": [25272, 25276, 25272]}, "new": {"preview": [33, 46, 36], "confirm": [8, 9, 9], "push": [13, 15, 15], "mcp": [7, 8, 8], "mcp_push": [12, 13, 12], "undo": [5, 6, 5], "redo": [7, 11, 8], "preview_bytes": [2748, 2744, 2744], "mcp_bytes": [23824, 23828, 23828], "push_bytes": [25272, 25272, 25272]}}, "extrude 領域 join(ボディを貫く)": {"base": {"preview": [4014, 4159, 5083], "confirm": [20, 21, 23], "push": [17, 18, 19], "mcp": [2308, 2419, 2617], "mcp_push": [1836, 1766, 1948], "undo": [5, 6, 6], "redo": [6, 6, 7], "preview_bytes": [1480856, 1480860, 1480856], "mcp_bytes": [696460, 696456, 696456], "push_bytes": [696976, 696976, 696976]}, "new": {"preview": [4110, 4250, 4105], "confirm": [32, 22, 35], "push": [19, 20, 30], "mcp": [2423, 2481, 2489], "mcp_push": [1715, 1823, 1913], "undo": [6, 10, 9], "redo": [7, 12, 7], "preview_bytes": [1480856, 1480860, 1480860], "mcp_bytes": [696460, 696460, 696464], "push_bytes": [696976, 696976, 696976]}}, "extrude 領域 cut(貫通)": {"base": {"preview": [2233, 2304, 2431], "confirm": [27, 19, 21], "push": [18, 22, 32], "mcp": [687, 617, 787], "mcp_push": [1709, 1649, 1925], "undo": [6, 6, 12], "redo": [6, 7, 6], "preview_bytes": [1481300, 1481300, 1481300], "mcp_bytes": [696408, 696408, 696408], "push_bytes": [696928, 696928, 696928]}, "new": {"preview": [2318, 2378, 2379], "confirm": [18, 21, 29], "push": [17, 20, 31], "mcp": [672, 634, 689], "mcp_push": [1745, 1896, 1779], "undo": [6, 6, 6], "redo": [6, 7, 10], "preview_bytes": [1481300, 1481300, 1481300], "mcp_bytes": [696408, 696408, 696408], "push_bytes": [696928, 696928, 696928]}}, "extrude 面 join 0.5": {"base": {"preview": [2179, 2447, 2615], "confirm": [18, 20, 22], "push": [13, 17, 15], "mcp": [1000, 998, 1057], "mcp_push": [1404, 1357, 1646], "undo": [5, 5, 10], "redo": [6, 6, 6], "preview_bytes": [1484500, 1484500, 1484500], "mcp_bytes": [346780, 346780, 346780], "push_bytes": [345852, 345852, 345852]}, "new": {"preview": [2292, 2419, 2584], "confirm": [21, 19, 23], "push": [14, 15, 20], "mcp": [1047, 1095, 988], "mcp_push": [1389, 1531, 1545], "undo": [7, 6, 10], "redo": [9, 8, 6], "preview_bytes": [1484504, 1484504, 1484500], "mcp_bytes": [346784, 346780, 346776], "push_bytes": [345852, 345852, 345852]}}, "extrude 側面 join 1": {"base": {"preview": [2333, 2463, 2525], "confirm": [18, 21, 19], "push": [15, 18, 26], "mcp": [1040, 1092, 1146], "mcp_push": [1256, 1345, 1505], "undo": [5, 5, 6], "redo": [5, 5, 8], "preview_bytes": [1484524, 1484520, 1484520], "mcp_bytes": [346800, 346800, 346804], "push_bytes": [345876, 345876, 345876]}, "new": {"preview": [2390, 2695, 2542], "confirm": [19, 19, 18], "push": [14, 14, 14], "mcp": [1075, 1112, 1166], "mcp_push": [1426, 1526, 1526], "undo": [6, 6, 9], "redo": [5, 7, 5], "preview_bytes": [1484520, 1484520, 1484524], "mcp_bytes": [346800, 346800, 346800], "push_bytes": [345876, 345876, 345876]}}}
```

1つの柱の押し出しは近道を通らないので、前後の差は揺れの範囲(配信の大きさも同じ。±4 バイトは基準の回どうしでも出る)。
着手直後に測った1回目の big(カメラの段込み)はゴーストが 6〜9 秒だったが、同じ機械で別の計測を並べて走らせていたので捨てた。

### A.2 60 領域(`regions_session.py`)

```json
// 1 組目
{"repo": "/tmp/claude-0/-home-user-duocad/c2ce15ca-6a00-577e-b385-39402af037a6/scratchpad/base", "mode": "new", "count": 60, "apply_median": 1445, "preview_median": 1510, "apply": [1445, 1473, 1287], "preview": [1384, 1545, 1510], "faces": 360}
{"repo": "/home/user/duocad", "mode": "new", "count": 60, "apply_median": 191, "preview_median": 249, "apply": [191, 201, 141], "preview": [249, 250, 231], "faces": 360}
{"repo": "/tmp/claude-0/-home-user-duocad/c2ce15ca-6a00-577e-b385-39402af037a6/scratchpad/base", "mode": "join", "count": 60, "apply_median": 1456, "preview_median": 1577, "apply": [1447, 1515, 1456], "preview": [1634, 1577, 1504], "faces": 306}
{"repo": "/home/user/duocad", "mode": "join", "count": 60, "apply_median": 194, "preview_median": 292, "apply": [203, 194, 188], "preview": [365, 292, 268], "faces": 306}
// 2 組目(最大 RSS も)
{"repo": "/tmp/claude-0/-home-user-duocad/c2ce15ca-6a00-577e-b385-39402af037a6/scratchpad/base", "mode": "new", "count": 60, "max_rss_mb": 471, "apply_median": 1357, "preview_median": 1376, "apply": [1357, 1442, 1301], "preview": [1317, 1502, 1376], "faces": 360}
{"repo": "/home/user/duocad", "mode": "new", "count": 60, "max_rss_mb": 470, "apply_median": 151, "preview_median": 299, "apply": [164, 151, 145], "preview": [267, 299, 309], "faces": 360}
{"repo": "/tmp/claude-0/-home-user-duocad/c2ce15ca-6a00-577e-b385-39402af037a6/scratchpad/base", "mode": "join", "count": 60, "max_rss_mb": 477, "apply_median": 1364, "preview_median": 1545, "apply": [1486, 1360, 1364], "preview": [1454, 1545, 1570], "faces": 306}
{"repo": "/home/user/duocad", "mode": "join", "count": 60, "max_rss_mb": 477, "apply_median": 197, "preview_median": 299, "apply": [197, 230, 193], "preview": [334, 299, 273], "faces": 306}
```

### A.3 確かめの出力

```
e2e_compare.py 600 1000
  bad         cases  68 mismatches 0  old   16835 ms  new    2809 ms
  cut box     cases  64 mismatches 0  old   20761 ms  new    4098 ms
  faces       cases  53 mismatches 0  old   49319 ms  new   19813 ms
  join box    cases  67 mismatches 0  old   16263 ms  new    3723 ms
  join plate  cases  68 mismatches 0  old    7172 ms  new    6188 ms
  new         cases 149 mismatches 0  old   36102 ms  new    6249 ms
  revolve     cases  75 mismatches 0  old   32777 ms  new   32342 ms
  taper       cases  56 mismatches 0  old   18023 ms  new   17827 ms
total mismatches 0; fast path taken 534 times

tools/replay_fingerprint.py --compare(基準 → 1つ目のコミット / 基準 → 2つ目のコミット)
64 documents, 0 differ. open total 15906 ms -> 11524 ms
64 documents, 0 differ. open total 15906 ms -> 9200 ms

lids_dump.py 80(1つ目のコミット と 2つ目のコミット の出力を cmp): IDENTICAL
```

## 付録 B. 再現の手順と台本

基準のコードは `git archive 4ef67570 duocad tools | tar -x -C <base>` で書き出し、`DUOCAD_REPO=<base>` で同じ venv から走らせた。
台本はこのブランチには入れていない(書いてよいファイルの外)ので、主なものをここに載せる。

台本の中の一時フォルダの絶対パス(`/tmp/claude-0/...`)は、クラウドの作業場所。手元では置き場所に合わせて替える。

<details><summary><code>bench_wrap.py</code> — bench の押し出しだけを、カメラの段を飛ばして測る(前後の表 A.1 と 5章の cProfile)</summary>

```python
"""tools/bench_all_ops.py をカメラ・選択の段を飛ばして走らせる(押し出しの前後を何度も測るため)。

    DUOCAD_REPO=<dir> python bench_wrap.py <bench_all_ops の引数...>
"""
import os
import runpy
import sys
from pathlib import Path

repo = Path(os.environ.get("DUOCAD_REPO", "/home/user/duocad"))
sys.path.insert(0, str(repo / "tools"))
sys.path.insert(0, str(repo))
import bench_all_ops as bench  # noqa: E402

assert Path(bench.__file__).resolve().parents[1] == repo.resolve(), bench.__file__
bench.camera_and_selection = lambda *args, **kwargs: None
sys.argv = [str(repo / "tools" / "bench_all_ops.py"), *sys.argv[1:]]
bench.main()
```

</details>

<details><summary><code>make_fixture.py</code> — bench の合成の部品を作り、ボディを BRep に書き出す(`stages.py` などの題材。`<fx>/big.brep`)</summary>

```python
"""bench の合成の部品(big/small)を作り、ボディの形を BRep に書き出す(押し出しの段ごとの計測の題材)。"""
import sys
import time
from pathlib import Path

REPO = Path("/home/user/duocad")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from OCP.BRepTools import BRepTools  # noqa: E402

import bench_all_ops as bench  # noqa: E402
from duocad import kernel  # noqa: E402
from duocad.session import Session  # noqa: E402
import tempfile  # noqa: E402

out = Path(sys.argv[1])
size = sys.argv[2]
out.mkdir(parents=True, exist_ok=True)
s = Session(kernel.DEFAULT_DEVIATION, models_dir=Path(tempfile.mkdtemp()))
t0 = time.perf_counter()
body_id = bench.build_part(s, **bench.PARTS[size])
print("built", time.perf_counter() - t0)
shape = s.document.get_body(body_id).shape
BRepTools.Write_s(shape, str(out / f"{size}.brep"))
print("faces", kernel._brep_topology.face_count(shape))
```

</details>

<details><summary><code>stages.py</code> — 領域の押し出しの後処理を段ごとに測る(1.1 の表)</summary>

```python
"""押し出し(領域・面)の後処理を段ごとに測る。kernel_extrude._combine_region_solids と brep_operations.unify_same_domain を
同じ順で写し、段ごとの時間を出す。最後に本物の extrude_regions を呼んで全体の時間も出す。

    uv run python stages.py <fx dir> <big|small> [repeat]
"""
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/user/duocad")

from OCP.BRep import BRep_Builder  # noqa: E402
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism  # noqa: E402
from OCP.BRepTools import BRepTools  # noqa: E402
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain  # noqa: E402
from OCP.TopoDS import TopoDS, TopoDS_Shape  # noqa: E402
from OCP.gp import gp_Pnt, gp_Vec  # noqa: E402

from duocad import brep_checks, brep_geometry, brep_history, brep_operations, brep_topology, kernel, kernel_extrude  # noqa: E402

fx = Path(sys.argv[1])
size = sys.argv[2]
repeat = int(sys.argv[3]) if len(sys.argv) > 3 else 3


def load():
    shape = TopoDS_Shape()
    BRepTools.Read_s(shape, str(fx / f"{size}.brep"), BRep_Builder())
    return shape


def rect(cx, cy, w, z=0.0):
    poly = BRepBuilderAPI_MakePolygon(
        gp_Pnt(cx - w / 2, cy - w / 2, z), gp_Pnt(cx + w / 2, cy - w / 2, z),
        gp_Pnt(cx + w / 2, cy + w / 2, z), gp_Pnt(cx - w / 2, cy + w / 2, z), True)
    return BRepBuilderAPI_MakeFace(poly.Wire(), True).Face()


T = {}


def tick(name, fn):
    t0 = time.perf_counter()
    out = fn()
    T.setdefault(name, []).append((time.perf_counter() - t0) * 1000)
    return out


def staged(target, faces, distance, mode):
    offset = gp_Vec(0, 0, distance)
    prisms = []
    for face in faces:
        prism = tick("prism", lambda: BRepPrimAPI_MakePrism(TopoDS.Face_s(face), offset).Shape())
        tick("prism volume", lambda: brep_geometry.volume(prism))
        prisms.append(prism)
    combined = tick("fuse_all", lambda: brep_operations.fuse_all(prisms, "x"))
    volume_before = tick("volume(target)", lambda: brep_geometry.volume(target))
    builder = tick("boolean", lambda: brep_operations.build_boolean("fuse" if mode == "join" else "cut", target, combined))
    result = builder.Shape()
    volume_after = tick("checked_volume(result)", lambda: brep_checks._checked_volume(result, mode, volume_before=volume_before))
    final, history, unified = result, None, False
    if mode == "join":
        copy = tick("unify: copy", lambda: BRepBuilderAPI_Copy(result).Shape())
        trial = ShapeUpgrade_UnifySameDomain(copy, True, True, False)
        tick("unify: trial Build", lambda: trial.Build())
        candidate = trial.Shape()
        nc = tick("unify: face counts", lambda: (brep_topology._face_map(candidate).Extent(), brep_topology._face_map(result).Extent()))
        ok = nc[0] < nc[1]
        if ok:
            vc = tick("unify: volume(candidate)", lambda: brep_geometry.volume(candidate))
            ok = abs(vc - volume_after) <= 1e-6 * max(1.0, abs(volume_after))
        if ok:
            ok = tick("unify: is_valid(candidate)", lambda: brep_checks.is_valid(candidate))
        if ok:
            unify = ShapeUpgrade_UnifySameDomain(result, True, True, False)
            tick("unify: real Build", lambda: unify.Build())
            brep_geometry.forget_volume(result)
            final = unify.Shape()
            tick("unify: is_valid(unified)", lambda: brep_checks.is_valid(final))
            history, unified = unify.History(), True
    if not unified:
        tick("require_valid(final)", lambda: brep_checks._require_valid(final, mode))
    final_map = tick("face_map(final)", lambda: brep_topology._face_map(final))
    successors = tick("target_successors", lambda: brep_history.target_successors(builder, target, history, final_map))
    tick("projection", lambda: brep_topology._face_map_projection(successors, final_map.Extent()))
    tick("volume(final)", lambda: brep_geometry.volume(final))
    return final, unified


def main():
    base = load()
    bbox = kernel.geometry(base) if hasattr(kernel, "geometry") else None
    print("faces", brep_topology.face_count(base))
    cases = {
        "region join through": ("join", lambda: [rect(0.0, 0.0, 6.0)], 23.0),
        "region cut through": ("cut", lambda: [rect(0.0, 0.0, 6.0)], 23.0),
    }
    for name, (mode, make, distance) in cases.items():
        T.clear()
        totals = []
        real = []
        for _ in range(repeat):
            target = kernel.copy_shape(base)
            t0 = time.perf_counter()
            staged(target, make(), distance, mode)
            totals.append((time.perf_counter() - t0) * 1000)
            target = kernel.copy_shape(base)
            t0 = time.perf_counter()
            kernel_extrude.extrude_regions(make(), distance, mode, (0, 0, 1), target_shape=target)
            real.append((time.perf_counter() - t0) * 1000)
        print(f"\n## {name}  staged total median {statistics.median(totals):.0f} ms, real extrude_regions {statistics.median(real):.0f} ms")
        for stage, values in T.items():
            print(f"  {stage:<30} {statistics.median(values):8.1f} ms  (n={len(values)})")


if __name__ == "__main__":
    main()
```

</details>

<details><summary><code>check_faces.py</code> — BRepCheck の時間を面ごとに割る(1.2 の表)</summary>

```python
"""BRepCheck の時間がどの面に掛かっているか(面ごとに BRepCheck_Analyzer を掛ける)。"""
import sys
import time

sys.path.insert(0, "/home/user/duocad")
sys.path.insert(0, "/tmp/claude-0/-home-user-duocad/c2ce15ca-6a00-577e-b385-39402af037a6/scratchpad")

from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy  # noqa: E402
from OCP.BRepCheck import BRepCheck_Analyzer  # noqa: E402
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain  # noqa: E402
from OCP.TopAbs import TopAbs_WIRE  # noqa: E402

from duocad import brep_operations, brep_topology, kernel  # noqa: E402



def timed(fn):
    t0 = time.perf_counter()
    out = fn()
    return (time.perf_counter() - t0) * 1000, out


def per_face(shape, label):
    fmap = brep_topology._face_map(shape)
    rows = []
    for i in range(1, fmap.Extent() + 1):
        face = fmap.FindKey(i)
        t, ok = timed(lambda: BRepCheck_Analyzer(face, True, False).IsValid())
        rows.append((t, i, brep_topology._extrude_count(face, TopAbs_WIRE), ok))
    rows.sort(reverse=True)
    total = sum(r[0] for r in rows)
    print(f"{label}: faces {len(rows)} sum per-face {total:.0f} ms; top: " + ", ".join(f"#{i} {t:.0f}ms wires={w}" for t, i, w, _ in rows[:4]))
    t, ok = timed(lambda: BRepCheck_Analyzer(shape, True, True).IsValid())
    print(f"   whole parallel {t:.0f} ms valid={ok}")
    t, ok = timed(lambda: BRepCheck_Analyzer(shape, True, False).IsValid())
    print(f"   whole serial   {t:.0f} ms valid={ok}")


from stages import load, rect  # noqa: E402

base = load()
target = kernel.copy_shape(base)
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism  # noqa: E402
from OCP.gp import gp_Vec  # noqa: E402

per_face(target, "base plate")
prism = BRepPrimAPI_MakePrism(rect(0, 0, 6), gp_Vec(0, 0, 23)).Shape()
fuse = brep_operations.build_boolean("fuse", target, prism).Shape()
per_face(fuse, "join fuse result")
trial = ShapeUpgrade_UnifySameDomain(BRepBuilderAPI_Copy(fuse).Shape(), True, True, False)
trial.Build()
per_face(trial.Shape(), "join unified candidate")
target2 = kernel.copy_shape(base)
cut = brep_operations.build_boolean("cut", target2, prism).Shape()
per_face(cut, "cut result")
```

</details>

<details><summary><code>merged_face_why.py</code> — 統合でできた面の BRepCheck が pcurve の保存で速くなるか(1.2)</summary>

```python
"""統合した面の BRepCheck が遅い理由を探る: 写しの平面の辺へ pcurve を保存したら速くなるか。"""
import sys
import time

sys.path.insert(0, "/home/user/duocad")
sys.path.insert(0, "/tmp/claude-0/-home-user-duocad/c2ce15ca-6a00-577e-b385-39402af037a6/scratchpad")

from OCP.BRep import BRep_Tool  # noqa: E402
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy  # noqa: E402
from OCP.BRepCheck import BRepCheck_Analyzer  # noqa: E402
from OCP.BRepLib import BRepLib  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism  # noqa: E402
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain  # noqa: E402
from OCP.TopAbs import TopAbs_EDGE, TopAbs_WIRE  # noqa: E402
from OCP.TopExp import TopExp_Explorer  # noqa: E402
from OCP.TopoDS import TopoDS  # noqa: E402
from OCP.gp import gp_Vec  # noqa: E402

from duocad import brep_operations, brep_topology, kernel  # noqa: E402
from stages import load, rect  # noqa: E402


def timed(fn):
    t0 = time.perf_counter()
    out = fn()
    return (time.perf_counter() - t0) * 1000, out


base = load()
target = kernel.copy_shape(base)
prism = BRepPrimAPI_MakePrism(rect(0, 0, 6), gp_Vec(0, 0, 23)).Shape()
fuse = brep_operations.build_boolean("fuse", target, prism).Shape()
trial = ShapeUpgrade_UnifySameDomain(BRepBuilderAPI_Copy(fuse).Shape(), True, True, False)
trial.Build()
cand = trial.Shape()
fmap = brep_topology._face_map(cand)
slow = None
for i in range(1, fmap.Extent() + 1):
    f = TopoDS.Face_s(fmap.FindKey(i))
    if brep_topology._extrude_count(f, TopAbs_WIRE) > 300:
        t, _ = timed(lambda: BRepCheck_Analyzer(f, True, False).IsValid())
        print(f"candidate face #{i} wires={brep_topology._extrude_count(f, TopAbs_WIRE)} check {t:.0f} ms")
        if t > 500:
            slow = f
bmap = brep_topology._face_map(base)
for i in range(1, bmap.Extent() + 1):
    f = TopoDS.Face_s(bmap.FindKey(i))
    if brep_topology._extrude_count(f, TopAbs_WIRE) > 300:
        t, _ = timed(lambda: BRepCheck_Analyzer(f, True, False).IsValid())
        print(f"base face #{i} wires={brep_topology._extrude_count(f, TopAbs_WIRE)} check {t:.0f} ms")

# pcurve を作ってから測り直す(写しで)
copy = BRepBuilderAPI_Copy(cand).Shape()
t0 = time.perf_counter()
cm = brep_topology._face_map(copy)
for i in range(1, cm.Extent() + 1):
    f = TopoDS.Face_s(cm.FindKey(i))
    exp = TopExp_Explorer(f, TopAbs_EDGE)
    while exp.More():
        BRepLib.BuildPCurveForEdgeOnPlane_s(TopoDS.Edge_s(exp.Current()), f)
        exp.Next()
print("BuildPCurveForEdgesOnPlane ms", (time.perf_counter() - t0) * 1000)
cmap = brep_topology._face_map(copy)
for i in range(1, cmap.Extent() + 1):
    f = TopoDS.Face_s(cmap.FindKey(i))
    if brep_topology._extrude_count(f, TopAbs_WIRE) > 300:
        t, _ = timed(lambda: BRepCheck_Analyzer(f, True, False).IsValid())
        print(f"with pcurves face #{i} check {t:.0f} ms")
t, ok = timed(lambda: BRepCheck_Analyzer(copy, True, True).IsValid())
print("whole with pcurves", t, ok)
t, ok = timed(lambda: BRepCheck_Analyzer(cand, True, True).IsValid())
print("whole candidate", t, ok)
```

</details>

<details><summary><code>verb_profile.py</code> — Session の上で MCP の確定・ゴーストを cProfile する(1.1 の面 join・ゴーストの内訳)</summary>

```python
"""押し出しの各ケースを Session の上で、MCP の確定(apply)とプレビュー(preview_job.run)に分けて cProfile する。

    uv run python verb_profile.py <big|small> <case substring> [mcp|preview|both] [top]
"""
import cProfile
import io
import pstats
import sys
import tempfile
import time
from pathlib import Path

REPO = Path("/home/user/duocad")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import bench_all_ops as bench  # noqa: E402
from duocad import kernel, kernel_worker  # noqa: E402
from duocad.session import Session  # noqa: E402

size, needle = sys.argv[1], sys.argv[2]
which = sys.argv[3] if len(sys.argv) > 3 else "both"
top = int(sys.argv[4]) if len(sys.argv) > 4 else 30

s = Session(kernel.DEFAULT_DEVIATION, models_dir=Path(tempfile.mkdtemp()))
body_id = bench.build_part(s, **bench.PARTS[size])
link = bench.Wire(s)
link.push()
cases = [c for c in bench.cases_for(s, body_id, None) if needle in c[0]]
name, verb, setup = cases[0]
print("case", name)


def show(profile, label, elapsed):
    print(f"\n===== {label}: {elapsed:.0f} ms")
    out = io.StringIO()
    stats = pstats.Stats(profile, stream=out)
    stats.sort_stats("cumulative").print_stats(top)
    print("\n".join(line for line in out.getvalue().splitlines() if line.strip())[:12000])
    out = io.StringIO()
    stats = pstats.Stats(profile, stream=out)
    stats.sort_stats("tottime").print_stats(15)
    print("\n".join(line for line in out.getvalue().splitlines() if line.strip())[:6000])


before = len(s.document.log.entries)
if which in ("mcp", "both"):
    args = setup()
    p = cProfile.Profile()
    t0 = time.perf_counter()
    p.enable()
    s.apply(verb, args)
    p.disable()
    show(p, "mcp apply", (time.perf_counter() - t0) * 1000)
    p = cProfile.Profile()
    t0 = time.perf_counter()
    p.enable()
    link.push()
    p.disable()
    print(f"\n===== mcp push: {(time.perf_counter() - t0) * 1000:.0f} ms")
    while len(s.document.log.entries) > before:
        s.undo()
    s.clear_selection()
    link.push()
if which in ("preview", "both"):
    args = setup()
    job = s.preview_job(verb, args)
    p = cProfile.Profile()
    t0 = time.perf_counter()
    p.enable()
    with kernel_worker.preview_context():
        job.run()
    p.disable()
    show(p, "preview run", (time.perf_counter() - t0) * 1000)
```

</details>

<details><summary><code>regions_session.py</code> — 60 領域の押し出しの確定・ゴースト・最大 RSS(2.1 の表)</summary>

```python
"""長方形 150 個のスケッチから 60 領域を選んで押し出す(bench_server_ops の B と同じ配置)を、Session の確定(apply)と
プレビュー(preview_job.run)で測る。DUOCAD_REPO で測るコードを選ぶ。

    DUOCAD_REPO=<dir> python regions_session.py <mode new|join> [count] [repeat]
"""
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(os.environ.get("DUOCAD_REPO", "/home/user/duocad"))
sys.path.insert(0, str(REPO))

from duocad import kernel, kernel_worker  # noqa: E402
from duocad.session import Session  # noqa: E402
from duocad import brep_topology  # noqa: E402

mode = sys.argv[1] if len(sys.argv) > 1 else "new"
count = int(sys.argv[2]) if len(sys.argv) > 2 else 60
repeat = int(sys.argv[3]) if len(sys.argv) > 3 else 3


def build():
    s = Session(kernel.DEFAULT_DEVIATION, models_dir=Path(tempfile.mkdtemp()))
    body = None
    if mode == "join":
        s.apply("sketch_create", {"base_plane": "XY"})
        sid = s.state_meta()["sketches"][-1]["sketch_id"]
        s.apply("add_rectangle", {"sketch_id": sid, "corner": [-5, -5], "width": 190, "height": 130})
        s.leave_sketch()
        s.modify_selection(add=[{"sketch": sid, "index": 1}])
        s.apply("extrude", {"distance": -4, "mode": "new"})
        body = s.document.bodies[-1].id
        s.clear_selection()
    s.apply("sketch_create", {"base_plane": "XY"})
    sid = s.state_meta()["sketches"][-1]["sketch_id"]
    for i in range(150):
        s.apply("add_rectangle", {"sketch_id": sid, "corner": [(i % 15) * 12, (i // 15) * 12], "width": 10, "height": 10})
    s.leave_sketch()
    return s, sid, body


rows = {"apply": [], "preview": [], "faces": None}
for _ in range(repeat):
    s, sid, body = build()
    refs = [{"sketch": sid, "index": i} for i in range(1, count + 1)]
    args = {"distance": 5, "mode": mode, **({"body_id": body} if body else {})}
    s.modify_selection(add=refs)
    job = s.preview_job("extrude", args)
    t0 = time.perf_counter()
    with kernel_worker.preview_context():
        job.run()
    rows["preview"].append((time.perf_counter() - t0) * 1000)
    s2, sid2, body2 = build()
    s2.modify_selection(add=[{"sketch": sid2, "index": i} for i in range(1, count + 1)])
    t0 = time.perf_counter()
    s2.apply("extrude", {"distance": 5, "mode": mode, **({"body_id": body2} if body2 else {})})
    rows["apply"].append((time.perf_counter() - t0) * 1000)
    rows["faces"] = brep_topology.face_count(s2.document.bodies[-1].shape)
import resource  # noqa: E402

print(json.dumps({"repo": str(REPO), "mode": mode, "count": count, "max_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024),
                  "apply_median": round(statistics.median(rows["apply"])), "preview_median": round(statistics.median(rows["preview"])),
                  "apply": [round(v) for v in rows["apply"]], "preview": [round(v) for v in rows["preview"]], "faces": rows["faces"]}))
```

</details>

<details><summary><code>e2e_compare.py</code> — 前後のコードで BRep の書き出し・由来・体積を比べる(2.1 の確かめ。`FX` を `make_fixture.py` の出力に)</summary>

```python
"""前のコード(柱を `fuse_all` で順に束ねる)と今のコード(離れた柱は組むだけ)で、同じ入力を別々に作り直して掛け、
結果の形の BRep の書き出しと由来(provenance)・体積が完全に一致するかを比べる。

    uv run python e2e_compare.py <trials> <seed0>
"""
import json
import math
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, "/home/user/duocad")

from OCP.BRep import BRep_Builder, BRep_Tool  # noqa: E402
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeWire  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox  # noqa: E402
from OCP.BRepTools import BRepTools  # noqa: E402
from OCP.TopAbs import TopAbs_VERTEX  # noqa: E402
from OCP.TopoDS import TopoDS, TopoDS_Shape  # noqa: E402
from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pnt  # noqa: E402

from duocad import brep_operations, brep_topology, kernel_extrude  # noqa: E402

NEW = kernel_extrude._fused_solids
OLD = lambda solids, what: brep_operations.fuse_all(solids, what)  # noqa: E731
TMP = tempfile.mkdtemp()
FX = "/tmp/claude-0/-home-user-duocad/c2ce15ca-6a00-577e-b385-39402af037a6/scratchpad/fx"


def rect_wire(x, y, w, h, z):
    return BRepBuilderAPI_MakePolygon(gp_Pnt(x, y, z), gp_Pnt(x + w, y, z), gp_Pnt(x + w, y + h, z), gp_Pnt(x, y + h, z), True).Wire()


def circle_wire(x, y, r, z):
    return BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(gp_Circ(gp_Ax2(gp_Pnt(x, y, z), gp_Dir(0, 0, 1)), r)).Edge()).Wire()


def region(rng, x, y, size, z, bad_vertex=False):
    kind = rng.choice(["rect", "circle", "holed", "poly"])
    if kind == "rect":
        face = BRepBuilderAPI_MakeFace(rect_wire(x, y, rng.uniform(0.2, 1) * size, rng.uniform(0.2, 1) * size, z), True).Face()
    elif kind == "circle":
        face = BRepBuilderAPI_MakeFace(circle_wire(x + size / 2, y + size / 2, rng.uniform(0.1, 0.5) * size, z), True).Face()
    elif kind == "holed":
        maker = BRepBuilderAPI_MakeFace(rect_wire(x, y, size, size, z), True)
        hole = circle_wire(x + size / 2, y + size / 2, size / 4, z)
        hole.Reverse()
        maker.Add(hole)
        face = maker.Face()
    else:
        k = rng.randint(3, 8)
        poly = BRepBuilderAPI_MakePolygon()
        for i in range(k):
            poly.Add(gp_Pnt(x + size / 2 + size / 2.2 * math.cos(2 * math.pi * i / k), y + size / 2 + size / 2.2 * math.sin(2 * math.pi * i / k), z))
        poly.Close()
        face = BRepBuilderAPI_MakeFace(poly.Wire(), True).Face()
    if bad_vertex:
        # 頂点を曲線の端から 2e-5 ずらし、許容差を 1e-7 のままにする(ブーリアンの後処理が許容差を上げる形)
        explorer = brep_topology.indexed_map(face, TopAbs_VERTEX)
        vertex = TopoDS.Vertex_s(explorer.FindKey(1))
        p = BRep_Tool.Pnt_s(vertex)
        BRep_Builder().UpdateVertex(vertex, gp_Pnt(p.X() + 2e-5, p.Y(), p.Z()), 1e-7)
    return face


def regions(seed, n, z=0.0, bad=False, cols=None):
    rng = random.Random(seed)
    cells = list(range(n * 3))
    rng.shuffle(cells)
    pitch = rng.uniform(5, 20)
    size = pitch * rng.uniform(0.3, 0.95)
    cols = cols or rng.randint(2, 12)
    return [region(rng, (c % cols) * pitch + rng.uniform(-0.5, 0.5), (c // cols) * pitch, size, z, bad_vertex=bad and i == 0)
            for i, c in enumerate(cells[:n])]


def plate():
    shape = TopoDS_Shape()
    BRepTools.Read_s(shape, f"{FX}/small.brep", BRep_Builder())
    return shape


def box_for(n):
    return BRepPrimAPI_MakeBox(gp_Pnt(-5, -5, -8), 400.0, 400.0, 8.0).Shape()


def dump(shape):
    path = os.path.join(TMP, "s.brep")
    BRepTools.Write_s(shape, path)
    with open(path) as f:
        return f.read()


def run(which, make):
    kernel_extrude._fused_solids = which
    try:
        t0 = time.perf_counter()
        out = make()
        elapsed = time.perf_counter() - t0
    except ValueError as error:
        return ("error", str(error)), 0.0
    finally:
        kernel_extrude._fused_solids = NEW
    prov = json.dumps(out["provenance"], sort_keys=True)
    return (dump(out["shape"]), prov, repr(out["volume_after"]), out.get("after_face_indices")), elapsed


def cases(seed):
    rng = random.Random(seed * 7 + 1)
    n = rng.choice([2, 3, 4, 6, 9, 15, 30, 60])
    kind = rng.choice(["new", "new", "join box", "cut box", "join plate", "revolve", "faces", "taper", "bad"])
    taper = 4.0 if kind == "taper" else 0.0
    if kind in ("new", "taper", "bad"):
        return kind, n, lambda: kernel_extrude.extrude_regions(regions(seed, n, bad=kind == "bad"), 5.0, "new", (0, 0, 1), taper=taper)
    if kind == "join box":
        return kind, n, lambda: kernel_extrude.extrude_regions(regions(seed, n, z=0.0), 3.0, "join", (0, 0, 1), target_shape=box_for(n))
    if kind == "cut box":
        return kind, n, lambda: kernel_extrude.extrude_regions(regions(seed, n, z=0.0), -4.0, "cut", (0, 0, 1), target_shape=box_for(n))
    if kind == "join plate":
        return kind, n, lambda: kernel_extrude.extrude_regions(regions(seed, min(n, 9), z=8.0, cols=3), 2.0, "join", (0, 0, 1), target_shape=plate())
    if kind == "revolve":
        return kind, n, lambda: kernel_extrude.revolve_regions(regions(seed, n), (-30, 0, 0), (0, 1, 0), (0, 0, 1), 90.0, "new")

    def faces_case():
        # 板に離れた柱を立て、その上面をまとめて押し出す
        base = kernel_extrude.extrude_regions(regions(seed, n, z=0.0), 3.0, "join", (0, 0, 1), target_shape=box_for(n))["shape"]
        fmap = brep_topology._face_map(base)
        tops = []
        for i in range(1, fmap.Extent() + 1):
            face = fmap.FindKey(i)
            from duocad import brep_geometry
            if brep_geometry.classify_face(face) == "plane":
                normal = brep_geometry._face_normal(face)
                centre = brep_geometry._face_props(face)[1]
                if normal[2] > 0.999 and abs(centre[2] - 3.0) < 1e-6:
                    tops.append(i)
        return kernel_extrude.extrude_faces(base, tops, 1.0, "join", target_shape=base)
    return kind, n, faces_case


FAST = [0]
_real_union = kernel_extrude._apart_union


def _counted(solids):
    FAST[0] += 1
    return _real_union(solids)


kernel_extrude._apart_union = _counted
if os.environ.get("NO_POSTTREAT"):
    def _plain(solids):
        FAST[0] += 1
        return brep_topology.compound([*reversed(solids[2:]), solids[0], solids[1]])
    kernel_extrude._apart_union = _plain

trials = int(sys.argv[1]) if len(sys.argv) > 1 else 40
seed0 = int(sys.argv[2]) if len(sys.argv) > 2 else 0
mismatch = 0
stats = {}
for seed in range(seed0, seed0 + trials):
    kind, n, make = cases(seed)
    old, t_old = run(OLD, make)
    new, t_new = run(NEW, make)
    same = old == new
    entry = stats.setdefault(kind, [0, 0, 0.0, 0.0])
    entry[0] += 1
    entry[1] += 0 if same else 1
    entry[2] += t_old
    entry[3] += t_new
    if not same:
        mismatch += 1
        which = [name for name, a, b in zip(("brep", "provenance", "volume", "after"), old, new) if a != b]
        print(f"MISMATCH seed={seed} kind={kind} n={n}: {which}")
for kind, (count, bad, t_old, t_new) in sorted(stats.items()):
    print(f"  {kind:<11} cases {count:3d} mismatches {bad}  old {t_old * 1000:7.0f} ms  new {t_new * 1000:7.0f} ms")
print(f"total mismatches {mismatch}; fast path taken {FAST[0]} times")
```

</details>

<details><summary><code>make_corpus.py</code> — 離れた領域の押し出しの後に添字で操作する文書を作る(`DUOCAD_REPO=<基準>` で作り、`tools/replay_fingerprint.py` で前後を再生)</summary>

```python
"""押し出しの変更(離れた柱の近道・行き先の面の探し方)が効く操作を含む文書を作り、保存する。後の操作が面・辺の添字を指すので、
再生の指紋(tools/replay_fingerprint.py)が前後のコードで一致すれば番号も一致している。

    uv run python make_corpus.py <out dir>
"""
import random
import sys
import tempfile
import traceback
from pathlib import Path

import os
REPO = Path(os.environ.get("DUOCAD_REPO", "/home/user/duocad"))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import bench_all_ops as bench  # noqa: E402
from duocad import kernel  # noqa: E402
from duocad.session import Session  # noqa: E402

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)


def session():
    return Session(kernel.DEFAULT_DEVIATION, models_dir=out)


def sketch(s, plane="XY"):
    s.apply("sketch_create", {"base_plane": plane} if plane else {})
    return bench.last_sketch(s)


def regions_of(s):
    return s.state_meta()["sketches"][-1]["regions"]


def select_regions(s, sid, indices):
    bench.select(s, [{"sketch": sid, "index": i} for i in indices])


def last_body(s):
    return s.document.bodies[-1].id


def planes(s, body, nz=None, z=None):
    faces = [f for f in s.geometry(body)["faces"] if f["kind"] == "plane"]
    if nz is not None:
        faces = [f for f in faces if abs(f["normal"][2] - nz) < 1e-6]
    if z is not None:
        faces = [f for f in faces if abs(f["centroid"][2] - z) < 1e-6]
    return faces


def follow_ups(s, body, rng):
    """後の操作: 添字で辺を丸め、添字で面を押し、面にスケッチを貼って押し出す(番号がずれると別の形になる)。"""
    edges = [e for e in s.edges(body)["edges"] if e["kind"] == "line" and e["length"] > 2.0]
    rng.shuffle(edges)
    for edge in edges[:6]:
        try:
            bench.select(s, [bench.edge_ref(body, edge["index"])])
            s.apply("fillet", {"radius": 0.4})
            break
        except Exception:  # noqa: BLE001
            s.clear_selection()
    tops = planes(s, body, nz=1.0)
    rng.shuffle(tops)
    if tops:
        bench.select(s, [bench.face_ref(body, tops[0]["index"])])
        try:
            s.apply("extrude", {"distance": 1.5, "mode": "join"})
        except Exception:  # noqa: BLE001
            pass
    edges = [e for e in s.edges(body)["edges"] if e["kind"] == "line" and e["length"] > 1.0]
    rng.shuffle(edges)
    for edge in edges[:6]:
        try:
            bench.select(s, [bench.edge_ref(body, edge["index"])])
            s.apply("chamfer", {"distance": 0.3})
            break
        except Exception:  # noqa: BLE001
            s.clear_selection()
    s.clear_selection()


def grid_sketch(s, plane, count, rng, pitch=12.0, size=10.0, z_offset=None, holes=False, circles=False):
    sid = sketch(s, plane)
    cols = rng.randint(3, 10)
    for i in range(count):
        x, y = (i % cols) * pitch, (i // cols) * pitch
        if circles and i % 2:
            s.apply("add_circle", {"sketch_id": sid, "center": [x + size / 2, y + size / 2], "diameter": size * 0.8})
        else:
            s.apply("add_rectangle", {"sketch_id": sid, "corner": [x, y], "width": size, "height": size * rng.uniform(0.5, 1.0)})
            if holes and i % 3 == 0:
                s.apply("add_circle", {"sketch_id": sid, "center": [x + size / 2, y + size / 4], "diameter": size / 5})
    s.leave_sketch()
    return sid


def doc_new_many(rng, name, count, holes, circles, taper=0.0):
    s = session()
    sid = grid_sketch(s, "XY", count, rng, holes=holes, circles=circles)
    regs = regions_of(s)
    # 穴の領域(内側の円)は選ばない: 面積の大きい順に count 個
    chosen = sorted(regs, key=lambda r: -r["area"])[:count]
    select_regions(s, sid, [r["index"] for r in chosen])
    s.apply("extrude", {"distance": 5.0, "mode": "new", **({"taper": taper} if taper else {})})
    follow_ups(s, last_body(s), rng)
    s.save_document(name)


def doc_join_plate(rng, name, count, mode):
    s = session()
    sid = sketch(s, "XY")
    s.apply("add_rectangle", {"sketch_id": sid, "corner": [-10, -10], "width": 150, "height": 100})
    s.leave_sketch()
    select_regions(s, sid, [1])
    s.apply("extrude", {"distance": 8.0, "mode": "new"})
    body = last_body(s)
    top = planes(s, body, nz=1.0, z=8.0)[0]
    bench.select(s, [bench.face_ref(body, top["index"])])
    rid = sketch(s, None)
    cols = rng.randint(3, 8)
    for i in range(count):
        x, y = (i % cols) * 14.0 - 5, (i // cols) * 14.0 - 5
        if i % 2:
            s.apply("add_circle", {"sketch_id": rid, "center": [x + 5, y + 5], "diameter": 7})
        else:
            s.apply("add_rectangle", {"sketch_id": rid, "corner": [x, y], "width": 9, "height": 6})
    s.leave_sketch()
    regs = [r for r in regions_of(s) if r["area"] < 1000]
    select_regions(s, rid, [r["index"] for r in regs])
    s.apply("extrude", {"distance": 4.0 if mode == "join" else -4.0, "mode": mode, "body_id": body})
    if mode == "join":
        tops = planes(s, body, nz=1.0, z=12.0)
        rng.shuffle(tops)
        pick = tops[: rng.randint(2, max(2, len(tops)))]
        bench.select(s, [bench.face_ref(body, f["index"]) for f in pick])
        s.apply("extrude", {"distance": 2.0, "mode": "join"})
    follow_ups(s, body, rng)
    s.save_document(name)


def doc_revolve(rng, name, count):
    s = session()
    sid = sketch(s, "XY")
    for i in range(count):
        s.apply("add_rectangle", {"sketch_id": sid, "corner": [i * 12.0, 5.0], "width": 8.0, "height": 6.0})
    s.leave_sketch()
    select_regions(s, sid, [r["index"] for r in regions_of(s)])
    s.apply("revolve", {"axis": "x", "angle": 120.0, "mode": "new"})
    follow_ups(s, last_body(s), rng)
    s.save_document(name)


def doc_adjacent(rng, name):
    s = session()
    sid = sketch(s, "XY")
    for i in range(4):
        s.apply("add_rectangle", {"sketch_id": sid, "corner": [i * 10.0, 0.0], "width": 10.0, "height": 10.0})
    s.apply("add_rectangle", {"sketch_id": sid, "corner": [60.0, 0.0], "width": 10.0, "height": 10.0})
    s.leave_sketch()
    select_regions(s, sid, [r["index"] for r in regions_of(s)])
    s.apply("extrude", {"distance": 5.0, "mode": "new"})
    follow_ups(s, last_body(s), rng)
    s.save_document(name)


made = 0
for seed in range(int(sys.argv[2]) if len(sys.argv) > 2 else 24):
    rng = random.Random(seed)
    kind = ["new", "new holes", "new circles", "join", "cut", "revolve", "taper", "adjacent"][seed % 8]
    name = f"corpus-{seed:03d}-{kind.replace(' ', '_')}.json"
    try:
        if kind == "new":
            doc_new_many(rng, name, rng.choice([5, 12, 30, 60]), False, False)
        elif kind == "new holes":
            doc_new_many(rng, name, rng.choice([6, 15]), True, False)
        elif kind == "new circles":
            doc_new_many(rng, name, rng.choice([6, 20]), False, True)
        elif kind == "join":
            doc_join_plate(rng, name, rng.choice([4, 9, 16]), "join")
        elif kind == "cut":
            doc_join_plate(rng, name, rng.choice([4, 9]), "cut")
        elif kind == "revolve":
            doc_revolve(rng, name, rng.choice([2, 4]))
        elif kind == "taper":
            doc_new_many(rng, name, rng.choice([4, 9]), True, False, taper=3.0)
        else:
            doc_adjacent(rng, name)
        made += 1
        print("made", name, flush=True)
    except Exception:  # noqa: BLE001
        print("FAILED", name)
        traceback.print_exc(limit=3)
print("documents", made)
```

</details>

<details><summary><code>faces_many.py</code> — 板に柱を立てた形で、上面 n 枚の面の押し出しを測る(2.2 の表)</summary>

```python
"""穴の多い板(big/small)に離れた柱を n 本立て、その上面 n 枚をまとめて押し出す(extrude_faces)を段ごとに測る。

    uv run python faces_many.py <big|small> <n> [repeat] [--profile]
"""
import cProfile
import pstats
import statistics
import sys
import time

import os
sys.path.insert(0, os.environ.get("DUOCAD_REPO", "/home/user/duocad"))
sys.path.insert(0, "/tmp/claude-0/-home-user-duocad/c2ce15ca-6a00-577e-b385-39402af037a6/scratchpad")

from duocad import brep_geometry, brep_topology, kernel, kernel_extrude  # noqa: E402
import stages  # noqa: E402

size, n = sys.argv[1], int(sys.argv[2])
repeat = int(sys.argv[3]) if len(sys.argv) > 3 and not sys.argv[3].startswith("-") else 3
stages.fx = __import__("pathlib").Path("/tmp/claude-0/-home-user-duocad/c2ce15ca-6a00-577e-b385-39402af037a6/scratchpad/fx")
stages.size = size
base = stages.load()
# 板の上面 z=8 に、リブ(y=-35..-32, -5..-2, 25..28)を避けた 2 列に 3x3 の柱を並べる
xs = [-54 + i * (108 / max(1, (n + 1) // 2)) for i in range((n + 1) // 2)]
ys = [-20.0, 10.0]
faces = [stages.rect(x + 2.0, y, 3.0, 8.0) for y in ys for x in xs][:n]
t0 = time.perf_counter()
bossed = kernel_extrude.extrude_regions(faces, 3.0, "join", (0, 0, 1), target_shape=base)
print(f"bosses join n={n}: {(time.perf_counter() - t0) * 1000:.0f} ms, faces {brep_topology.face_count(bossed['shape'])}")
shape = bossed["shape"]
fmap = brep_topology._face_map(shape)
tops = [i for i in range(1, fmap.Extent() + 1)
        if brep_geometry.classify_face(fmap.FindKey(i)) == "plane"
        and brep_geometry._face_normal(fmap.FindKey(i))[2] > 0.999
        and abs(brep_geometry._face_props(fmap.FindKey(i))[1][2] - 11.0) < 1e-6]
print("tops", len(tops))
times = []
find = []
NAME = "_find_translated_faces" if hasattr(kernel_extrude, "_find_translated_faces") else "_find_translated_face"
real_find = getattr(kernel_extrude, NAME)


def timed_find(*a, **k):
    t = time.perf_counter()
    try:
        return real_find(*a, **k)
    finally:
        find.append(time.perf_counter() - t)


setattr(kernel_extrude, NAME, timed_find)
p = cProfile.Profile() if "--profile" in sys.argv else None
for _ in range(repeat):
    target = kernel.copy_shape(shape)
    t0 = time.perf_counter()
    if p:
        p.enable()
    out = kernel_extrude.extrude_faces(target, tops, 1.0, "join", target_shape=target)
    if p:
        p.disable()
    times.append((time.perf_counter() - t0) * 1000)
print(f"extrude_faces {len(tops)} lids: median {statistics.median(times):.0f} ms; _find_translated_face total/run {sum(find) * 1000 / repeat:.0f} ms; after={out['after_face_indices'][:5]}...")
if p:
    pstats.Stats(p).sort_stats("cumulative").print_stats(18)
```

</details>

<details><summary><code>lids_dump.py</code> — 面の押し出しの由来を JSON 行で書く(2.2 の確かめ。前後の出力を `cmp`)</summary>

```python
"""離れた柱を立てた板の上面を、乱数で選んだ順にまとめて押し出し、由来(after_face_indices を含む)を JSON 行で書く。"""
import json, os, random, sys
sys.path.insert(0, os.environ["DUOCAD_REPO"])
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
from OCP.gp import gp_Pnt
from duocad import brep_geometry, brep_topology, kernel_extrude
for seed in range(int(sys.argv[1])):
    rng = random.Random(seed)
    n = rng.randint(2, 30)
    cols = rng.randint(2, 8)
    faces = []
    for i in range(n):
        x, y = (i % cols) * 11.0, (i // cols) * 11.0
        w, h = rng.uniform(2, 9), rng.uniform(2, 9)
        faces.append(BRepBuilderAPI_MakeFace(BRepBuilderAPI_MakePolygon(gp_Pnt(x, y, 0), gp_Pnt(x + w, y, 0), gp_Pnt(x + w, y + h, 0), gp_Pnt(x, y + h, 0), True).Wire(), True).Face())
    plate = BRepPrimAPI_MakeBox(gp_Pnt(-5, -5, -6), 120.0, 400.0, 6.0).Shape()
    height = rng.choice([1.0, 2.5, 4.0])
    base = kernel_extrude.extrude_regions(faces, height, "join", (0, 0, 1), target_shape=plate)["shape"]
    fmap = brep_topology._face_map(base)
    tops = [i for i in range(1, fmap.Extent() + 1) if brep_geometry.classify_face(fmap.FindKey(i)) == "plane"
            and brep_geometry._face_normal(fmap.FindKey(i))[2] > 0.999 and abs(brep_geometry._face_props(fmap.FindKey(i))[1][2] - height) < 1e-9]
    rng.shuffle(tops)
    picked = tops[: rng.randint(1, len(tops))]
    mode = rng.choice(["join", "cut", "new"])
    dist = rng.choice([0.5, 3.0]) if mode != "cut" else -rng.choice([0.5, 2.0])
    try:
        out = kernel_extrude.extrude_faces(base, picked, dist, mode, target_shape=None if mode == "new" else base)
        print(json.dumps({"seed": seed, "provenance": out["provenance"], "after": out["after_face_indices"]}, sort_keys=True))
    except ValueError as e:
        print(json.dumps({"seed": seed, "error": str(e)}))
```

</details>

<details><summary><code>touching_multi.py</code> — 触れ合う・重なる柱の、順の fuse と1回の多入力 fuse(3.4 の表)</summary>

```python
"""触れ合う柱(辺を共有する格子)を順に fuse する時間と、1回の多入力 fuse の時間・面の並びが同じかを比べる(案の測定)。

    uv run python touching_multi.py <n> [repeat]
"""
import statistics
import sys
import time

sys.path.insert(0, "/home/user/duocad")

from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse  # noqa: E402
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism  # noqa: E402
from OCP.TopTools import TopTools_ListOfShape  # noqa: E402
from OCP.gp import gp_Pnt, gp_Vec  # noqa: E402

from duocad import brep_geometry, brep_operations, brep_topology  # noqa: E402

n = int(sys.argv[1]) if len(sys.argv) > 1 else 60
repeat = int(sys.argv[2]) if len(sys.argv) > 2 else 3


def prisms(gap):
    out = []
    for i in range(n):
        x, y = (i % 10) * (10 + gap), (i // 10) * (10 + gap)
        poly = BRepBuilderAPI_MakePolygon(gp_Pnt(x, y, 0), gp_Pnt(x + 10, y, 0), gp_Pnt(x + 10, y + 10, 0), gp_Pnt(x, y + 10, 0), True)
        out.append(BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(poly.Wire(), True).Face(), gp_Vec(0, 0, 5)).Shape())
    return out


def multi(solids):
    fuse = BRepAlgoAPI_Fuse()
    args, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
    args.Append(solids[0])
    for s in solids[1:]:
        tools.Append(s)
    fuse.SetArguments(args)
    fuse.SetTools(tools)
    fuse.Build()
    return fuse.Shape()


def signature(shape):
    fmap = brep_topology._face_map(shape)
    return [tuple(round(v, 6) for v in brep_geometry._face_props(fmap.FindKey(i))[1]) for i in range(1, fmap.Extent() + 1)]


for label, gap in (("touching grid", 0.0), ("overlapping grid", -1.0)):
    seq, mul = [], []
    for _ in range(repeat):
        a = prisms(gap)
        t0 = time.perf_counter()
        r1 = brep_operations.fuse_all(a, "x")
        seq.append((time.perf_counter() - t0) * 1000)
        b = prisms(gap)
        t0 = time.perf_counter()
        r2 = multi(b)
        mul.append((time.perf_counter() - t0) * 1000)
    s1, s2 = signature(r1), signature(r2)
    print(f"{label} n={n}: sequential {statistics.median(seq):.0f} ms, one multi-argument fuse {statistics.median(mul):.0f} ms; "
          f"faces {len(s1)} vs {len(s2)}, same face order: {s1 == s2}, same face set: {sorted(s1) == sorted(s2)}")
```

</details>

<details><summary><code>medians.py</code> — bench の JSON から中央値の表を作る(A.1)</summary>

```python
"""bench2/ の JSON(part × base/new × 3 回)から、ケースごと・列ごとの中央値と生の値を表にする。"""
import json
import statistics
import sys
from pathlib import Path

root = Path(sys.argv[1])
cols = ("preview", "confirm", "push", "mcp", "mcp_push", "undo", "redo")
for part in ("small", "big"):
    table = {}
    for side in ("base", "new"):
        for i in (1, 2, 3):
            path = root / f"{part}_{side}_{i}.json"
            if not path.exists():
                continue
            for row in json.loads(path.read_text())["rows"]:
                if not row.get("case", "").startswith("extrude"):
                    continue
                cell = table.setdefault(row["case"], {}).setdefault(side, {})
                for c in cols + ("preview_bytes", "mcp_bytes", "push_bytes"):
                    if c in row:
                        cell.setdefault(c, []).append(row[c])
                if "error" in row:
                    cell.setdefault("error", []).append(row["error"])
    print(f"\n### --part {part}(ms、3 回の中央値。前 → 後)\n")
    print("| ケース | " + " | ".join(cols) + " |")
    print("|---|" + "---|" * len(cols))
    for case, sides in table.items():
        cells = []
        for c in cols:
            b, n = sides.get("base", {}).get(c), sides.get("new", {}).get(c)
            cells.append(f"{statistics.median(b):.0f} → {statistics.median(n):.0f}" if b and n else "-")
        print(f"| {case} | " + " | ".join(cells) + " |")
    print("\n生の値:")
    print("```json")
    print(json.dumps({case: {side: {c: [round(v) if isinstance(v, float) else v for v in vals] for c, vals in d.items()} for side, d in sides.items()} for case, sides in table.items()}, ensure_ascii=False))
    print("```")
```

</details>

