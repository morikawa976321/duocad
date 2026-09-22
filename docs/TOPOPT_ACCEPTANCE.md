# 最終形状の合格条件

現在の合格条件は、`models/hanger_bar_attachment.json` の最終出力の **水密性（自己干渉なし）・単一ソリッド** の2つ。
保持・除外は既定の合否判定から外し、平滑化後のCADブーリアン演算で反映する。

製品経路は `密度 → 島除去 → 表面生成 → Taubin平滑化 → CAD化 → (メッシュ－保持－除外)をまとめてcut → 保持ごとに除外とのcut → 除外済み保持をfuse → 水密・単一ソリッド検査 → 文書保存`。
重複領域は除外が優先する。演算のエラー・警告、空の結果、自己干渉、分断を成功扱いにしない。
結果はCADソリッドとしてB-rep本文を操作ログに保存し、再読込・undo/redoは外部ファイルに依存しない。
表示とSTLはそのCADの三角形近似。安全率は処理前の密度の解析値で、最終CADの再解析値ではない。

```powershell
.venv/Scripts/python.exe topopt_checks/check_final_shape.py --mesh result.stl --out output/acceptance.json
.venv/Scripts/python.exe topopt_checks/check_final_shape.py --model result.json --solid-body <body-id> --out output/solid_acceptance.json
.venv/Scripts/python.exe topopt_checks/replay_finalization.py output/optimization_runs/<run-id>
```

判定器は既定で2条件だけを出力する。旧保持・除外比較は調査用の `--check-constraints` で明示的に有効化する。
診断記録のCADは `--model-dir output/optimization_runs/<run-id> --model document_after.json` で指定できる。
`replay_finalization.py` はSIMPを再実行せず、保存済み平滑化後表面を製品と同じ後処理に通し、
`output/finalization_checks/` に各段階のB-rep、診断、成功時の文書/STLを保存する。
ただし元の実行が finalize 失敗で mesh-world snapshot を残していない場合は finalize-input から
読み直すフォールバックになり、その回はgridが無いため成功しても最終STL(`final.stl`)は保存しない
（文書と各段階のB-repは保存する）。

検証は `tests/test_finalization.py`、`tests/test_final_shape.py`、`tests/test_optimization_trace.py` と
`topopt_checks/check_trace.py`。後者は小型モデルで製品経路・文書再生・最終STLを確認する。

## 2026-09-15 実装検証

単体・記録・判定器の37テストと、小型モデルの製品経路（CAD追加、文書再読込、STL照合、中断）に合格。
保存済みhanger実行 `20260914T133910-1d5adc93ee4d` の後処理再実行では、保持unionは警告なしで完了。
除外cutは `BOPAlgo_AlertAcquiredSelfIntersection`、`BOPAlgo_AlertFaceBuilderUnusedEdges`、
`BOPAlgo_AlertSolidBuilderUnusedFaces` を返したため、不合格として停止した。
hangerが2条件に合格したという結果ではない。最適化を再実行したり、平滑化回数を変更したりしていない。
詳細は `output/finalization_checks/20260915T051337-f41fab250251/` の診断・拒否結果B-repを参照。

## 2026-09-22 除外cutの警告の原因（hanger）

実行 `20260921T185659-b6799e00457d`（探索後の finalize で同じ3種の警告）の保存済みB-rep
（`000155-before-boolean`・`000158-preserve-union`）を読み、`BOPAlgo_BOP`（cut・非破壊・fuzzy 0）を組み替えて切り分けた。
全部入りで元の警告（9/8/1件）が再現した。2026-09-15 の実行も同じ hanger で同じ3種だが、同じ原因かは未確認。

- **除外 `hanger_bar_attatchment_11` の単独 cut で再現**する。他の除外7個は単独では警告なし。
- **メッシュだけ（保持の和集合なし）の cut は警告なし**。保持のうち **`hanger_bar_attatchment_7` を足したときだけ**再現し、他の保持3個では出ない。
- 除外11は半径13.288 mmのロッド（軸 y=-200, z=60、x -248〜252）で、中央 |x|<17.27 に浅いくびれ（トーラス R=352.8617, r=340）がある。
  保持7はそこにはまる厚さ5 mm・x ±17.5 mm の環で、**内面が除外11のくびれ面と同一のトーラス**（中心・軸・半径が一致、距離 1e-13 mm）。
  体積の重なりは0で、面だけが完全に一致して接する。
- `AcquiredSelfIntersection` 9件のうち7件は、この共有面の上（軸から半径 12.86〜13.15 mm、|x|<15 mm、除外11までの距離 ≤0.0005 mm）。
  1件は辺2本で、2本の長さが同一。
- 除外11を軸まわりに ×0.999（面が約13 µm離れる）にすると警告0件で有効なソリッド。×1.001（13 µm重なる）では
  `AcquiredSelfIntersection`・`SolidBuilderUnusedFaces` が消え、`FaceBuilderUnusedEdges` が2件残る。
- メッシュ側は原因でない（最短辺 0.385 mm、最小高さ 0.307 mm、縮退・極小三角形なし）。

保持と除外が同一の面を共有するモデルで、保持の和集合と除外の差集合が、その面上のメッシュとの交線をそれぞれ作る。
辺が二重になって OCCT が「引数の部品がツールを介してつながった」と見なす、と読んでいる（辺が二重になる点は推定。
共有面の同一性と、面をずらすと警告が消えることは実測）。

### 試した回避策（同じ実行の保存済みB-repで実測。モデルは変えない）

正しい体積の目安は 3,169,814 mm³（除外を引いたメッシュ 3,169,421 ＋ 保持が足す 393）。

| 案 | 結果 |
|---|---|
| 除外のcutを先、保持のunionを後 | cutは警告なし。後のunionが 13/6/2 件で、体積が 3,153,136 に減って壊れる |
| 元の順序のcutに fuzzy 1e-4 / 1e-3 mm | 警告は 6件 / 5件に減るだけ（体積は正しい）。1e-2 は15分以上終わらない |
| メッシュから保持と除外を1回で同時に引き、除外で削った保持を後でunion | 同時cutは警告なし。後のunionが 1/1 件（除外11の肩 x=-16.78）で、glueを付けても同じ。体積は正しい |
| `BOPAlgo_CellsBuilder` で (メッシュ ∪ 保持) − 除外 を1回の交差計算で作る | OCCTの警告は0件、体積 3,169,813.75、ソリッド1個で有効。**最終検査の自己干渉が1件残る**（頂点と辺。除外11の肩 x=-17.27、頂点の許容差 0.011 mm） |
| 上の残りを形状修復（`ShapeFix_Shape`・`BRepLib.UpdateTolerances`） | 消えない。`ShapeFix_Shape` は体積が変わって無効な形になる |
| CellsBuilder に fuzzy 1e-4 / 1e-3 / 3e-2 mm | 体積0・警告多数で壊れる。1.5e-2 は30分以上終わらない |
| 除外11を軸まわりに ×0.999 / ×1.001 して cut | ×0.999 は警告0・有効（自己干渉検査は未実施）。×1.001 は `FaceBuilderUnusedEdges` が2件残る。どちらも除外の実形状から約13 µmずれる |

許容差 0.011 mm の頂点は、保持と除外を同時に扱った時点（`cut(メッシュ, 保持＋除外)`）で既に現れる。
逐次の union では最大 1.5e-4 mm。保持7の内面のトーラスと除外11の肩のBSpline面（x=±16.78〜17.27）が最大約0.01 mmずれて接するため、
OCCT が両者を同一面と見なして許容差を広げる、と読んでいる（推定）。
小さなモデル（くびれ付きロッドとはまる環、面をまたぐ凹凸メッシュ）では、元の順序は10通り中8通りで警告が出て、
CellsBuilder は10通りとも警告なし・有効だった。円筒だけのモデルでは元の順序でも警告が出ない。

## 2026-09-22 まとめてcut→保持だけ引き算→fuseへの置き換え（hanger実測）

前節の「メッシュから保持と除外を1回で同時に引き、除外で削った保持を後でunion」案を製品経路に採用した
（`duocad/topopt/finalize.py`）。手順: 1) メッシュから保持＋除外をまとめて1回のcutで引く
2) 保持ボディそれぞれについて単独で除外とのcutを計算する 3) 1の結果に2の「除外済みの保持」をfuseで足す。

実行 `20260921T185659-b6799e00457d`（探索後の finalize で自己干渉により失敗した run。
mesh-world snapshotが無いため `replay_finalization.py` を finalize-input からの再生に対応させた）を
新しい経路で再生すると、material-cut・保持0〜3個それぞれのcutは全て警告なしで通り、最後のfuseだけ
`BOPAlgo_AlertAcquiredSelfIntersection` 1件・`BOPAlgo_AlertFaceBuilderUnusedEdges` 1件が残る
（旧経路の9/8/1件から削減。`SolidBuilderUnusedFaces` は0件になった）。

残る1件は保持7・除外11の共有面（くびれ）ではなく別の干渉で、除外11の円筒面とBSpline遷移面（肩、x≈±16.78〜17.27）
の境目に出る。メッシュの三角形近似で肩の位置が理想円筒からわずかにずれ、fuse時にその境目がAcquiredSelfIntersection
になると読んでいる（推定。位置の一致は実測、原因の機序は未検証）。「警告が1件でもあればValueError」の判定は
変えていないため、hangerはこの変更だけでは合格しない（このタスクのスコープ外）。

保持ボディが除外に全部覆われて `P_i - 除外` が空になる場合、fuse前にその保持を除外する
(`_boolean` の `allow_empty` 分岐。エラー・警告なしで0ソリッドを返すcutを「寄与ゼロ」として扱う。
旧経路ではこの場合エラーにならなかったため、この分岐が無いと退行する)。

`tests/test_finalization.py` 8件（保持が除外に全部覆われるケースの回帰テストを含む）、
全体テスト1378 passed / 2 skipped。

## 2026-09-22 inspect_solid を面-面の交差だけでカウントするよう変更（hanger実測）

`duocad/kernel_brep.py` の `inspect_solid` は、`BOPAlgo_CheckerSI` の `checker.DS().Interferences()` を
種類を問わず `Extent()` でそのまま数えていた。頂点-頂点・頂点-辺・辺-辺・頂点-面のペアは許容差が
触れているだけで無条件にカウントされる（除外条件なし）一方、面-面のペアだけは実際に交差点・交差線が
あるかをOCCT自身が確認してから数える（`BOPAlgo_CheckerSI::PostTreat` を読んで確認）。
両端の形状が `TopAbs_FACE` であるペアだけをカウントするよう変更した（数値のしきい値は導入していない）。

**`_boolean` が拒否した preserve-fuse の結果（`000018-preserve-fuse-rejected.json`、前節）を
直接 `inspect_solid` に通すと、旧ロジックでは `self_intersections=1`（頂点-辺のペア）だったのが、
新ロジックでは `self_intersections=0`・`watertight=True`・`single_solid=True` になる**
（実測: `{'watertight': True, 'single_solid': True, 'solids': 1, 'self_intersections': 0, 'valid': True, 'closed': True}`）。
`inspect_solid` 単体は狙いどおり動く。

**しかし `topopt_checks/replay_finalization.py output/optimization_runs/20260921T185659-b6799e00457d` の
再生は、この変更後も従来と同じ場所・同じ2種の警告で不合格のまま**（`preserve-fuse: ブーリアン演算に
エラーまたは警告がある: {..., 'warnings': 'BOPAlgo_AlertAcquiredSelfIntersection\nBOPAlgo_AlertFaceBuilderUnusedEdges\n'}`）。
原因は `inspect_solid` ではない。`finalize.py` の `_boolean` は `BOPAlgo_BOP`（fuse演算そのもの）の
`HasWarnings()` を見て、警告が1件でもあれば`inspect_solid`を呼ぶ前に`ValueError`を投げる
（`_boolean` の「警告が1件でもあればValueError」という既存判定。`inspect_solid`のCheckerSIとは別の、
独立したOCCT呼び出し）。`_boolean`が例外を投げた時点で`inspect_solid`は一度も呼ばれないため、
**`inspect_solid`の判定基準をどう変えても、この経路を通る限りhangerは合格しない**。

この事実は今回初めて判明した（依頼元の背景説明は「頂点-辺のペア1件・面-面のペア0件」という
`inspect_solid`のCheckerSIの実測を前提にしていたが、実際にhangerを不合格にしているのは
`_boolean`の別のゲートだった）。`finalize.py`の変更は本タスクの制約外（「モデル・ブーリアン演算の
処理順は変更しない」「inspect_solidの判定基準だけを変える」）であり、`_boolean`の
「警告が1件でもあればValueError」を緩めるかどうか（例: 面-面以外の警告は許容する等）は
このタスクのスコープ外・本人判断が必要。

カウントのロジック自体は `_count_face_interferences(ds, pairs)` として切り出し、
`tests/test_kernel_brep.py` でスタブの `ds`/ペアを使って検証する（面-面だけを数える・
頂点/辺を含むペアは数えない・片方だけ面のペアは数えない・混在時に面-面だけを残す、の4ケース）。
実際の `BOPAlgo_CheckerSI` を
自己交差のある形状で動かす検査は、再現性のある小さな題材を安定に作るのが難しい（実測: 単純な
2ソリッドのタッチだけでも `BOPAlgo_CheckerSI` 自体が `HasWarnings()=True` を返して
`inspect_solid` の前段ガードに阻まれることがある）ため、実データ（上記）での直接確認に留めた。
`tests/test_finalization.py` 含む全体テスト1382 passed / 2 skipped。

以下は2026-09-14までの仕様。現在は `--check-constraints` を指定した調査時だけ適用する。

| 条件 | 判定方法 |
|---|---|
| 保持部分がすべて残る | 各保持CADソリッドから最終出力を引き、残存ソリッドがない |
| 除外部分に材料がない | 各除外CADソリッドと最終出力の共通部分にソリッドがない |
| 水密 | 穴・非多様体・重複面・縮退面・向きの矛盾・自己交差がなく、有効な閉じた境界を作る |
| 島がない | 材料領域のソリッド数が1。内部空洞は島として数えない |

保持・除外の条件はモデル文書の `optimize.preserve_bodies` と
`optimize.exclude_bodies` から読み込む。除外部分の境界面に触れるだけなら許容する。
保持指定が空、入力不正、CAD演算の警告・失敗は合格にしない。

## 実行

リポジトリ直下から、ワールド座標・mm単位の最終STLを指定する。

```powershell
.venv/Scripts/python.exe topopt_checks/check_final_shape.py --model hanger_bar_attachment.json --mesh result.stl --out output/hanger_acceptance.json
```

`--model` の既定値は `hanger_bar_attachment.json`。
文書に保存済みの最終メッシュを検査する場合は、`--mesh result.stl` の代わりに
`--mesh-body <ボディID>` を指定する。

標準出力とJSONに4条件の `PASS / FAIL / UNKNOWN` を出す。
JSONには各保持・除外ボディの判定、残存体積、ソリッド数、入力のSHA-256も保存する。
終了コードは全条件合格なら0、判明した不合格があれば1、不合格が未確定で判定不能があれば2。
水密検査で止まった場合、保持・除外・単一ソリッドは判定不能になる。
スクリプトは最適化を実行せず、入力メッシュも修復しない。

## 「厳密」の範囲

ボクセル中心や表面のサンプル点の一致ではなく、元のCADと最終メッシュ全体の
集合演算を使う。残存体積の割合による合格許容値は設けない。
微小でも残存ソリッドが検出されたら不合格になる。

ただし計算は有限精度のOCCTであり、数学的な完全厳密判定ではない。
縫合・包含判定にはOCCTの `Precision.Confusion`（現在1e-7 mm）を使い、
集合演算にはCADが持つトポロジーごとの公差も作用する。JSONの `tolerance_mm` は
検査で指定した基準値であり、全演算の誤差上限ではない。
STLの座標丸めや曲面の三角形近似も判定対象に含まれる。
曲面の保持部を内接する三角形だけで表すと、CADとの差が残り、不合格になりうる。
大規模メッシュでは面の縫合・自己交差検査・集合演算に時間がかかる。
実行中の段階と、保持・除外のどのボディを判定しているかを標準エラー出力に表示する。

## 判定器の回帰検査

```powershell
.venv/Scripts/python.exe -m pytest tests/test_final_shape.py -q
```

判定器のテストが通ることと、対象モデルの最適化結果が合格することは別に記録する。
対象モデルの目標達成には、実際の最終出力を指定して4条件すべての合格報告が必要。

## 現存する画面出力の検査（2026-09-14）

ローカルサーバの状態スナップショットから、文書名が `hanger_bar_attachment.json`、
保持4個・除外8個のボディIDが保存文書と一致することを確認し、
メッシュ `body-95ff8a6b9c4e`（`optimized`）を検査した。
このメッシュを生成した時点のコード版・計算条件は未確認。

- 三角形12,048枚、頂点5,968個、境界の連結成分1個。
- 境界辺・非多様体辺/頂点・向きの矛盾・縮退面・重複面はいずれも0。
- OCCTの自己干渉検査で18件を検出し、水密条件は不合格。
- 保持・除外・単一ソリッドは判定不能。境界成分1個だけでは単一の有効ソリッドを証明できない。

状態スナップショットのSHA-256:
`89d098373c87ed89be4af2a9a01c03e71f08a67f7a0d81691aa2879df7c49354`。
次の切り分け対象は自己干渉箇所と、表面抽出・平滑化のどの段階で生じたか。

### 条件の両立と保存参照密度による切り分け

保持4個×除外8個の32組をCADの共通部分演算で検査し、全組で残存ソリッド0を確認した。
保持と除外が体積的に重なる矛盾は検出されなかった。

保存済み `simp_convergence_hanger_2.4mm_ref.pkl` から、コードを実行しない
`pickletools` の静的解析で密度の数値バイト列を抽出し、現在の `build_surface` に渡した。
格子は78×138×109、spacingは2.4075637497046305 mm。
この参照計算は上記の画面メッシュとは異なるため、画面メッシュの平滑化前形状とは扱わない。

| 対象 | 三角形 | 自己干渉 | ソリッド数 | 表面・ソリッド検査時間 |
|---|---:|---:|---:|---:|
| 平滑化前 | 48,392 | 0 | 1 | 25.36秒 |
| 平滑化後 | 48,392 | 0 | 1 | 36.20秒 |

どちらも穴・非多様体・向きの矛盾・縮退面・重複面は0。
これは水密と単一ソリッドを満たす実例であり、保持・除外の合格を示すものではない。
参照キャッシュのSHA-256:
`21d085d0645668b1e0318a6c118d725d1e20025a1e1bed866393d0727f3d772f`。
