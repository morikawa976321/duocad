# duocad — build123d / OCCT / STEP / 実行環境の落とし穴

**読むとき**: OCCT・build123d・OCP の呼び出しで結果が疑わしいとき、STEP/STL を扱うとき、
`.venv` / pytest / MCP サーバの挙動でつまずいたとき。

**コードの特定箇所に紐づく罠は、その箇所のコメントが正**である(ここには写さない)。
いま何がどこに書いてあるかの索引だけ置く:

| 罠 | 場所 |
|---|---|
| `TopoDS_Shape` は `hash` が一致しても `__eq__` が False(dict / `lru_cache` が1回も当たらない)。`IsSame` は Orientation を見ない | `duocad/kernel.py` `_ShapeKey` / `BoundingBoxCache` |
| `BRepBndLib::Add` は面のトリムを見ない・`AddOptimal` は繰り返しても速くならない・`useTriangulation=True` は前回の三角形分割を見る・`SetGap(0.0)` の効き方は `Add` の前後で変わる | `duocad/kernel.py` `_bounding_box_extents` / `duocad/topopt/voxelize.py` `_shape_bbox` / [TOPOPT.md](TOPOPT.md)「OCCT の bbox はトリムを見ない」 |
| STEP 読み込みは `ReadFile()` の戻り値を見ずに `TransferRoots()` へ進むとプロセスごと落ちる(0xC0000005)。書き出しは `Transfer()` の戻り値も見る | `duocad/kernel.py` `read_step` / `write_step`、`duocad/io_step.py` |
| STEP 書き出しは C++ から stdout へ統計を吐く(`redirect_stdout` では止まらない。fd 1 を fd 2 へ振り替える) | `duocad/mcp_server.py` `_claim_stdout` |
| STL の偏差は `isRelative=False` で渡す(相対だと偏差を1/20にしても1バイトも変わらない) | `duocad/kernel.py` `tessellate` |
| 角度偏差の単位はラジアン(`DEFAULT_ANGULAR_DEFLECTION = 0.35` ≈ 20°) | `duocad/kernel.py` `DEFAULT_ANGULAR_DEFLECTION` |
| `gp_Ax3.ZReverse()` は X/Y を据え置く(左手系)。枠を3ベクトルで保存すると反転面で v 軸だけ鏡像になる | `duocad/kernel_sketch.py` `_ax3_from_frame` |
| 曲面から `MakePrism` すると `IsDone()` は真でも `BRepCheck_Analyzer` が偽(または体積が激減) | `duocad/kernel_extrude.py` `_check_coplanar_planes` |
| `ShapeUpgrade_UnifySameDomain` は入力そのものを壊す(コピーで試してから本番) | `duocad/kernel.py` `_unify_same_domain` |
| フィレット/面取りは例外を投げず失敗し、半径に対して単調でない | `duocad/kernel.py` `fillet_edges` / `chamfer_edges` |
| 由来(provenance)の取り出し方は操作ごとに違う(`History()` / `Modified()`、`IsRemoved` / `IsDeleted`) | `duocad/kernel.py` `normalize_provenance` |
| `Face.center()` の既定は `CenterOf.GEOMETRY` で面積重心ではない(曲面では面パッチの上にすら乗らない) | `duocad/query.py` `face_centroid` / `face_fingerprint` |
| 生の `TopoDS_Shape` を build123d で包む入口は `Compound.cast`(基底の `Shape.cast` は None を返す)。`faces()` / `edges()` は重複除去済み | `duocad/query.py` 冒頭 |
| `BRepClass3d_SolidClassifier` は材料内部の孤立点を OUT と誤判定することがある(神託として使うときの作法) | [TOPOPT.md](TOPOPT.md) |
| 面の添字は形状変更(フィレット)で散発的にずれる / STEP往復の体積は bit-exact でない / undo の再計算で体積の下位ビットが動く | [../README.md](../README.md)「実測で判明している技術的な地雷」、`web/scripts/smoke.mjs` `VOLUME_EPS` |

## build123d / OCP

- **`build123d.Mesher().read()` は点群ではなく OCCT の形状(faceted B-rep)を返す**(0.11.1 実測:
  `Box(10,10,10)` を STL に書いて読み戻すと `Solid` / 12面 / 体積 1000.0)。内部で
  三角形 → 平面 Face → `BRepBuilderAPI_Sewing` → Shell → (manifold なら) Solid までやるので、
  この経路を自前で書く必要は無い。ただし面数は三角形数と同じになる(同一平面の三角形は併合される
  が有機形状では併合されない)ので、10万三角形なら10万面のソリッドになり B-rep 編集は事実上できない。
  STEP 交換用途に限る。`is_valid` はプロパティで、`s.is_valid()` と呼ぶと `TypeError`。
- **build123d と OCP 直叩きの行数の差は「問い合わせ」に集中する**(同じ形状 — 箱60×40×20 + 貫通穴r8 +
  縦エッジr3フィレット — を3通りで実装、体積 43824.2481 で完全一致):

  | 書き方 | 合計行 | 由来マップ |
  |---|---|---|
  | build123d 純正 | 7 | 取れない |
  | OCP 直接 | 32 | 取れる |
  | 生 `TopoDS_Shape` を保持し build123d を問い合わせにだけ使う | 12 | 取れる |

  差のほぼ全部は述語による問い合わせ(Z に平行な辺を選ぶ: 11行 対 1行)。プリミティブ+ブーリアンは
  7行対3行、フィレット操作は5行対1行で小さい。**操作層を OCP で書き直す費用は 1 verb あたり +3〜5行**
  で、かさむ問い合わせ層は1回書けば済み履歴の有無に関係なく要る(MCP が述語を要求する)ので
  履歴のコストには数えない。これが「操作は OCP、問い合わせは build123d」(`duocad/query.py`)の根拠。
- **build123d 0.11.1 は推移依存で SVG 一式を連れてくる**: svgelements 1.9.6(パーサ。`transform`
  解決済み座標で Move/Line/Arc/CubicBezier/QuadraticBezier/Close に正規化。`Arc` は rx/ry/center/
  theta/delta を持つ)・svgpathtools 1.7.2・ocpsvg 0.6.0(SVG ↔ OCCT 形状)・svgwrite 1.4.3。
  `duocad/svg_import.py` はこれを前提に追加依存ゼロで書いてある。

- **OCCT の面分割の内部モデル(`IMeshData_Model`)から取ったハンドルは、モデルより長生きさせるとプロセスごと落ちる**
  (0xC0000005。モジュール変数に残す・context を先に捨てる、で再現)。寿命規則と解放順は
  `duocad/kernel_meshing.py` のモジュール docstring と `_Model.close()`。`BRepMesh_Context` は OCP に無いので
  標準の6部品を `IMeshTools_Context` に自分で組む(同ファイル `mesh_shape`)。OCCT 自身の境界交差検査が
  5度未満・小面積の交差を捨てる話は [TOPOPT_GAP_CAUSALITY.md](TOPOPT_GAP_CAUSALITY.md)。
- **`BRepOffsetAPI_MakeOffset` は面を渡してもワイヤの並びしか返さない**（`GeomAbs_Arc` で角は円弧）。外周と穴の
  区別は向きに無い（`BRepBuilderAPI_MakeFace` はどちら回りでも有界な面を作る）ので、ワイヤの入れ子の深さ
  （他のワイヤの面が1点を含むか）で外周と穴に分けて面を組み直す（`kernel_sketch._faces_from_wires`）。
- **負のオフセットは OCCT に直接頼めない**: 同心円で囲まれた面（座金の形）の内向きオフセットは距離によらず
  `IsDone()=False` で失敗し、円が完全に潰れる距離でも同じ失敗になる。正のオフセットは同じ形で通るので、
  縮小は「十分大きな箱 − (箱 − 面) の拡大」で取る（`_erode_faces`）。面が消えるときは空の結果になる。
- **R と d の円弧が同じ点で接する結果の結合は、既定の許容差(1e-7)だと取りこぼしうる**ので、ブーリアンに
  1e-6 のファジー値を付ける（`_ENVELOPE_TOLERANCE`）。ベジエ辺の領域は、オフセットが元の曲線の近似曲線を返し
  線分・円弧だけという指定を満たせないので拒む。

## 複数面の押し出し（`kernel_extrude.extrude_faces`）

- **法線の平行判定は内積ではなく外積の大きさで見る。** OCCT の `Precision::Angular`(1e-12) を許容値に
  使うと、`1 - cos` は倍精度で 1e-16 未満を区別できないので内積では判定にならない。同一平面の判定は
  先頭の面の重心からの法線方向の距離を `Precision::Confusion`(1e-7 mm) と比べる。STEP 由来で
  法線に微小なずれがある面は弾かれうる（エラー文に何番目の面がどの条件に反したかが出る）。
- 柱を `_fuse_all` で束ねてから1回だけ join / cut し、同一平面の統合（`_unify_same_domain`）は
  コピーで試して採否を決める。1面の結果は、廃止した verb `extrude_face` の保存済みログの再生結果と
  体積・面数が一致することをテストで固定している（`tests/test_extrude_face_replay.py`）。

## STEP ファイル

- **Fusion 360 のジェネレーティブデザイン結果を STEP で出すと長さ単位が `SI_UNIT(.CENTI.,.METRE.)`(cm)**
  になる。同じ設計の入力 STEP は mm なので入力と出力で単位が食い違う(実測: 入力
  `shelf_attachment.step` は mm、GD 結果 `shelf_attachment_8.step` は cm。座標を mm と読むと
  11.6×15.5×51.3 に見え、正しくは 60×154×113mm)。**1/10 でもスライサのビルドボリュームに収まるので
  エラーにならず気づきにくい。** 読み込んだ直後に実寸を確かめる。単位宣言は
  `grep -a -o "SI_UNIT([^)]*)" <file>` で先に見る。
- **STEP はテキスト(ISO-10303-21・1行1エンティティ)なので、CAD を開かずに grep/awk で寸法が取れる**:
  1. 単位 — `SI_UNIT(...)`。
  2. 外形 — `CARTESIAN_POINT` を全部拾って min/max。**名前付きの点(`CARTESIAN_POINT('Origin',(...))`)
     があるので `CARTESIAN_POINT('[^']*',([^)]*))` で拾う**(`''` 決め打ちだと 935点中121点しか取れなかった)。
  3. 実形状の角だけ — `VERTEX_POINT('',#N)` を解決すると B-spline の制御点が混ざらない。
  4. 穴径・フィレット半径・ザグリ — `CYLINDRICAL_SURFACE('',#axis,R)` / `CONICAL_SURFACE('',#axis,R,半角rad)`
     (0.7854 = 45° = 90°皿)/ `TOROIDAL_SURFACE('',#axis,大,小)`(大 = 母体R ± 小)。
  5. 穴の位置と向き — `#axis` → `AXIS2_PLACEMENT_3D('',#loc,#axis,#ref)` → `CARTESIAN_POINT` / `DIRECTION`。
  6. ザグリ深さ — 円錐面と円筒面の境界頂点の軸方向座標差。

  B-spline サーフェスが混ざると手作業では割に合わない。断面が押し出しでない形は代表寸法までで止める。
- **全周円筒は「穴」とは限らない。面の種類の前にソリッド数と体積を見る。** `shelf_attachment_GD.step` を
  「56面 = 平面34 + 全周円筒22 → 板に22個の穴」と読んだが、実際は**17個の独立したソリッド**
  (4面・体積 1767.1 = π·5²·22.5 の円柱、3面・117.8 = π·2.5²·6 の小円柱)で、ネジ・スペーサー類の
  集まりだった(`_GD` が与件、`_result.stl` が成果物という命名)。面の種類は向きを持たないので
  穴の内壁もピンの外側も同じ「全周円筒」になる。ソリッド数 → 1部品かアセンブリか、体積を既知の式
  (π·r²·h)と突き合わせる、の順が安い。他人の配布データが混在するフォルダで面の統計から
  「本人の設計」を推定するのは危うい。
- **duocad 自身の STEP 書き出し(`io_step.py export_step` → `kernel.write_step`、`STEPControl_Writer` を AsIs で使用)は
  `Interface_Static` を触っていないので OCCT 既定の AP214・mm で出る。部品名は載らない。** 実測(2026-10-06、
  穴あき直方体 50×30×10・φ6 と 20×20×10 ×2): OCP/build123d で再読込して体積 14717.26 / 8000、面 7、
  `BRepCheck_Analyzer` と `is_valid` とも True。ヘッダは `FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'))`、
  `SI_UNIT(.MILLI.,.METRE.)`、`PRODUCT('Open CASCADE STEP translator 7.9 1',…)`。Body 名は出力に反映されず
  (XDE 未使用)、複数ボディは `NEXT_ASSEMBLY_USAGE_OCCURRENCE` でアセンブリ構造になる。非表示ボディは黙って除外
  (応答の `skipped_hidden` に載る)、メッシュボディは例外で拒否(`io_step.py` L78-84)。Fusion/FreeCAD/SolidWorks での
  実読は未検証。部品名を付けるなら XDE(`STEPCAFControl_Writer`)への切替か、build123d で label を付けて再書き出し(未試験)。
## Python 実行環境(`.venv` / pytest)

- **PATH の `python` で `python -m pytest` を回すと、OCP を import する全モジュールが
  `ModuleNotFoundError: No module named 'OCP'` で収集エラーになる**(件数はファイル数で変わる。
  19件 / 48件)。PATH の `python` はセッションごとに実体が違う(旧 taskbox runner では別プロジェクトの
  venv を拾っていた)。**「テストが全部壊れている」に見えるが、インタプリタを間違えているだけ。**
  正しい呼び方は `.\.venv\Scripts\python.exe -m pytest -q`(`pyproject.toml` の `pythonpath=["."]`
  があるので venv の python なら素の `pytest` でも通る)。
  **`python -m pytest -q 2>&1 | tail` と書くとパイプ末尾の終了コード(0)が返る**ので、
  件数を読まずに終了コードだけ見ると「緑」と誤読する。duocad で赤を見たら、まず環境と負荷を疑う。
- **`.venv` は uv 管理で pip が無い**(`python -m pip` は `No module named pip`)。パッケージ操作は
  `uv pip ...`。依存の解決だけ見るなら
  `uv pip install --dry-run --python ./.venv/Scripts/python.exe <package>`。
  Python は 3.11.15(`pyproject.toml` が `>=3.11,<3.12`)。宣言依存は build123d / cadquery-ocp-novtk /
  pytest / websockets の4件で、numpy 2.4.6 / scipy 1.17.1 は推移依存で既に入っている
  (scikit-image は形状最適化のために手で入れてある。[TOPOPT.md](TOPOPT.md))。
  入っていないもの: trimesh・pyvista・vtk・gmsh・matplotlib・numba・cupy・torch・pyamg・scikit-sparse・meshio。
- **検査の在庫は `.\.venv\Scripts\python.exe -m pytest -q --collect-only` で数える。**
  在庫は `--collect-only`、**契約と「なぜその形か」は各モジュールの docstring** —— 片方を
  もう片方の代わりにしない。契約を散文でまとめた `tests/README.md` は削除した(a83857e)
  (中身をコードのそばへ移した。別ファイルに置くと、コードを変える人の視界に入らないまま
  後追いで同期することになり、実際に実装と食い違う記述が5件たまった)。
- **`importlib` でファイル1つだけを単体ロードして `@dataclass` を使うなら、`exec_module` より前に
  `sys.modules[spec.name] = module` を入れる。** `from __future__ import annotations` と併用すると
  `dataclasses` が型注釈の解決に `sys.modules[cls.__module__]` を引くため
  `AttributeError: 'NoneType' object has no attribute '__dict__'` で落ちる(import 時なので
  そのモジュールを読む全テストが一斉に赤になる)。`duocad/catalog.py` を、OCP を読み込む
  `duocad/__init__.py` を経由せずに読む `tools/generate_protocol_ts.py` / `tests/test_ui_command_wiring.py`
  がこの形。

## サーバとセッション

- **`duocad.mcp_server` は MCP(stdio)と WebSocket(人の画面)を同一プロセスで両方サーブし、コマンド表は
  モジュール読み込み時に1回だけ組む。** `catalog.py` / `net/ws.py` を変えたら再起動まで含めて1手
  (古いプロセスが生きたままだと人が画面で新 verb を使った瞬間に `unknown command`。
  Vite の HMR が効くフロントと違い、バックエンドの鮮度 = Python プロセスの生存期間)。
  **ただし確認なしに再起動しない** —— 文書は接続より長生きするので、別セッションが開いたままの
  未保存文書(画面に「未保存」と出ているもの)がそのプロセスに乗っていることがあり、再起動すると
  誰のものでも消える。他に繋いでいるセッションが無いか・未保存でないかを先に確かめ、
  確かめられなければ pytest 等の静的検証で済ませてライブ確認を見送る(フロントだけの変更は
  `strictPort:true` の Vite dev サーバを別に立てれば同じ生きたバックエンドへ繋ぎ直せる)。
- **`bodyId`(`body-01a54896757b` 等)はインポートごとにその場で振られ、STEP の内容から決定的に
  導かれない。** 同じ `PC_case_20251122.step`(27部品)を別々のサーバ起動で読んで矩形選択を比べると、
  選ばれた面の総数(2835件・27ボディ)は一致するが `bodyId` は1件も一致しなかった。
  **起動をまたいで選択結果を突き合わせるときは `bodyId` を無視し、ボディごとの面インデックス集合
  (ソート済み配列)をキーにグルーピングして集合どうしを比べる。** 同一起動内なら `bodyId` は変わらない。
