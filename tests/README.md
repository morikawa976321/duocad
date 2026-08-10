# duocad M0 — テストが定義するAPI契約

このディレクトリのテストが **M0(コアの骨格)のAPI契約そのもの**。次タスク（M0コア実装）は
ここにあるテストを全部通すことだけを目標にする。仕様の正は
`C:\Users\tm976\mf\reports\cad_v1_implementation_plan_2026-08-10.md`（このREADMEと食い違ったら
そちらを優先する）。

**M2（スケッチ・領域検出・押し出し）の契約は本文書の末尾に追記してある（2026-08-11）。**
M1（`extrude_face` / `make_editable_copy` / `tessellate`）の契約は
`tests/test_extrude_face.py` のモジュールdocstringが持つ（ここには無い）。

## 読み方: 「実装済み(素で通る)」と「スタブ(NotImplementedErrorで赤)」の区別

M0の対象は「文書・ボディのリスト・操作ログ・undo・STEP読み書き・STL書き出し」で、**振る舞い
(ロジック)は全部スタブ**（`raise NotImplementedError`）。ただし**day1の予防として最初から
正しく書いておくべきデータモデルの定義そのもの**（`Document.units` の不変性、`Body.kind` の
値検査、`Operation.provenance` フィールドの存在、`duocad/` のimport境界）は**実装済みで
実際に通る**。これはテストを緩めているのではなく、実装計画書§6「day1に払う予防」を
テストコードの側で先に払っているという位置づけ。

現状（2026-08-10、監査後）: **40件収集・収集エラー0・28 failed / 12 passed**。
- 28件の赤は**全件 `NotImplementedError` 由来**（`--tb=line` で例外種別を数えて確認済み。
  `ImportError` や収集失敗による赤は0件）。
- 緑12件は `units`不変性・`kind`検査・`bodies`が空リスト・`provenance`スロット存在・
  importの境界検査（5件）で、いずれも**day1で実装済みのデータモデル/静的検査**。
  これは意図した設計であり、次タスクがこれらを壊さないことも契約の一部。

## モジュール一覧

| モジュール | 役割 | OCP直接import | build123d直接import |
|---|---|---|---|
| `duocad/document.py` | `Document` / `Body` のデータモデル | 禁止 | 禁止 |
| `duocad/oplog.py` | `Operation` / `OpLog`（操作ログ） | 禁止 | 禁止 |
| `duocad/io_step.py` | STEP読み込み・STEP/STL書き出し | 禁止 | 禁止 |
| `duocad/query.py` | 問い合わせ層のインタフェース（build123dラッパ） | 禁止 | **ここだけ許可** |
| `duocad/kernel.py` | OCP(OCCT)を直接叩く層 | **ここだけ許可** | 禁止 |

境界は `tests/test_boundaries.py` がソースを静的走査して機械的に検査する（実行環境が
無くても走る）。**verbがOCP/build123dを直接叩き始めるのを防ぐ唯一の安全網であり、
これが一番大事**（タスクファイルの記載どおり）。検査の効き方:

- `duocad/` を **再帰的に**走査する（サブパッケージを作っても逃げられない）。
- `import OCP` / `import OCP.X` / `from OCP import X` / `from OCP.X import Y` に加え、
  **動的import**（`importlib.import_module("OCP.X")` / `__import__("OCP")`）も検出する。
- 免除は**パッケージ直下の `kernel.py` / `query.py` だけ**（相対パス一致で判定）。
  ファイル名一致だと `duocad/verbs/kernel.py` を置くだけで境界を無効化できてしまう。
- 検査関数そのものを既知の違反ソースで検査するテスト
  （`test_boundary_scan_detects_every_import_form`）を持つ。これが無いと、境界テストは
  「緑だが何も見ていない」状態に静かに退化しうる。
- 検出できない残りの穴: `exec()`/`eval()` 文字列、C拡張経由の間接呼び出し。
  ここまでやる相手は想定していない（規律の事故防止が目的で、悪意への防御ではない）。

## `duocad/document.py`

### `BodyId = str`

### `Body`（dataclass）

```python
Body(id: BodyId, name: str, kind: str, shape: Any = None, visible: bool = True)
```

- `kind` は `"solid"`（編集操作の対象）か `"reference"`（読み込んだSTEP等・読み取り専用）以外は
  `ValueError`（**実装済み**、`__post_init__` で検査）。
- `shape` は将来 `TopoDS_Shape` を持つ。M0スタブ段階では `None` を許容する。

### `Document`

```python
Document()
Document.units -> str                          # 常に "mm"。セッター無し(実装済み・不変)
Document.bodies: list[Body]                     # 実装済み。空リストで初期化
Document.log: OpLog                             # 実装済み。Document生成時に自動でOpLog()を持つ
Document.add_body(body: Body) -> BodyId         # スタブ。追加し、追加したボディを常にactiveにする
Document.remove_body(body_id: BodyId) -> None   # スタブ。bodiesから取り除く。activeだったら解除
Document.active_body() -> Body | None           # スタブ
Document.set_active_body(body_id: BodyId) -> None  # スタブ。存在しないIDはKeyError
Document.is_editable(body_id: BodyId) -> bool   # スタブ。kind=="solid"のときだけTrue
```

**契約**: `units` は "mm" 固定で変更できない。`bodies` は常にリストで持つ（v1のUIは1ボディしか
見せないが、参照STEPを読み込む時点で複数ボディが自然発生するため、データモデルはday1から
リストにする）。`add_body` は追加したボディを常にactiveにする（v1は単一フォーカスのUIなので、
最新の操作対象を自動でactiveにする設計）。

## `duocad/oplog.py`

### `Operation`（dataclass）

```python
Operation(seq: int, verb: str, args: dict = {}, selection: list = [], provenance: Any | None = None)
```

`provenance` は**day1で用意する空スロット**（実装計画書§6予防2）。値は `None` でよく、
**スロットの存在自体が契約**（フィールドが定義されていることをテストする）。

### `OpLog`

```python
OpLog()
OpLog.entries -> list[Operation]                          # 実装済み。有効な(undoされていない)エントリ
OpLog.append(verb: str, args: dict, selection: list | None = None) -> Operation   # スタブ
OpLog.undo(document: Document) -> Operation | None         # スタブ
OpLog.redo(document: Document) -> Operation | None         # スタブ
OpLog.to_json() -> str                                      # スタブ
OpLog.from_json(data: str) -> OpLog        # classmethod。スタブ
OpLog.replay(document: Document) -> None                    # スタブ
```

**契約**:

- `seq` は `append` を呼ぶたびに1から単調増加する。
- 保存形式は **JSON**。SQLiteは使わない（gitで差分が読める／AIが全履歴を読める／
  1モデル=1ファイルでコピー・共有できる、が理由。実装計画書§3.6）。
- `to_json()` → `from_json()` の往復で、`entries`（`seq`/`verb`/`args`/`selection`/`provenance`
  すべて）が完全一致すること。
- `append()` は**ログへの記録のみ**を行う（副作用の実行はしない）。副作用（ボディの追加など）は
  呼び出し側（例: `io_step.import_step`）が `document` を直接操作した**あとで** `append()` を
  呼ぶ。
- `undo`/`redo`/`replay` は verb ごとに `document` への反映方法を知っている必要がある。
  **M0で実在するverbは `"import_step"` のみ**で、その `args` は
  `{"path": str, "body_id": BodyId}` を持つ（`io_step.import_step` が積む。この形は
  `tests/test_io_step.py::test_import_step_logs_path_and_body_id_in_args` が固定する）。
  - `replay(document)` は**自分の `entries` を書き換えない**（再生のたびに履歴が増えないこと。
    `test_replay_does_not_append_to_the_log_it_replays` が固定する）。
    「開くときはログから再生する」（実装計画書§3.1）運用では
    `doc = Document(); doc.log = OpLog.from_json(...); doc.log.replay(doc)` の形になり、
    再生後のログがそのままその文書のログになる。`document.log` を差し替えるのは呼び出し側の責務。
  - `undo` の実装イメージ: `document.remove_body(args["body_id"])`
  - `redo`/`replay` の実装イメージ: `kernel.read_step(args["path"])` で形状を読み直し、
    `args["body_id"]` を**そのまま使って** `Body(id=args["body_id"], kind="reference", ...)` を
    構築して `document.add_body()` する。
    **`io_step.import_step()`（公開API）を内部から呼び直してはいけない** —
    `import_step` はそれ自体が `document.log.append(...)` を行うため、`redo`/`replay` の中で
    呼ぶと同じ操作が二重に記録される。`redo`/`replay` は `kernel.py` を直接使うか、
    `io_step.py` 内にログを積まない非公開ヘルパ（例: `_do_import_step(document, path, body_id)`）
    を用意してそちらを共有すること。
  - M1以降でverbが増えるたびに、このマッピング（verb名→do/undoの実体）を拡張する。
    **`oplog.py` の中に verb名の if 連鎖を書かないこと**（M2で7 verbになると
    「汎用のログ」が全verbの意味を知る層に化ける）。verb名→(do, undo) を登録する
    レジストリを `oplog.py` 側に置き、`io_step.py` 等の**verbを定義した側が登録する**形にする。
    M0では登録されるverbが `"import_step"` 1つなので、どちらで書いても通る — だからこそ
    ここで形を決めておく。

## `duocad/io_step.py`

```python
import_step(doc: Document, path: str | Path) -> BodyId
export_step(doc: Document, path: str | Path) -> None
export_stl(doc: Document, path: str | Path, deviation: float) -> None
```

**契約**:

- `import_step` はSTEPを **`kind="reference"`** のボディとして `doc.bodies` に追加し、
  `doc.log` に `verb="import_step"`, `args={"path": str(path), "body_id": <新BodyId>}` を
  積んで、そのBodyIdを返す。
- `export_stl` の `deviation` は**絶対の線形偏差(mm)**。`BRepMesh_IncrementalMesh` へ
  **`isRelative=False`** で渡すこと（後述の実測。相対にすると偏差を変えてもメッシュが変わらない）。
- **失敗を黙って通さない**（実装計画書§6予防6・§7）。3つとも `doc.bodies` / `doc.log.entries` に
  変更を残さないこと:

  | 失敗 | 契約 |
  |---|---|
  | パスが存在しない | `FileNotFoundError` |
  | 存在するがSTEPとして壊れている | `ValueError` |
  | 書き出す形状が無い（空の文書） | `ValueError`。**ファイルを作らない**（0バイトの成果物を残さない） |

  「壊れているファイル」のテストが「存在しないパス」と別に要るのは、**存在チェックだけでは
  `ReadFile()` の戻り値検査を強制できない**ため。`Path.exists()` を見るだけの実装は前者を
  通してしまうが、後者では `ReadFile()` が `IFSelect_RetFail` を返し、そのまま `TransferRoots()`
  を呼ぶとプロセスごと落ちる（下記）。

### `export_step` / `export_stl` が何を書き出すかは「M0の決定」であって仕様書由来ではない

実装計画書は §5 で `export_step(path)` / `export_stl(path, deviation)` としか書いておらず、
**対象ボディを規定していない**。タスクファイルのシグネチャも `body_id` を取らない。
そこで **M0では `doc.active_body()` の形状を対象にする**と決める。

**この決定は仕様書から導かれたものではない**ので、次のことを明示しておく:

- M0の対象は参照STEP 1ボディだけなので、「activeだけ」と「全ボディ」に差は出ない。
  そのため**テストはどちらの解釈でも通る**（意図的に、テストでは縛っていない）。
- 複数ボディが同時に存在するようになったとき（M2以降）は再検討が要る。実装計画書§7の
  「このツールで作りかけてFusion360で仕上げられる」という位置づけからは、
  成果物の書き出しは**文書全体**（少なくとも `kind="solid"` の全ボディ）が自然に読める。
  そのときは `export_step(doc, path, body_ids=None)` のように引数を足す方向で拡張する。

### 実測済みの危険な落とし穴（2026-08-10・最重要）

OCPの `STEPControl_Reader` は、`ReadFile()` が `IFSelect_RetDone` 以外を返した（＝読み込み失敗）
状態のまま `TransferRoots()` を呼ぶと、**Python例外にはならず、プロセスごと落ちる**
（Windowsで終了コード `-1073741819` = `0xC0000005` アクセス違反）。2026-08-10の監査で
別プロセスに切り出して独立に再現した。**失敗の種類は2つあり、どちらでも落ちる**:

| 与えたもの | `ReadFile()` の戻り値 | `TransferRoots()` を呼ぶと |
|---|---|---|
| 存在しないパス | `IFSelect_RetError` | 0xC0000005 で即死 |
| 存在するがSTEPでない中身 | `IFSelect_RetFail`（`**** ERR StepFile : Undefined Parsing` が出る） | 0xC0000005 で即死 |

`ReadFile()` の**呼び出しだけ**なら（戻り値を見て止まる限り）どちらも安全に返る。

```python
from OCP.STEPControl import STEPControl_Reader
r = STEPControl_Reader()
r.ReadFile(r"存在しないパス.step")   # ここは IFSelect_RetError を返すだけで安全
r.TransferRoots()                     # ここでプロセスごとセグフォルトする
```

これは実装計画書§7が警告する「OCCTの操作は例外を投げず黙って失敗する」（フィレットの
`IsDone()=False` 例）と同種だが、**例外どころかプロセスが死ぬ分こちらの方が悪性**。
`import_step` の実装は**必ず `ReadFile()` の戻り値を先に検査し**、失敗なら
`TransferRoots()`/`OneShape()` を呼ぶ前に `FileNotFoundError`（または同等の例外）を
送出すること（存在しないパス→`FileNotFoundError`、壊れた中身→`ValueError`）。
`tests/test_io_step.py::test_import_step_missing_file_raises_and_does_not_mutate_document` と
`::test_import_step_malformed_file_raises_value_error_and_does_not_mutate_document`
がこの契約を検査するが、**この2件はstub段階でしか安全に実行できない** — 実装が
上記の検査を怠ると、pytestプロセスごと落ちて**他のテスト結果も含めて何も報告されない**
（AssertionErrorではなく無言の即死になるので、原因が分かりにくい）。実装したら、まず
この2件を**単体で**明示的に実行し、プロセスが正常終了することを確認すること:

```
.venv\Scripts\python.exe -m pytest tests/test_io_step.py -k "missing_file or malformed_file" -v
```

### 実測値: STEP往復の体積は bit-exact ではない（2026-08-10・実装計画書の記述を訂正／監査で再現）

実装計画書§7は「STEP往復は厳密（差 0.00e+00 を確認済み）」と書いているが、その行の実測対象は
**26面・体積 23340.849375・書き出し62.1KB** の形状であり、受入対象の `shelf_attachment_GD.step`
（56面・体積 91018.2223598035・書き出し約129KB）**とは別の形状**である。同§8の
「書き戻して体積が完全一致すること」も、この形状では文字どおりには成立しない。

2026-08-10の監査で独立に測り直した結果（`cadquery-ocp-novtk==7.9.3.1.1` / `build123d==0.11.1`、
`STEPControl_Writer`(AsIs) + `BRepGProp.VolumeProperties_s` 既定）:

```
trial1: v1=91018.2223598035  v2=91018.22235980343  equal=False abs=7.276e-11 rel=7.994e-16 faces 56->56
trial2: v1=91018.2223598035  v2=91018.22235980343  equal=False abs=7.276e-11 rel=7.994e-16 faces 56->56
同じファイルを2回読んだだけの体積は完全一致（equal=True）
```

差は2回とも同じで、同一ファイルを2回読む分には差が出ないので、**原因はSTEP往復そのもの**
（浮動小数点の積分順序に由来する1ULP級の丸め）であり、幾何欠損ではない。

このため `test_export_step_roundtrip_preserves_volume` は `==` ではなく
`math.isclose(rel_tol=1e-6)` で比較する。実測誤差の**約10億倍のマージン**であり、
面の欠落・重複など実際のジオメトリ破損（体積差は通常オーダーがまるごと変わる）は
確実に検出できる一方、IEEE演算の丸めノイズでは落ちない。**この訂正は実測に基づくもので、
テストを緩めるための恣意的な選択ではない**。

なお `1e-6` をこれ以上詰めない理由も実測にある: `BRepGProp.VolumeProperties_s` に
適応積分の `Eps` を渡す実装にすると誤差が跳ね上がる（`Eps=1e-9` で相対1.8e-10、
`Eps=1e-6` で相対3.7e-8）。`rel_tol` を 1e-9 まで詰めると、`volume()` の実装がどちらを
使うかでテストの合否が変わってしまう。**`1e-6` は「体積の求め方の選択に依存しない」ための下限**。

面数（`describe_shape(shape)["face_count"]`）は往復後も**厳密一致（`==`）** で問題ない
（実測: 56 → 56、変化なし）。

### 実測値: STL書き出しサイズ

2026-08-10、同じ環境・同じファイル（`shelf_attachment_GD.step`）での実測。
`BRepMesh_IncrementalMesh(shape, deviation, isRelative, 0.5, False)` → `StlAPI_Writer`。
形状は毎回読み直し、`BRepTools.Clean_s` で既存の三角形分割を捨ててから測っている
（監査で独立に再現）:

| deviation (mm) | バイナリ (`ASCIIMode=False`) | ASCII | **`isRelative=True` にした場合** |
|---|---|---|---|
| 0.2 | 112084 B（109.5KB） | 582418 B（568.8KB） | 112084 B |
| 0.05 | 143884 B（140.5KB） | 747778 B（730.3KB） | 112084 B |
| 0.01 | 302684 B（295.6KB） | 1573538 B（1536.7KB） | 112084 B |

**実装上の落とし穴（監査で発見）**: 4列目のとおり、`isRelative=True`（相対偏差）で渡すと
**deviationを1/20にしてもメッシュが1バイトも変わらない**。`build123d` の `export_stl`
（`exporters3d.py`）は内部で `isRelative=True` を使っているので、**そのまま流用すると
`test_export_stl_smaller_deviation_yields_larger_file` は必ず落ちる**。
`deviation` は**絶対の線形偏差(mm)**として `isRelative=False` で渡すこと。

実装計画書§7に書かれた数字（0.2mm→約31KB / 0.05mm→約47KB / 0.01mm→約171KB）とは
バイナリ・ASCIIのどちらとも一致しない。§7のSTEP行が26面の別形状での実測であることから、
**STL行も同じ別形状での実測**と考えるのが自然（比 31:47:171 と 112:144:303 も一致しない）。
タスクファイルが明記するとおり**「厳密一致は求めない」**ため、
`test_export_stl_smaller_deviation_yields_larger_file` は
**絶対サイズではなく単調性（0.2mm時 < 0.05mm時 < 0.01mm時）だけ**を契約として縛る。

## `duocad/query.py`

```python
describe_shape(shape: Any) -> dict
volume(shape: Any) -> float
```

`describe_shape` の戻り値の形:

```python
{
    "face_count": int,
    "edge_count": int,
    "faces_by_type": {
        "plane": int,
        "cylinder_full": int,     # 全周円筒(穴)。UVBoundsのU範囲が350度超で判定
        "cylinder_partial": int,  # 全周でない円筒
        "torus": int,
        "cone": int,
        "bspline": int,
        "other": int,
    },
}
```

全周円筒の判定基準（実装計画書§8）: 円筒面の `BRepAdaptor_Surface.FirstUParameter()` /
`LastUParameter()` から求めたU範囲(度)が **350度を超えるかどうか**。
`shelf_attachment_GD.step` では検出された円筒22面全部が全周（=穴）だった
（`cylinder_full=22`, `cylinder_partial=0`、2026-08-10実測）。

**契約**: `faces_by_type` の全キーの合計は常に `face_count` と一致すること
（`sum(faces_by_type.values()) == face_count`）。既知カテゴリ間の付け替え（例えば全周円筒を
部分円筒に誤判定する等）はこの合計検査では捕まえられない点に注意（個別カテゴリの値そのものを
検査する各アサーションが担う）が、**面の数え漏れ・二重計上・未知キーの混入**はこの合計検査で
検出できるため、`test_import_step_face_breakdown_matches_measured_reference` はこの検査も行う。

`edge_count` は**重複を除いた辺の数**（`TopExp.MapShapes_s` で数える）。
`TopExp_Explorer` で素朴に数えると隣接2面から同じ辺を2回拾ってしまう
（`shelf_attachment_GD.step` 実測: 重複除去 **66** / 素朴な列挙 132、2026-08-10監査）。
M0のテストは `edge_count` の値を縛っていないので、ここで数え方だけ決めておく。

`volume` は `mm^3`（documentの `units` が常に `"mm"` であることに対応）。
実装は `BRepGProp.VolumeProperties_s(shape, props)`（`Eps` を渡さない既定の積分）を使うこと。
`Eps` 付きの適応積分は精度が落ちる（上記「STEP往復」節の実測）。

## `duocad/kernel.py`

```python
read_step(path: str) -> TopoDS_Shape
write_step(shape: TopoDS_Shape, path: str) -> None
write_stl(shape: TopoDS_Shape, path: str, deviation: float) -> None
```

`io_step.py` の各関数は最終的にここへ委譲する想定（`io_step.py` 自体はOCPを直接importしない
契約のため）。`read_step` は**`ReadFile()` の戻り値を必ず先に検査**し、失敗時は
`TransferRoots()`/`OneShape()` を呼ぶ前に例外を送出すること（上記「実測済みの危険な
落とし穴」参照）。`write_stl` の `deviation` は
`BRepMesh_IncrementalMesh(shape, deviation, False, 0.5, False)` の**第2引数（絶対mm）**として
渡すこと（第3引数 `isRelative` は必ず `False`。上記STLの節を参照）。

## 実測環境（2026-08-10）

- Python 3.11.15、`C:\Users\tm976\mf\duocad\.venv`（`uv venv --python 3.11`で作成）
- `build123d==0.11.1`
- `cadquery-ocp-novtk==7.9.3.1.1`（importは `OCP`。`C:\Users\tm976\mf\cad-deixis-probe\.venv`
  で検証済みの環境と同一バージョン）
- `pytest==9.1.1`
- 受入対象STEP: `C:\Users\tm976\mf\3d-models\shelf_attachment_GD.step`
  （読み取りのみ・変更禁止。面56 = 平面34 + 全周円筒22、トーラス0・円錐0・spline0、
  体積91018.2223598035mm³、辺66（重複除去）。いずれも2026-08-10にOCPで直接実測し、
  同日の監査で独立に再測して一致を確認した。円筒22面のU範囲は**全て正確に360.0度**で、
  350度しきい値の付近に一件も無い＝しきい値の選び方に結果が依存しない）

## テスト一覧とタスクファイル項番の対応

| タスクファイルの項番 | テスト |
|---|---|
| 1 | `test_document.py::test_document_units_is_mm`, `::test_document_units_cannot_be_reassigned` |
| 2 | `test_document.py::test_new_document_has_no_bodies`, `::test_new_document_active_body_is_none`, `::test_add_body_*`, `::test_remove_body_*`, `::test_set_active_body_*`, `::test_reference_body_is_not_editable`, `::test_solid_body_is_editable` |
| 3 | `test_oplog.py::test_append_seq_is_monotonically_increasing` |
| 4 | `test_oplog.py::test_undo_reverts_document_state`, `::test_redo_reapplies_undone_operation`, `::test_redo_does_not_record_the_operation_twice` |
| 5 | `test_oplog.py::test_to_json_from_json_roundtrip_matches_exactly`, `::test_to_json_emits_actual_json_with_the_logged_fields`（往復の自己整合だけでは「JSONであること」を縛れないため） |
| 6 | `test_oplog.py::test_replay_reproduces_same_document_state`, `::test_replay_does_not_append_to_the_log_it_replays` |
| 7 | `test_oplog.py::test_operation_has_provenance_slot_defaulting_to_none`, `::test_operation_dataclass_declares_provenance_field` |
| 8 | `test_io_step.py::test_import_step_face_breakdown_matches_measured_reference` |
| 9 | `test_io_step.py::test_import_step_creates_reference_body` |
| 10 | `test_io_step.py::test_import_step_is_logged_and_undoable`, `::test_import_step_logs_path_and_body_id_in_args` |
| 11 | `test_io_step.py::test_export_step_roundtrip_preserves_volume`（`==`ではなく`math.isclose`。理由は上記） |
| 12 | `test_io_step.py::test_export_step_roundtrip_preserves_face_count` |
| 13 | `test_io_step.py::test_export_stl_smaller_deviation_yields_larger_file` |
| 14 | `test_io_step.py::test_export_stl_works_from_reference_body` |
| 15 | `test_boundaries.py::test_only_kernel_module_imports_ocp`, `::test_kernel_module_actually_imports_ocp`, `::test_boundary_scan_detects_every_import_form`, `::test_boundary_scan_covers_every_source_file` |
| 16 | `test_boundaries.py::test_only_query_module_imports_build123d`（同上の自己検査2件が下支え） |
| 17 | `test_io_step.py::test_import_step_missing_file_raises_and_does_not_mutate_document`, `::test_import_step_malformed_file_raises_value_error_and_does_not_mutate_document`, `::test_export_from_empty_document_raises_and_writes_no_file` |

## 実行方法

```
cd C:\Users\tm976\mf\duocad
.venv\Scripts\python.exe -m pytest -v
```

`duocad` は venv へインストールしていないので、`pyproject.toml` の
`[tool.pytest.ini_options] pythonpath = ["."]` で rootdir を `sys.path` に入れている。
これが無いと `python -m pytest`（CWDが `sys.path` に入る）でしか走らず、素の
`.venv\Scripts\pytest.exe` では `ModuleNotFoundError: No module named 'duocad'` で
**収集エラー3件**になる（2026-08-10監査で実測し、`pythonpath` を追加して修正した）。
どちらの起動方法でも同じ結果になることを確認済み。

---

# M2 — スケッチと領域検出のAPI契約（2026-08-11追記）

ここから下がM2の契約。`tests/test_sketch.py` / `tests/test_region.py` / `tests/test_extrude.py`
の3ファイルが実体で、次タスク（M2ロジックの実装）は**これを全部緑にすることだけ**を目標にする。
上のM0の節は当時のまま残してある。

## 現状（テスト先行の段階・2026-08-11）

**97件収集・収集エラー0・29 failed / 68 passed。**

- 29件の赤は**全件 `NotImplementedError` 由来**（`--tb=line` で例外種別を数えて確認済み。
  `ImportError` や収集失敗による赤は0件）。
- 68件の緑のうち **61件はM0・M1のテストで、1行も触っていない**。M2で新たに緑になったのは
  7件だけで、いずれも**day1で実装済みのデータモデル/静的検査**（`Document.sketches` が
  空リスト・`BASE_PLANES`・`face_plane()` の由来スロット・`Entity.kind` の検査・
  `RegionRef` の由来スロット・`region_ref()`・M2ファイルへの import 境界）。
  M0と同じ設計で、テストを緩めているのではない。

## モジュール一覧（M2で増える分）

| モジュール | 役割 | OCP直接import | build123d直接import |
|---|---|---|---|
| `duocad/sketch.py` | `Sketch` / `Entity` / PlaneRef とスケッチの4 verb | 禁止 | 禁止 |
| `duocad/region.py` | `Region` / `RegionRef` と領域検出 | 禁止 | 禁止 |
| `duocad/verbs/extrude.py` | verb `extrude`（領域の押し出し） | 禁止 | 禁止 |

幾何（`BRepAlgoAPI_Splitter` による領域検出、プリズム生成、ブーリアン）は**すべて
`kernel.py` に置く**。M0の境界がそのまま効く（`tests/test_sketch.py::
test_m2_modules_are_covered_by_the_import_boundary_scan` が、この3ファイルが走査対象に
入っていること自体を固定する。M0の `test_boundary_scan_covers_every_source_file` は
M0の6ファイルしか見ないため、新規ファイルが走査から漏れても気づけない）。

## `duocad/sketch.py`

```python
BASE_PLANES = ("XY", "XZ", "YZ")
ENTITY_KINDS = ("line", "circle", "rectangle")

base_plane(name) -> dict            # 実装済み {"kind":"base","name":..,"provenance":None}
face_plane(body_id, face_index)     # 実装済み {"kind":"face","body":..,"index":..,"provenance":None}
get_sketch(doc, sketch_id) -> Sketch                     # 実装済み。無ければKeyError

sketch_create(doc, plane) -> SketchId                    # スタブ
add_line(doc, sketch_id, p0, p1) -> EntityId             # スタブ
add_circle(doc, sketch_id, center, radius) -> EntityId   # スタブ
add_rectangle(doc, sketch_id, corner, width, height) -> EntityId   # スタブ
entity_params(doc, sketch_id, entity_id) -> dict[str, float]       # スタブ
```

**契約**:

- `plane` は `"XY"|"XZ"|"YZ"` か `face_plane(body_id, face_index)`。
  **平面でない面（円筒面など）は `ValueError`**。黙って通すと、その上の領域検出と
  押し出しが「意味を失ったまま成功する」。
- **座標はスケッチ平面上の2D**。基準平面なら (x,y)/(x,z)/(y,z) に対応する。
- `Entity.params` のキーは図形ごとに固定（スカラー1個＝将来のパラメータ1個になるよう平らに持つ）:
  `line: x0,y0,x1,y1` / `circle: cx,cy,radius` / `rectangle: x,y,width,height`。
- 操作ログの `args`:
  `sketch_create` → `{"sketch_id":.., "plane": <PlaneRefのdict>}`、
  図形追加 → `{"sketch_id":.., "entity_id":.., "kind":.., "params": {...}}`。
  **作ったIDを記録する**（記録が無いと再生で別のIDになり、保存した選択集合が指す先を失う）。
- verb名は `sketch_create` / `add_line` / `add_circle` / `add_rectangle` の4つで、
  登録は `oplog.register_verb` へ **sketch.py 側から**行う（M0の契約どおり、`oplog.py` に
  verb名の if 連鎖を書かない）。図形追加3つは do/undo の実体を共有してよい。
- 存在しないスケッチIDへの操作は `KeyError`。

## `duocad/region.py`

```python
Region(index: int, area: float)                 # frozen dataclass。実装済み
RegionRef(sketch: SketchId, index: int, provenance=None)   # 実装済み。to_dict/from_dict付き
region_ref(sketch_id, index) -> RegionRef       # 実装済み
regions(doc, sketch_id) -> list[Region]         # スタブ
```

**契約**:

- `index` は**1始まりの連番**（面の添字と同じ流儀）。`area` は mm²。
- **列挙順は安定**であること（同じスケッチを2回評価して同じ順序・同じ面積）。選択集合は
  領域を添字で指す（§3.5）ので、順序が揺れると保存した選択が別の領域を指す。
- 図形が無いスケッチは**空リスト**（例外にしない）。存在しないスケッチIDは `KeyError`。
- 実装は `BRepAlgoAPI_Splitter`（実装計画書§3.3。実測で成立を確認済み）。
- `RegionRef` は**由来スロットを持つ**。実装計画書§6「由来スロットの適用先は3つ」の
  3つ目（面/辺の添字・スケッチを貼った面・**領域の同一性**）がこれ。
- 操作ログへ入れるのは `to_dict()` の素のdict（`OpLog.to_json()` がJSONにできること）。

## `duocad/verbs/extrude.py`

```python
extrude(doc, regions, distance, mode="join", body_id=None) -> Operation   # スタブ
MODES = ("join", "cut")
```

**契約**:

- `regions` は `RegionRef` のリスト。**空リストは `ValueError`**、
  **存在しない添字も `ValueError`**（領域は計算されるものなので、指した先が消えているのは
  普通に起きる。§7「領域はパラメータ編集で変わる」）。
- `distance` はスケッチ平面の法線方向への押し出し量。**0は `ValueError`**。
  未知の `mode` も `ValueError`。
- `mode="join"` … 対象ボディへ足す。**対象が無ければ新しいソリッド（`kind="solid"`）を
  1つ作り、アクティブにする**（最初の板はこれで生まれる）。
  `mode="cut"` … 対象から引く。**対象が無ければ `ValueError`**。
- `body_id` の既定はアクティブボディ。`kind="reference"` は `ValueError`（M0の読み取り専用契約）。
- ログの `args` は `{"regions": [<dict>...], "distance":.., "mode":.., "body_id":..}`、
  `selection` は同じ `RegionRef` のdictの並び。**join が作ったボディのIDも `args["body_id"]`
  に記録する**。
- 失敗時は **document にもログにも変更を残さない**（幾何計算を `append` より先に済ませる。
  `extrude_face` と同じ順序）。

## M2の決定（仕様書から導かれないもの・M0の「export対象」と同じ位置づけ）

実装計画書が規定していないため、**テストを書く側の決定として明示する**:

1. **verbの戻り値の流儀** — 新しいものを作るverbは新しいID（`make_editable_copy` と同じ）、
   既存の形状を変えるverbは `Operation`（`extrude_face` と同じ）。スケッチの4 verbは前者、
   `extrude` は後者。
2. **`Sketch` は `regions` フィールドを持たない**。実装計画書§3.3は `regions` を
   「導出物」と明記しているので、**契約は `region.regions()` というアクセサに固定**し、
   キャッシュを持つかどうかは実装の自由にした。導出物をデータモデルに置くと、
   「更新されていないキャッシュ」が正のように見える瞬間が必ずできる。
3. **`entity_params()` というアクセサを挟む**。M2では `Entity.params` の素通しだが、
   **M3で「数値を図形に焼き込まず名前つきの値として持つ」へ移す**と `Entity.params` の
   中身は参照に変わる。テストがこのアクセサ越しに数値を読んでいれば、M3の変更で
   M2の契約は壊れない（M3のタスクファイル項番1がまさにこれを要求している）。
   **M3の実装者へ**: `Entity.params` の形は変えてよいが、`entity_params()` は
   解決済みの数値を返し続けること。
4. **join の対象が無いときは新しいボディを作る**（1のverbが「作る」と「足す」を兼ねる）。
   スケッチだけがある文書から最初の板を作る経路が他に無いため。cut は対象必須。
5. **`Document.sketches` を追加した**（実装計画書§3.1の文書定義どおり）。型注釈は付けていない
   —— `document.py` → `sketch.py` の依存を作らないため（依存は sketch.py → document.py の一方向）。

## 実測値の出どころ

| 値 | 出どころ |
|---|---|
| 円(r=20)の面積 **1256.637** | タスクファイル記載の実測値（真値 π·20²=1256.6370614…） |
| y=5 の線分で割った **826.215 / 430.422** | 同上（真値 826.2152… / 430.4218…。合計が円の面積と一致する） |
| 線2本で領域 **3個** | 同上 |
| 板と穴の面構成 **平面6 + 全周円筒1 = 7面**、体積 **11717.2566611769…** | **2026-08-11にOCPで直接実測**（40×30×10 の box から r=3・高さ20 の円柱を `BRepAlgoAPI_Cut` で引き、`query.describe_shape` / `query.volume` で数えた） |

面積の比較は `rel_tol=1e-6`。上の実測値は小数3桁なので真値との差は最大でも約 3e-7 で、
この幅に確実に収まり、かつ**領域の取り違え（桁が変わる）は確実に落ちる**。
体積の比較は M1 と同じ `rel_tol=1e-9`（ブーリアンの積分順序で1ULP級の差が出るため `==` は使わない）。

## テスト一覧とタスクファイル項番の対応（M2）

| タスクファイルの項番 | テスト |
|---|---|
| 1（基準平面） | `test_sketch.py::test_sketch_create_on_each_base_plane`（XY/XZ/YZ）, `::test_sketch_create_rejects_an_unknown_base_plane` |
| 2（既存面・平面でない面の拒否） | `test_sketch.py::test_sketch_create_on_a_planar_face_of_an_existing_solid`, `::test_sketch_create_rejects_a_non_planar_face` |
| 3（図形の保持） | `test_sketch.py::test_line_circle_and_rectangle_are_kept_in_the_sketch`, `::test_adding_an_entity_to_an_unknown_sketch_fails` |
| 4（ログとundo） | `test_sketch.py::test_sketch_and_entities_are_logged_and_undoable` |
| 5（円1個・1256.637） | `test_region.py::test_a_single_circle_yields_one_region` |
| 6（2領域・合計一致） | `test_region.py::test_a_line_splits_the_circle_into_two_regions` |
| 7（線2本で3領域） | `test_region.py::test_two_lines_split_the_circle_into_three_regions` |
| 8（列挙順の安定） | `test_region.py::test_region_enumeration_order_is_stable`（＋`::test_regions_of_a_sketch_without_entities_is_empty`, `::test_regions_of_an_unknown_sketch_fails`） |
| 9（片側だけ押し出し＝面積×距離） | `test_extrude.py::test_extruding_one_half_of_a_split_circle_gives_area_times_distance`（＋join の合成は `::test_join_fuses_into_the_existing_body`） |
| 10（cutで穴） | `test_extrude.py::test_cut_opens_a_hole_in_the_existing_body` |
| 11（ログとundo） | `test_extrude.py::test_extrude_is_logged_and_undoable`, `::test_undo_restores_the_shape_for_fuse_and_cut`（＋再生は `::test_replay_reproduces_the_split_circle_extrusion`, `::test_replay_reproduces_the_plate_with_the_hole`） |
| 12（対象が無ければ失敗・文書不変） | `test_extrude.py::test_extrude_without_an_existing_region_fails_and_changes_nothing`, `::test_extrude_rejects_a_zero_distance_and_an_unknown_mode`, `::test_cut_without_a_target_body_fails`, `::test_extrude_rejects_a_reference_body` |
| 13（板と穴・面の構成） | `test_extrude.py::test_a_plate_with_a_hole_has_only_planes_and_a_full_cylinder` |
| 14（import境界） | `test_sketch.py::test_m2_modules_are_covered_by_the_import_boundary_scan` |
| 15（RegionRefの由来スロット） | `test_region.py::test_region_ref_declares_a_provenance_slot`（＋貼った面の側は `test_sketch.py::test_face_plane_ref_declares_a_provenance_slot`） |

## QAで足した契約（2026-08-11・cursor auto のレビュー指摘を反映）

初版に対して cursor(auto) が major 1件・minor 6件を指摘し、全件を反映した。**契約が増えた分**:

- `add_line` / `add_rectangle` のログ `args` の形と、**線を含む文書の再生**
  （初版は円しか見ておらず「`Entity` は正しいがログだけ別形」が緑のまま残る穴があった）
- **fuse-join と cut の undo**（undoの実体は「作ったボディを消す」と「形状を戻す」の2通りに
  分かれるが、初版は前者しか見ていなかった）
- 参照ボディ（`kind="reference"`）の拒否、領域添字 `0` / 負数の拒否
- `Entity` / `Region` のフィールド検査を**完全一致から包含へ**（補助フィールドの追加まで
  禁じるのは過剰拘束）
