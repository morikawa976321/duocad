# 最終形状の合格条件

現在の合格条件は、`models/hanger_bar_attachment.json` の最終出力の **水密性（自己干渉なし）・単一ソリッド** の2つ。
保持・除外は既定の合否判定から外し、平滑化後のCADブーリアン演算で反映する。

製品経路は `密度 → 島除去 → 表面生成 → Taubin平滑化 → CAD化 → 保持と和集合 → 除外との差集合 → 水密・単一ソリッド検査 → 文書保存`。
重複領域は最後に適用する除外が優先する。演算のエラー・警告、空の結果、自己干渉、分断を成功扱いにしない。
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

検証は `tests/test_finalization.py`、`tests/test_final_shape.py`、`tests/test_optimization_trace.py` と
`topopt_checks/check_trace.py`。後者は小型モデルで製品経路・文書再生・最終STLを確認する。

## 2026-09-15 実装検証

単体・記録・判定器の37テストと、小型モデルの製品経路（CAD追加、文書再読込、STL照合、中断）に合格。
保存済みhanger実行 `20260914T133910-1d5adc93ee4d` の後処理再実行では、保持unionは警告なしで完了。
除外cutは `BOPAlgo_AlertAcquiredSelfIntersection`、`BOPAlgo_AlertFaceBuilderUnusedEdges`、
`BOPAlgo_AlertSolidBuilderUnusedFaces` を返したため、不合格として停止した。
hangerが2条件に合格したという結果ではない。最適化を再実行したり、平滑化回数を変更したりしていない。
詳細は `output/finalization_checks/20260915T051337-f41fab250251/` の診断・拒否結果B-repを参照。

## 旧4条件の判定仕様と観測記録

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
