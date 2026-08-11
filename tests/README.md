# duocad M0 — テストが定義するAPI契約

このディレクトリのテストが **M0(コアの骨格)のAPI契約そのもの**。次タスク（M0コア実装）は
ここにあるテストを全部通すことだけを目標にする。仕様の正は
`C:\Users\tm976\mf\reports\cad_v1_implementation_plan_2026-08-10.md`（このREADMEと食い違ったら
そちらを優先する）。

**M2（スケッチ・領域検出・押し出し）と M3（パラメータ・矩形反復）の契約は、
この順で本文書の末尾に追記してある（2026-08-11）。**
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

## 現状（2026-08-11・M2ロジック実装後）

**106件収集・収集エラー0・106 passed。** M2のロジックは同日の後続タスクで実装された。
内訳は M0/M1 の61件 ＋ M2の契約36件 ＋ **`tests/test_m2_regressions.py` の9件**。
最後のファイルは契約ではなく**再発防止**で、QAが「97件が全部緑のまま壊れていた経路」を
4種類実測したことによる（面に貼ったスケッチが `regions()` で必ず例外になっていた／
別スケッチの領域を混ぜると体積0の退化ソリッドを黙って作っていた／NaN等の壊れた数値が
永続化され正常な領域まで消えた／参照STEPを読んだ直後に `join` が最初の板を作れなかった）。
**契約側の3ファイルは1行も変えていない**（契約が誤っていたのではなく、契約が触れていない
経路に穴があった）。

### テスト先行の段階でどうだったか（契約タスク完了時点の記録）

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

## M2のQAで足した契約（2026-08-11・cursor auto のレビュー指摘を反映）

初版に対して cursor(auto) が major 1件・minor 6件を指摘し、全件を反映した。**契約が増えた分**:

- `add_line` / `add_rectangle` のログ `args` の形と、**線を含む文書の再生**
  （初版は円しか見ておらず「`Entity` は正しいがログだけ別形」が緑のまま残る穴があった）
- **fuse-join と cut の undo**（undoの実体は「作ったボディを消す」と「形状を戻す」の2通りに
  分かれるが、初版は前者しか見ていなかった）
- 参照ボディ（`kind="reference"`）の拒否、領域添字 `0` / 負数の拒否
- `Entity` / `Region` のフィールド検査を**完全一致から包含へ**（補助フィールドの追加まで
  禁じるのは過剰拘束）

---

# M3 — パラメータと矩形反復のAPI契約（2026-08-11追記）

`tests/test_parameters.py` / `tests/test_pattern.py` がM3の契約。次タスク（M3ロジックの実装）は
**これを全部緑にすることだけ**を目標にする。上のM0・M2の節は当時のまま。

## 現状（2026-08-11・M3ロジック実装後）

**141件収集・収集エラー0・141 passed。** M3のロジックは同日の後続タスクで実装された。
内訳は M0〜M2 の115件（M2の回帰9件を含む）＋ M3の契約21件 ＋
**`tests/test_m3_regressions.py` の5件**。最後のファイルは契約ではなく**再発防止**で、
QAが「131件が全部緑のまま壊れていた経路」を4種類実測したことによる（`set_parameter` の
作り直しがアクティブボディの選択を捨てる／作り直しの失敗で空の文書が残る／
`add_circle` が弾く値を `set_parameter` から書き込める／外のログを `replay` すると
黙って空の文書ができる）。いずれも**「全部消して作り直す」が、消してはいけないものと
失敗したときの後始末を持っていなかった**ことに由来する。

### テスト先行の段階でどうだったか（契約タスク完了時点の記録）

**129件収集・収集エラー0・22 failed / 107 passed。**

- **M0〜M2の106件は1行も触っておらず、単独実行でも 106 passed。**
- 22件の赤の内訳: **19件が `NotImplementedError`（スタブ）由来**、
  **3件が `AssertionError`** — いずれも
  「M2の `add_*` はまだ素の数値を `Entity.params` に入れている」ことを検出している
  （`test_numbers_become_named_values_and_entities_hold_references` /
  `test_every_entity_kind_holds_references_not_numbers` /
  `test_parameter_names_are_released_by_undo_and_reused`）。
  **これは収集エラーやimport事故ではなく、M3が変える対象そのもの**（数値を座標に
  焼き込まない、が未達であること）を指している。M2の節と違って
  「赤は全件NotImplementedError」とは書けないので、ここに明記しておく。
- 新たに緑になったのは1件（M3ファイルへのimport境界の走査）。

## モジュール一覧（M3で増える分）

| モジュール | 役割 | OCP直接import | build123d直接import |
|---|---|---|---|
| `duocad/parameters.py` | 名前つきの寸法値・`set_parameter`・再生 | 禁止 | 禁止 |
| `duocad/verbs/pattern.py` | verb `pattern_rectangular` | 禁止 | 禁止 |

データモデルの追加（テスト先行の段階で実装済み）: `Document.parameters`（§3.1）、
`Sketch.patterns` と `Pattern`（`duocad/sketch.py`）。

## `duocad/parameters.py`

```python
register_value(doc, value) -> str            # 名前つきの値を登録し名前を返す
resolve(doc, ref) -> float                   # パラメータ名なら現在値、素の数値ならそのまま
parameter_of(doc, sketch_id, owner_id, key) -> str   # 図形/反復のkeyに結びついた名前
set_parameter(doc, name, value) -> Operation
rebuild(doc, upto_seq) -> None               # seq<=upto_seq のログから文書を作り直す
```

**契約**:

- **数値は図形の座標に焼き込まない**（§6予防5）。`add_circle(doc, sk, (0,0), 20)` は
  `cx` / `cy` / `radius` の3つを名前つきの値として登録し、`Entity.params` には
  **その名前（str）**を入れる。`entity_params()` は解決済みの数値を返し続ける（M2の契約）。
- **名前は `d1` `d2` …**（Fusion360と同じ流儀）。使われていない**最小の正整数**を使う。
  undoで消えた名前は再利用される。**決定的であることが要件** — 再生で名前の振られ方が
  揺れると、`set_parameter` が記録した名前が別の値を指す。
- **操作ログの `args` には打った数値がそのまま入る**（パラメータ名ではない）。M2の
  `test_sketch_and_entities_are_logged_and_undoable` が固定しており、ログが唯一の正で
  パラメータはそこから再生されるものだから、この形が正しい。
- `set_parameter`:
  - 存在しない名前は **`KeyError`**、文書にもログにも変更を残さない
  - `args={"name":.., "value":.., "previous":..}`。`previous` は undo が値を戻すため
  - **形状を追随させる**（§3.1「形状はログからの導出物」）。値を変えたらログを再生して
    bodies を作り直す。作り直さなければ「形が追随する」は嘘になる
  - **再生で押し出しの対象領域が失われたら `ValueError`。文書は変更前のまま戻す**
    （§3.3の注意点・§7「領域はパラメータ編集で変わる」。壊れたモデルを静かに残さない）
  - undo は `previous` に戻して同じく作り直す。
    **落とし穴**: `OpLog.undo` はハンドラを呼んでからカーソルを戻すので、undoの中で
    ログ全体を再生すると**取り消すはずの操作まで再適用される**。再生範囲は
    `seq < operation.seq` に限ること

## `duocad/verbs/pattern.py`

```python
pattern_rectangular(doc, sketch_id, entities, dx, dy, nx, ny) -> PatternId
```

**契約**:

- **反復は1つのverb・ログ1行**。nx×ny個の図形へ展開しない（展開すると個数も間隔も
  後から変えられなくなり、反復の存在理由が消える。§2「verbの粒度」）。
  スケッチが持つのは `Pattern` 1個で、nx×ny個は**幾何を作るときの導出物**。
- `nx` / `ny` は**元を含めた個数**（格子は (0,0) を含む）。したがって
  **反復に参照されている図形は単独では描かない**（描くと (0,0) の辺が二重になる）。
- `dx` / `dy` / `nx` / `ny` の4つとも名前つきの値になる（個数も間隔も後から変えられる）。
- `args={"sketch_id":.., "pattern_id":.., "entities":[..], "dx":.., "dy":.., "nx":.., "ny":..}`。
- 失敗（`nx`/`ny` が1以上の整数でない・`entities` が空・別スケッチの図形・二重反復）は
  例外で、文書にもログにも残さない。

## M3の決定（仕様書から導かれないもの）

1. **`Sketch.patterns` を `entities` と分けた**。反復は「図形を参照する別のもの」で、
   `Entity.params` がスカラーだけを持つ形（M3のパラメータ化がそのまま効く形）を保つため。
   `Entity` に `sources` を足すと、線・円・長方形の全部が使わないフィールドを持つ。
2. **パラメータ名は `d1` `d2` …**。改名機能はv1の範囲外（§9の「以降」）。
3. **`set_parameter` が再生の起点**（アプリ層でもverbでもなく、この関数が bodies を作り直す）。
   「値を変えたら形が追随する」をAPIの内側で閉じるため。
4. **穴の位置の検証は全周円筒面の重心で行う**（`Region` に重心を足さない）。
   §3.3の `Region : { index, area }` を広げずに間隔の追随を確かめられる。

## テスト一覧とタスクファイル項番の対応（M3）

| タスクファイルの項番 | テスト |
|---|---|
| 1（名前つきの値・参照を持つ） | `test_parameters.py::test_numbers_become_named_values_and_entities_hold_references`, `::test_every_entity_kind_holds_references_not_numbers`, `::test_parameter_names_are_d1_d2_d3_in_creation_order`, `::test_parameter_names_are_released_by_undo_and_reused`, `::test_parameter_of_rejects_unknown_keys_and_owners` |
| 2（値の変更で形が変わる） | `test_parameters.py::test_set_parameter_changes_the_resulting_shape` |
| 3（ログとundo） | `test_parameters.py::test_set_parameter_is_logged_and_undoable` |
| 4（存在しない名前） | `test_parameters.py::test_set_parameter_rejects_an_unknown_name` |
| 5（保存→読込→再生） | `test_parameters.py::test_parameters_survive_save_load_and_replay` |
| 6（nx×ny個に増える） | `test_pattern.py::test_pattern_multiplies_the_entity_into_nx_times_ny_regions` |
| 7（1 verb・1行・展開しない） | `test_pattern.py::test_pattern_is_one_operation_and_is_not_expanded_into_entities`（`Pattern.params` も参照であることを含む） |
| 8（押し出すと穴がnx×ny個） | `test_pattern.py::test_extruding_a_pattern_opens_nx_times_ny_holes` |
| 9（nx・dxの変更が追随） | `test_pattern.py::test_changing_the_spacing_moves_the_holes`, `::test_changing_the_count_changes_the_regions_and_the_next_extrusion`, `::test_reducing_the_count_below_an_extruded_selection_is_refused` |
| 10（反復のundo） | `test_pattern.py::test_pattern_can_be_undone` |
| 11（半径1箇所で全穴が追随） | `test_pattern.py::test_editing_one_radius_moves_every_hole`（＋再生は `::test_pattern_survives_save_load_and_replay`） |
| 12（import境界） | `test_parameters.py::test_m3_modules_are_covered_by_the_import_boundary_scan` |
| 13（領域が壊れたら黙って通さない） | `test_parameters.py::test_set_parameter_refuses_an_edit_that_destroys_an_extruded_region`（＋個数・二重反復の検査は `test_pattern.py::test_pattern_rejects_counts_that_are_not_positive_integers`, `::test_pattern_rejects_an_empty_or_foreign_entity_list`, `::test_pattern_rejects_patterning_the_same_entity_twice`） |

## M3で**契約にしなかった**こと（範囲の明示・2026-08-11のQA指摘への回答）

タスクファイルの項番13は「領域が**分裂・合体・消失**した場合に黙って通さない」だが、
**M3が縛るのは「押し出しの対象が失われた（＝添字が消えた）」場合だけ**にした。

理由: 領域の**取り違え**（添字は残っているが、指している領域が別物に入れ替わる）を
検出するには、面積や添字ではなく**領域の同一性**を追う必要があり、それは実装計画書§6が
「由来スロットの適用先は3つ」の3つ目として **`RegionRef.provenance` に空で用意してある**もの
（＝後で埋める前提の穴）である。分割線を動かすと2領域の面積が入れ替わり、面積降順で並べる
現在の列挙では添字も入れ替わるが、**これを一律に失敗にすると「値を編集したら形が追随する」
というM3の目的そのものを潰す**（線を動かすのは正当な編集である）。

したがって現状の位置づけは次のとおりで、これは既知の制限である:

- **失われた領域を指したまま再生 → `ValueError` で止まる**（M3が固定した）
- **入れ替わった領域を指したまま再生 → 静かに新しい方へ適用される**（未解決。
  解くには `RegionRef.provenance` を実際に埋める＝L4化が要る。M4以降）


## M3ロジック実装で判明した契約の誤りと、その直し方（2026-08-11）

**テストを1件だけ書き直した。** 初版の `test_changing_the_count_and_the_spacing_moves_the_holes`
は「6個の穴を開けたあとで `nx` を3→2に減らすと、穴の数が追随する」ことを要求していたが、
これは**同じM3の項番13（押し出していた領域が消えたら黙って通さない）と両立しない** ——
`extrude` は領域を**添字の集合**として記録する（§3.5）ので、個数を減らせば記録済みの
選択が指す先が消える。実装がどちらかを裏切るしかない状態だった（「通らないから緩めた」
のではなく、契約自身が矛盾していた）。

そこで1件を**3件に割った**。範囲は狭めていない — 3件目は新しく足した拘束である:

1. `test_changing_the_spacing_moves_the_holes` — `dx` の変更（個数は不変）は、
   **開けた穴の位置まで**追随する
2. `test_changing_the_count_changes_the_regions_and_the_next_extrusion` — `nx` の変更は
   **領域の個数**に追随し、そのあと押し出せば穴の数も追随する
3. `test_reducing_the_count_below_an_extruded_selection_is_refused` — 押し出し済みの領域が
   反復の縮小で**消える**編集は `ValueError` で止まり、文書は元のまま

**既知の制限（M4で解ける）**: 押し出し済みの選択は個数の変更に追随しない。増やしても
記録済みの選択は増えず、減らせば上の3で止まる。選択を「このスケッチの全領域」のような
**述語**で書けるようになるのは M4（§5 `propose_selection(predicate | refs)`）であり、
それまでは「個数を変えたら押し出し直す」が正しい手順。

## M3の実装で決めたこと（契約の外側・次に触る人向け）

- **`parameters.rebuild` はログを2回走査する**。`set_parameter` はログの上では影響する
  図形や押し出しより**後ろ**に載るので、順番どおり再生すると「古い値で作った形状」が
  できてしまう。先に範囲内の `set_parameter` を走査して最終値の表を作り、各操作の直後に
  当てる。
- **`rebuild` は失敗したら文書を元へ戻す**（作り直しはログ全部の再生＝`import_step` の
  読み直しを含むので、値と無関係な理由で失敗しうる）。
- **`rebuild` はアクティブボディの選択を保つ**（選択はログに載らないため）。
- **`set_parameter` を含むログは `document.log` へ束ねてから `replay` すること**。
  外のログを直接 `replay(document)` すると、作り直しがどのログを見るべきか決められないので
  **例外で止まる**（黙って別物の文書を作らないため）。M0のREADMEが書いている
  `doc = Document(); doc.log = OpLog.from_json(...); doc.log.replay(doc)` が正しい使い方。

---

# M3.5 — パーツ分解・可視性・差分送信のAPI契約（2026-08-11追記）

`tests/test_import_split.py` / `tests/test_visibility.py` / `tests/test_incremental_update.py`
がM3.5の契約。次タスク（M3.5ロジックの実装）は**これを全部緑にすることだけ**を目標にする。
上のM0・M2・M3の節は当時のまま。

## 何を直しているのか（実機で測った問題・2026-08-10）

本人がM1のビューアでPCケースを操作した実測（`measure_log.jsonl`）:

| モデル | 面数 | kernel(OCCT) | 幾何更新 合計 |
|---|---|---|---|
| 小さい部品 | 57 | 42–48ms | 107–113ms |
| **PCケース** | **2895–2971** | **4326–5101ms** | **6602–7275ms** |

加えて**ピック失敗12回／押し出し7回（失敗率63%）**。所見は「押し出しをEnterしてから反映まで
5秒くらいかかる」「選択と押し出し自体は問題ない」「パーツごとの表示・非表示が実装されれば
すべての面を選択できるようになるはず」。

原因は `import_step` が **STEPを1つのBodyとして読んでいる**こと（`OneShape()` の compound を
そのまま `Body(...)` に入れていた）。`extrude_face` は2835面の塊全体にOCCT演算をかけ、更新の
たびに全面を測り直す。実際に触るのは1部品の数十〜233面だけである。
**性能の問題ではなくデータモデルの問題であり、ソリッドごとにBodyへ分ける1つの変更が
「選択できない」「遅い」「パーツを消せない」を同時に解く。**

## 現状（2026-08-11・テスト先行の段階）

**193件収集・収集エラー0・45 failed / 148 passed。**

- **M0〜M3の141件は1行も触っておらず、この3ファイルを除いた単独実行でも 141 passed。**
- 新規52件のうち **45件が赤・7件が緑**。赤の内訳は
  **44件が `AssertionError`／1件が `NotImplementedError`** で、`ImportError` や収集失敗に
  よる赤は0件。M2の節と違って「赤は全件NotImplementedError」とは書けない理由:
  分解は**既存の `import_step` の振る舞いそのものを変える**変更なので、スタブに差し替える
  ことができない（差し替えるとM0のテストが即座に赤くなり、「着手前は緑」を保てない）。
  多くのテストは「17個／27個のBodyがあること」という前提の検査で落ちており、これは
  収集事故ではなく**M3.5が変える対象そのもの**を指している。M3が
  `test_numbers_become_named_values_and_entities_hold_references` 等3件を
  `AssertionError` で赤にしたのと同じ位置づけである。
- **1件の `NotImplementedError`** は `test_a_client_that_knows_nothing_receives_every_body`
  （分解に依存せず `Session.delta` へ直接届くため）。
- 緑の7件は**分解前でも真になるもの**だけで、実装後も真であり続ける回帰ガードである:
  `test_split_preserves_the_total_face_count`（4モデル分。1 Bodyのときは自明に成立する）、
  `test_every_measured_model_is_split_into_its_solids[plate_1by1_24]`（単一ソリッド）、
  `test_split_bodies_are_reference_bodies`、
  `test_m35_modules_are_covered_by_the_import_boundary_scan`。

## モジュール一覧（M3.5で増える分）

| モジュール | 役割 | OCP直接import | build123d直接import |
|---|---|---|---|
| `duocad/selection.py` | 選択機構（何が選べるか。可視性を含む） | 禁止 | 禁止 |
| `duocad/verbs/visibility.py` | verb `set_visible` | 禁止 | 禁止 |

変更するモジュール: `duocad/io_step.py`（分解）、`duocad/server.py`（差分送信）、
`duocad/verbs/extrude_face.py`（不可視Bodyの拒否）。

## `duocad/io_step.py` — ソリッド分解

```python
import_step(doc: Document, path: str | Path) -> list[BodyId]      # 戻り値が変わる
```

**契約**:

- STEPの**SOLIDごとに1つの `Body`** を作る（`kind="reference"` は変わらない、`visible=True`）。
  数え方は `TopExp.MapShapes_s(shape, TopAbs_SOLID)`（重複除去）。
- **単一ソリッドでも1要素のリストを返す**。「1個のときだけ素のBodyIdを返す」にしない
  （戻り値の型が入力に依存すると、呼び出し側が必ず分岐を書く羽目になる）。
- 名前は既定で `<ファイル名(拡張子なし)>_1` … `_N`。**一意かつ非空**であることは名前の
  付け方によらない契約。STEP由来の部品名が容易に取れるなら使ってよいが、
  **そのためにXDE/XCAFへ移行しない**。
- **操作ログは1行**（`verb="import_step"`,
  `args={"path": str(path), "body_ids": [BodyId, ...]}`）。undo 1回で全Bodyがまとめて消え、
  redo/replay は**記録したIDをそのまま使って**同じ順序で復元する。
- SOLIDが1つも無いSTEP（面・シェルだけ）は、形状全体を1つのBodyにする（0個のBodyを
  黙って作らない）。**この経路のテストは無い**（手元の4モデルは全てSOLIDを持つ）ので、
  実装者の判断として残す。

### 実装時に**書き換えが要る既存テスト**（重要・このタスクでは触っていない）

戻り値が `BodyId` → `list[BodyId]`、ログの `args` が `body_id` → `body_ids` に変わるため、
下記は実装と同じコミットで直す必要がある。**この一覧は2026-08-11に呼び出し箇所を実際に
数えたもの**で、テストを先に書いた時点では1行も触っていない（着手前・完了時とも141件は緑）:

| ファイル | テスト／行 | 何が起きるか |
|---|---|---|
| `test_io_step.py` | `test_import_step_face_breakdown_matches_measured_reference` | `next(b for b in doc.bodies if b.id == body_id)` がリスト比較になる。面56は**17 Bodyの合計**として数え直す |
| `test_io_step.py` | `test_import_step_creates_reference_body` | 同じ `next(...)` の形。全Bodyの `kind` を見る形へ |
| `test_io_step.py` | `test_import_step_logs_path_and_body_id_in_args` | `args["body_id"]` → `args["body_ids"]` |
| `test_io_step.py` | `test_export_step_roundtrip_preserves_face_count` | `== 56` は成立しない（activeは最後の1ソリッド＝3〜4面） |
| `test_io_step.py` | `test_export_step_roundtrip_preserves_volume` / `test_export_stl_smaller_deviation_yields_larger_file` / `test_export_stl_works_from_reference_body` | 書き出し対象が「最後のソリッド」に静かに変わる。M0の節が申し送っていた `export_step(doc, path, body_ids=None)` を入れるのが自然なタイミング |
| `test_oplog.py` | `test_undo_reverts_document_state`(48行) / `test_redo_reapplies_undone_operation`(61行) / `test_redo_does_not_record_the_operation_twice`(81行) | `len(doc.bodies) == 1` → 17（3件とも） |
| `test_extrude_face.py` | `session` フィクスチャ（38–40行）、`len(document.bodies) == 2`（57行）、`reference_id`（58/71/114/352行）、**`face_count == 56`（62/121/367行）** | `make_editable_copy(document, reference_id)` にリストが渡る。`len == 2` → 18。分解後は1ソリッド3〜4面なので56面の前提が全部崩れる |
| `test_m2_regressions.py` | 161–172行 | `created.id != reference_id`（リスト比較）、`len(document.bodies) == 2` → 18 |
| `test_m3_regressions.py` | 48–50行 | `make_editable_copy(document, reference, ...)` にリストが渡る |
| `duocad/server.py` | `Session.load_step`（122–123行） | テストではないが同じ理由で壊れる。`make_editable_copy(self.document, reference_id)` にリストが渡る |

`web/`（アプリ層）も `state` の形が変わるので追随が要る。**パーツ一覧パネルは次々タスクの
範囲**だが、既存のビューアが動かなくなる点は認識しておくこと。

## `duocad/verbs/visibility.py` — 可視性のverb

```python
set_visible(doc: Document, body_id: BodyId, visible: bool) -> Operation
```

- `args={"body_id":.., "visible": bool, "previous": bool}`。`previous` は undo が
  **元の値へ**戻すため（M3の `set_parameter` と同じ流儀）。これが無いと undo は
  「Trueに戻す」しかできず、元から不可視だったBodyを勝手に表示してしまう。
- 存在しない `body_id` は **`KeyError`**、documentにもログにも変更を残さない
  （`Document.is_editable` / `set_active_body` / `remove_body` と揃える）。
- **アプリ層のチェックボックスとして作らない。** コアのverbにすればログに載り、undoでき、
  M4で自動的にMCPから使える（実装計画書§2「コアはUIが無くても機能的に完全」）。

## `duocad/selection.py` — 可視性は選択機構の一部

```python
selectable_bodies(doc) -> list[BodyId]                       # visible なBodyだけ・bodies順
select_face(doc, body_id, face_index) -> dict                # §3.5 の ShapeRef
```

- ShapeRef の形は `{"body": BodyId, "kind": "face", "index": int, "provenance": None}`。
- `select_face` の失敗: 存在しないBody → `KeyError`、`visible=False` → `ValueError`、
  1..`face_count` の外 → `ValueError`。
- **`extrude_face` も不可視Bodyを拒否する**（`ValueError`・文書は不変）。
  「見えないものは指せない」がアプリ層だけの約束だと、MCP（M4）や再生経路から
  画面に出ていない部品を黙って変形できてしまう。実機の失敗率63%の直接の対処がこれである。

## `duocad/server.py` — 差分送信

```python
Session.state() -> {
    "units", "active_body_id",
    "bodies": [{"body_id","name","kind","visible","revision",
                "face_count","volume","geometry": <tessellate結果 | None>}, ...],
    "log", "can_undo", "can_redo", "applied_count", "measure_log",
}
Session.body_revision(body_id) -> int      # 形状の差し替え**と可視性の変更**で増える
Session.geometry(body_id) -> dict          # キャッシュ付き。**同じオブジェクトを返す**
Session.delta(known_revisions) -> {"bodies": [<上と同じ形・版が変わった分だけ>],
                                   "unchanged": [BodyId, ...]}
Session.extrude(face_index, distance, client_op=None, body_id=None) -> applied
                                           # applied["changed_bodies"] = [BodyId, ...]
```

**契約**:

- `Session.load_step` は**ソリッドの数だけ押し出せるBody（`kind="solid"`）**を作る。
  参照ボディをどう扱うか（残す／隠す／作らない）は**実装の自由**とし、テストは
  「1ソリッドにつき1つ押し出せるBodyがある」ことだけを縛る。
- **同時に見える（`visible` かつ `geometry` を持つ）Bodyは1ソリッドにつき1つ**。
  参照ボディと編集用コピーの両方を可視のまま出すと、同じ形が二重に測られて二重に送られ、
  手前の部品を消しても参照側のメッシュが残ってピック失敗が再発する。
- **不可視のBodyは測らない**。`state()` / `delta()` は `geometry: None` を出し、
  `Session.geometry()` は **`ValueError`** を送出する（低い層を直接呼ぶ実装が隠したBodyを
  測り続ける穴を、入口の側で閉じる）。テセレーションはこの系で最も高い処理
  （PCケース全体で892.5ms）で、参照ボディを隠して保持する実装を選んでも費用が増えない
  ための逃げ道でもある。
- **版（`body_revision`）は形状の差し替えと可視性の変更のどちらでも増える。**
  形状だけの版にすると、`set_visible` したBodyは `delta()` で `unchanged` に入り、
  **非表示にしたことがクライアントへ一度も届かない**（＝このマイルストーンの目的が
  差分プロトコルの上で成立しない）。undoで形状が戻った場合も同じ経路で再送される。
- **テセレーションの再利用は版ではなく「形状」で決まる。** `geometry()` は形状が変わらない
  限り**前回と同じオブジェクト**を返す（可視性を切り替えただけで測り直さない）。
  「同じ内容を作り直す」は再利用ではない（費用が変わらない）ので、テストは `is` で縛り、
  加えて `kernel.tessellate` の**呼ばれた回数**でも縛る（押し出し1回につき1回）。
- `delta()` で、クライアントが知っているのに `bodies` にも `unchanged` にも現れないIDは
  **文書から消えた**ことを意味する（専用の `removed` は持たない）。

## 実測値の出どころ（すべて2026-08-11にOCPで直接測った）

タスクファイル記載の値を独立に再測し、**全て一致**した。

| 値 | 測り方 |
|---|---|
| PC_case_20251122.step: **27ソリッド / 2835面 / 最大233・中央94・最小6** | `TopExp.MapShapes_s` の SOLID と FACE（どちらも重複除去） |
| shelf_attachment_GD.step: **17ソリッド / 56面** | 同上 |
| monitor_attachment_for_24inch_GD.step: **26ソリッド / 92面** | 同上 |
| plate_1by1_24.step: **1ソリッド / 74面** | 同上（単一ソリッドが壊れないことの確認用） |
| 233面ソリッド1個の `extrude_face_on_shape`: **面1 726.0ms / 面2 517.7ms / 面3 757.9ms** | `time.perf_counter()`。94面ソリッドなら 169.5 / 203.2 / 244.8ms |
| 27ソリッド全部のテセレーション **892.5ms**、STEP読み込み **1272.6ms** | `kernel.tessellate(solid, 0.2)` を27回 |
| 全Body分のJSON **6,003,765B**、1Bodyで最大 **600,764B（10.0%）** | `len(json.dumps(...))` |

**閾値の決め方**（推測ではなく上の実測から引いている）:

- 押し出し **1000ms未満** … タスクファイル指定。分解前の4326–5101msに対して4〜5倍の余裕だが、
  **実測726msに対しては約1.4倍しかない**。落ちたときは「閾値が厳しすぎる」ではなく、
  実測値と比べてどれだけ悪化したかを見ること。
- 再送データ量 **全Body分の1/4未満** … 実測の最悪値10.0%に対して2.5倍の余裕。

**計測値はテスト出力に必ず出す**（タスクファイルの要求「閾値の合否より数値の推移が重要」）。
`print` は通ったテストでは既定で表示されないため、`MeasurementReport`（`UserWarning` の
サブクラス）で **warnings summary** に出す。`-s` を付ければ `print` 側でも読める。

## テスト一覧とタスクファイル項番の対応（M3.5）

| タスクファイルの項番 | テスト |
|---|---|
| 1（27個のBody） | `test_import_split.py::test_pc_case_is_split_into_27_bodies`, `::test_every_measured_model_is_split_into_its_solids` |
| 2（面数の合計2835） | `test_import_split.py::test_split_preserves_the_total_face_count`, `::test_pc_case_face_counts_per_body_match_the_measured_spread` |
| 3（単一ソリッド） | `test_import_split.py::test_a_single_solid_step_still_yields_exactly_one_body` |
| 4（独立した visible） | `test_import_split.py::test_each_body_has_its_own_visible_flag` |
| 5（一意のidと名前） | `test_import_split.py::test_split_bodies_have_unique_ids_and_non_empty_names`, `::test_split_body_names_are_the_file_stem_with_a_1_based_index`（＋`::test_split_bodies_are_reference_bodies`） |
| 6（ログ1行・undoで全消し） | `test_import_split.py::test_the_split_import_is_a_single_operation`, `::test_undo_removes_every_body_of_the_split_import`, `::test_redo_restores_every_body_with_the_same_ids`, `::test_replay_reproduces_every_body_with_the_same_ids` |
| 7（set_visible が visible を変える） | `test_visibility.py::test_set_visible_hides_one_body_and_leaves_the_others`, `::test_set_visible_can_show_a_hidden_body_again` |
| 8（ログとundo） | `test_visibility.py::test_set_visible_is_logged_with_the_previous_value`, `::test_set_visible_is_undoable`, `::test_undo_of_set_visible_restores_a_body_that_was_already_hidden`, `::test_set_visible_survives_redo_and_replay` |
| 9（存在しないbody_id） | `test_visibility.py::test_set_visible_rejects_an_unknown_body_and_changes_nothing` |
| 10（不可視は選択対象外） | `test_visibility.py::test_selectable_bodies_lists_every_visible_body_in_order`, `::test_hidden_bodies_are_not_selectable`, `::test_select_face_returns_a_shape_ref_for_a_visible_body`, `::test_select_face_refuses_a_hidden_body`, `::test_select_face_rejects_an_unknown_body_and_an_out_of_range_index`, `::test_extrude_face_refuses_a_hidden_body`, `::test_redo_of_an_extrusion_refuses_a_hidden_body` |
| 11（変更のあったBodyだけ再送） | `test_incremental_update.py::test_only_the_changed_body_is_resent_after_an_extrusion`, `::test_a_client_that_knows_nothing_receives_every_body`, `::test_the_revision_of_an_untouched_body_does_not_move`, `::test_hiding_a_body_is_delivered_as_a_delta`, `::test_showing_a_body_again_is_delivered_as_a_delta`, `::test_undo_and_redo_of_set_visible_are_delivered_as_deltas`, `::test_undo_resends_the_restored_body` |
| 12（テセレーションの再利用） | `test_incremental_update.py::test_unchanged_bodies_reuse_their_tessellation`, `::test_tessellation_is_not_recomputed_for_unchanged_bodies`, `::test_showing_a_body_again_does_not_retessellate_it` |
| 13（1部品1000ms未満） | `test_incremental_update.py::test_extruding_one_part_of_the_pc_case_stays_under_1000ms`, `::test_pc_case_part_sizes_stay_within_the_measured_spread` |
| 14（再送データ量） | `test_incremental_update.py::test_the_delta_after_an_extrusion_is_much_smaller_than_the_full_state` |
| 前提（サーバの状態の形） | `test_incremental_update.py::test_load_step_makes_one_editable_body_per_solid`, `::test_load_step_does_not_show_the_same_solid_twice`, `::test_state_lists_every_body_separately`, `::test_hidden_bodies_are_not_tessellated`, `::test_geometry_refuses_a_hidden_body` |
| 境界（新規ファイル） | `test_visibility.py::test_m35_modules_are_covered_by_the_import_boundary_scan` |

## M3.5の決定（仕様書から導かれないもの・M0の「export対象」と同じ位置づけ）

1. **`import_step` の戻り値を `list[BodyId]` にした**（単一ソリッドでも1要素のリスト）。
   入力によって型が変わるAPIは呼び出し側に必ず分岐を強いる。代償は上の「書き換えが要る
   既存テスト」で、これは**契約の変更に伴う正当な代償**であって、テストを緩めた結果ではない。
2. **不可視のBodyは押し出せない**（項番10を `extrude_face` まで広げた）。項番10の文面は
   「選択の対象にならない」だが、選択機構の一部であるなら**選択を受け取るverbも同じ判定に
   従う**のが筋である。ここを緩めると、画面から消したつもりの部品がM4のMCPから黙って変形する。
3. **`Session.load_step` の参照ボディの扱いは縛らない**。縛るのは「1ソリッドにつき1つ
   押し出せるBodyがある」ことだけ。参照を残す／隠す／作らないは、**不可視のBodyを
   テセレーションしない**という契約があれば費用に差が出ない。
4. **面の添字をテストに決め打ちしない**。`first_extrudable_face()` で「最初に押し出せる面」を
   探す。決め打ちにすると、分解の順序が変わっただけでテストが**別の面を押している**状態に
   静かに退化する（`extrude_face_on_shape` は入力の形状を変更しないので試行を繰り返せる）。
5. **`delta` に `removed` を持たせない**。`bodies` と `unchanged` の和集合が現在の全Bodyなので、
   クライアントが知っているのにどちらにも無いIDが「消えた」を意味する。テストで裏取り
   できない項目を契約に足さないための選択。

## M3.5で**契約にしなかった**こと（範囲の明示）

- **パーツ一覧パネル（UI）** … 次々タスク。ここはコアとサーバまで。
- **MCPツールとしての登録** … M4。ただし `set_visible` をコアのverbにしてあるので、
  M4では自動的に使えるようになる。
- **STEP由来の部品名** … 使ってよいが必須にしない（XDE/XCAFへの移行はしない）。
- **`export_step` の複数ボディ対応** … M0の節が申し送ったまま未解決。分解によって
  「activeの1ソリッドだけ書き出す」が実質的な後退になるため、実装時に併せて判断すること。

## M3.5のQAで足した契約（2026-08-11・cursor のレビュー指摘を反映）

レビューは3ラウンド。1回目に major 3件・minor 6件、2回目に major 1件・minor 1件、
3回目に major 1件の指摘があり、**全件を反映した**（テストは44件→52件）。
指摘のうち3件は「全部緑にしても実問題が残る」種類のもので、契約側の誤りだった:

- **major: 可視性が差分に載らない** … 初版は「版は形状が変わったときだけ増える」と決めて
  いたが、`delta()` は版の不一致だけを送るので、`set_visible` の結果がクライアントへ
  **一度も届かない**。パーツを消して奥の面を選ぶという目的そのものが差分の上で成立して
  いなかった。版を「形状**または**可視性」に直し、
  `test_hiding_a_body_is_delivered_as_a_delta` と
  `test_showing_a_body_again_does_not_retessellate_it`（版と再利用の判定を分ける）を足した。
- **major: 参照ボディと編集用コピーの二重表示** … 「1ソリッドにつき1つ押し出せるBody」
  しか縛っておらず、両方を可視のまま出す実装を許していた。同じ形が二重に測られ二重に
  送られ、手前を消しても参照側のメッシュが残る。
  `test_load_step_does_not_show_the_same_solid_twice` を足した。
- **major: undo後の再送とキャッシュ無効化が未契約** … 差分を押し出し方向にしか見ておらず、
  undoしても画面が押し出し後のまま残る経路が緑のまま通る（M3で実際に起きた事故と同型）。
  `test_undo_resends_the_restored_body` を足した。
- **minor** … `_do_extrude_face`（redo/replay経路）の可視性判定
  （`test_redo_of_an_extrusion_refuses_a_hidden_body`）、`Session.geometry()` の不可視Body
  に対する挙動（`test_geometry_refuses_a_hidden_body`）、不可視Bodyを実際に測っていない
  ことの検査（`test_hidden_bodies_are_not_tessellated` に `kernel.tessellate` の spy を追加）、
  上の「書き換えが要る既存テスト」の表の漏れ3件（`test_extrude_face.py` の56面前提・
  `test_oplog.py` の3件目・`server.py::load_step`）、性能テストの計測区間の書き分け。

2回目のレビューで追加された分:

- **major: 再表示が差分に載らない** … 1回目の修正で「非表示」は届くようになったが、
  **戻す方向を縛っていなかった**ため、「隠したときだけ版を上げる」実装でも全テストが通り、
  消したパーツを画面へ戻せないまま緑になる。
  `test_showing_a_body_again_is_delivered_as_a_delta` を足した。
- **minor: redo拒否の対称性** … `test_redo_of_an_extrusion_refuses_a_hidden_body` が
  「拒否される」ことしか見ておらず、可視に戻せば同じredoが通ることを確認していなかった
  （＝そもそも押せない面を選んでいた場合でも緑になる）。後半に足した。

3回目のレビューで追加された分:

- **major: `set_visible` の undo/redo が差分に載らない** … 版を「verbを適用したとき」だけ
  上げる実装（`_undo_set_visible` は `previous` に戻すだけ）でも全テストが通り、undoしても
  画面から消えたままになる。`test_undo_and_redo_of_set_visible_are_delivered_as_deltas`
  を足した。

**この3ラウンドで同じ形の穴が経路を変えて3回出た**（非表示→再表示→undo/redo）。
根本は「版を経路ごとに数え上げる」という発想そのものなので、`Session.body_revision` の
docstringに **版は経路ではなく状態（`(形状オブジェクトの同一性, visible)` の組）から導くこと**
と明記した。経路の数え上げを続ける限り、次のverbでまた同じ穴が開く。

**QAの実施者について（2026-08-11の実情）**: `gpt-5.6-sol-high` と `gpt-5.6-terra-high`
（cursorの個別モデル指定枠）は当日どちらも使用量制限に達していて呼べず、**cursor の
auto 枠**でレビューした。auto は Composer/Grok 系へルーティングされるため「実行者
（Claude）と別モデル」という条件は満たしている。

---

# M4（最小の先取り） — MCPと上り方向のAPI契約（2026-08-11追記）

**AIが「前面パネルを隠して」と言えるようにする**ためだけの最小の先取り。M4本体（選択の
提案・修正＝`propose_selection` / `modify_selection` / `apply`）は**範囲外**で、
`tests/test_mcp.py::test_tools_list_is_exactly_the_minimal_set` がそれを機械的に固定する
（ツールが3つより増えたら赤になる）。

## モジュール一覧（M4で増える分）

| モジュール | 役割 | OCP直接import | build123d直接import |
|---|---|---|---|
| `duocad/mcp_server.py` | MCPサーバ（stdio・JSON-RPC 2.0）とツール3つ | 禁止 | 禁止 |

接続方法（Claude Code / Claude Desktop への登録）は `MCP.md` が正。

## `duocad/mcp_server.py` — 公開するツールは3つだけ

    describe_document(include_references=False, include_bbox=False)
        ボディ一覧（body_id・名前・種別・可視性・面数・体積）とパラメータ一覧。
        **既定では参照ボディ（kind="reference"）を出さない** — STEPを読むとソリッドごとに
        「参照（隠されている）」と「編集用コピー」の対ができるので、そのまま見せると同じ部品が
        2つ並び、隠しても画面が変わらない方をAIが掴む。隠した件数は `reference_count` で必ず返す。
    set_visible(body_id, visible)
        コアの verb `set_visible` を経由する（`Session.set_visible`）。操作ログに載り、undoで戻る。
    describe_shape(body_id)
        `query.describe_shape` の値 ＋ 体積 ＋ `kernel.bounding_box`。読み取りのみ。

**層の位置づけ**: MCPサーバは**アプリ層ではなくコアのもう一人のクライアント**。ブラウザと
MCPは同じ `Session` を叩く対等なクライアントで、MCPはブラウザを操作しない。

**プロトコル**: 公式SDK（`mcp`）を入れず素で実装した（依存を4つに固定しているため。理由は
`duocad/mcp_server.py` のdocstring）。したがって**プロトコルの正しさはテストでしか担保されない**
——`initialize` の版交渉・通知に返事を書かない・バッチ拒否・`tools/call` の引数検査・
実行時エラーを `isError` で返すこと、を `tests/test_mcp.py` が固定する。

**stdoutに出してよいのはJSON-RPCの行だけ**（起動メッセージ・警告はstderr）。
`test_the_process_starts_and_answers_over_real_stdio` が実プロセスを起動して、stdoutの全行が
JSON-RPCとしてパースできることを見る。1行汚すだけでMCPクライアントは繋がらない。

## `duocad/server.py` — 上り方向（コアが発火し、アプリ層が購読する）

    Session.add_listener(callback)   文書が変わったら callback(applied) で知らせる
    Server.bind_loop(loop)           配信に使うイベントループを覚える（起動時に呼ぶ）

**これが無いと交互操作は原理的に成立しない**（実装計画書§2「外してはいけない3つ」の2番目）。
MCPからAIが可視性を変えると、`Session` が発火 → `Server` が購読 → 接続している全ブラウザへ
配信 → 画面が描き直る。契約は3つ:

1. 1つの操作につき購読者へ**1回だけ**届く（`test_core_emits_one_event_per_mutation`）
2. WebSocket経由の操作は `_dispatch` が自分で配信するので、購読者側は配らない
   （`test_websocket_commands_are_not_broadcast_twice`。二重配信すると `applied` の付いた
   配信と付かない配信が交錯して画面側の相関が壊れる）
3. MCPからの変更に `applied`（操作元への確認応答）を**付けない**。ブラウザは
   `applied.client_op` を自分の計測と突き合わせるので、他人の操作に付けると知らない
   `client_op` を自分のものとして拾う経路ができる

購読者が例外を投げても**操作は成功のまま**にする（確定した文書の変更を通知の失敗で
巻き戻すことはできない）。落ちたことは警告として画面へ出す。

## `duocad/kernel.py` — M4で増える分

    bounding_box(shape) -> {"min","max","center","size","diagonal"}
        **幾何から**求める（`useTriangulation=False`）。既定のTrueだと既存の三角形分割を使うため、
        `tessellate` を呼んだ後だけ違う値が返るという再現性の無い挙動になる
        （`test_bounding_box_does_not_depend_on_tessellation` が固定）。
        `SetGap(0.0)` を入れてもOCCTは内部許容差ぶん（実測1e-7mm）膨らませるので、
        厳密な寸法の根拠には使わない。

## テスト一覧とタスクファイル項番の対応（M4）

| 項番（タスクファイル） | テスト |
|---|---|
| 完了条件1（MCPクライアントから呼べる） | `test_the_process_starts_and_answers_over_real_stdio` ほかプロトコル群 |
| 完了条件2（コアのverb経由・ログ・undo） | `test_set_visible_goes_through_the_core_verb` / `test_set_visible_is_undoable` |
| 完了条件3（ブラウザに反映される） | `test_mcp_change_reaches_every_connected_websocket_client` ＋ `web/scripts/smoke-mcp.mjs` |
| 完了条件4（接続方法の文書） | `MCP.md` |
| 検証1〜4（実機・27部品） | `web/scripts/smoke-mcp.mjs`（実ブラウザ・実MCP） |

## 実行方法（M4）

```
.venv\Scripts\python.exe -m pytest tests/test_mcp.py -q      # 契約（軽いSTEPだけ使う）
cd web && node scripts/smoke-mcp.mjs                          # 実機（PCケース27部品・実ブラウザ）
```

`smoke-mcp.mjs` は自分で `python -m duocad.mcp_server` を起動する（**先に `duocad.server` を
立てない**。ポートがぶつかる）。既定は8865/8866で、本人が実機で開いている8765/8766を避けてある。

## M4のQAで足した契約（2026-08-11・cursor auto のレビュー指摘を反映）

QAは `gpt-5.6-sol-high` → `gpt-5.6-terra-high` の順で試したが**どちらも月次の使用量上限**
（8/19リセット）で呼べず、**cursor の auto 枠**でレビューした。auto は Composer/Grok 系へ
ルーティングされるため「実行者（Claude）と別モデル」の条件は満たしている。major 3件・
minor 7件のうち、対応したのは以下（残りは理由をつけて据え置き、`duocad/mcp_server.py` の
docstringに明記した）。

- **major: `_push` の `state` 使い回しによる新旧の逆転** … M3.5のQAが「単一ユーザーなので
  顕在化しない・M4で再訪」として見送っていたレースが、**MCPで「人とAIが同時に操作する」が
  実在の経路になった**ため本番化した。`state` を**ロックの中で送る直前に作る**ように変更
  （送る値が常にその瞬間の状態になるので、古い状態が後から上書きすることが原理的に起きない）。
  `state` と `delta` の食い違いも同時に消えた。
- **major: 配信タスクの投げっぱなし** … `create_task` の戻りを捨てるとGCされうるうえ、
  配信中の例外を誰も回収しないので「ブラウザは古いまま・警告も出ない」という**完了条件3が
  静かに壊れる形**になる。`Server._spawn_push` で保持し、失敗を警告として残すようにした。
- **major: 参照ボディの可視性をAIが触れた** … 表示に戻すと同じソリッドが二重に描かれ、
  M3.5が塞いだ状態がAI経路だけで復活する。`set_visible` が `kind=="reference"` を拒否する
  （`test_set_visible_refuses_reference_bodies`）。
- **minor: `can_undo` の食い違い** … MCPが生の `log.can_undo` を返していたため、土台しか
  ログが無い状態で「戻せる」と答え、実際に呼ぶと拒否される。`Session.can_undo` を新設して
  `state_meta()` とMCPの答えを1つの出どころに揃えた（`test_can_undo_agrees_with_the_session`）。
- **minor: テストが実装に追従して緑になる箇所** … プロトコル版を実装から import していたのを
  テスト側のリテラルに変更（版を削っても緑、を防ぐ）。「全クライアントへ届く」テストが接続を
  1本しか張っておらず**2本目に届かない実装でも緑**だったので2本に増やした。
  可視性をフィクスチャが直接代入して文書とログをずらしていたのを、**テストごとに Session を
  作り直す**方式に変更。`Session.remove_listener` を足して購読の後始末ができるようにした。

**据え置いた指摘と理由**（`duocad/mcp_server.py` docstring「既知の割り切り」）:
`notifications/initialized` を `tools/*` の前提条件にしない（厳格化しても弾けるのは行儀の
悪いクライアントだけで、繋がらない失敗を新たに作る）／ツール実行がイベントループを塞ぐ
（既存の `Session.extrude` と同じ性質。実行器スレッドへ逃がすと `Server` のスレッド前提を
作り直すことになるので、M4本体で交互操作を作るときにまとめて再訪する）。

---

# 3verb前倒し（フィレット・面取り・円弧） — テストが定義する契約（2026-08-11追記）

v1.1／v1.2に置いていた3verbを前倒しした（2026-08-11 本人決定）。**このタスクはテストだけ**で、
実装は次の2タスク（`fillet-chamfer-logic` / `arc-logic`）が行う。**赤で終わるのが正常**。

## 現状（2026-08-11・テスト先行の段階）

- 新規3ファイル **32件が赤・4件が緑**、赤は**全件 `NotImplementedError`**（`--tb=line` で
  例外種別を数えて確認済み。`ImportError`・収集エラーは0件）。
- 緑の4件は**実装に依存しない実測の記録**: 円弧が `ENTITY_KINDS` に入っていること、
  積層プレート74面の内訳、PCケースの脚5面の内訳、アロマ拡散器の部分円筒6面。
  これらは「何が要るか」の到達点であり、実装が進んでも意味が変わらない。

## なぜ前倒したのか（実測）

| モデル | ソリッド | 面 | 足りないもの |
|---|---|---|---|
| PCケースの脚 `PC_case_feet.step` | 1 | 5（平面3 + 全周円筒1 + 円錐1） | 面取りだけ |
| 積層プレート `plate_1by1_24.step` | 1 | 74（平面18 + 部分円筒28 + 円錐8 + トーラス12 + bspline8） | 面取り・フィレット |
| アロマ拡散器 `aroma_oil_diffuser.step` | 4 | 352（平面338 + 全周円筒8 + 部分円筒6） | 円弧 |
| PCケース `PC_case_20251122.step` | 27 | 2835 | フィレット |

（2026-08-11に `query.describe_shape` で測り直した値。プレートの内訳はタスクファイル記載の
「その他8」が `bspline` 8 であることまで確認した。）

## モジュール一覧（この3verbで増える分・すべてスタブ）

| モジュール | 役割 | OCP直接import | build123d直接import |
|---|---|---|---|
| `duocad/verbs/fillet.py` | verb `fillet` | 禁止 | 禁止 |
| `duocad/verbs/chamfer.py` | verb `chamfer` | 禁止 | 禁止 |

`duocad/sketch.py` に `arc`（`ENTITY_KINDS` と `add_arc`）、`duocad/kernel.py` に
`edge_count` / `classify_edge` / `edge_of` / `fillet_edges` / `chamfer_edges`、
`duocad/query.py` に `describe_edges` を足した（幾何とOCPの境界は従来どおり）。

## API契約

    fillet(doc, edges, radius, body_id=None) -> Operation
    chamfer(doc, edges, distance, body_id=None) -> Operation
        `edges` は**1始まりの辺の添字**のリスト（面の添字と同じ流儀）。
        `radius`/`distance` は float（全辺同じ）か辺ごとのリスト（長さが合わなければ ValueError）。
        ログに `args={"edges": [...], "radius"|"distance": ..., "body_id": ...}` を積む。

    query.describe_edges(shape) -> [{"index","kind","length","start","end"}, ...]
        `kind` は "line" | "circle" | "other"。**辺の選択は問い合わせ層越しに行う**
        （verbが build123d を直接呼ばない。§6予防4）。**端点まで返す**のは
        「縦の4辺だけ丸める」のような選び方が端点なしでは表現できず、添字の決め打ちに
        退化するため（形が変われば別の辺を指す）。

    sketch.add_arc(doc, sketch_id, center, radius, start_deg, end_deg) -> EntityId
        **中心・半径・角度**で持つ（3点で受け取らない）。§3.4「パラメトリック・プリミティブ」と
        §6予防5に従い、半径・角度を後から `set_parameter` で編集できる形にするため。
        3点で持つと円弧だけが「数値を持たない図形」になる。
        `params` は `cx` / `cy` / `radius` / `start_deg` / `end_deg`（度・反時計回り・X軸から）。

## 実測値の出どころ（2026-08-11に測った。推測で書いていない）

20mm立方体・全辺フィレット（duocadの語彙 = `faces_by_type`）:

| 半径 | IsDone | 面の内訳 |
|---|---|---|
| 3.0 | True | 平面6 / 部分円筒12 / other(球)8 = 26面 |
| 10.0 | **False** | （例外を投げずに黙って失敗） |
| 10.5 | True | 平面6 / 部分円筒12 / bspline7 / other(球)1 = 26面 |
| 30.0 | **False** | |
| 1.0〜3.75をバラバラ | True | 平面6 / 部分円筒12 / bspline8 = 26面 |

- **球は `other` に落ちる**（`FACE_KINDS` に球は無い）。タスクファイルの「円筒12+球8+平面6」を
  duocadの語彙へ翻訳したのがこの表であり、**`FACE_KINDS` に球を足すのは範囲外**
  （足すと既存の内訳の意味が変わる）。
- 貫通穴（r=4）の円い辺2本に r=1.5 → 平面6 / 全周円筒1 / **トーラス2** = 9面。
  **まっすぐな辺は円筒、円い辺はトーラス**になる。
- 半円（r=5・180°）+ 弦を3mm押し出す → **部分円筒1**（U範囲180度）/ 平面3 = 4面。
- 1辺だけフィレットしたときの面の添字の動き: 1→1 / 2→7 / 3→6 / 4→4 / 5→5 / 6→2
  （2026-08-07には i=5→9 の観測）。**生の添字を保存してはいけない**（§7）の実行可能な根拠。
- `BRepFilletAPI_MakeFillet` は `History()` を**持たない**（`Modified`/`Generated`/`IsDeleted`
  は持つ）。`kernel.normalize_provenance` に通すと `modified`/`generated`/`deleted` が埋まる
  ことを実測で確認した。ブーリアンは `History()` を持つので取り出し方が違う ＝ アダプタの存在理由。

## タスクファイルの項番8を実測で訂正した

タスクファイルは「**箱のエッジに面取りをかけると円錐面ができる**こと」と書いているが、実測は:

    箱のまっすぐな辺を面取り（2mm） -> 平面7・面7   ← 平面が1枚増えるだけ。**円錐にならない**
    円筒の円い縁を面取り（1mm）     -> 平面2 + 全周円筒1 + **円錐1**・面4

円錐は**円い辺**から生まれる。タスクファイル自身の「推測で書かない・その場で測ってから書く」に
従い実測どおりに書いた。項番8の意図（面取りが新しい面を作る）は
`test_chamfering_a_straight_edge_makes_a_plane`、項番9の意図（実物の円錐が作れる）は
`test_chamfering_a_circular_edge_makes_a_cone` が引き受ける。

## 既存テストを1件だけ書き換えた（理由）

`tests/test_sketch.py::test_entity_kind_is_validated` は「知らない種別」の例として `"arc"` を
使っていた（M2時点では円弧はv1.2の範囲外だったため）。**前倒しで arc が有効になったので
例を `"spline"` に差し替えた**。見ている契約（登録されていない種別を拒否する）は変えていない。

## テスト一覧とタスクファイル項番の対応

| 項番 | テスト |
|---|---|
| 1（円筒+球+平面） | `test_fillet.py::test_fillet_on_a_box_makes_cylinders_spheres_and_planes` / `..._vertical_edges_only` |
| 2（半径バラバラでspline） | `test_varied_radii_produce_bspline_faces` |
| 3（円い辺でトーラス） | `test_fillet_on_a_circular_edge_produces_torus` |
| 4（失敗を黙って通さない） | `test_a_failing_fillet_raises_and_leaves_nothing_behind[10.0/30.0]` / `test_the_failure_is_not_monotonic_in_radius` |
| 5（r=10.5は成功するが歪） | `test_radius_10_5_succeeds_but_the_result_is_odd` |
| 6（ログ・undo） | `test_fillet_is_logged_and_undoable` |
| 7（由来の回収） | `test_fillet_records_provenance_through_the_normalizing_adapter` |
| 8（面取りで新しい面） | `test_chamfer.py::test_chamfering_a_straight_edge_makes_a_plane`（**実測で訂正**） |
| 9（実物の脚の構成） | `test_chamfering_a_circular_edge_makes_a_cone` / `test_the_real_foot_needs_a_cone` / `test_chamfer_can_produce_every_face_kind_the_real_foot_needs` |
| 10（面取りも黙って失敗しない） | `test_a_failing_chamfer_raises_and_leaves_nothing_behind` / `test_chamfer_rejects_an_unknown_edge_index` |
| 11（ログ・undo） | `test_chamfer_is_logged_and_undoable` / `test_chamfer_records_provenance_...` |
| 12（円弧を追加できる） | `test_arc.py::test_arc_is_a_sketch_entity_kind` / `test_add_arc_stores_named_numbers` / `test_add_arc_is_logged_and_undoable` / `test_arc_radius_can_be_edited_afterwards` / `test_broken_arcs_are_refused` |
| 13（円弧を含む閉領域） | `test_a_region_closed_by_an_arc_and_a_line_is_detected` / `test_an_open_arc_alone_does_not_close_a_region` |
| 14（部分円筒になる） | `test_extruding_an_arc_region_makes_a_partial_cylinder` / `test_the_partial_cylinder_spans_the_arc_angle` |
| 15（ログ・undo） | `test_arc_survives_a_log_round_trip` |
| 16（板→穴→フィレット） | `test_plate_then_hole_then_fillet` |
| 17（添字がずれる） | `test_fillet_shifts_face_indices` |

## この前倒しで**契約にしなかった**こと（範囲の明示）

- **円形反復**（`pattern_circular`）は範囲外のまま（今回の前倒しに含まれていない）。
- **可変半径フィレット・面取りの角度指定**などの拡張は入れない。
- **受入テストの実物への差し替えは行わない** — 本人の判断事項。次々タスクの引継ぎに
  「受入テスト再定義待ち」と書く。脚と寸法まで同じ形を組むテストを書かなかったのもこれが理由で、
  ここで固定したのは「必要な面種がverbで作れること」までである。
- `FACE_KINDS` に球（sphere）を足さない。

## この前倒しのQAで足した契約（2026-08-11・cursor auto のレビュー指摘を反映）

QAは `gpt-5.6-sol-high` → `gpt-5.6-terra-high` の順で試したが**どちらも月次の使用量上限**
（8/19リセット）で呼べず、**cursor の auto 枠**でレビューした（実行者Claudeとは別モデル）。
major 4件・minor 7件。対応:

- **major: 項番9が集合の包含で通っていた** … `set(FEET_KINDS) <= produced` は4面でも5面でも
  緑になり、**環状平面が無い実装でも「実物が作れる」と主張できた**。実測値そのもの
  （平面2 + 全周円筒1 + 円錐1 = 4面）に落とし、実物との差が**平面1枚**であることと、
  それが**非対称な面取り（角度指定＝今回の範囲外）**を要することを
  `test_how_close_the_verbs_get_to_the_real_foot` に明記した。
- **major: `pytest.raises(Exception)` が緩すぎた** … `IsDone()` を一度も見ない実装が
  引数検査の TypeError で落ちても緑になる。失敗系はすべて **`ValueError`**（既存の
  `extrude` 系と同じ）に絞った。**この型が実装の契約**である。
- **major: 面取りとフィレットを同じボディへ順にかけるテストが無かった** …
  積層プレートは円錐8とトーラス12を同時に持つので、片方だけ動く実装では実物に届かない。
  `test_chamfer_and_fillet_compose_on_the_same_body` を足した（実測: 面取り後 平面6/全周円筒1/
  円錐1 = 8面 → フィレット後 +トーラス1 = 9面）。**面取りで辺の添字が変わるので選び直す**
  ことも同テストで示している。
- **major: 円弧の角度を見ていると書いて長さしか見ていなかった** … docstringを実際に見ている値
  （円弧の長さ = r·θ。角度と1対1）へ揃え、面側の内訳も同時に確認するようにした。
- **minor 対応**: 半径バラバラの期待値を実測どおり厳密化（`bspline > 0` → 内訳の完全一致）／
  面取りにしかなかった**範囲外の辺の添字**・**参照ボディの拒否**・**辺0本の拒否**を
  フィレットにも追加／面取りの由来にも `face_map` を要求（片方だけ緩いと面取りだけ由来が
  空でも緑になる）／**90度の扇形**のテストを追加（実物の部分円筒は75〜97°で、半円だけを
  契約にすると「半円のときだけ通る」実装を止められない。実測: 平面4 + 部分円筒1 = 5面）／
  **角度は反時計回り・0度をまたいでよい**（`start_deg=350, end_deg=10` は20度）を契約に明記。

**項番8の訂正（円錐→平面）はQAも妥当と判定した**（直線辺の面取りは平面ベベル、円い縁の
面取りが円錐、という切り分けは幾何的に自然）。

**実装者への申し送り（QAが「未決」と指摘した点。実装時に決めてよいが、決めたらここへ書くこと）**:
`Operation.selection` に辺を載せるか（既存の `extrude` は領域を載せている）／辺の添字が
形状変更でずれる問題に対する由来スロットの持ち方（`face_map` と同じ形にするか別にするか）／
複数ボディにまたがる辺の選択（今回は単一ボディのみ）。

## フィレット・面取りの実装で分かったこと（2026-08-11・`fillet-chamfer-logic`）

**テストは全26件が緑**（`tests/test_fillet.py` + `tests/test_chamfer.py`）。実装は cursor(auto) へ
委譲し、委譲元（Claude）が検証・修正して取り込んだ。

### 契約を1つ強めた — 項番5「r=10.5 は成功するが歪」の正体

実装中に測ったら、**歪さは機械判定できた**:

    r=3.0   IsDone=True   BRepCheck.IsValid=True    volume 7572.619
    r=10.5  IsDone=True   **BRepCheck.IsValid=False**  volume 9246.464

タスクファイル項番5は「テストで歪さを判定するのは難しいので、成功すること自体を固定し
docstringに注意を書く」と指示していたが、`BRepCheck` が偽になると分かった以上、
**duocadはこの形状を文書に入れない**のが正しい（§6予防6・§7「黙って壊れた形状を残さない」。
`extrude_face_on_shape` も `BRepCheck` を最後の関門にしている）。したがって契約は
「r=10.5 は成功する」→「**r=10.5 は `IsDone` を通るが `BRepCheck` で止まる**」へ変更した。
テスト名も `test_radius_10_5_is_reported_as_done_but_is_actually_broken` に改めてある。

**壊れ方は半径に対して単調でも一様でもない**（`test_failures_do_not_follow_a_simple_rule_about_the_radius`）:

| 半径 | どうなるか |
|---|---|
| 3.0 | 通る |
| 10.0 | `IsDone()=False` で落ちる |
| 10.5 | `IsDone()` は真、**`BRepCheck` で落ちる**（落ちる関門が違う） |
| 30.0 | `IsDone()=False` で落ちる |

範囲検査で代用できないうえ、**どの関門で捕まるかも半径で変わる**ので、毎回すべての関門
（`IsDone` / 空でない / 体積 > 0 / `BRepCheck`）を通すしかない。

### 実装で決めたこと（契約の外側・次に触る人向け）

- `kernel._blend_edges(shape, values, verb)` に**フィレットと面取りを1つの実装として置いた**。
  `BRepFilletAPI_MakeFillet` と `BRepFilletAPI_MakeChamfer` は同じ系統で、失敗の仕方も由来の
  取り出し方も同じだから。2つに書き分けると片方だけ検査が抜ける（面取りは「まっすぐな辺なら
  平面が増えるだけ」の無害に見える操作なので、検査を省きたくなる）。
- 由来は **`normalize_provenance` の戻りから射影を作る**（`maker.Modified()` を verb 側や
  kernel 側で直接叩き直さない）。フィレット系が `History()` を持たない差を吸収するのが
  アダプタの存在理由なので、そこを迂回すると同じ分岐が写経される（§6予防2）。
  `TopoDS_Shape` は辞書の鍵にできないので `IsSame` で引く（`_lookup_by_identity`）。
- `query.describe_edges` は**添字と種別を kernel 側（`edge_count`/`edge_of`/`classify_edge`）から
  取り、長さと端点だけ build123d で測る**。build123d の `.edges()` の順序が
  `kernel._edge_map` と一致する保証が無く、ずれると**「選んだ辺と違う辺が丸まる」**という
  例外の出ない事故になるため。
- `ShapeUpgrade_UnifySameDomain` は**通さない**（押し出し専用の処置。フィレットの結果を統合すると
  丸めた面が潰れる）。
- undo は `extrude.py` と同じ `WeakKeyDictionary` のスナップショット方式。

### 委譲の記録

- cursor(auto) 2回。**1回目は最終メッセージにコードが1文字も含まれず成果物ゼロ**
  （ファイル書き込みがフックで拒否され、テキストにも出さなかった）。2回目に
  「前回コードが無かった。必ず最終メッセージ本文にコードを書け」と明示し、出力範囲を
  5関数＋2ファイルに絞ったところ成功した。**中継方式では「本文に書け」を毎回明示する**こと。
- 委譲元が直したもの: `kernel.py` の import 追記（cursorが申し送っていた）／辺マップを
  1回だけ作るよう変更（関数呼び出しごとに作り直していた）／`normalize_provenance` の
  戻り値を捨てて `maker.Modified()` を直接叩いていたのをアダプタ経由に統一／
  `describe_edges` の実装に伴い誤って消してしまった `_wrap`/`_unwrap` の復元。
- `gpt-5.6-sol-*` / `gpt-5.6-terra-*` は月次上限（8/19リセット）で使えず、**QAは委譲元の
  Claude が行った**（実装=cursor auto と別モデルなので「QAは実行者と別モデル」は満たす）。

## 円弧の実装で分かったこと（2026-08-11・`arc-logic`）

**テストは全16件が緑**（`tests/test_arc.py`）。cursor(auto) へ委譲し**1巡で通った**
（フィレット・面取りと違い、スケッチ側だけで完結しOCCTの黙殺失敗が絡まないため）。
変更は3ファイル・38行だけ。

- `sketch.add_arc` は `_add_entity` を呼ぶだけ（`add_circle` と同じ形）。数値の検査・
  名前つき値への登録・ログ・undo は既存の共通経路が面倒を見る。
- **`VERB_ADD_ARC` を登録ループへ足すのを忘れない**。忘れると追加自体は動くのに
  undo だけが `verb 'add_arc' has no registered handler` で落ちる（do/undo の実体は
  `args["kind"]` を見る共通関数なので、足すのは1語だけ）。
- `kernel._entity_edges` の `arc` 分岐は `BRepBuilderAPI_MakeEdge(gp_Circ(axis, r), u1, u2)`。
  **0度またぎは `sweep = (end_deg - start_deg) % 360` で表現し、`u1 〜 u1+sweep` を渡す**
  （`u2` に `end_deg` をそのまま渡すと 350→10 が20度ではなく340度の円弧になる）。
  向きが反時計回りであることは `gp_Ax2(center, N, XDir)` の定義から導けるが、
  **実測で確かめたのは `test_angles_run_counter_clockwise_and_may_cross_zero`**
  （委譲先は「コード上の確認のみ・未実測」と正直に申告した）。
- `kernel._entities_bbox_2d` の `arc` は**円と同じ外接箱**で安全側に囲む（補助面が少し
  大きくなるだけで領域検出の結果は変わらない）。
- `region._translate_params` の `arc` は `cx`/`cy` を動かすだけ（半径・角度は不変）。

**v1のverbで残るのは `円形反復` の1つだけ**になった。そして**受入テストを実物に戻せる**
状態に近づいた:

| 段階 | 受入対象 | 構成（実測） |
|---|---|---|
| 最小の実物 | `PC_case_feet.step` | 5面 = 平面3 + 全周円筒1 + 円錐1 |
| 本命 | `plate_1by1_24.step`（積層プレート） | 74面 = 平面18 + 部分円筒28 + 円錐8 + トーラス12 + bspline8 |

**受入テストの再定義は本人の判断事項**なので行っていない（`受入テスト再定義待ち`）。
なお最小の実物である脚も、**寸法まで同じ形は今のAPIでは作れない**（非対称な面取り＝
角度指定が要る。上の「フィレット・面取りの実装で分かったこと」参照）。

---

# 円形反復 — テストが定義する契約（2026-08-11追記・v1最後のverb）

`pattern_rectangular`（M3）の**兄弟**。**このタスクはテストだけ**で、実装は次のタスク
（`circular-pattern-logic`）が行う。**赤で終わるのが正常**（26件が赤・全件
`NotImplementedError`。非 `NotImplementedError` の赤は0件）。

    pattern_circular(doc, sketch_id, entities, center, count,
                     total_angle=360.0, rotate_copies=True) -> PatternId

## この契約で決めたこと（矩形反復に無かったぶん）

1. **`Pattern.kind`**（`"rectangular"` | `"circular"`）を足した。`Body.kind` / `Entity.kind` と
   同じ語彙で、既定が `"rectangular"` なのでM3の矩形反復は何も変えずに動く。
   種別が無いと `_expanded_entities` が展開の仕方を選べない。
2. **`rotate_copies` は `params` に入れず `Pattern.options` へ**。`params` は
   「`set_parameter` で後から編集できる**寸法値**」の置き場で、真偽の切り替えを混ぜると
   `0.5` を書き込める余地ができる。**寸法でないものを名前つきの値にしない。**
3. **刻み角の場合分け**（一番踏みやすい）:
   - 全周（`total_angle == 360`）… 刻みは **`360 / count`**。
   - 全周未満 … **端点を含む**ので刻みは **`total_angle / (count - 1)`**。
   - `count == 1` は複製が無いので刻みを使わない（0除算しないこと）。
   全周を `count-1` で割ると**最後の複製が最初と重なり、穴の数が1つ減る**。
   逆に全周未満を `count` で割ると、指定した角度に最後の複製が届かない。
   `test_a_full_circle_divides_360_by_count` と `test_a_partial_angle_includes_both_ends`
   がこの2つを別々に固定している。
4. **回転させた長方形は展開時に4本の線分へ落とす**。`Entity` の長方形は軸平行の
   `x, y, width, height` しか持てず、回転した矩形をその語彙で表せない。展開結果
   （`region._expanded_entities` の戻り）は**幾何を作るための導出物**なので、元と同じ種別で
   ある必要はない（`kernel._entity_edges` は線分を知っている）。
5. **回転中心と図形の中心が一致したら ValueError**（項番14の「挙動を決めて固定する」）。
   全複製が元と完全に重なり、「反復したのに何も増えない」が成功として通ってしまうため。
   許すか拒否するかはどちらでもよいという指定だったので、**黙って壊れない**方を選んだ。
6. **`total_angle` は `0 < angle <= 360`**。360超は1周を超えて並べる意味になり複製が必ず
   重なる。どう振る舞うべきか決められないものは受け取らない。NaN・負も同様。
7. **`count` の検査は `set_parameter` 経由にも効かせる**（`parameters.is_positive_int` の
   分岐に `count` を足す）。作成時だけ検査していると、後から書き込む経路が素通りする
   ——`nx`/`ny` で既に踏んだ穴と同じ（`test_reducing_the_count_below_one_is_refused_afterwards_too`）。

## 期待値の出どころ

角度と位置は**幾何から導ける**ので実測は要らない（6個・全周なら60度ずつ、3個・180度なら
90度ずつ）。位置は `Region` が `index` と `area` しか持たない（§3.3）ため、
**押し出して開いた穴の全周円筒面の重心**で見る（`test_pattern.py` と同じ流儀）。

## やらないこと（範囲の明示）

- 3D側のフィーチャ反復（穴そのものを反復する）は範囲外。**スケッチ内の図形の反復**だけ。
- 軸を任意に取る反復は範囲外（スケッチ平面の法線まわりのみ）。

## 完了後の状態

このverbが実装されると **v1のverb集合が揃う**: 線分・円・円弧・長方形・矩形反復・円形反復・
押し出し（結合/切取）・面押し出し・フィレット・面取り ＋ 寸法の数値入力・既存面へのスケッチ・
領域検出。

## 円形反復の実装で分かったこと（2026-08-11・`circular-pattern-logic`）

**テストは全27件が緑**（26件＋実装中に足した1件）。cursor(auto) へ委譲し**1巡で通った**
（既存の `pattern_rectangular` を手本として読ませたのが効いた）。**これで v1 のverb集合が揃った。**

- 展開は `region._expanded_entities` が `pattern.kind` で分岐する。刻み角は
  `count==1 → 0` / `total_angle==360 → 360/count` / それ以外 → `total_angle/(count-1)`。
  **`total_angle == 360` は厳密等価で判定している**（`isclose` にしていない）。
  360を少しでも下回る値は「全周ではない」＝端点を含む配置、という解釈になる。
- 回転は `_rotate_point` / `_rotate_params` に閉じている。`rotate_copies=False` は
  **図形の中心だけを回して、その差分を平行移動**する（向きは元のまま）。
  `arc` は `rotate_copies=True` のとき `start_deg`/`end_deg` にも角度を足す
  （位置だけ回すと円弧の向きが揃ってしまう）。
- **`set_parameter` の後追い検査に `total_angle` も足した**（実装中に見つけた穴）。
  `count` だけ検査していると**角度の側から同じ壊れ方を作れる** — 0度にすれば全複製が重なり、
  360超なら1周を超えて重なるのに例外が出ない。「作成時だけ検査していると後から書き込む経路が
  素通りする」は `nx`/`ny` で既に踏んだ穴で、同じ扱いに揃えた
  （`test_changing_the_total_angle_to_an_unusable_value_is_refused`）。
- **別経路の確認**（タスクファイル指定）: 円を8個・全周に反復して押し出し `cut` したところ、
  穴は**8個**、中心から見た角度は 0/45/90/135/180/225/270/315度で、
  **隣り合う中心角はすべて厳密に45度**だった（`360/8`。`360/(8-1)=51.43度` ではない）。

### v1のverb集合が完成した

線分・円・**円弧**・長方形・**矩形反復**・**円形反復**・押し出し（結合/切取）・面押し出し・
**フィレット**・**面取り** ＋ 寸法の数値入力・既存面へのスケッチ・領域検出。
**残るのは受入テスト（M5）と M4本体（MCPによる交互操作＝選択の提案・修正）。**
