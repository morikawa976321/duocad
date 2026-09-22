# 最適化の途中記録

製品の `Session.run_optimization` → `optimize_run.run` では、毎回自動で診断記録を作る。
画面のデバッグ表示設定によらず保存する。保存のために最適化の設定や計算結果は変更しない。
実行中の既存Pythonサーバにはコード変更が反映されないため、導入後の初回はバックエンドを再起動する。
ブラウザの再読み込みだけでは反映されない。

## 保存場所と入口

`output/optimization_runs/<UTC日時>-<実行ID>/`。
同時実行や繰り返し実行も別フォルダになり、過去の記録を上書き・自動削除しない。
結果と進捗メッセージにも `diagnostic_path` を付ける。

| ファイル | 内容 |
|---|---|
| `manifest.json` | 実行ID、プロセスID、開始・終了、状態、最終イベント、保存時間、入力・最終出力のハッシュ |
| `input.json` / `input-state` の記録 | 実行直前の文書、保持・除外・荷重・拘束・パラメータ・ボディID |
| `cad/*.step` | 実行直前の各ソリッド。三角形近似前のCAD |
| `source.zip` / `environment` の記録 | ソース、依存定義、Python・ライブラリ版、CPU、スレッド環境変数、Git差分、読み込み済み関数のハッシュ |
| `events.jsonl` | 時間順の進捗、各段階の親子関係、スナップショットのファイル名、エラー |
| `連番-段階名.json` | その段階の引数・結果・メタデータ。配列は内容ハッシュで参照 |
| `arrays/*.npz` | dtype・shapeを保った損失なし圧縮配列。pickle不要。同じ内容は1つだけ保存 |
| `final.stl` | **採用した最終形状**のワールド座標mm。ASCIIの17桁で座標を保存 |
| `document_after.json` / `result` の記録 | 結果ボディを含む文書、採用材料量、安全率、探索履歴、ボディID |

現在の製品経路は、平滑化後にメッシュから保持＋除外をまとめてcut→保持ごとに除外とcut→除外済み保持をfuseの順で
適用し、水密・単一ソリッド検査後に `done` となる。過去の記録の `done` は、この検査を行った意味ではない。
失敗は `failed`、中断は `cancelled`。条件不足の早期終了も専用の状態を残す。
プロセス強制終了時は `running` のまま残ることがある。プロセスIDと実際の稼働状態を確認する。

## 保存する段階

1. **入力と解決後の条件**：利用者の指定値と、実際の格子サイズ・上限・材料定数・面対応。
2. **設計空間**（`derive`。1実行に1回）：ボディの外接箱・凸包の平面式・余白・格子・段階ごとのセル数、
   条件ごとの占有と所属成分、最終マスクのハッシュ、設計・保持マスク、CAD表面の三角形、面ID、
   アンカーの節点・面積・法線。段階ごとのマスク（外枠・凸包・除外・除外後・接続後）は `derive-masks`。
3. **境界条件の確定**（`boundary-mapping`。2026-09-17〜、1実行に1回）：導出出力に接する節点だけへ
   条件面を対応付けた確定表の検証（`anchor.condition_mapping`：面ごとの節点数・対応不能標本とその座標・
   被覆・合力・CAD面の重心からのずれ・モーメント誤差とそれぞれの許容〈探索半径〉・対応距離）、表の統計（`mapping="material"`、許可節点数）、
   幾何の表との差（節点数・被覆されない節点数・節点集合の差）、設計マスクのハッシュ。
   導出の `derive-output` が持つアンカーは幾何の表（`mapping="geometry"`）で、探索が使うのは
   こちらの確定表である。
4. **形状探索**（`search-result`。2026-09-17〜。旧「材料量探索」は`safety_loop.py`ごと
   削除した）：探索全体の結果(状態・終了理由・評価回数・採用材料量)を1回記録する。
   **個々の候補(操作の種類・中間マスク・各候補の安全率)は現時点ではディスクへ記録しない**
   （`binary_search.py`は`trace`を呼ばない。全候補が同じ設計空間マスクから出発することは
   `derive`の1回性で保証される—段階3・4のように候補ごとに塊を切り出し直す設計ではない）。
   戻り値の `diagnostics` には、段階3の `full_material`（全材料の島処理と、成分ごとの落ちた検査）、段階4の `reduction`（試行ごとの選んだ／削ったセル数・削減割合・
   後退倍率・落ちた条件・合格後の体積と安全率、FEM回数、止まった理由）が入る（`topopt_checks/verify_hanger_fix.py` が JSON に残す）。
5. **線形解析**：解法(方針 `auto` と実際に使った解法)、CG反復数、相対許容値、直接解法への切替、残差検算。解が不正な場合も失敗時の変位・右辺・自由度を残す。
6. **表面生成**：塗り直し前後の密度、島除去後・パディング後の密度、marching cubesの頂点・面・補間係数。
7. **平滑化**：Taubinの各反復のλ処理後とμ処理後の全頂点。面接続は同じsurface段階の記録と組み合わせる。
8. **応力評価**：最終密度で解き直した変位、換算係数、応力テンソル・von Mises・代表応力・安全率。
9. **採用結果**：選択された計算結果全体、格子座標のraw/smooth、ワールド座標のraw/smooth、製品へ渡した面接続。

**2026-09-17より前(SIMP時代)の記録には`iteration-input`/`iteration-output`/`iteration-analysis`
(設計反復ごとの密度・変位・感度・コンプライアンス)の段階があった。** 段階6でSIMPをコードごと
削除したため、現行の経路はこれらの段階を持たない(候補ごとの評価は1回のFEM解であって、
勾配法の反復ではない)。

2026-09-16 より前の記録には `reference` / `derive-bbox` / `derive-candidate` の段階がある（参照方式。候補ごとに塊を切り出していた）。読む側は段階名で新旧を見分ける。

巨大な組立済み剛性行列は保存しない。保存した格子、材料、自由度、荷重、密度と
ソースから再構築できるため。これは任意の途中命令からプロセスを再開するメモリダンプではない。
探索を再実行せずに表面生成を再実行する用途には、必要な入力が保存される。

## 読み方

```powershell
.venv/Scripts/python.exe topopt_checks/inspect_optimization_run.py output/optimization_runs/<実行ID>
```

全反復数・各段階の件数・保存容量・指定値・結果・段階の親子関係を表示する。
Pythonから特定の記録を読む場合：

```python
from duocad.topopt.trace import load_snapshot
record = load_snapshot("output/optimization_runs/<実行ID>/<連番>-surface-from-mask-input.json")
from duocad.topopt.mesh import build_surface_from_mask
surface = build_surface_from_mask(**record)
```

`load_snapshot` は配列のハッシュ・型・形を確認し、辞書とNumPy配列だけを返す。
クラスの復元やpickleの実行はしない。`$type` は元の型名、`$omitted_type` は
直列化しないCADオブジェクト・コールバック等の型名である。

最終STLの水密・単一ソリッドの2条件を判定する：

```powershell
.venv/Scripts/python.exe topopt_checks/check_final_shape.py --model-dir output/optimization_runs/<実行ID> --model input.json --mesh output/optimization_runs/<実行ID>/final.stl --out output/optimization_runs/<実行ID>/acceptance.json
```

最新候補のscratch STLではなく、**採用結果の `final.stl`** を使う。

ブーリアン処理は `finalize` 段階に記録する。`before-boolean`、`material-cut`(メッシュから保持＋除外を
まとめて引く)、`preserve-<i>-cut`(保持ボディ`i`ごとに除外との差集合)、`preserve-fuse`(除外済み保持を
足し戻す)、`final-solid` はB-rep本文を保存する。警告・エラー時は `*-diagnostics` と、
取得できれば `*-rejected` も保存する。
新しい記録の `pre-boolean-mesh-world` が平滑化前後の頂点、`final-mesh-world` が
ブーリアン後CADの三角形分割。両者の面接続は同じとは限らない。
正本のCADは `document_after.json` の `add_solid_body` 操作に含まれ、
`--solid-body <body-id>` で直接検査できる。

## 費用と障害

密度・変位・感度を全反復、平滑化頂点を全段階で保存するため、実製品ではGB単位の保存容量になりうる。
配列の精度を落とす間引きは行わず、同一配列の重複排除と損失なし圧縮だけを使う。
記録は同期的に保存し、書き終えた配列を参照するJSONだけを公開する。
ファイルの完成前は `.tmp` であり、解析対象には使わない。

容量不足などの保存エラーは処理を止める。記録を黙って捨てて成功扱いにしない。
保存先自体へ書けないときは失敗状態も保存できないため、元のI/O例外と最後の記録を確認する。
`write_seconds` はスナップショットの配列圧縮・ハッシュ・JSON保存時間であり、
CAD出力・ソース収集・イベント記録等を含む総追加時間ではない。
反復時間や総時間には記録の影響が含まれるため、以前の計測値と直接比較しない。

2026-09-14の小型パッドモデルによる製品経路検査では、候補3件・完了した設計反復23回・
途中失敗1反復を含み、約24.9 MB、スナップショット保存約5.39秒、全体約27秒だった。
これは実製品2.4mmの容量や時間を予測する値ではない。
SIMP時代の hanger 2.4mm の実行 `20260914T133910-1d5adc93ee4d` は 6.45 GB で、その 95%(6.15 GB)が
反復ごとの記録(`iteration-*` / `taubin-*` / `linear-*` / `progress`)の配列だった。

**`output/optimization_runs/` は自動では消えない。** 不要になった実行は手で消す。2026-09-18 に、
参照の無いSIMP時代の実行とテストが残した実行を削り、文書と `check_design_space.py --run` が基準に
使う `20260914T133910-1d5adc93ee4d` は反復ごとの記録だけを削って残した(削った範囲は実行フォルダの
`PRUNED.md`。この実行に反復単位の再生・解析は掛けられない)。**テストは記録をここへ残さない** ——
`tests/conftest.py` が `trace.DEFAULT_ROOT` を一時フォルダへ向ける(外すと `tests/test_optimize_backend.py`
だけで1回12個・約18 MB 溜まる)。

## 検査

```powershell
.venv/Scripts/python.exe -m pytest tests/test_optimization_trace.py -q
.venv/Scripts/python.exe topopt_checks/check_trace.py
```

小型モデルで、記録有無の数値一致、密度の時点、表面の完全再現、全候補・平滑化全段階、
最終文書/STLの座標一致、失敗・中断・保存障害・同時実行の分離を確認する。
