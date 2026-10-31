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
| 面の `Orientation()==REVERSED` は平面の外向きを決めない。奇数回鏡映したボディの面は平面の `Position` が左手系で、REVERSED のとき `Direction` がすでに外向き(外向きは `BRepGProp_Face` の法線で決める) | `duocad/kernel_sketch.py` `_ax3_from_face` |
| 曲面から `MakePrism` すると `IsDone()` は真でも `BRepCheck_Analyzer` が偽(または体積が激減) | `duocad/kernel_extrude.py` `_check_coplanar_planes` |
| `ShapeUpgrade_UnifySameDomain` は入力そのものを壊す(コピーで試してから本番) | `duocad/kernel.py` `_unify_same_domain` |
| `ShapeUpgrade_UnifySameDomain` に壊れた形を渡すと仮想メモリを際限なく使う(フィレットの吸収の総当たりで 84GB、PC が落ちた)。重い総当たりは `tools/capped_run.py` で | `duocad/kernel.py` `_absorbed_fillet` / `duocad/kernel_overflow.py` `_require_plausible` |
| フィレット/面取りは例外を投げず失敗し、半径に対して単調でない | `duocad/kernel.py` `fillet_edges` / `chamfer_edges` |
| 薄肉(シェル後)の箱で、フィレットの半径が縁の面の幅とちょうど一致すると `MakeFillet.Build()` が access violation でプロセスごと落ちる(`BRepPrimAPI_MakeBox` の箱で実測。押し出しの箱は `IsDone()=False`)。窓は 2e-9 mm 未満 | `duocad/kernel_chains.py` `fillet_consumes_face`(当たれば元の形の `MakeFillet` を呼ばずにはみ出しの吸収へ回る) |
| 辺の曲線の最小曲率半径を超える一定半径のフィレットは `IsDone()=False`(楕円 6×2 の柱の上縁、先端の曲率半径 b²/a=0.667 に対し r0.65 は通り r0.7 で失敗)。転がり球の接触線(辺を半径だけ内へずらした曲線)が先端で燕尾状に自己交差するので、なめらかな一定半径の解は無い。自己交差した部分を切り落とし先端に稜線を残せば一定半径のまま立体になる(Fusion はこれを返す。r1 では稜線は上端から 0.057 mm・長さ約 0.43 mm)が、`MakeFillet` は切り落としをしない。**可変半径(`Add(TColgp_Array1OfPnt2d, edge)`、先端 0.5・短軸の端 1.0〜1.5)なら通り `BRepCheck` 真**(2026-10-21 実測) | `duocad/kernel.py` `fillet_edges` |
| `MakeFillet` は丸めの面が隣り合わない別の形に当たると、断る・壊れた形を返すほか、**その形にも球を触れさせた別の形を黙って返す**(issue #208) | `duocad/kernel.py` `_blend_reaches_other_faces` / [FILLET_OVERFLOW.md](FILLET_OVERFLOW.md) |
| `ShapeUpgrade_UnifySameDomain` は軸が逆向きの円柱を同じ面と見なさない(押し出した円柱面の軸の向きは押し出す向きで決まり、`BRepPrimAPI_MakeCylinder` とスケッチの押し出しで逆) | `duocad/kernel_overflow.py` `_column` |
| `BRepOffsetAPI_MakeThickSolid.MakeThickSolidBySimple` は面の向きによって体積が負(裏返し)の立体を返す | `duocad/kernel_overflow.py` `_column` |
| `BRepExtrema_DistShapeShape` は辺に沿って弧で触れていても、解を頂点にしか返さないことがある | `duocad/kernel_overflow.py` `_touches_inside_of` |
| `BRepOffsetAPI_MakePipe` の既定の向き(補正フレネ)は、まっすぐな道で断面がずれて体積が 2% 小さい(離散の向きなら合う)。道の曲がりの半径が断面の半径より小さいと体積 0 の立体になる | `duocad/kernel_overflow.py` `_swept_balls` |
| `GeomAPI_Interpolate.Load` に端の向きを `Scale=True` で渡すと、端で曲線が波打つ(媒介変数は弦の長さなので、長さ 1 の向きを `Scale=False` で) | `duocad/kernel_overflow.py` `_interpolated` |
| 面に接する立体(ちょうど半径の球を掃いた管)を引くと、引いた残りが接線でつながったまま分かれない。わずかに膨らませる。球を並べるなら球の間のくぼみより深く | `duocad/kernel_overflow.py` `_ROLL_SWELLS` / `_CHAIN_SWELL` |
| 重なり合う道具を先に結合してから引くと遅く、結果も1回の引き算に全部渡したときと食い違う。球を1回のブーリアンで全部結合するのは、順に1つずつより遅く重い | `duocad/kernel_overflow.py` `_rolled_material` / `_swept_balls` |
| 円柱・トーラスで済む立体を B スプラインの管で作ると、ブーリアンが桁で遅い(角から引くのが 5.5 秒 → 0.07 秒) | `duocad/kernel_overflow.py` `_exact_tube` |
| 円錐面の延長を頂点の手前で止めると先に穴が残り、箱を内と外に割り切れない(頂点ちょうどまで延ばす) | `duocad/kernel_overflow.py` `_extended_face` |
| 由来(provenance)の取り出し方は操作ごとに違う(`History()` / `Modified()`、`IsRemoved` / `IsDeleted`) | `duocad/kernel.py` `normalize_provenance` |
| `Face.center()` の既定は `CenterOf.GEOMETRY` で面積重心ではない(曲面では面パッチの上にすら乗らない) | `duocad/query.py` `face_centroid` / `face_fingerprint` |
| 生の `TopoDS_Shape` を build123d で包む入口は `Compound.cast`(基底の `Shape.cast` は None を返す)。`faces()` / `edges()` は重複除去済み。ただし `edges()` は縮退辺(球の極・円錐の頂点・フィレットの角。長さ0)を除くので、辺の数は kernel の `edge_count` から取る(球で 1 と 3、全辺 R3 のフィレット立方体で 48 と 56) | `duocad/query.py` 冒頭・`describe_shape` |
| 左手系のスケッチ枠で `gp_Ax2(中心, ax3.Direction(), X)` を作ると Y が逆になり、円弧は時計回り・楕円は角度が逆に出る(立方体6面のうち5面) | `duocad/kernel_sketch.py` `_curve_axis` |
| `gp_Elips` は長半径 >= 短半径でないと `Standard_ConstructionError`(縦長の楕円は軸を 90° 回して入れ替える) | `duocad/kernel_sketch.py` `_ellipse_gp` |
| 離心率0の `gp_Elips`(長半径と短半径の差 5e-8 以下)の辺を `BRepOffsetAPI_MakeOffset` に渡すと access violation でプロセスごと落ちる(縦横が等しい楕円は辺を `gp_Circ` にする) | `duocad/kernel_sketch.py` `_ellipse_gp` |
| `BRepOffsetAPI_MakeOffset` で平面の輪郭を内へずらすと、領域が消える距離(半径 1 の円を 3)でプロセスごと落ちる。楕円では継ぎ目が 6° 折れ、面積が 1.5% ずれる | `duocad/kernel_tight.py` `_center_path` |
| `BRepOffsetAPI_MakeFilling` に、支える面の上の座標(pcurve)を持たない辺を G1 の境界で渡すと `Build()` がアクセス違反でプロセスごと落ちる | `duocad/kernel_tight.py` `_cap` |
| `BRepAdaptor_CompCurve` の弧長の媒介変数は、辺の境目だけが弧長の位置で、辺の中は辺の媒介変数の一次の伸ばし | `duocad/kernel_tight.py` `_contour_pieces` |
| `BRepOffsetAPI_MakePipeShell` の既定の許容(1e-4)の面は、掃いた断面の端の軌跡から 3.8e-5 ずれ、隣の面と縫い合わせられない | `duocad/kernel_tight.py` `_side` |
| `ShapeFix_Solid` は面を置き換えることがあり、それより前の操作の由来(`Modified`)が結果の面に届かない | `duocad/kernel_tight.py` `_closed_solid` |
| 円・直線を B-スプラインに直した辺(ブーリアン・オフセットの途中で出る)をさらにオフセットすると、元の型に戻らず `GeomAbs_OffsetCurve` のまま返る(曲線そのものは円・直線で、標本点の残差は 1e-10 以下)。標本点を当てはめて直す | `duocad/kernel_sketch.py` `_entities_of_offset_curve` |
| 楕円を広げて縮めた面(辺が楕円のずらし曲線のずらし曲線)を `BRepOffsetAPI_MakeOffset` でさらに広げると、例外も `IsDone()=False` も出さずに壊れたワイヤを返す(20×10 の楕円を R=1, d=1.5 で包むと面積 43.3 と −16.8 の2枚。正しくは 236.81 の1枚)。同じ面でも距離によっては `IsDone()=False` で失敗する(角度 30 度で R=1, d=2)。`BRepBuilderAPI_NurbsConvert` で有理 B-スプラインにしてから渡すと access violation。広げるのに失敗するか、結果が「1枚で面積が減らない」でなければ、その辺を有理でない多項式の B-スプラインに近似し直してやり直す | `duocad/kernel_sketch.py` `_dilate_faces`・`_offset_ready` |
| `GeomConvert_ApproxCurve` に連続性 C2・次数上限 3 を頼むと、断らずに5次の B-スプラインを返す(C2 には5次が要る)。3次で頼めるのは C1 まで | `duocad/kernel_sketch.py` `_approximated_beziers` |
| 辺の長さは `GCPnts_AbscissaPoint.Length_s` の既定の許容差も build123d の `Edge.length` も楕円・B-spline で粗い(楕円 40×2 で +1.74%)。許容差を渡す | `duocad/kernel.py` `edge_length` |
| 螺旋の歯を芯の円柱へ `Fuse` すると、`BRepCheck` が真のまま芯が消える寸法がある(M5×0.8 で体積 −84%) | [THREAD.md](THREAD.md)「形の作り方」 |
| `VolumeProperties_s` の既定積分(Eps 無し)は何周も巻いた B-spline の面で狂う(歯だけで −19%、ねじの軸で 1.5e-03) | [THREAD.md](THREAD.md)「体積の既定積分はねじ山で狂う」 |
| `BRepBuilderAPI_Sewing` に蓋と帯を辺を共有させたまま渡すと、端の辺を縮退扱いにして殻にならない | [THREAD.md](THREAD.md)「採る: 面から直接組む」 |
| `BRepClass3d_SolidClassifier` は材料内部の孤立点を OUT と誤判定することがある(神託として使うときの作法) | [TOPOPT.md](TOPOPT.md) |
| `BRepAlgoAPI_Defeaturing` / `BOPAlgo_RemoveFeatures` は閉じられない面を例外にも `IsDone()=false` にもせず、警告だけ出して元の形(か消せた分だけ消した形)を返す。OCP の Defeaturing は警告の口を出していない | [FACE_EDIT.md](FACE_EDIT.md)「OCCT の罠」 |
| `BOPAlgo_RemoveFeatures.Perform` は計算中ずっと GIL を握る(3 秒の計算で主スレッドが 3 秒止まる)。実物の部品で丸め1枚に 61 秒かかる例がある | [FACE_EDIT.md](FACE_EDIT.md)「OCCT の罠」 |
| `BRepOffset_MakeOffset` は接線でつながる面を1組として同じ距離ずらす・SOLID でなく SHELL を返す・面積0の面を残した形を `BRepCheck` 合格で返す・由来は `Modified` でなく `Generated` に入る | [FACE_EDIT.md](FACE_EDIT.md)「OCCT の罠」 |
| BinTools(バイナリ BRep)の往復は厳密、テキスト BRep は 1e-14 ずれる(面の並びは保つ) | [FACE_EDIT.md](FACE_EDIT.md)「由来と再生の決定性」 |
| 面の添字は形状変更(フィレット)で散発的にずれる / STEP往復の体積は bit-exact でない | [DESIGN_DECISIONS.md](DESIGN_DECISIONS.md)(面の添字)、[DEVELOPMENT.md](DEVELOPMENT.md)「実測で判明している技術的な地雷」(STEP往復) |

## build123d / OCP

- **`build123d.Mesher().read()` は点群ではなく OCCT の形状(faceted B-rep)を返す**(0.11.1 実測:
  `Box(10,10,10)` を STL に書いて読み戻すと `Solid` / 12面 / 体積 1000.0)。内部で
  三角形 → 平面 Face → `BRepBuilderAPI_Sewing` → Shell → (manifold なら) Solid までやるので自前で書く必要は無い。
  ただし面数は三角形数と同じ(同一平面の三角形は併合されるが有機形状では併合されない)ので、
  10万三角形なら10万面のソリッドになり B-rep 編集は事実上できない。STEP 交換用途に限る。
  `is_valid` はプロパティで、`s.is_valid()` と呼ぶと `TypeError`。
- **build123d と OCP 直叩きの行数の差は「問い合わせ」に集中する**(同じ形状 — 箱60×40×20 + 貫通穴r8 +
  縦エッジr3フィレット、体積 43824.2481 で一致 — の実測): build123d 純正 7行(由来マップ取れない)/
  OCP 直接 32行(取れる)/ 生 `TopoDS_Shape` を保持し build123d を問い合わせにだけ使う 12行(取れる)。
  差のほぼ全部は述語による問い合わせ(Z に平行な辺を選ぶ: 11行 対 1行)。**操作層を OCP で書き直す費用は
  1 verb あたり +3〜5行**で、問い合わせ層は履歴の有無に関係なく要る(MCP が述語を要求する)。
  これが「操作は OCP、問い合わせは build123d」(`duocad/query.py`)の根拠。
- **build123d 0.11.1 は推移依存で SVG 一式を連れてくる**: svgelements 1.9.6(`transform` 解決済み座標で
  Move/Line/Arc/CubicBezier/QuadraticBezier/Close に正規化。`Arc` は rx/ry/center/theta/delta を持つ)・
  svgpathtools 1.7.2・ocpsvg 0.6.0・svgwrite 1.4.3。`duocad/svg_import.py` は追加依存ゼロでこれを前提にしている。
- **svgelements の変換済み `Arc` は `rx` `ry` `theta` `delta` `point()` が信用できない**: 実体は center / prx / pry
  (元の楕円の t=0・π/2 の点)と start / end と sweep で、変換は5点にそのまま掛かる。`rx` `ry` `theta` `get_rotation`
  は prx ⊥ pry を仮定して3点から作り直す値なので、せん断・回転後の非等倍スケールでは主軸でなく共役半径が入り
  (端点が数mmずれる)、反転(行列式が負)では sweep の符号だけが裏返る。`point(0.5)` もせん断で外れる(実測で7mm)。
  使えるのは center / prx / pry / start / end と |sweep|(`svg_import._arc_frame`)。
  同じライブラリは `<symbol>` `<mask>` `<marker>` `<switch>` の子を親の列へ平らに並べるので、読んだ後では描かれない定義を
  見分けられない(読む前に `<defs>` で包む。`svg_import._prepare`)。
- **OCCT の面分割の内部モデル(`IMeshData_Model`)から取ったハンドルは、モデルより長生きさせるとプロセスごと落ちる**
  (0xC0000005。モジュール変数に残す・context を先に捨てる、で再現)。寿命規則と解放順は
  `duocad/kernel_meshing.py` のモジュール docstring と `_Model.close()`。`BRepMesh_Context` は OCP に無いので
  標準の6部品を `IMeshTools_Context` に自分で組む(同ファイル `mesh_shape`)。OCCT 自身の境界交差検査が
  5度未満・小面積の交差を捨てる話は [TOPOPT_GAP_CAUSALITY.md](TOPOPT_GAP_CAUSALITY.md)。
- **`BRepOffsetAPI_MakeOffset` は面を渡してもワイヤの並びしか返さない**(`GeomAbs_Arc` で角は円弧)。外周と穴の
  区別は向きに無い(`BRepBuilderAPI_MakeFace` はどちら回りでも有界な面を作る)ので、ワイヤの入れ子の深さ
  (他のワイヤの面が1点を含むか)で外周と穴に分けて面を組み直す(`kernel_sketch._faces_from_wires`)。
- **負のオフセットは OCCT に直接頼めない**: 同心円で囲まれた面(座金の形)の内向きオフセットは距離によらず
  `IsDone()=False` で失敗し、円が完全に潰れる距離でも同じ失敗になる。正のオフセットは通るので、
  縮小は「十分大きな箱 − (箱 − 面) の拡大」で取る(`_erode_faces`)。面が消えるときは空の結果になる。
- **`BRepOffsetAPI_MakeThickSolid`(シェル)は `IsDone()` 真・`BRepCheck` 真のまま何もしない・別物を返すことがある**
  (2026-10-13 実測。箱 40×30×20、`MakeThickSolidByJoin`、Skin、許容差 1e-3、内向き = 負の厚さ):
  - 上面を抜いて t=-2 は正しい(体積 24000 → 7152 = 外 − 内 36×26×18)。円柱・縦辺 r3 のフィレット付きの箱も通る。
  - **厚すぎる(t=-16)と元の形のまま返る**(体積が変わらない)。
  - **抜く面がフィレット面に接していると、`Intersection` は元の形のまま、`Arc` は `BRepCheck` 偽**(全辺 r3 の箱の上面、t=-2)。
    `intersection` / 許容差 1e-6 を変えても同じ。抜く面に接する辺を丸めなければ通る。
  - **抜く面を空にすると中空にならず、内側へオフセットした中身を返す**(箱では 36×26×16 の中実、全辺フィレットの箱では SHELL)。
    閉じた中空は `MakeOffsetShape`(内向き)で作った中身を元から cut すれば取れる(箱で体積 9024・Solid 1つ・`BRepCheck` 真)。
  - フィレット半径以上の厚さ(r3 に t=-4)は `IsDone()=False` で正直に失敗する。
  - **出っ張りの半径を超える厚さは、そこだけ薄い壁のまま成功する**(箱に半径3の円柱を足し、底を抜いて t=-4。
    円柱の壁は外半径3・内半径1で厚さ2。体積も `BRepCheck` も通る。2026-10-14 sol の指摘で実測)。
  - 許容差 1e-3 のまま厚さ 0.001 にすると `IsDone()=False`(1e-6 なら通り、体積も解析値と一致)。
  したがって `IsDone` と `BRepCheck` だけでは足りず、**体積が実際に減ったこと・結果が Solid 1つであること・
  内壁と外壁の最短距離が厚さを割らないこと**を見る(`kernel_shell.shell_solid`)。開口の縁(抜いた面の上に残る面)は
  由来で新しい面として記録されることがある(NURBS にした箱)ので、内壁から外す。
- **`BRepExtrema_DistShapeShape` は同心の円筒どうし・球と円筒の面の対で `StdFail_NotDone` を投げる**(最短の組が
  無数にある。丸めた箱の内壁と外壁で実測)。その対を1つでも含む複合形状どうしも同じく落ちる。
  面の縁の上の点から測れば測れる(`kernel_shell._face_distance`)。**角を丸めた箱に、丸めを同心にした中子を隙間 0.2 で入れた形でも落ち**
  (r=3 と r=2 で再現、隙間 0.1 の r=3 では落ちない。`SetMultiThread(True)` だと `Standard_ProgramError: Multiple exceptions` で上がる)、
  面の対ごとに測って落ちた対だけ縁の点から測ると 0.1999999718 と測れる(Issue #186 の試作。Git `3c3fb655` の `tools/interference_probe/concentric.py`)。
  全周の同心円筒(ピンと穴)と、円筒の穴の中の球は落ちなかった。
- **`BRepExtrema_DistShapeShape.InnerSolution()` は当てにしない**: 片方がもう片方の中にあるとき True になる一方、体積の重なりが 0 の
  接触の組でも True が出た(Issue #186 の試作、PC ケースの STEP)。
- **General Fuse / BOP は既定の破壊モードで入力の形状許容差を書き換える**: PC ケースの STEP 27 体に `BOPAlgo_Builder` を掛けると、
  11 体の最大許容差が変わった(例 1e-7 → 1.5e-7)。`SetNonDestructive(True)` で 0 体、時間は同じ。文書の形に結果を残さない計算
  (問い合わせ・プレビュー)では非破壊にするか写し(`kernel.copy_shape`)に掛ける(Issue #186 の試作 `astra_checks.py`)。
- **`BOPAlgo_BOP.PDS()` や `PPaveFiller().DS()` が返す DS に Python から触るとプロセスが落ちる**(終了コード 127。小さな形では
  インタプリタの終了時に落ちる)。ラッパがポインタの持ち主のつもりで解放する。DS を読むなら `BOPAlgo_PaveFiller` を自分で作り
  `DS()`(参照)を読む(`kernel_brep.inspect_solid` の `BOPAlgo_CheckerSI.DS()` も参照なので安全)。
- **BOP の DS の FF 干渉は「面が触れている」ではない**: 隙間 0.2 で離れた2部品でも面の組が 114 件記録される(境界箱が重なる面の
  組の記録)。接触は GF の像の共有(両方の像に同じ面・辺・頂点があるか)で読む([INTERFERENCE.md](INTERFERENCE.md))。
- **OCP からは OCCT の計算を途中で止められない**: `BOPAlgo_Builder.Perform(theRange)` は進捗の範囲を受け取るが、止める側の
  `Message_ProgressIndicator` は OCP で構築子が無く Python で継承できない(`TypeError: No constructor defined!`)。山形が一致した
  ねじ山(M8×1.25、らせんの B-spline 面どうしが重なる)の2体 GF は 240 秒で終わらなかった(M20×2.5 では 4.2 秒で終わる)。
  時間の上限が要る計算は子プロセスで走らせて止める。
- **`BRepExtrema_ShapeProximity.OverlapSubShapes1/2()` は OCP 7.9.3 で戻り値の型(`NCollection_DataMap<int, TColStd_PackedMapOfInteger>`)が
  登録されておらず `TypeError` になる。**
- **manifold3d の `Manifold.min_gap(other, search)` は結果を [0, search] に丸める**: search を 0 にすると全部 0(接触)になる。
- **R と d の円弧が同じ点で接する結果の結合は、既定の許容差(1e-7)だと取りこぼしうる**ので、ブーリアンに
  1e-6 のファジー値を付ける(`_ENVELOPE_TOLERANCE`)。ベジエ辺の領域は、オフセットが元の曲線の近似曲線を返し
  線分・円弧だけという指定を満たせないので拒む。
- **外部の罠: 同一の面を共有する2つのソリッド(体積の重なりは0)が同じ `BOPAlgo_BOP` の cut に入ると、OCCT が面を捨てて不正な形を返すことがある。**
  hanger の保持(厚さ5 mm の環)の内面と除外(半径13.288 mm のロッド)のくびれ面が同一のトーラス(距離 1e-13 mm)で、道具がもう1つ以上あると
  `AcquiredSelfIntersection`・`FaceBuilderUnusedEdges`・`SolidBuilderUnusedFaces` の警告のあと `BadOrientationOfSubshape` が出る(メッシュ次第で出たり出なかったりする)。
  片方を軸まわりに ×0.999(面が約13 µm離れる)にすると警告0、×1.001 では `FaceBuilderUnusedEdges` が2件残る。fuzzy 1e-4 / 1e-3 mm は警告が6 / 5件に減るだけで、cut と union の順の入れ替え・先に道具を fuse・2回に分けた cut でも警告は残る
  (推定: 辺が二重になって部品をツール経由でつながったと見なす)。製品の最終処理はこの経路を持たない(メッシュ集合演算。[TOPOPT.md](TOPOPT.md)「最終処理はメッシュ集合演算」)。
- **テーパー付き押し出しの2経路は、どちらも単独では足りない**(2026-10-17 実測、issue #182。長方形 20×10・円 r5・
  直線+円弧+ベジエ・楕円 6×3・円穴 r2 の板、高さ 10、角度 ±5° ほか):
  - **`LocOpe_DPrism(face, h, angle)` の `h` は側面に沿った斜めの長さ**(上面の z = h·cos(angle))。正の角度が細る向き。
    ベジエ・楕円の辺も通る。ただし**穴のある面は穴を無視した体積を返し**(0° で 2000 = 穴なしと同じ。-5° は `BRepCheck` 偽)、
    **太る向きでは角が円錐面で丸まる**(長方形 -5° で円錐面4枚)。**断面が潰れる角度でも例外を出さず、潰れた高さで
    打ち切った形を `BRepCheck` 真のまま返す**(長方形 40°・60°、円 40°)。
  - **素の柱を `BRepOffsetAPI_DraftAngle` で傾ける**(側面を `Add(face, 押し出し方向, angle, 底面の平面)`)と、高さは垂直のまま、
    太る向きでも角は尖ったまま(平面6枚)、円は円錐1枚で解析体積と一致、穴は同じ向き指定で外形が細り穴が広がる(抜き勾配)。
    潰れる角度(長方形 40°)は `IsDone()=False`。**ただしベジエ・楕円を押し出した側面(`SurfaceOfExtrusion`)は `AddDone()=False`**。
  - **輪郭線ごとに `BRepOffsetAPI_MakeDraft(wire, 方向, |angle|)` + `SetOptions(BRepBuilderAPI_RightCorner)` で側面の殻を作り、
    底面と上の自由境界から張った面を縫う**と、ベジエ・楕円・L字の凹角まで角が尖ったまま通り、長方形・円は DraftAngle と体積が一致する。
    **細る/太るは角度の符号でも輪郭の向きでもなく `SetDraft(True)` で内向きになる**(既定は外向き。`Perform(L)` の L は斜辺の長さ)。
    穴は穴の輪郭を逆向きに傾けた立体を外周の立体から cut すれば、外周が太ると穴が狭まる(DraftAngle の穴あき板と体積一致)。
    潰れる角度は長方形・L字では殻が `BRepCheck` 偽になるが、**円は頂点を越えて砂時計形の円錐を返し、`BRepCheck` も
    `BOPAlgo_ArgumentAnalyzer` の自己交差検査も通る**(円 r5・高さ 10・40°)。潰れの検出は自前で要る。
    楕円 6×3 を高さ 10 で 5° 細らせると先端の曲率半径が約 0.6 になり、全辺 r1 のフィレットは失敗する(r0.5 や高さ 5 なら通る)。
  - **`MakeDraft` は `Perform` の長さが 7e-5 mm 以下だと access violation でプロセスごと落ちる**(1e-4 は通る。角度 0.01°〜45°・
    輪郭の大きさ 0.01〜1万倍によらない。2026-10-18 実測、sol レビューで発覚)。円・楕円は 1e-5 で `IsDone()=False` を返す。
    `kernel_extrude.MIN_TAPER_DISTANCE` で先に断る。
  - **`MakeDraft` は角度 89.8° 以上で別の形を成功として返す**(20×10・高さ 10・89.9° で体積 2000、期待は約 4.4 億)。89.5° までは正しく、
    89.6〜89.7° は `BRepCheck` 偽で断られる。距離・輪郭の大きさによらず角度だけで決まる(2026-10-18 実測)。上限は `MAX_TAPER` = 89°。
  - **`MakeDraft` は輪郭の始点(=終点)の角で、最初と最後の側面をつながずに隙間を残すことがある**(2026-10-31 実測。
    円 Φ10 と楕円 8×4 の和の輪郭、高さ 10・±2°/±5°)。`IsDone()` も殻の `BRepCheck` も真のまま、殻の自由境界が底・縦の隙間・上を
    一周する1本になり、許容差 1e-2 mm で縫っても閉じない。同じ輪郭でも始点の角を変えると通ることがあり(どの角なら通るかは
    凹凸・滑らかさで決まらない)、辺の中点を始点にするとほぼ通る。`kernel_extrude._midpoint_starts` が失敗時に長い辺の中点から
    最大3回やり直す(最初の試行は元の始点のままなので、通っていた輪郭の面の数は変わらない)。
    円・楕円の継ぎ目で割れた弧も同じく閉じない原因になる(楕円 10×8・20° と楕円 8×4 の和、+5°。継ぎ目のそばに 0.13 rad の弧が残る。
    同じ形を 200° で描けば通る)。`ShapeUpgrade_UnifySameDomain` は継ぎ目で割れた弧をまとめないので、やり直しの最初に
    `kernel_extrude._joined_seams` で同じ円・楕円の弧を1本にした輪郭を試す。
  - **`MakeDraft`(RightCorner)の角は、辺の端の接線の延長で尖らせた断面になる**(曲線をそのまま延ばした形ではない)。
    円と楕円の和・楕円2つの和・長方形と楕円の和を ±2〜5° で傾けると、ふたの輪郭の点は「底の辺か辺の端の接線の延長から
    z·tan(taper) の距離」から 8e-5 mm 以内にあり、曲線を延ばした形からは細る向きの凹角のそばで reach の 2〜22% ずれる(2026-10-31 実測)。
    このため、円と楕円の和を細らせて「曲線を延ばした断面」なら上端で2つに離れる組み合わせ(円Φ10+楕円8×4、高さ10・-4°)も、
    接線の延長どうしがつながって1つの立体になる。直線と円弧だけの輪郭の `DraftAngle` は円錐をそのまま延ばすので、こちらは離れると断る。
  - **端の1階微分が0のベジエ(端の制御点が重なる)で `MakeDraft` は `gp_Vec::Normalize` の Standard_ConstructionError を投げる**。
    `Approx_CurvilinearParameter` で付け直すと NaN になる。重なった制御点を 1e-7 mm ずらせば通る(端点は動かない)。
    同じ点で `BRepLProp_CLProps` は接線ありと答えるのに `Normal` が LProp_NotDefined を投げる。
  - **曲率の極値は `Geom2dLProp_CurAndInf2d.PerformCurExt` で式で取れる**(曲率半径 0.001 mm の鋭いベジエの山を t=0.507 ちょうどで返す。
    極が重なった直線状のベジエでは `IsDone()=False`)。OCP の例外は共通の基底を持たず `Exception` を直に継ぐ。
  - **楕円を傾けた側面と底・ふたの縫い目は、押し出し量 0.1 mm だと許容差 1e-6 で閉じない**(自由辺2本・`BRepCheck` 偽。1e-5 で閉じる)。
    殻の `ShapeAnalysis_ShapeTolerance` は 1e-7 と答えるので当てにならない。両端の極が重なった直線のベジエを極をずらして傾けると、
    体積の積分が 2.4% ずれる(sol レビューで再現。線分に置き換えれば正確)。
  - **`MakeDraft` は長さ 0.14 mm の辺(0.1 mm の面取り)を持つ多角形で、角度 ±0.3〜5° のどれでも Standard_OutOfRange を投げる**
    (輪郭の向きによらない)。同じ輪郭を `DraftAngle` は作れ、面取り辺が消える角度(−1°)では `IsDone()=False` を返す。
    直線と円弧だけの輪郭は `DraftAngle` に任せる。
- **`BRepGProp.VolumeProperties` の既定の積分と適応積分は、面の種類で得手が逆になる**(2026-10-19 実測、sol レビュー):
  - 既定の積分は、細い楕円を傾けた B スプラインの側面と B スプラインの曲線で囲まれた平面のふたで 3〜152% ずれる
    (幅12・高さ1・距離10・5° で 215.53、真の値 208.48)。`Eps=1e-9` の適応積分なら合う。
  - 適応積分は押し出し面(楕円柱の側面)で重心を 0.02 mm ずらす(幅120・高さ60・距離10。Eps=1e-6 では 0.25 mm)。既定なら正確。
  - 面を1枚ずつ `VolumeProperties_s` に渡すと、基準点が面の位置変換に合わせて動き、寄与を足しても体積にならない(平面のふたが 0)。
    共通の基準点で `BRepGProp_Vinert(BRepGProp_Face, BRepGProp_Domain, 基準点[, Eps])` を面ごとに作って `GProp_GProps.Add` する。
    基準点は**閉じた殻ごとにその殻の境界箱の中心**にする(原点だと、Z=100 の 12×6 の楕円を 0.001 mm 押した形で体積が 1.3% ずれ
    重心が立体の外へ出た。形全体の境界箱の中心でも、離れた2つの薄い殻を持つボディで重心が 0.24 mm ずれた)。
    形全体を既定の積分で測る経路(原点が基準)は、箱・円柱を Z=10000 に置いても相対 2e-10 で問題ない。
  `kernel._volume_props` は、曲面か境界の辺が B スプライン・ベジエ・平行曲線の面だけ適応積分にし、無ければ形全体を既定の積分で測る。
- **`MakeDraft` で楕円を傾けた側面は近似で、底・ふたとの継ぎ目に約 1e-5 mm のすき間が残る**(12×6 の楕円を 0.001 mm・5° で押すと、
  縫い合わせは 1e-6 で閉じず 1e-5 で閉じ、辺の許容差は 8.8e-6。2026-10-19 実測)。体積・重心はこのすき間 × 基準点からの距離(重心は2乗)
  だけずれる: その板を 100 mm 離して2枚置き Φ0.2 の軸でつないだ1つの殻では、重心が 0.0085 mm ずれる。既定の積分・形全体の適応積分・
  殻ごとの基準点の面ごとの積分のどれでも同じ値になる(積分のやり方ではなく形のすき間が原因)。板1枚だけなら重心は正しい。
  sol レビュー8回目の指摘で、追いかけるのはここで止めた(正すには楕円の傾いた面を `MakeDraft` に頼らず正確に組む必要がある。
  元の曲線と `Geom_OffsetCurve` の間の線織面(`BRepFill.Face`)は、細い楕円で対応点がずれ途中の断面が 1.1 mm 外れて使えなかった)。
- **`MakeDraft` は、継ぎ目が曲率のきつい点にある1本の辺の閉じた輪郭で Standard_OutOfRange を投げる**(幅120・高さ1 の楕円、継ぎ目は
  長軸の端)。継ぎ目を短軸の端へ移すか、4本に分ければ通る(`kernel_extrude._split_closed_edge`)。
- **`BRepGProp.LinearProperties` と `GCPnts_AbscissaPoint.Length` は、精度を渡さないと半径 6・3 の楕円の周長を 29.1035 と返す**
  (真の値 29.0653。0.13% 大きい。`Length_s(curve, 1e-9)` なら 29.065345。2026-10-19 実測)。検算に使うなら精度を渡す。

## 複数面の押し出し（`kernel_extrude.extrude_faces`）

- **法線の平行判定は内積ではなく外積の大きさで見る。** OCCT の `Precision::Angular`(1e-12) を許容値に
  使うと、`1 - cos` は倍精度で 1e-16 未満を区別できないので内積では判定にならない。同一平面の判定は
  先頭の面の重心からの法線方向の距離を `Precision::Confusion`(1e-7 mm) と比べる。STEP 由来で
  法線に微小なずれがある面は弾かれうる(エラー文に何番目の面がどの条件に反したかが出る)。
- 柱を `_fuse_all` で束ねてから1回だけ join / cut し、同一平面の統合(`_unify_same_domain`)は
  コピーで試して採否を決める。**統合を掛けるのは束ねたときだけ**(柱が複数・相手ボディへの join / cut、
  それと `boolean_bodies` の union)。柱1本の new は統合しない — 輪郭の分割で割れた側面までまとまり、
  保存済みログの後続の操作が指す面・辺の番号がずれる(領域の押し出し `extrude_regions` も同じ規則)。1面の結果は、廃止した verb `extrude_face` の保存済みログの再生結果と
  体積・面数が一致することをテストで固定している(`tests/test_extrude_face_replay.py`)。

## ロフト（`BRepOffsetAPI_ThruSections`、#189 2026-10-17）

実測(OCP、`ThruSections(isSolid=True, ruled=False, 1e-6)`、断面は 20 角・r10 前後、間隔 30):

- **`CheckCompatibility` の既定は True**(呼ばない結果と一致)。False にすると辺の数が違う断面(正方形→円)は
  `BRepCheck` 偽・体積が半分以下になる。True は辺の数を揃え、始点と回る向きを揃える。
- **`BRepAlgoAPI_Check(shape, bTestSE, bTestSI)` の第3引数が自己干渉の検査。** `(shape, True, False)` と呼ぶと
  自己干渉を見ず、下の壊れた形をすべて通す(当初この呼び方で「自己干渉の検査も通す」と誤記録し、sol レビューで発覚)。
  `bTestSI=True` なら下の潰れ・蝶ネクタイ形・裏返りを捕まえる(1形状 25〜80 ms。正常な形でも辺の多い断面で 100〜180 ms)。
- **以下の壊れ方は `BRepCheck_Analyzer` を通す**。体積が 0・負・小さく出る:
  - **2断面の平面のなす角がちょうど 90°** だと、始点・向きの揃え方が平面の軸の取り方しだいで変わる。
    duocad の XY 正方形 → YZ+30 の正方形は既定で**体積 0**(中央で潰れる)。XZ+30 なら正常(12000)。
    箱の上面 → 側面の正方形は ±X で母線が交差して 500、±Y で正常な 1500(幾何は90°回転で合同)。
    2つ目のワイヤの始点を1つずらす・逆回りにすると正常側に移る。円→円・正方形→円は 90° でも正常。
  - **法線のなす角が 90° を超える**(91°〜179°)と線の向きを逆と見なして裏返る(91° で体積 −106)。180°(向かい合う平行)は正常。
  - **楕円 → 長軸の向きが違う楕円**は継ぎ目を揃えない(30°〜135° で形が乱れ、80°・90° で体積 0)。
    辺の始点を前もって揃える(`MakeEdge(elips, t0, t0+2π)`)と直る。
  - **円 → 円で継ぎ目のずれが約 35° 未満**だと揃えずにその角度だけねじる(30° で胴がくびれ体積 −4.5%)。40° 以上は揃える。
    duocad の円の継ぎ目はスケッチ平面の u 軸の向き。これは自己交差ではないので `bTestSI` でも捕まらない。
- **L字(凹) → 円・楕円(1辺の閉曲線)は `IsDone()=False`** が継ぎ目の位置しだいで出る(45° だけ通った)。
  円を前もって 6〜12 本の円弧に割ると通る(2〜4 本は不可)。U字・星 → 円は通る。
- 同じ形を回した断面(正方形 60°)は**最小のねじれ**(−30°)を選ぶ。長方形 40×10 は 60° をそのまま採る(180° 対称のため)。
- 正常だったもの: 平行な平面での始点・回る向き・円の継ぎ目(40° 以上)の違い、辺の数の違い(3〜12角形・角丸・星)、
  中心のずれ、大きさの差(2 角 → r50)、傾き 89° まで、3断面。生成は 1〜3 ms。
- 2断面の間は直線でつながる(`ruled=True` と同体積)。傾いた平面でも曲がった継手にはならない。
- 対応を 180° ずらした形(長方形 → 円は側面が軸を横切る、正方形どうしは中央が1点にすぼまる)と、半径 10・1・1 の円を
  なめらかにつなぐ形(断面の間で軸まで食い込む)は、体積が正のまま `BRepCheck` を通し、`bTestSI=True` で捕まる。
- **重心から見て裏に回り込む凹み(U字)は、どの揃え方でも通るとは限らない。** duocad の U字 → 正方形は、長さの割合で
  割っても OCCT の揃えでも自己交差か `IsDone()=False`(どの始点からでも)。U字 → 円は始点を1〜2頂点ずらすと通る。
- 対策は `kernel_extrude.loft_regions`(向き・始点・辺の数を自前で揃えて `CheckCompatibility(False)`、`bTestSI=True` の検査)。
  上の 90°・楕円・円の継ぎ目・L字 → 円のケースは全部、合同な配置で体積が一致する正常な形になる(`tests/test_loft.py`)。

## 平面でのボディの分割（`BRepAlgoAPI_Splitter`、`kernel_split`、#193 2026-10-21）

- **平面を道具にした `BRepAlgoAPI_Splitter` で一度に割る**。平面は境界箱の対角より大きい `BRepBuilderAPI_MakeFace(gp_Pln, -h, h, -h, h)` の面にする。結果は Solid の compound なので `kernel.split_solids` で塊に分け、**各塊の体積重心が平面のどちら側にあるかで正側・負側へ振る**(塊は必ず片側に収まる)。半空間 `BRepPrimAPI_MakeHalfSpace` との `Common` / `Cut` を2回やる案より、演算が1回で切り口の面が両側で共有される。箱・円柱(軸を通る/横切る/斜め)・球・トーラス・全辺を丸めた箱で、体積の合計は相対 1e-6 以内、各塊は `BRepCheck` を通った。1回 5〜50 ms(丸めた箱 47 ms)。
- **平面が面に接しているだけ・ボディの外にあるときも `IsDone()` は真で、元の形そのままの Solid が1つ返る**(エラーにならない)。「交わらない」は自分で見る: 境界箱の中心から平面までの距離が、法線方向の箱の半幅を超えれば外・等しければ接している(1e-6 mm の余白)、それ以外でも**片側に塊が無ければ**(曲面が箱の角だけを掠める)断る。体積比 1e-9 以下の塊も断る(1e-7 mm 手前の平面は接触と同じ扱い、1e-4 mm 手前は体積 0.01 mm³ の薄板が通る)。
- **由来は Splitter 自身の `Modified` / `IsDeleted` で取れる**(`kernel._target_successors` を元のボディ・Splitter で呼ぶ)。切り口の面は `new_faces`(どの元の面の後継でもない面)に入る。塊ごとに面の添字が別なので、`face_map` の値は塊ごとに作る。
- **塊の並びは `TopExp.MapShapes_s` の走査順に頼らず、(側, 重心の x, y, z を小数6桁に丸めた値)で並べ直す**。引き継ぎ先(同体積のときは正側)と新しいボディへの ID・番号の割り当てがログの再生で同じになる条件。

## STEP ファイル

- **Fusion 360 のジェネレーティブデザイン結果を STEP で出すと長さ単位が `SI_UNIT(.CENTI.,.METRE.)`(cm)**
  になる。同じ設計の入力 STEP は mm なので食い違う(実測: 入力 `shelf_attachment.step` は mm、GD 結果
  `shelf_attachment_8.step` は cm。座標を mm と読むと 11.6×15.5×51.3 に見え、正しくは 60×154×113mm)。
  **1/10 でもスライサのビルドボリュームに収まるのでエラーにならず気づきにくい。** 読み込んだ直後に実寸を確かめる。
  単位宣言は `grep -a -o "SI_UNIT([^)]*)" <file>` で先に見る。
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
- **STEP を面の種類から読む前に、ソリッド数と体積を見る。** 全周円筒は穴の内壁もピンの外側も同じ種類になる
  (`shelf_attachment_GD.step` の事例は [DESIGN_DECISIONS.md](DESIGN_DECISIONS.md)「全周円筒のしきい値」)。
  ソリッド数 → 1部品かアセンブリか、体積を既知の式と突き合わせる、の順が安い。他人の配布データが混在するフォルダで
  面の統計から「本人の設計」を推定するのは危うい。
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
  `ModuleNotFoundError: No module named 'OCP'` で収集エラーになる**(19件 / 48件等、ファイル数で変わる)。
  PATH の `python` はセッションごとに実体が違う。
  **「テストが全部壊れている」に見えるが、インタプリタを間違えているだけ。**
  正しい呼び方は `.\.venv\Scripts\python.exe -m pytest -q`(`pyproject.toml` の `pythonpath=["."]`
  があるので venv の python なら素の `pytest` でも通る)。
  **`python -m pytest -q 2>&1 | tail` と書くとパイプ末尾の終了コード(0)が返る**ので、
  件数を読まずに終了コードだけ見ると「緑」と誤読する。duocad で赤を見たら、まず環境と負荷を疑う。
- **`.venv` は uv 管理で pip が無い**(`python -m pip` は `No module named pip`)。パッケージ操作は
  `uv pip ...`。依存の解決だけ見るなら
  `uv pip install --dry-run --python ./.venv/Scripts/python.exe <package>`。
  Python は 3.14(`pyproject.toml` が `>=3.14,<3.15`)。上限は C 拡張の固定版(cadquery-ocp-novtk /
  gudhi / manifold3d / scikit-image)の Windows 用 wheel が cp314 までしか無いことで決まる。
  `uv sync` は uv 管理の 3.14 が無ければ Python インストールマネージャの
  `%LOCALAPPDATA%\Python\pythoncore-3.14-64` を拾って `.venv` を作る(それを消すと `.venv` も壊れる)。
  宣言依存は `pyproject.toml` のとおりで、numpy 2.4.6 / scipy 1.17.1 は推移依存で入っている。
  入っていないもの: trimesh・pyvista・vtk・gmsh・matplotlib・numba・cupy・torch・pyamg・scikit-sparse・meshio。
- **検査の在庫は `.\.venv\Scripts\python.exe -m pytest -q --collect-only` で数える。**
  契約と「なぜその形か」は各モジュールの docstring が持つ(契約を散文でまとめた別ファイルは、
  コードを変える人の視界に入らず実装と食い違うので置かない)。
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
  **ただし確認なしに再起動しない** —— 別セッションが開いたままの未保存文書(画面に「未保存」と出ているもの)が
  そのプロセスに乗っていることがあり、再起動すると誰のものでも消える。他に繋いでいるセッションが無いか・
  未保存でないかを先に確かめ、確かめられなければ pytest 等の静的検証で済ませてライブ確認を見送る
  (フロントだけの変更は `strictPort:true` の Vite dev サーバを別に立てれば同じ生きたバックエンドへ繋ぎ直せる)。
- **`bodyId`(`body-01a54896757b` 等)はインポートごとにその場で振られ、STEP の内容から決定的に
  導かれない。** 同じ `PC_case_20251122.step`(27部品)を別々のサーバ起動で読んで矩形選択を比べると、
  選ばれた面の総数(2835件・27ボディ)は一致するが `bodyId` は1件も一致しなかった。
  **起動をまたいで選択結果を突き合わせるときは `bodyId` を無視し、ボディごとの面インデックス集合
  (ソート済み配列)をキーにグルーピングして比べる。** 同一起動内なら `bodyId` は変わらない。
