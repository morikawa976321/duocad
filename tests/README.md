# duocad — テストが定義するAPI契約

このディレクトリのテストが、duocadのコア・選択機構・MCPが守る**現在のAPI契約**そのもの。
以下はモジュール単位に整理してあり、各節は「いま何が真か」だけを書く（経緯・変更の理由・
日付・レビュー指摘の帰属はここには置かない — それらは `git log` で辿れる）。仕様の背景は
`docs/REQUIREMENTS.md`、設計の到達点は `docs/IMPLEMENTATION_PLAN.md`、AI側の使い方は
`MCP.md`、アーキテクチャ全体は `readme.md` が正で、ここに書くのはそれらと重複しない
**テストが実際に固定している契約**だけである。

境界（`duocad/` 配下でOCPを直接importしてよいのは `kernel.py` だけ、build123dは
`query.py` だけ）と、その他のアーキテクチャ上の決めごとは `readme.md`
「モジュールとimport境界」が正。この文書はその境界の**中身**（各モジュールが何を
保証するか）を書く。

## 境界検査の仕組み（`tests/test_boundaries.py`）

`duocad/` を再帰的に静的走査し、`import OCP` 系（動的import含む）と `import build123d` 系を
検出する。免除は相対パス一致で判定する `kernel.py` / `query.py` のみ（ファイル名一致にすると
`duocad/verbs/kernel.py` を置くだけで無効化できてしまうため、パスで判定する）。検査関数自身を
既知の違反ソースで検査する自己検査（`test_boundary_scan_detects_every_import_form`）を持ち、
新規ファイルが走査対象から漏れていないこと自体も別途固定してある（各モジュール群のimport境界
自己検査を参照）。検出できないのは `exec()`/`eval()` 文字列と C拡張経由の間接呼び出しで、
これは規律の事故防止が目的であり悪意への防御ではないことによる意図的な割り切り。

## `duocad/document.py`

```python
BodyId = str

Body(id: BodyId, name: str, kind: str, shape: Any = None, visible: bool = True)
```

- `kind` は `"solid"`（編集対象）以外は `ValueError`（`__post_init__` で検査）。
  読み取り専用の「参照ボディ」（`kind="reference"`）は2026-09-03に概念ごと撤去した。

```python
Document()
Document.units -> str                              # 常に "mm"。セッター無し・不変
Document.bodies: list[Body]
Document.log: OpLog                                 # 生成時に自動で持つ
Document.sketches                                    # スケッチの置き場（duocad/sketch.py参照）
Document.parameters                                   # 名前つき値の置き場（duocad/parameters.py参照）
Document.selection                                    # 選択集合の置き場（duocad/selection.py参照）
Document.add_body(body: Body) -> BodyId              # 追加したボディを常にactiveにする
Document.remove_body(body_id: BodyId) -> None
Document.active_body() -> Body | None
Document.set_active_body(body_id: BodyId) -> None     # 存在しないIDはKeyError
Document.is_editable(body_id: BodyId) -> bool         # kind=="solid" のときだけTrue
```

**契約**: `units` は `"mm"` 固定で変更できない。`bodies` は常にリストで持つ（1つの文書に
複数ボディが自然発生する — STEPを読めば1回で複数ソリッドが増える）。`add_body` は追加した
ボディを常にactiveにする。`Document` → `sketch.py` / `selection.py` への依存は作らない
（`document.py` 自体はそれらの型を知らない。依存は逆方向の一方向）。

## `duocad/oplog.py`

```python
Operation(seq: int, verb: str, args: dict = {}, provenance: Any | None = None)

OpLog()
OpLog.entries -> list[Operation]                  # 有効な（undoされていない）エントリ
OpLog.append(verb: str, args: dict) -> Operation
OpLog.undo(document: Document) -> Operation | None
OpLog.redo(document: Document) -> Operation | None
OpLog.to_json() -> str
OpLog.from_json(data: str) -> OpLog                # classmethod
OpLog.replay(document: Document) -> None
OpLog.redo_count -> int                             # undoで捨てたが取り消せる本数
OpLog.FORMAT_VERSION = 2
```

**契約**:

- `seq` は `append` のたびに1から単調増加する。
- 保存形式は**JSON**（SQLiteは使わない — gitで差分が読める・AIが全履歴を読める・1モデル=1
  ファイルでコピーできる、が理由）。`to_json()` → `from_json()` の往復で `entries` が完全一致する。
- `append()` は**ログへの記録のみ**を行う（副作用の実行はしない）。副作用は呼び出し側が
  `document` を直接操作した**あとで** `append()` を呼ぶ。
- `undo`/`redo`/`replay` の反映方法は verb ごとに **`register_verb(name, do=, undo=)` で
  登録する**（`oplog.py` の中に verb名の if 連鎖を書かない — verbを定義した側が登録する）。
- `replay(document)` は**自分の `entries` を書き換えない**（再生のたびに履歴が増えない）。
  「開くときはログから再生する」運用は
  `doc = Document(); doc.log = OpLog.from_json(...); doc.log.replay(doc)` の形になり、
  `document.log` の差し替えは呼び出し側の責務。
- `undo` の実装は「作ったものを消す」系と「形状を元へ戻す」系の2通りに分かれる（verbによる）。
  **`OpLog.undo` はハンドラを呼んでからカーソルを戻す**ので、undoの中でログ全体を再生すると
  取り消すはずの操作まで再適用される — 再生範囲は `seq < operation.seq` に限ること
  （`parameters.rebuild` はこれを踏まえた実装になっている）。
- 保存形式の版は `FORMAT_VERSION = 2`。**版1のファイルは明示的に断る**。旧版は
  `args={"path": str, "body_id": BodyId}` の形で `import_step` を記録していたが、
  現行は `{"source_name": str, "step": <STEP本文>, "body_ids": [...]}`
  で**形そのものがログの中にある**（再生は外部ファイルを一切読まない）。
  **旧`reference_ids`キー**（2026-09-03の参照ボディ撤去前に書かれた保存ファイルが持つ）は
  `from_json`が未知のキーとして黙って無視する。
- `Operation.selection` フィールドは廃止済み（読むのはテストのみだった。存在しない）。
  **後方互換**: 旧フィールドを含む保存ファイルは `from_json` が黙って無視して読み込める。

## `duocad/io_step.py` と verb `import_step`（`duocad/verbs/import_editable.py`）

STEPの読み込みverbは**1本だけ**（`import_step`）。参照ボディという概念自体が2026-09-03に
撤去されたため、それを足す読み込みも、後から編集可能にする複製のverb（`make_editable_copy`。
これは2026-08-26に既に削除済み）も無い。

```python
# duocad/verbs/import_editable.py
import_step(doc: Document, path: str | Path) -> list[BodyId]   # 戻り値は編集用ボディのID
```

- ソリッドごとに**編集用ボディ**（`kind="solid"`・可視）を1つ追加する。名前は
  `<ファイル名(拡張子なし)>_N`。**単一ソリッドでも1要素のリストを返す**（戻り値の型が
  入力に依存すると呼び出し側が必ず分岐を書く羽目になる）。
- **操作ログは1行**（`args={"source_name":.., "step": <STEP本文>, "body_ids":[...]}`）。
  **形はログの中にある** — `step` にSTEP本文を生テキストで持つので、redo/replayは
  外部ファイルを一切読まない。undo1回で全ボディがまとめて消える。
- 参照ボディを可視のまま持つと同じ形が二重にテセレーション・二重に送信され、手前を消しても
  参照側のメッシュが残ってピック失敗が再発するため、**参照ボディは追加した時点で
  `visible=False`**。
- 失敗（パス無し=`FileNotFoundError`、壊れたSTEP=`ValueError`）は `kernel.read_step_text` の
  中で止まり、document・logのどちらにも変更を残さない。
- STEPは**バイト列で読み、UTF-8で復号できなければlatin-1へ落とす**（`io_step.read_step_source`。
  latin-1はどのバイト列でも失敗しないため、ヘッダにASCII外のバイトを持つ実ファイルでも読める）。

### `export_step` / `export_stl` — 対象は「いま表示中のBody」

```python
export_step(doc: Document, path: str | Path) -> None
export_stl(doc: Document, path: str | Path, deviation: float) -> None
```

対象を選ぶ引数は無く、**書き出し対象は固定の2段階の規則**で決まる（`io_step._bodies_to_export`）:

1. **候補は `kind=="solid"` の全Body**。1つも無ければ形状を持つ全Body（参照だけの文書の救済）。
   `Session.load_step`（＝`import_step`）は各ソリッドを参照と編集用の対で持つため、
   「形状を持つ全Body」を素の既定にすると同じソリッドが二重に書き出される — この理由で
   **kindによる絞り込みを先に行う**。
2. 候補のうち**可視（`visible=True`）のものだけ**を書き出す。複数あれば1つのcompoundへまとめる。

候補が最初から無ければ「書き出す形状が無い」の `ValueError`（**ファイルを作らない**）、候補は
あるが全部非表示なら別の `ValueError`（`set_visible(visible=true)` で戻す手順つき）。
**この2段の順序（kindで絞ってから可視性を見る）を逆にしてはいけない** —
編集用ソリッドを全部隠すと参照ボディが代わりに書き出されてしまう。

**失敗を黙って通さない**（3つとも `doc.bodies` / `doc.log.entries` に変更を残さない）:

| 失敗 | 契約 |
|---|---|
| パスが存在しない | `FileNotFoundError` |
| 存在するがSTEPとして壊れている | `ValueError` |
| 書き出す形状が無い（対象なし、または対象が全部非表示） | `ValueError`。ファイルを作らない |

**実測済みの危険な落とし穴**: OCPの `STEPControl_Reader` は、`ReadFile()` が
`IFSelect_RetDone` 以外を返した（＝読み込み失敗）状態のまま `TransferRoots()` を呼ぶと、
Python例外にはならず**プロセスごと落ちる**（Windowsで終了コード `0xC0000005` アクセス違反。
存在しないパス・壊れた中身のどちらでも起きる）。`kernel.read_step` は**必ず `ReadFile()` の
戻り値を先に検査**し、失敗なら `TransferRoots()`/`OneShape()` を呼ぶ前に例外を送出する
（`Path.exists()` のチェックだけでは壊れたファイルを防げない）。この安全弁は `kernel.read_step`
1本に集約してあり、ログ埋め込み本文からの復元（`kernel.read_step_text`）も一時ファイルへ
書いてから同じ関数を通る。

`deviation` は**絶対の線形偏差(mm)**として `BRepMesh_IncrementalMesh` へ
**`isRelative=False`** で渡す（`isRelative=True` にすると deviation を1/20にしてもSTLが
1バイトも変わらない — build123d の既定の `export_stl` はこれを使っているので、そのまま
流用すると単調性の契約が必ず落ちる）。**STLサイズの絶対値は縛らず、単調性
（deviationが小さいほど大きい）だけを契約にする**。

STEP往復の体積は**bit-exactではない**（浮動小数点の積分順序に由来する1ULP級の丸め、
実測相対誤差 約8e-16）。比較は `math.isclose(rel_tol=1e-6)` — 実測誤差の約10億倍の
マージンで、面の欠落・重複など実際のジオメトリ破損は確実に検出しつつIEEE演算の丸めノイズでは
落ちない（`rel_tol` をこれ以上詰めない理由: `BRepGProp.VolumeProperties_s` に適応積分の
`Eps` を渡す実装にすると誤差が跳ね上がるため、`volume()` の実装の選択に契約が依存してしまう）。
面数は往復後も**厳密一致（`==`）**でよい。

## `duocad/query.py`

build123dをimportしてよいのはこのモジュールだけ。トポロジの走査は build123d に任せるが、
**OCCTの値そのものが契約になる2つ**（体積・全周円筒の判定）は `kernel.py` へ委譲する。

```python
describe_shape(shape) -> {"face_count", "edge_count", "faces_by_type": {...}}
volume(shape) -> float                       # mm^3。BRepGProp.VolumeProperties_s（Epsなし）
describe_edges(shape) -> [{"index","kind","length","start","end"}, ...]
describe_faces(shape) -> [{"index","kind","area","normal","center"}, ...]
describe_vertices(shape) -> [{"index","point"}, ...]
edges_of_face(shape, face_index) -> [int, ...]     # その面の**外周**の辺の添字（昇順）
```

**契約**:

- `faces_by_type` の全キーの合計は常に `face_count` と一致する。全周円筒の判定は
  `BRepAdaptor_Surface` のU範囲(度)が **350度を超えるかどうか**（build123dの `geom_type` は
  円筒を全周/部分に分けない）。`edge_count` は重複を除いた辺の数。
- **添字と種別は必ず `kernel` 側（`face_of`/`edge_of`/`classify_face`/`classify_edge`）から
  取り、長さ・端点・法線・面積だけを build123d で測る。** build123dの `.faces()`/`.edges()`
  の列挙順が `kernel` の添字体系（`_edge_map` 等）と一致する保証は無く、ずれると「選んだ辺と
  違う辺が丸まる」という例外の出ない静かな事故になる。`describe_vertices` も同じ理由で
  座標と順序を `kernel.vertex_positions` から取る。
- `describe_faces` の `normal` は**外向き**で、`kernel.tessellate` の `faces[].normal` と
  同じ約束（2つは別コードパスなので、どちらかだけ壊れると述語だけが静かにずれる —
  両者が同じ面を指すことをテストが突き合わせて検査する）。曲面の `normal`/`center` は
  UV範囲の中点での値でしかなく、曲面を厳密に選ぶ用途には使わない。
- `edges_of_face` が返すのは**外周（outer wire）だけ**で、面に空いた穴の縁は含めない
  （外周を丸めたつもりの操作が黙って穴まで変えないため。穴の縁を丸めるとトーラスになる）。

## `duocad/kernel.py`

OCPを直接叩いてよいのはこのモジュールだけ。`io_step.py`/`query.py` の各関数は最終的に
ここへ委譲する。

```python
read_step(path: str) -> TopoDS_Shape
read_step_text(text: str) -> TopoDS_Shape        # ログ埋め込み本文からの復元（一時ファイル経由）
write_step(shape, path: str) -> None
write_stl(shape, path: str, deviation: float) -> None
bounding_box(shape) -> {"min","max","center","size","diagonal"}
tessellate(shape, deviation: float) -> {...}
```

- `read_step`/`read_step_text` は上記「実測済みの危険な落とし穴」の安全弁を持つ唯一の場所。
- `write_stl` の `deviation` は第2引数（絶対mm）として渡し、第3引数 `isRelative` は必ず
  `False`。
- `bounding_box` は**幾何から**求める（`useTriangulation=False`）。既定の `True` だと
  既存の三角形分割を使うため、`tessellate` を呼んだ後だけ違う値が返るという再現性の無い
  挙動になる。`SetGap(0.0)` を入れてもOCCTは内部許容差ぶん（実測1e-7mm）膨らませるので、
  厳密な寸法の根拠には使わない。

## `duocad/sketch.py`

```python
BASE_PLANES = ("XY", "XZ", "YZ")
ENTITY_KINDS = ("line", "circle", "rectangle", "arc")   # _ENTITY_KIND_TABLE のキーが唯一の正

base_plane(name) -> dict                # {"kind":"base","name":..,"provenance":None}
face_plane(body_id, face_index) -> dict # {"kind":"face","body":..,"index":..,"provenance":None}
get_sketch(doc, sketch_id) -> Sketch    # 無ければ KeyError

sketch_create(doc, plane) -> SketchId
add_line(doc, sketch_id, p0, p1) -> EntityId
add_circle(doc, sketch_id, center, radius) -> EntityId
add_rectangle(doc, sketch_id, corner, width, height) -> EntityId
add_arc(doc, sketch_id, center, radius, start_deg, end_deg) -> EntityId
entity_params(doc, sketch_id, entity_id) -> dict[str, float]   # 解決済みの数値
```

**契約**:

- `plane` は `"XY"|"XZ"|"YZ"` か `face_plane(...)`。**平面でない面（円筒面など）は
  `ValueError`** — 黙って通すとその上の領域検出と押し出しが意味を失ったまま成功する。
- 座標はスケッチ平面上の2D。`Entity.params` のキーは図形ごとに固定
  （`line: x0,y0,x1,y1` / `circle: cx,cy,radius` / `rectangle: x,y,width,height` /
  `arc: cx,cy,radius,start_deg,end_deg`。度・**反時計回り**・X軸から）。
- **数値は図形の座標に焼き込まない**。`add_circle` はスカラーごとに `duocad/parameters.py`
  へ名前つきの値として登録し、`Entity.params` にはその**名前（str）**を入れる。
  `entity_params()` は解決済みの数値を返し続ける — これがM3以降も変わらないアクセサで、
  座標がリテラルから参照へ変わっても呼び出し側の契約は壊れない。
- 種別ごとの純幾何演算（検査・平行移動・回転・中心）は `_ENTITY_KIND_TABLE`
  （`entity_kind_ops(kind)`）が唯一の正。**表に無い種別は即時 `ValueError`**（silent default
  は無い）。反復（後述）の展開・回転はこの表を通して行われる。
- 円弧は**中心・半径・角度**で持つ（3点で受け取らない — 半径・角度を後から
  `set_parameter` で編集できる形にするため）。**0度またぎ**（例: `start_deg=350,
  end_deg=10`）は20度の円弧として扱う（反時計回りの `sweep = (end - start) % 360`）。
- 操作ログの `args`: `sketch_create` → `{"sketch_id":.., "plane": <dict>}`、図形追加 →
  `{"sketch_id":.., "entity_id":.., "kind":.., "params": {...}}`（**作ったIDを記録する** —
  記録が無いと再生で別のIDになり、保存した選択集合が指す先を失う）。
- 存在しないスケッチIDへの操作は `KeyError`。

### 反復（`Sketch.patterns` / `Pattern`）

```python
Pattern(kind: str = "rectangular")   # "rectangular" | "circular"
```

- **反復は1つのverb・ログ1行**であり、nx×ny（または矩形/円形の個数ぶん）個の図形へ展開しない
  — 展開すると個数も間隔も後から変えられなくなる。スケッチが持つのは `Pattern` 1個で、
  複製された図形は**幾何を作るときの導出物**（`region._expanded_entities`）。
- **1つの図形を2つの反復で使い回すことはできない**（`ValueError`）。
- 回転した長方形は展開時に4本の線分へ落とす（`Entity` の長方形は軸平行の
  `x,y,width,height` しか持てないため。展開結果は幾何を作るための導出物なので元と同じ
  種別である必要はない）。

## `duocad/region.py`

```python
Region(index: int, area: float)                      # frozen dataclass
RegionRef(sketch: SketchId, index: int, provenance=None)
region_ref(sketch_id, index) -> RegionRef
regions(doc, sketch_id) -> list[Region]
```

**契約**:

- `index` は**1始まりの連番**（面の添字と同じ流儀）。`area` はmm²。
- **列挙順は安定**（同じスケッチを2回評価して同じ順序・同じ面積）。選択集合は領域を添字で
  指すので、順序が揺れると保存した選択が別の領域を指す。
- 図形が無いスケッチは**空リスト**（例外にしない）。存在しないスケッチIDは `KeyError`。
- 実装は `BRepAlgoAPI_Splitter`。`RegionRef` は由来スロット（`provenance`）を持つが、
  **領域の取り違え（添字は残るが指す実体が入れ替わる）を検出する用途にはまだ使われていない**
  （下記「既知の制限」）。
- `Sketch` 自体は `regions` フィールドを持たない — 領域は導出物であり、アクセサ
  （`region.regions()`）越しにしか読めない。導出物をデータモデルに持たせると「更新されて
  いないキャッシュ」が正のように見える瞬間ができるため。

### 既知の制限 — 領域の取り違えは検出しない

`set_parameter` によるパラメータ編集で領域の面積順が入れ替わると、添字は残ったまま
**指している領域が別物にすり替わる**ことがある。押し出しの対象領域が**失われた**（添字が
消えた）場合は再生時に `ValueError` で止まるが、**入れ替わった**場合は静かに新しい方へ
適用される。線を動かすなど正当な編集で面積順は普通に入れ替わるため、これを一律に失敗へ
すると「値を編集したら形が追随する」という目的そのものを潰す。解くには `RegionRef.provenance`
を実際に埋める必要があり、未着手のまま残っている。

## `duocad/verbs/extrude.py`

```python
extrude(doc, regions, distance, mode="join", body_id=None) -> Operation
MODES = ("join", "cut", "new")
```

**契約**:

- `regions` は `RegionRef` のリスト。**空リストは `ValueError`**、**存在しない添字も
  `ValueError`**（領域は計算されるものなので、指した先が消えているのは普通に起きる）。
- `distance` はスケッチ平面の法線方向への押し出し量。**0は `ValueError`**。未知の `mode`
  も `ValueError`。
- `mode="join"` … 対象ボディへ足す。**対象が無ければ新しいソリッドを1つ作りアクティブに
  する**（最初の板はこれで生まれる）。
- `mode="cut"` … 対象から引く。**対象が無ければ `ValueError`**。
- `mode="new"` … **対象があっても融合せず、常に新しいソリッドを1つ作る**。`join` の
  「行き先が無ければ作る」という落ち方を明示的に選べるようにしたもの — これが無いと
  既存のボディがいる文書では2つ目の板を作る経路が無かった。**`body_id` を渡すと
  `ValueError`**（黙って無視すると「そのボディへ足したつもりが別ボディになっていた」が
  形が増えるぶん失敗に見えないまま通る）。再生でも既に同じIDのボディがいたら `ValueError`
  （黙って融合・上書きしない）。
- `body_id` の既定はアクティブボディ。編集不可なkind（現状は存在しないが将来の安全弁として
  ガードは残してある）は `ValueError`。
- ログの `args` は `{"regions":[<dict>...], "distance":.., "mode":.., "body_id":..}`。`join`
  が新しくボディを作った場合もそのIDを `args["body_id"]` に記録する。
- 失敗時は document・ログのどちらにも変更を残さない（幾何計算を `append` より先に済ませる）。

## `duocad/parameters.py`

```python
register_value(doc, value) -> str            # 名前つきの値を登録し名前を返す
resolve(doc, ref) -> float                   # パラメータ名なら現在値、素の数値ならそのまま
parameter_of(doc, sketch_id, owner_id, key) -> str
set_parameter(doc, name, value) -> Operation
rebuild(doc, upto_seq) -> None               # seq<=upto_seq のログから文書を作り直す
is_positive_int(value) -> bool
```

**契約**:

- **名前は `d1` `d2` …**（Fusion360と同じ流儀）。使われていない最小の正整数を使う。undoで
  消えた名前は再利用される。**決定的であること**が要件 — 再生で名前の振られ方が揺れると、
  `set_parameter` が記録した名前が別の値を指す。改名機能は範囲外。
- **操作ログの `args` には打った数値がそのまま入る**（パラメータ名ではない）。ログが唯一の
  正でパラメータはそこから再生されるものだから、この形が正しい。
- `set_parameter`:
  - 存在しない名前は **`KeyError`**、文書にもログにも変更を残さない。
  - `args={"name":.., "value":.., "previous":..}`。`previous` はundoが値を戻すため。
  - **形状を追随させる**（値を変えたらログを再生してbodiesを作り直す）。
  - **再生で押し出しの対象領域が失われたら `ValueError`。文書は変更前のまま戻す**（壊れた
    モデルを静かに残さない）。
  - undoは `previous` に戻して同じく作り直す。
- `rebuild` はログを2回走査する（`set_parameter` はログ上で影響する図形・押し出しより
  **後ろ**に載るため、順番どおり再生すると古い値で作った形状ができる — 先に範囲内の
  `set_parameter` を走査して最終値の表を作り、各操作の直後に当てる）。**失敗したら文書を
  元へ戻す**。**アクティブボディの選択は保つ**（選択はログに載らないため）。
- **`set_parameter` を含むログは `document.log` へ束ねてから `replay` すること**。外の
  ログを直接 `replay(document)` すると、作り直しがどのログを見るべきか決められないので
  **例外で止まる**（`doc = Document(); doc.log = OpLog.from_json(...); doc.log.replay(doc)`
  が正しい使い方）。
- `is_positive_int` の検査は**作成時だけでなく `set_parameter` 経由の書き込みにも効く**
  （反復の `nx`/`ny`/`count`、円形反復の `total_angle` を含む）。作成時だけ検査していると
  後から書き込む経路が素通りしてしまう。

## `duocad/verbs/pattern.py` — 矩形反復・円形反復

```python
pattern_rectangular(doc, sketch_id, entities, dx, dy, nx, ny) -> PatternId
pattern_circular(doc, sketch_id, entities, center, count,
                 total_angle=360.0, rotate_copies=True) -> PatternId
```

**共通の契約**:

- `nx`/`ny`/`count` は**元を含めた個数**（矩形反復の格子は(0,0)を含む）。したがって
  **反復に参照されている図形は単独では描かない**（描くと(0,0)の辺が二重になる）。
  個数・間隔・角度は名前つきの値になり、後から `set_parameter` で編集できる。
- 失敗（個数が1以上の整数でない・`entities` が空・別スケッチの図形・同じ図形を二重に
  反復）は例外で、文書にもログにも残さない。
- **押し出し済みの選択は個数の変更に追随しない**（既知の制限）。選択は添字の集合として
  記録されるため、個数を減らして記録済みの選択が指す先が消える場合は再生時に `ValueError`
  で止まる（`extrude` と同じ「領域が消えたら黙って通さない」契約）。選択を「このスケッチの
  全領域」のような述語で書ける経路はまだ無く、個数を変えたら押し出し直すのが現在の手順。

**矩形反復固有**: `args={"sketch_id":.., "pattern_id":.., "entities":[..], "dx":.., "dy":..,
"nx":.., "ny":..}`。

**円形反復固有**:

- `rotate_copies`（真偽）は `params` ではなく `Pattern.options` に入る — `params` は
  `set_parameter` で編集できる**寸法値**の置き場であり、真偽値を混ぜると `0.5` を書き込める
  余地ができてしまう。
- 刻み角: **全周（`total_angle == 360`、厳密等価判定）は `360 / count`**、**全周未満は
  端点を含むので `total_angle / (count - 1)`**、`count == 1` は複製が無いので刻みを使わない
  （0除算しない）。全周を`count-1`で割ると最後の複製が最初と重なり穴が1つ減り、逆に全周未満を
  `count`で割ると最後の複製が指定角度に届かない。
- `rotate_copies=False` は図形の中心だけを回してその差分を平行移動する（向きは元のまま）。
  `arc` は `rotate_copies=True` のとき `start_deg`/`end_deg` にも角度を足す（位置だけ回すと
  円弧の向きが揃ってしまう）。
- **回転中心と図形の中心が一致すると `ValueError`**（全複製が完全に重なり「反復したのに
  何も増えない」が成功として通ってしまうため）。
- `total_angle` は **`0 < angle <= 360`**（360超・0・NaN・負は `ValueError`）。

## `duocad/verbs/fillet.py` / `duocad/verbs/chamfer.py`

```python
fillet(doc, edges, radius, body_id=None) -> Operation
chamfer(doc, edges, distance, body_id=None) -> Operation
```

- `edges` は**1始まりの辺の添字**のリスト（面の添字と同じ流儀、順序は選択順であって
  昇順ではない）。`radius`/`distance` はfloat（全辺同じ）か辺ごとのリスト（長さが合わなければ
  `ValueError`）。範囲外の添字・参照ボディ・0本の辺・未知のBodyはすべて `ValueError`
  （`extrude` 系と揃えた型）。ログに `args={"edges":[...], "radius"|"distance":..,
  "body_id":..}` を積む。
- **両方とも同じ実装**（`kernel._blend_edges`）に乗る — `BRepFilletAPI_MakeFillet` と
  `BRepFilletAPI_MakeChamfer` は同じ系統で、失敗の仕方も由来の取り出し方も同じだから。
  由来は `normalize_provenance` の戻りから射影を作る（フィレット系は `History()` を
  持たないため、`maker.Modified()` を直接叩き直すとその差を吸収するアダプタを迂回することに
  なる）。`ShapeUpgrade_UnifySameDomain` は通さない（押し出し専用の処置。フィレットの結果を
  統合すると丸めた面が潰れる）。
- **OCCTは失敗を例外ではなく `IsDone()=False` で返す。半径に対して単調でも一様でもない**
  （既に readme.md §6 が要約しているが、ここでは**どの関門で捕まるかも半径で変わる**という
  詳細まで固定する: 20mm立方体・全辺フィレットで r=3.0 は成功、**r=10.0 は `IsDone()=False`
  で落ちる**、**r=10.5 は `IsDone()` は真だが `BRepCheck.IsValid` が偽**（＝壊れた形状を
  作ってしまうので文書に入れない）、r=30.0 は `IsDone()=False`。範囲検査での代用はできず、
  毎回すべての関門（`IsDone` / 空でない / 体積>0 / `BRepCheck`）を通す必要がある。
- **面の添字は形状変更で壊れる**（同じ面がフィレットで例えば i=5→9 のように動く）。
  生の添字を保存・再利用しないこと（readme.md §6と同じ地雷。ここではfillet/chamferで
  実際に起きることを明記する）。
- `query.describe_edges` / `edges_of_face` から得た辺だけをverbへ渡す（build123dの列挙順を
  信用しない、上記query.py節と同じ理由）。

### 円弧（`duocad/sketch.py` の `add_arc` と合わせて使う面の種別）

まっすぐな辺の面取りは平面を1枚増やすだけ（**円錐にはならない**）。円錐は**円い辺**の面取り
から生まれる。円い辺のフィレットは球ではなく**トーラス**になる（半径が全周円筒面の半径以下の
場合）。フィレット・面取りを同じボディへ順にかけられる（先に面取りで辺の添字が変わるので、
後段のフィレットは選び直した添字を使う）。

## `duocad/selection.py` — 選択集合（可視性を含む）

選択集合はコアの第一級オブジェクトで、交互操作（AIが提案 → 人が過不足を直す → AIが適用）が
満たすべき5条件（永続／双方向／種別を保持／差分編集可能／出所を区別）をすべて満たす。

```python
Selection（dataclass）
  items  : [ShapeRef | RegionRef]      追加順を保つ
  origin : "user" | "proposed"         集合単位（項目ごとではない）
  to_dict() -> {"items","origin","count"}
  is_proposed() -> bool

get_selection(doc)                       -> Selection   読むだけ（発火しない）
propose_selection(doc, refs=|predicate=) -> Selection   置き換え・origin="proposed"
modify_selection(doc, add=(), remove=()) -> Selection   差分編集・origin="user"
commit_selection(doc)                    -> Selection   中身は変えず origin="user"
clear_selection(doc)                     -> Selection   空に（origin="user"）
confirmed_items(doc, kind=None)          -> [ref]       proposed なら SelectionNotCommitted
edge_indices(doc, body_id=None)          -> [int]       fillet/chamfer へそのまま渡せる形
require_confirmed(doc, kind, exactly_one=False) -> [ref]
require_edges(doc)                       -> ([int], BodyId)   添字とBodyをまとめて取る
select_face / select_edge / select_vertex / select_region -> ref
ref_kind(ref) -> "face"|"edge"|"vertex"|"region"
same_ref(a, b) -> bool

set_visible(doc, body_id, visible) -> dict
selectable_bodies(doc) -> list[BodyId]     # visible なBodyだけ・bodies順
select_face(doc, body_id, face_index) -> dict
```

`ShapeRef` は `{"body","kind","index","provenance"}`、`RegionRef` は
`{"sketch","index","provenance"}`（**`kind` キーを持たない**。種別は `"sketch"` キーの
有無から `ref_kind` が導く）。添字は `kernel` の数え方（1始まり）に揃える。

**遷移規則 — 人が触ること自体が承認**:

| API | 呼ぶ主体 | 遷移後の `origin` |
|---|---|---|
| `propose_selection` | AI | `"proposed"` |
| `modify_selection` | 人（差分編集） | `"user"` |
| `commit_selection` | 人（中身を変えずに承認）／AI（自分の提案を実行する前の1手） | `"user"` |
| `clear_selection` | どちらでも | `"user"`（空集合の origin は常に user） |

`modify_selection` を "proposed" のままにしないのは、人が中身を見て直したのにもう1回
「承認」を要求すると往復に無意味な1手が増えるため。

**例外の語彙**（すべて `ValueError` の派生。名前が実際の状態を正確に言う）:

| 例外 | いつ | MCPのコード |
|---|---|---|
| `SelectionNotCommitted` | 提案（`origin=="proposed"`）のまま `confirmed_items` を呼んだ | `selection_not_committed` |
| `SelectionEmpty` | 確定選択が**丸ごと空** | `selection_empty` |
| `SelectionKindMismatch`（`SelectionEmpty`の派生） | 空ではないが要求した種別が無い（例: fillet呼び出しだが確定が面3枚） | `selection_kind_mismatch` |
| `SelectionAmbiguous` | `exactly_one=True` で2件以上確定している | （黙って先頭を選ばない） |

`SelectionKindMismatch` を `SelectionEmpty` の派生にしてあるのは、`require_*` を型で
捕まえている既存の経路をそのまま生かすため。**この2つを1つのコードに潰してはいけない**
（「選択が空」と「面3枚が確定しているが辺が要る」は名前が違う話で、`present_kinds`
（実際に確定している種別と件数）を返せるのは分けてあるおかげ）。

**`require_confirmed`/`require_edges` が「選択 → verbの対象集合」の唯一の入口**。MCPも
WebSocketも、面・辺・領域を引数で受ける口を一つも持たない — `Session` のverbラッパが
この2つを呼ぶので、AIの経路も人の経路も同じ規則を通る。`require_edges` は**添字とBodyを
まとめて取る**ため、呼び出し側が片方だけ渡し忘れる形が構造的に存在しない（fillet/chamferが
選択の指すBodyと違うBodyを丸めていた実装バグへの直接の対処）。

**拒否はコアがする。MCPは名前を付けるだけ** — `selection_not_committed` を返すために
`origin` を先に見て弾くMCP層の実装は不可。拒否できるのは `confirmed_items` だけであり、
MCPは落ちたあとに `isinstance` で判定してコード名を付ける。同じ規則が2箇所に生まれると
片方だけ緩んだときに誰も気づけない。

**0件一致と不正な述語・不正な参照は別物**:

- **0件一致はエラーではない**（空集合を返す。件数は `to_dict()["count"]`）。
- **不正な述語・不正な参照はValueErrorで、選択を変更しない**。実装は「全条件を検査してから
  評価する」順序でこれを守る（途中まで評価して落ちる作りだと、選択の巻き戻しが実装の
  偶然になる）。
- `body` を明示して**不可視Body**を指した場合も同じくエラー（「指定したのに0件」と
  「見えないので対象外」はAIから区別が付かない）。

**参照の検査は `select_*` と揃える**: 形が不正（必須キー欠落・未知の`kind`・範囲外の添字）
なら `ValueError`、存在しないBodyなら `KeyError`。`propose_selection` と `modify_selection`
の両方に同じ契約がかかる。**既に入っている参照を再度addしても位置は動かさない**（末尾へ
移し替えない — `fillet` の辺リストと半径リストの対応がずれないように）。

**領域も選択集合の第一級の項目**。`select_region` は `duocad.region` を使い回す（参照は
`region.region_ref(...).to_dict()` が作り、範囲は `region.regions(doc, sketch_id)` を
**その場で数え直して**検査する — 領域は計算されるものなので、図形を1本足しただけで
個数が変わる）。存在しないスケッチは `KeyError`、範囲外の添字は `ValueError`。

### 可視性 — 選択機構の一部（verbではない）

```python
set_visible(doc, body_id, visible) -> dict
selectable_bodies(doc) -> list[BodyId]
select_face(doc, body_id, face_index) -> dict
```

**可視性は選択機構の一部**（「何が選べるか」の定義そのもの）であり、コアのverbでは**ない**
— 操作ログに載らず、**undo/redoでは戻らない**。存在しない `body_id` は `KeyError`、
document・ログのどちらにも変更を残さない。

- `select_face` の失敗: 存在しないBody→`KeyError`、`visible=False`→`ValueError`、
  1..`face_count`の外→`ValueError`。
- **不可視Bodyは押し出せない**（`extrude_face`等が拒否。`ValueError`・文書は不変） — 見えない
  ものは指せないがアプリ層だけの約束だと、MCPや再生経路から画面に出ていない部品を黙って
  変形できてしまう（もとは実機での高いピック失敗率への直接の対処）。
- **選択の変更・可視性の変更はログに積まないが、イベントは発火する**（`Session._emit` →
  `add_listener` の購読者 → 全接続へ配信。`set_visible` と選択操作の両方が同じ経路に乗る）。
  選択をログに載せない理由はundoの単位（undo1回で消える単位）を汚さないため。

### 形を変えるverbの確定後は選択を全リセットする

**添字は形状変更で壊れる**（同じ面がフィレットで別の添字を指すようになる等）という問題への
答えは「後継を追跡する」ではなく「**確定後は選択を空にする**」。`duocad/catalog.py` の
`Command.resets_selection` が対象verbの単一の宣言で、`Session._reset_selection_if_needed`が
確定後に1箇所で発火する。

- 対象: `extrude`/`extrude_face`/`fillet`/`chamfer`/`pattern_rectangular`/
  `pattern_circular`/`add_line`/`add_circle`/`add_rectangle`/`add_arc`/`import_svg`/
  `set_parameter`/`undo`/`redo`（undo/redoは「何を戻したか」に関わらず**常に**リセットする
  単純な規則。`duocad/catalog.py` の `resets_selection=True` が付くverbがこの全量）。
- リセットしない: 形を変えない操作（`set_visible`・選択操作そのもの・`commit_selection`・
  保存/書き出し・`describe_*`）と、**失敗したverb**（失敗する半径では理由が画面に出て、
  選択は保持されたまま戻る）。
- `Session` はWebSocketとMCPのどちらも同じインスタンスを叩く唯一の適用パイプラインなので、
  ここに置けばUI経由・MCP経由のどちらでも均等に効く。
- プレビュー（`Session.preview` → `speculative()`）の中では見かけ上効かない — 投機実行は
  `doc.selection` の巻き戻し対象に選択そのものを含んでおり、`with` を抜けると選択は元へ
  戻る（イベントも`_suppress_events`で配らないので、クライアントには一切見えない）。
  **確定（Enter）した本物の呼び出しだけが実際にリセットする。**

## `duocad/predicates.py` — 述語による選択

述語は**構造化JSON**（DSL文字列は採らない — MCPのツールがスキーマを持てるので書式を外した
瞬間に落ちる。文字列DSLだと「パースに成功して意味が違う」＝黙って別の辺を丸めるという
最悪の失敗を作りうる）:

```json
{"kind": "edge",
 "body": "part-3",
 "where": [{"op": "parallel_to", "axis": "Z"},
           {"op": "length_gt", "value": 20}]}
```

`where` は**AND**。空リストはその種別の全部。**ORは持たない**（論理演算子を足すとパーサを
書き始めることになる。ORが要るときは2回評価して `modify_selection(add=...)` で足す）。
`body` は省略時**可視Body全部**が対象。`face`/添字は**Body局所の1始まり添字**なので、
可視Bodyが複数あるときは `body` の指定が要る（無ければ `ValueError`。「全Bodyの3番目の面」
という解釈は取らない）。

使えるop（`predicates.describe_ops()` が唯一の正。MCPのスキーマもここから作る）:

| kind | op | 引数 |
|---|---|---|
| face | `normal_dot_gt` | `axis`（"X"/"Y"/"Z" または3要素）, `value` |
| face | `area_gt` | `value` |
| face | `kind_is` | `value`（`kernel.classify_face` の種別） |
| edge | `parallel_to` | `axis` |
| edge | `length_gt` / `length_lt` | `value`（**厳密な不等号**。境界を含まない） |
| edge | `kind_is` | `value` |
| edge | `on_boundary_of_face` | `face`（面の1始まり添字）— 関係述語 |

`on_boundary_of_face` が返すのは**外周（outer wire）だけ**（`query.edges_of_face` と同じ
契約）。**領域の述語は無い**（`predicates` の `kind` は face/edge だけ。`select_region` で
参照は作れるが述語では選べない）。

**評価の順序**: 「全条件を検査してから評価する」— 不正な述語は選択を変更しない、という
契約を守るための実装上の要件（`selection.py` 節と同じ規律）。**不可視Bodyは述語のスキャン
対象に含まれない**。

## `Session`（`duocad/session.py`、配りものは `duocad/view.py`）— 状態配信・差分・プレビュー

`Session` は文書1つを持つサーバ側の状態で、WebSocket（`duocad/net/ws.py`）とMCP
（`duocad/mcp_server.py`）の**どちらも同じインスタンスを叩く唯一の適用パイプライン**。
モジュールの役割分担は readme.md「モジュールとimport境界」が正。ここに書くのはテストが
固定している契約。

```python
Session.state() -> {
    "units", "active_body_id", "selection",
    "bodies": [{"body_id","name","kind","visible","revision",
                "face_count","volume","geometry": <tessellate結果 | None>}, ...],
    "sketches": [...], "parameters": {...},
    "log", "can_undo", "can_redo", "applied_count", "redo_count",
}
Session.body_revision(body_id) -> int      # 形状の差し替え**と可視性の変更**で増える
Session.geometry(body_id) -> dict          # キャッシュ付き。**同じオブジェクトを返す**（不可視は ValueError）
Session.delta(known_revisions) -> {"bodies": [<版が変わった分だけ>], "unchanged": [BodyId, ...]}
Session.preview(verb, args) -> dict        # 投機的実行（後述）
```

**契約**:

- **同時に見える（`visible` かつ `geometry` を持つ）Bodyは1ソリッドにつき1つ**。参照ボディと
  編集用コピーの両方を可視のまま出すと、同じ形が二重に測られ二重に送られ、手前を消しても
  参照側のメッシュが残ってピック失敗が再発する。
- **不可視のBodyは測らない**。`state()`/`delta()` は `geometry: None` を出し、`geometry()`
  は不可視Bodyに対して `ValueError`。
- **版（`body_revision`）は形状の差し替えと可視性の変更のどちらでも増える**（形状だけの版に
  すると `set_visible` した結果が `delta()` の `unchanged` に埋もれ、クライアントへ一度も
  届かなくなる）。undo/redoで形状や可視性が戻った場合も同じ経路で再送される。
- **テセレーションの再利用は版ではなく「形状オブジェクトの同一性」で決まる**。`geometry()`は
  形状が変わらない限り**前回と同じオブジェクト**を返す（可視性を切り替えただけでは測り
  直さない）。版は「経路ではなく状態（形状オブジェクトの同一性, visible の組）」から導く —
  verbごとに「これはbodyを変える」と数え上げる実装だと、verbが増えたときに静かに漏れる
  （漏れてもpytestは通り、ブラウザだけが古いままになる）。
- `delta()` に専用の `removed` は無い — `bodies` と `unchanged` の和集合が現在の全Body
  なので、クライアントが知っているのにどちらにも無いIDは文書から消えたことを意味する。

### 差分送信の上り方向（コアが発火し、アプリ層が購読する）

`Session.add_listener(callback)` / `Server.bind_loop(loop)` を軸に、MCPやWebSocketが文書を
変えると `Session` が発火し、購読者（`Server`）が接続している全ブラウザへ配信する。これが
無いと交互操作は原理的に成立しない。契約は3つ:

1. 1つの操作につき購読者へ**1回だけ**届く。
2. WebSocket経由の操作は `_dispatch` が自分で配信するので、購読者側は配らない（二重配信すると
   `applied` の付いた配信と付かない配信が交錯して画面側の相関が壊れる）。
3. **MCPからの変更に `applied`（操作元への確認応答）を付けない** — ブラウザは
   `applied.client_op` を自分の計測と突き合わせるので、他人の操作に付けると知らない
   `client_op` を自分のものとして拾う経路ができる。

購読者が例外を投げても**操作は成功のまま**にする（確定した文書の変更を通知の失敗で巻き戻す
ことはできない）。選択の変更・可視性の変更も同じ上り方向に乗る（イベントは `applied` とは
**キーで区別**する。`{"change":"selection", "selection":{...}, "client_op":...}` の形）。

### `Session.preview` — 投機的実行（`duocad/speculative.py`）

`PREVIEWABLE_VERBS` に登録されたverb（`extrude`/`extrude_face`/`fillet`/`chamfer`）だけが
対象。**反復は対象外** — 変わるのはスケッチの図形でソリッドではないので、応答（形が変わった
Bodyの一覧）が常に空になるため。

1. **形を返す** — 変わった可視Bodyだけ。本物を実行したときの体積と一致する。
2. **何も残さない** — 操作ログ・カーソル・形状・体積・選択・`applied_count`・テセレーション
   のキャッシュ。連続で何度呼んでも同じ結果。
3. **何も発火しない** — 経路によらず（MCPから直接呼んでも中間状態は配信されない）。
4. **失敗しても何も残さない**（例: 距離0で落としても文書は同一）。
5. **登録制**（`PREVIEWABLE_VERBS`）— ソケットから届いた文字列で `Session` の任意の
   メソッドを呼べる作りにしない。
6. `preview` は**状態を配信しない**（要求した接続にだけ返る）。`_apply` で囲まない —
   囲むと、押し出し中にMCPから届いた**本物の**変更まで「WebSocket経由だから配信不要」と
   誤判定して捨ててしまう。

### ファイル操作

`import_step`/`import_svg` は文書を変えるので通常の状態配信（全接続へ）、`save_document`/
`export_step`/`export_stl` は文書を変えないので**要求した接続にだけ**結果（書いた先の
絶対パスとバイト数）を返す。`open_document` は文書を丸ごと差し替えるので全接続へ配る。

- **保存できる先は「いま編集しているファイル」1つだけ**（2026-08-30）。
  `save_document()`（引数なし）は現在のファイルへ上書きし、`save_document(name)` は
  **新しい名前でのみ**保存する（既存名は、それが現在のファイルの名前であっても拒否）。
  現在名は `Session._current_name` が持ち、`open_document` の成功と `save_document(name)` の
  成功で入り、`new_document` で消える。**`state.current_name` として配信にも載る**ので、
  保存は `file` の封筒を返した後に**状態も配り直す**（それだけが変わるため `applied` は付かない）。
  これは「文書を変えないものは要求した接続にだけ返す」の唯一の例外で、理由は
  現在ファイルが**サーバの状態**であって書き出し先の報告ではないこと。
- **パスの解決はサーバだけが行う**（相対はサーバの作業ディレクトリ基準）。書いた先は必ず
  絶対パスで応答に載せる。
- **開くときはログを文書へ束ねてから再生する** — `parameters.rebuild` が `document.log` を
  頭から読むため、`log.replay(doc)` を先に呼ぶと `set_parameter` を含むログは開けない。
- **版（revision）の数え上げ状態は開くたびに捨てる**が、Bodyのidは操作ログの再生で同じ値が
  再現しうるため、**版まで1へ戻してはいけない** — 戻すと、そのidを知っているクライアントが
  「変わっていない」と誤判定して前の文書の形を描き続ける。
- **開くのに失敗しても今の文書は壊れない**（新しい `Document` の上で再生し切ってから
  差し替える）。

### `new_document` — 空の文書へ差し替える

`open_document` と同じ「文書を丸ごと差し替える」経路を使うが、対象が**空の `Document`**で
ある点だけが違う。

- **verbではない** — 操作ログに載らないので**undoでは戻らない**（新しい文書のログは空で、
  取り消せる操作が無い）。捨てた文書を取り戻す手段は「保存してあれば開き直す」だけ。
- 応答は全接続へ配る。`name`/`path` は `null`。
- 選択は `Document` が持つので、差し替えの時点で自動的に空になる。
- **AIには開けていない**（MCPツール一覧に出ない。WebSocketからは踏める）— 今の文書を丸ごと
  捨ててundoでも戻せない操作なので、捨てる判断は人がする。

## `duocad/mcp_server.py` — MCPプロトコルとverb公開の規律

ツール一覧・登録手順・使い方は `MCP.md` が正、AIの確認要否の判定は `MCP.md`「AIはどこまで
自分でやるか」が正。ここに書くのは、**MCPが選択機構・verb層とどう繋がるか**というテストが
固定する実装レベルの規律で、MCP.mdと重複しない部分だけ。

### 形は選択から、数値は引数から（背骨）

**面・辺・領域を引数で受けるツールは1つも無い。** `fillet(radius)` であって
`fillet(edges, radius)` ではない。形は確定した選択（`selection.require_confirmed`/
`require_edges`）から取り、数値・名前・パスだけを引数で渡す。Bodyだけは `body_id` 引数で
受ける（Bodyは選択集合の項目ではないため）。効き目は3つ:

1. 「提案のままではverbに渡せない」が全verbへ自動的にかかる（形を引数で受ける裏口が1つでも
   あると、その経路だけ提案と確定の区別が素通りになる）。
2. 参照の検査をMCP層に二重で書かなくて済む（`selection` 側が既に持っている）。
3. 「述語または明示的な参照で与えられる」が `propose_selection` の1箇所に集まる。

代償はフィレット1回が3呼び出し（提案→確定→適用）になること — これは代償ではなく設計の
主張（交互操作の一巡そのもの）。人が居ない場面でもAIが `commit_selection()` を明示すれば
一息に実行できる。

**拒否はコアがする。MCPは名前を付けるだけ**（`selection.py`節と同じ規律の繰り返しになるが
ここが実際に検査で固定されている場所）— `selection_not_committed` を返すために `origin` を
先に見て弾く実装は不可。拒否できるのは `confirmed_items`/`require_*` だけで、MCPは落ちた
あとに例外の型（`isinstance`）でコード名を付ける。メッセージ文字列に依存すると、コア側の
文言を変えた瞬間に別のコードへ落ちてしまう。

### 成功は封筒に入れて返す

ログに1行積むツールは次を必ず返す: `verb`（undo/redoは`"undo"`/`"redo"`）・`seq`・
`changed_bodies`（この操作で形状か可視性が変わったBodyId）・`bodies`（変更後の姿。キーは
`body_id`/`name`/`kind`/`visible`/`face_count`/`volume` の6つ）・`can_undo`/`can_redo`。
加えて作ったものの**IDを返す**（`sketch_create`→`sketch_id`、`add_*`→`entity_id`、
`pattern_*`→`pattern_id`、`import_step`→`body_ids`）。

**`changed_bodies` は `Session.body_revision` の呼び出し前後の差から導く**（verbごとに
「これはbodyを変える」と数え上げる実装だと増設のたびに静かに漏れる。上の`Session`節と同じ
規律）。`export_step`/`export_stl` はログに載らない（文書を変えないのでverbではない）ので、
代わりに `{"path", "bytes", "body_ids"}` を返す — AIは自分でファイルを確かめられないので
戻り値が唯一の確認手段。

### 失敗も封筒に入れて返す

`{"error": {"code": <7語彙>, "message": ..., "tool": ..., "exception": ...}}` を
`isError: true` で返す。7語彙の内容とMCP側の使い分けは `MCP.md`「実装のメモ」が正
（`not_found`/`invalid_argument`/`selection_not_committed`/`selection_empty`/
`selection_kind_mismatch`/`invalid_predicate`/`internal`）。ここで補うのはテストが固定して
いる2点だけ:

- **`FileNotFoundError` を `not_found` に含めることを忘れない**。パスの間違いはAIが直せる
  誤りなので、`internal` に落とすと直せるものを直さずに諦めさせる。
- コードを分ける基準は**「AIが次に打つ手が変わるか」**。`selection_not_committed` を
  `invalid_argument` に潰すと、AIは半径を変えて再試行しはじめる（正しい次の手は
  `commit_selection`）。

### `describe_predicates` / `measure` / `describe_document`

- `describe_predicates` は `predicates.describe_ops()` を**そのまま**返す（MCP側で書き直すと
  スキーマとバリデータが二重管理になり、片方だけ増えたopが黙って通る）。
- `measure` は verb ではないので**`confirmed_items` を経由しない**（提案のままでも測れる）。
  確定を要求すると、AIは中身を見ずにcommitする動機を持ってしまい提案と確定の区別が弱くなる。
  値は問い合わせ層のものをそのまま渡す（測り直すと述語が使う値と食い違いうる）。**頂点は
  測れない**（`{"measured": false, "reason": ...}` で返す。黙って落とすと「3つ選んだのに
  2つしか返らない」をAIが自分の誤りと解釈する）。
- `describe_document` は各図形について `params`（解決済みの数値）と `parameters`
  （その数値を持つパラメータ名）の**両方**を出す — 数値だけでは `set_parameter` の引数が
  作れず、名前だけでは今の値が分からない。領域の数え上げは計算コストがあるので
  `include_regions` で任意にする。

### ツール名 = verb名の一致規律

**ツール名は操作ログのverb名と同じ綴りにする**（1つのツールを引数で複数のverbに打ち分け
ない）。AIは `describe_document` のログを読んで次の一手を決めるので、ログのverb名と呼べる
ツール名が違うと対応表を推測させることになる。一致は
`test_tool_names_match_the_verb_names_in_the_operation_log` が**両方向**で固定する
（ツール名の集合が登録verbに含まれるだけでなく、登録verbにも対応するツールがあることまで
見る — 片方向の検査だと「ログに出るverb名を呼べるツールが無い」状態が緑のまま通ってしまう）。

### stdoutの汚染対策

STEP書き出し時、OCCTのC++層が標準出力へ統計を吐く。stdioトランスポートは**stdoutを
JSON-RPC専用**にしているため、`export_step` を公開した状態でこれを放置すると接続が壊れる。
Python層の `contextlib.redirect_stdout` では止まらない（C++が実fdへ直接書く）ため、ツール
実行の間だけ **fd 1 を fd 2 へ振り替える**（`os.dup2`）。検査は実プロセスを起こす
`test_a_browser_export_does_not_corrupt_the_stdio_channel` でしかできない（同一プロセス内で
`DocumentTools` を叩くテストでは、汚れるのがテストランナーのstdoutでありJSON-RPCの経路では
ないため原理的に検出できない）。

### 顔ぶれの上限は1箇所

公開ツールの顔ぶれを主張する契約は
`tests/test_mcp_verbs.py` の `EXPECTED_TOOLS` の**一致検査**（部分集合ではない）だけに置く
— 同じ主張を複数箇所（別のテストファイル・実機smokeスクリプト）に置くと、増減のたびに
片方の更新を忘れて必ず食い違う。

## `duocad/catalog.py` とWebSocketコマンド層

`COMMANDS`（`duocad/catalog.py`）が verb のメタ情報（名前・引数・カテゴリ・プレビュー可否・
MCPスキーマ・`resets_selection`）の**唯一の宣言**で、`duocad/net/ws.py` の
`SKETCH_COMMANDS`/`FEATURE_COMMANDS`/`FILE_COMMANDS`（WebSocketの許可リスト。
`duocad/server.py` から再輸出）と `duocad/mcp_server.py` のツール定義はどちらもここからの
導出。verbを1つ足すときにメタ情報を
書くのは宣言1エントリだけでよい（手順は `catalog.py` 冒頭docstringの5ステップ）。
`Command.category` は `query`/`selection`/`sketch`/`feature`/`file`/`history`/`protocol`
の7種、`Param.ws` が個々の引数をWebSocketの表へ載せるかどうかを決める。

**WebSocketの表（`ws=="table"`）に載せた引数は全部必須**（既定値で補うと、打ち忘れた寸法や
`mode` が黙って既定で入る）。省略を許したい引数を持つコマンドは表に載せず `ws="special"`
にする（`sketch_create` の `base_plane`、`open_document` のような応答の形が違うものが該当）。

**対象集合（面・辺・領域・押し出す対象の`body_id`）はWebSocketの引数としても受けない** —
コアの確定選択（`selection.require_*`）から取る。MCPと同じ形になるので「対象集合の指定
方法」はコア全体で1つしかない（`test_ui_command_wiring.py::
test_no_websocket_command_takes_a_target_set`）。Enter要否の判定規則そのものは
`readme.md`§0原則3が正。

### スケッチの配信形（`state.sketches[]`）

```
sketches[] = {
  sketch_id, plane,
  frame: {origin, u, v, normal},                 # p = origin + u*uAxis + v*vAxis
  entities: [{entity_id, kind, params, refs}],    # refs = パラメータ名（打ち直し用）
  drawn:    [{kind, params}],                     # 反復を展開した描画用
  regions:  [{index, area, centroid, positions, indices}],
}
```

- **`entities` と `drawn` を分ける** — 前者は人が値を打ち直す対象、後者は反復を展開した
  「実際に見えるもの」。1つにすると反復した図形が画面に出ないのに領域だけ増える。
- `frame` は**コアが配る**（`kernel.sketch_plane_frame`）— 基準3面の軸の取り方と、面に
  貼ったときの法線反転の扱いをアプリ層に再実装させると必ずずれる。
- 領域はキャッシュする（`BRepAlgoAPI_Splitter`＋テセレーションは重い）。キーは解決済みの
  図形と平面の内容の署名＋貼った面の**形状オブジェクトの同一性**（`is`比較。`id()`の整数を
  控えるとアドレス再利用で偽ヒットする）。

### 辺の配信（`get_edges`）

```
kernel.tessellate_edges(shape, deviation) -> [{index, kind, length, closed, points}]
get_edges -> {"type":"edges", body_id, revision, edges:[...]}
```

- 添字は面の添字と同じ `_edge_map` の1始まり体系（`edge_count`/`edge_of`/`fillet(edges=...)`
  と一致することを2経路の突き合わせで検査する — build123dの列挙順を信用しない、という
  `query.py`節の規律と同じ）。
- 折れ線は `GCPnts_QuasiUniformDeflection` で `deviation`(mm)以内に分割する（直線は2点、円は
  分割する — 弦1本で返すと丸い稜線が直線に見えて狙えない）。**分割に失敗した辺も端点2点で
  返す**（落とすとその辺だけ画面から消え、指せない辺になる）。
- **`get_edges` は `state`/`delta` に常時は乗らない**（`preview`と同じく要求した接続にだけ
  返る）。常時配ると初回配信に折れ線が丸ごと乗り、差分配信で稼いだ帯域を使い切る。
  `revision` を返すのは、クライアントが「今の形の辺か」を判定できるようにするため。

## `duocad/optimize_run.py` — 形状最適化の配線（`test_optimize_backend.py`）

4条件を `duocad.topopt` の `Conditions` へ翻訳し、端から端まで回して結果をメッシュボディとして
足す層。**SIMPを実際に回す検査はここには無い**（`topopt_checks/check_optimize_wiring.py`）。
pytestが固定しているのは以下:

```python
optimize_run.conditions_report(doc) -> dict     # {"ready", "problems", ...}
optimize_run.build_conditions(doc) -> ConditionBundle
optimize_run.resolution_limits(doc, *, rmin_mm=None) -> dict
optimize_run.run(doc, options=None, *, progress=None, should_cancel=None) -> dict
optimize_run.allowable_stress() -> dict
Session.optimize_conditions() / optimize_resolution(rmin_mm) / run_optimization(**opts) / cancel_optimization()
```

- **走らせられる条件は「保持ソリッド1つ以上・拘束面ちょうど1枚・荷重面ちょうど1枚・
  荷重の大きさ > 0」**。2枚以上の条件面は `OptimizeInputError` で断り、文面に理由
  （`Conditions.fixed_face` が面添字を1つしか持たない）を含む。指紋が合わない条件面も断る。
- **メッシュボディは保持・除外に指定できない**（`meshbody.reject` の文言。verbの入口と
  `conditions_report` の2箇所で断る。**`_apply_bodies` には門を置かない** — 過去のログの
  再生を後から足した門で落とさないため）。
- **面添字は `kernel.make_compound` でまとめた形の中の添字**で、ボディごとの `face_count` の
  累積だけずれる。`_verify_face_mapping` が面積・法線・**重心**で対応を測り直し、ずれていれば
  `FaceMappingError`（`RuntimeError` 派生 = `except ValueError` に拾われない）。
- **要素サイズの上限 = min(`rmin_mm` / `simp.MIN_RMIN`, min(sqrt(条件面のCAD面積)))**、
  下限は総要素数 `MAX_ELEMENTS` から。範囲外の指定は `OptimizeInputError`。
- **結果メッシュの頂点は要素添字空間で来る**ので `grid.origin + verts * grid.spacing` で
  ワールド[mm]へ直す。格子の箱から出ていたら `FaceMappingError`。
- **中断**は `should_cancel()` が真なら `OptimizeCancelled`（`RuntimeError` 派生）。
  `Session.run_optimization` は**入口で旗を畳む**（畳まないと前回の中断が次を即死させる）。
- **`import duocad.session` で `skimage` と `duocad.topopt` が来ない**（`duocad.topopt` の
  importは関数の中）。`scipy` は `duocad/view.py` が既に読んでいるので指標にならない。
- `duocad/topopt/safety_loop.py` 側で固定しているもの: 本番解像度で条件面が空なら
  `ConditionFacesEmptyError`（**生の `AttributeError` ではない**）／`run_progress` が
  `run_topopt` の `progress` へ素通しされる／`keep_model=True` が渡る／候補材料量が
  `derive_default(coarse_material_mm3=...)` へ届く。

## 回帰ガード（`test_m2_regressions.py` / `test_m3_regressions.py`）

契約テストとは別に、実測で見つかった壊れ方を再発防止として固定してある2ファイル。現在も
成立している必要がある挙動:

- 既存ソリッドの面に貼ったスケッチの `regions()` は例外にならない。
- 別スケッチの領域を混ぜて押し出しても、体積0の退化ソリッドを黙って作らない。
- NaN等の壊れた数値はパラメータとして永続化されない（正常な領域まで巻き込んで消えない）。
- 参照STEPを読んだ直後でも、最初の板（`join`で対象が無い場合の新規ソリッド）を作れる。
- `set_parameter` によるログの作り直しは、アクティブボディの選択・失敗時の文書状態を壊さない。

## 契約として明示されている範囲外（現在も有効）

- **領域の取り違え検出**（`RegionRef.provenance`を実際に埋めての再識別）は未着手 —
  `region.py`節「既知の制限」。
- **スケッチ図形の削除・移動・拘束**を行うverbは無い（`sketch_create`/`add_*`/
  `set_parameter`のみ）。
- **頂点を引数に取るverbは無い**（頂点は選択集合の種別としては存在するが、どのverbも
  消費しない）。
- **操作ログに実行主体（人かAIか）は永続化されない**（`Operation`のフィールドは
  `seq`/`verb`/`args`/`provenance`のみ）。UI側が「自分がやった操作」に印を付けられるのは
  同じ接続内での応答待ち（`client_op`）の突き合わせによるものであって、ログを読んで
  分かることではない。
- **操作履歴の任意の位置へ一気に飛ぶ操作は無い**（undo/redoの回数を重ねるだけ）。
- **複数ブラウザ・複数人の同時編集は前提にしない**（`readme.md`「前提にしないこと」参照）。
