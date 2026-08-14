# duocad M0 — テストが定義するAPI契約

このディレクトリのテストが **M0(コアの骨格)のAPI契約そのもの**。次タスク（M0コア実装）は
ここにあるテストを全部通すことだけを目標にする。仕様の正は
`C:\Users\tm976\mf\duocad\docs\IMPLEMENTATION_PLAN.md`（このREADMEと食い違ったら
そちらを優先する）。

**M2（スケッチ・領域検出・押し出し）と M3（パラメータ・矩形反復）の契約は、
この順で本文書の末尾に追記してある（2026-08-11）。**
M1（`extrude_face` / `make_editable_copy` / `tessellate`）の契約は
`tests/test_extrude_face.py` のモジュールdocstringが持つ（ここには無い）。

## 【無人drainへの注意】負荷で赤になるテストが1本ある（2026-08-14）

**`test_incremental_update.py::test_extruding_one_part_of_the_pc_case_stays_under_1000ms` は
マシンが混むと閾値を超えて赤になる。** 単独5回で 942 / 961 / 973 / 1076 / 1164 ms（コードは無変更）。
**この1本だけが赤いなら性能回帰ではなく負荷なので、コードを直さず・閾値も上げず、
タスクを `failed/` へ送らないこと。** 報告に「この1本のみ負荷由来で赤」と書いて進める。
扱いは本人判断で保留中。

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
- `cadquery-ocp-novtk==7.9.3.1.1`（importは `OCP`。`C:\Users\tm976\mf\cad-deixis-probe\.venv`(2026-08-13に削除済み。当時の観測)
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

本人がM1のビューアでPCケースを操作した実測（2026-08-10の実機計測。記録は削除済み）:

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
    "log", "can_undo", "can_redo", "applied_count",
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
提案・修正＝`propose_selection` / `modify_selection` / `apply`）は**範囲外**とし、
`test_tools_list_is_exactly_the_minimal_set` がツール3つを機械的に固定していた。

> **【2026-08-11 F1で退役】その上限はもう無い。** F が §0 の到達表のAI列を埋めるので
> ツールは28個になり、選択も述語もここから出る（顔ぶれの契約は
> `tests/test_mcp_verbs.py` にひとつだけ置く。本文末尾の「F —」節が正）。
> 当該テストは `test_the_m4_tools_survive`（M4の3つが名前も振る舞いも変わらず
> 生き残っていることの回帰ガード）へ置き換えた。**以下のM4の記述は、
> 「3つだけ」という範囲の主張を除いてすべて現役である。**

## モジュール一覧（M4で増える分）

| モジュール | 役割 | OCP直接import | build123d直接import |
|---|---|---|---|
| `duocad/mcp_server.py` | MCPサーバ（stdio・JSON-RPC 2.0）とツール3つ | 禁止 | 禁止 |

接続方法（Claude Code / Claude Desktop への登録）は `MCP.md` が正。

## `duocad/mcp_server.py` — 公開するツールは3つだけ

> **【2026-08-11 F1で失効】この「3つだけ」は退役した。** F が §0 の到達表のAI列を埋めるので
> ツールは28個になる（顔ぶれの契約は `tests/test_mcp_verbs.py` にひとつだけ置く。本文末尾の
> 「F —」節が正）。`test_tools_list_is_exactly_the_minimal_set` は
> `test_the_m4_tools_survive`（M4の3つが名前も振る舞いも変わらず生き残っていることの回帰ガード）
> へ置き換えた。以下の3つの**振る舞い**の記述は現役である。

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

---

# A — 選択集合をコアの第一級オブジェクトにする（2026-08-11追記・テスト先行の段階）

`tests/test_selection.py`（項番1〜14）と `tests/test_predicates.py`（項番15〜20）が正。
実装計画書 **§2.1（交互操作と選択の5条件）／§3.5（Selection のデータモデル）／§5（API）／§8b の A**。

**このタスク（A1）は契約だけを書き、実装は A2 が入れる。** 契約タスク完了時点で
新規77件のうち **71件が赤・6件が緑**（緑の6件は M3.5 で実装済みの `select_face` /
`selectable_bodies` / `Document` の周辺を見るもの）。既存297件は1行も触っていない。
**赤はすべて `NotImplementedError("A2で実装する(スタブ)")` である**（実測。他の理由で
落ちているものは無い ＝ テストの書き間違いで赤くなっているものが混ざっていない）。

## 何を成立させるためのものか

**交互操作（AIが選択を提案 → 人が過不足を直す → AIが適用）の唯一の必要条件**が
「選択集合が第一級の共有オブジェクトであること」である。M1の実装は**選択の正がブラウザ側**に
あり、AIからは読むことも書くこともできなかった（M1のQAが2回とも指摘し、
「M4の交互操作の前に払う債務」として持ち越されていた）。ここを払うのがAである。

満たすべき5条件（§2.1）: **永続 ／ 双方向（AIが書ける）／ 種別を保持 ／ 差分編集可能 ／
出所を区別（提案か確定か）**。

## モジュール一覧（Aで増える分）

| モジュール | 状態 | 役割 |
|---|---|---|
| `duocad/selection.py` | M3.5から拡張・**スタブ** | `Selection` と選択API。人のポインティング側の入口も持つ |
| `duocad/predicates.py` | **新規・スタブ** | 述語による選択（AIが引数を作る側の入口） |
| `duocad/query.py` | `describe_faces` / `edges_of_face` を**スタブ**追加 | 面の法線・面積、面と辺の関係 |
| `duocad/document.py` | `self.selection` を追加（実装済み） | 選択の置き場所 |
| `duocad/server.py` | `Session` の選択5メソッドを**スタブ**追加 | イベント発火の口 |

## `duocad/selection.py` — 選択集合

    Selection（dataclass）
      items  : [ShapeRef | RegionRef]      追加順を保つ
      origin : "user" | "proposed"         **集合単位**（項目ごとではない）
      to_dict() -> {"items","origin","count"}
      is_proposed() -> bool

    get_selection(doc)                       -> Selection   読むだけ（発火しない）
    propose_selection(doc, refs=|predicate=) -> Selection   置き換え・origin="proposed"
    modify_selection(doc, add=(), remove=()) -> Selection   差分編集・origin="user"
    commit_selection(doc)                    -> Selection   中身は変えず origin="user"
    clear_selection(doc)                     -> Selection   空に（origin="user"）
    confirmed_items(doc, kind=None)          -> [ref]       **proposed なら ValueError**
    edge_indices(doc, body_id=None)          -> [int]       fillet/chamfer へそのまま渡せる形
    select_face / select_edge / select_vertex / select_region -> ref
    ref_kind(ref) -> "face"|"edge"|"vertex"|"region"        種別判定の唯一の場所
    same_ref(a, b) -> bool                                  重複除去と remove が共有する判定

`ShapeRef` は §3.5 のとおり `{"body","kind","index","provenance"}`、`RegionRef` は
`{"sketch","index","provenance"}`。**添字は `kernel` の数え方（1始まり）に揃える** —
ここがずれると「選んだ辺と違う辺が丸まる」という、例外も出ない静かな事故になる
（`query.describe_edges` の警告と同じ）。

**領域も選択集合の第一級の項目である**（参照を作れるだけでは足りない）。ただし
`RegionRef` は §3.5 のデータモデルどおり **`kind` キーを持たない**ので、種別は
`"sketch"` キーの有無から導く（`ref_kind`。定数は `REGION_KIND = "region"`、
`kind=` に渡せる全体は `SELECTABLE_KINDS`）。判定を1箇所に集めるのは、
`confirmed_items` の絞り込みと `same_ref` の等価判定が別々の規則を持ち始めるのを防ぐため。

**`select_region` は M2 の `duocad.region` を使い回す** —— 参照そのものは
`region.region_ref(sketch_id, index).to_dict()` が作り、範囲は
`region.regions(doc, sketch_id)` を**その場で数え直して**検査する（領域は計算されるもので、
図形を1本足しただけで個数が変わる）。ここで別形式のdictを組み立て直すと、
操作ログの `selection` に2つの形が混ざる。存在しないスケッチは KeyError
（`sketch.get_sketch` に合わせる）、範囲外の添字は ValueError。

**参照が不正なときの例外は `select_*` と揃える** —— 形が不正（必須キーの欠落・未知の
`kind`・範囲外の添字）なら ValueError、存在しないBodyなら KeyError。どこが悪いかで
例外が変わらないと、呼び出し側（MCP）がAIへ返す説明を作れない。**`propose_selection` と
`modify_selection` の両方に同じ契約がかかる**（`modify` は人が触る主経路なので、
ここを素通りさせるとUIの実装ミスが壊れた参照として選択に紛れ込む）。

**既に入っている参照を再度 add しても位置は動かさない**（末尾へ移し替えない）。
人が同じものをもう一度クリックしただけで一覧の並びが変わると、`fillet` の辺リストと
半径リストの対応も一緒にずれる。

## 設計上の決定事項（タスクファイルの【要決定】への回答）

### 1. 選択の置き場所は `Document.selection`（コアの状態）

§2の図で、選択集合は文書・操作ログと**並んで**コアに置かれている。アプリ層に持たせると
AIから読み書きできず、交互操作が原理的に閉じない。`document.py` → `selection.py` は
`OpLog` と同じく**遅延import**で循環を避ける。

### 2. `origin` は集合単位。`pending: bool` は採らない

タスクファイルの案は「選択集合が `pending: bool` を持つ」だったが、**§3.5のデータモデルが
`origin: "user" | "proposed"` と定めている**のでそちらに合わせた。UI側の要件も
「AIの提案は見た目が違い、承認操作がある」＝集合まるごとの見た目であり、項目ごとに
出所を持つ必要は現時点で無い（必要になったら `ShapeRef` 側に足せる。逆は難しい）。

### 3. 遷移規則 — **人が触ること自体が承認**

| API | 呼ぶ主体 | 遷移後の `origin` |
|---|---|---|
| `propose_selection` | AI | `"proposed"` |
| `modify_selection` | 人（差分編集） | `"user"` |
| `commit_selection` | 人（中身を変えずに承認）／AI（自分の提案を実行する前の1手） | `"user"` |
| `clear_selection` | どちらでも | `"user"`（空集合の origin は常に user） |

`modify_selection` を "proposed" のままにしない理由: 人が中身を見て直したのに、
実行するのに**もう1回「承認」ボタンを押させる**ことになり、往復に無意味な1手が増える。
§5のAPI一覧でも `propose_selection` が「**提案**状態で書き込む」経路として名指しされており、
`modify_selection` は人の差分編集の側にある。

### 4. `pending` のまま verb を適用してよいか → **黙って通さない（禁止ではない）**

`confirmed_items(doc)` が `origin == "proposed"` のとき `SelectionNotCommitted`
（`ValueError` の派生）を送出する。これが提案と確定の区別を**実際に効かせている唯一の場所**である。
MCPはメッセージ文言ではなく `isinstance(..., SelectionNotCommitted)` で
`selection_not_committed` を付ける（コア側の文言に依存しない）。

AIが自分の提案を実行したいときは `commit_selection()` を明示的に呼べばよい。
**完全禁止にしなかった**のは、人が居ない場面（AI単独でモデルを作る）で提案→実行の経路が
塞がると、§0の到達表「19操作 × AI」が埋まらなくなるため。明示の1手を踏ませることで、
「提案のまま適用された操作」が後から区別できなくなる事故だけを防ぐ（§6予防6の
「黙って通さない」であって、機能の禁止ではない）。

### 5. 述語は**構造化（JSON）**。DSL文字列は採らない

    {"kind": "edge",
     "body": "part-3",                       # 省略時は**可視Body全部**
     "where": [{"op": "parallel_to", "axis": "Z"},
               {"op": "length_gt", "value": 20}]}

`where` は **AND**。空リストはその種別の全部。**ORは持たない** — 論理演算子を足した時点で
述語言語のパーサを書き始めることになる。ORが要るときは2回評価して
`modify_selection(add=...)` で足す（差分編集があるので表現力は落ちない）。

DSL文字列を採らない理由は **MCPのツールがスキーマを持つ**こと。構造化引数ならスキーマ検証が
効き、AIが書式を外した瞬間に落ちる。文字列DSLは「パースに成功して意味が違う」＝
**黙って別の辺を丸める**という最悪の失敗を作りうる。

使えるop（`predicates.describe_ops()` が唯一の正。F1のMCPスキーマはここから作る）:

| kind | op | 引数 |
|---|---|---|
| face | `normal_dot_gt` | `axis`（"X"/"Y"/"Z" または3要素）, `value` |
| face | `area_gt` | `value` |
| face | `kind_is` | `value`（`kernel.classify_face` の種別） |
| edge | `parallel_to` | `axis` |
| edge | `length_gt` / `length_lt` | `value`（**厳密な不等号**。境界を含まない） |
| edge | `kind_is` | `value` |
| edge | `on_boundary_of_face` | `face`（面の1始まり添字）← **関係述語** |

`on_boundary_of_face` は「上面の外周を丸める」に直結するので必須（項番17）。
返すのは**外周（outer wire）だけで、面に空いた穴の縁は含めない** —
外周を丸めたつもりの操作が黙って穴まで変えないため（穴の縁を丸めるとトーラスになる。
`test_fillet.py` 項番3で実測済み）。**貫通穴を開けた箱で対照している** —
穴の無い形だけで検査すると、面の全ワイヤを返す誤実装が緑のまま通る。

`face` は **Body局所の1始まり添字**である。したがって可視Bodyが複数あるときは
`body` の指定が要る（無ければ ValueError）。「全Bodyの3番目の面」という解釈は取らない —
部品ごとに面の数え方は無関係で、その集合には意味が無いため。

### 6. 0件一致と不正な述語は**別物**（項番19・20）

- **0件はエラーではない**。空集合を返し、`Selection.to_dict()["count"]` で件数が分かる。
  AIが述語を外したことに気づけないと、交互操作は人が画面を見るまで黙って止まる。
- **不正な述語は ValueError で、選択を変更しない**。実装は
  「**全条件を検査してから評価する**」順序でこれを守ること（途中まで評価して落ちる作りだと、
  守れるかどうかが実装の偶然になる）。黙って空集合を返すと上の2つが区別できなくなる。
- `body` を明示して**不可視Body**を指した場合も ValueError（「指定したのに0件」と
  「見えないので対象外」はAIから区別が付かない）。

### 7. 選択の変更は**操作ログに積まない**が、**イベントは発火する**（非対称）

| | 文書を変えるか | ログに載るか | イベント |
|---|---|---|---|
| 選択の変更 | いいえ | **載らない** | **発火する** |
| `set_visible` | はい（`Body.visible`） | **載る**（undoで戻る） | 発火する |

選択をログに載せない理由は、undoの単位（§2「verbの粒度」＝undo1回で消える単位）を
汚さないため。選択し直すたびにundo1手を消費する編集は使い物にならない。
一方、ログに載らないことと「アプリ層に知らせない」ことは**別**である。

### 8. イベントは**既存の上り方向にそのまま乗せる**（別系統を新設しない）

`set_visible` で実証済みの経路は
**`Session._emit` → `add_listener` の購読者（`Server._on_core_change`）→ 全接続へ配信**。
選択も同じ購読者リストに乗せる（購読をやめれば両方止まる、で検査している）。

イベントの形（verbの `applied` とは**キーで区別**する。選択はverbではない）:

    {"change": "selection",
     "selection": {"items": [...], "origin": "user"|"proposed", "count": int},
     "client_op": int | None}

`Session` の**戻り値**は `set_visible` と同じ流儀 —— 変更する4メソッドは `_emit(...)` の戻り
＝**発火したイベントそのもの**を返し、`get_selection` だけは発火せず
`Selection.to_dict()`（`{"items","origin","count"}`）を返す。**この2つは形が違う**ので、
呼び出し側は `["selection"]` を挟むかどうかを取り違えないこと。
`client_op` はそのまま載せて返す（出し手のいないMCP起点の変更では `None`）。

**加えて `Session.state_meta()` に `"selection"` を載せること。** `Server._on_core_change` は
MCP起点の変更から `applied` を落として配る（`applied` は操作を出したクライアントへの
確認応答なので他人の操作に付けられない）ため、**状態に載っていないとAIが書き換えた選択は
画面へ届かない**。`set_visible` の効果が `bodies[].visible` で届いているのと同じ場所である。
イベントだけ実装して状態に載せないと、**AI→人の方向だけが黙って死ぬ**
（人→AIは動くので、テストが無ければ気づけない）。

## 期待値の出どころ

30×30×40 の直方体（`BRepPrimAPI_MakeBox`）。面6・辺12・頂点8。
上向きの面（法線·Z > 0.9）は**1枚**、その外周は長さ30の辺4本、Zに平行な辺は長さ40の4本。
立方体にしなかったのは、3辺の長さが違えば「Zに平行な辺だけ」を選ぶ述語が長さでも
区別でき、**添字の偶然の一致でテストが通る余地が減る**ため。

**添字は決め打ちにせず毎回導く** — 上面は `kernel.tessellate` の外向き法線から、
辺の長さは `query.describe_edges` から取る（決め打ちは、分解や数え方の細部が変わったときに
テストが静かに別の実体を見に行く）。

## A1で**契約にしなかった**こと（範囲の明示）

- **形が変わったときの添字の付け替え**。選択は操作をまたいで保持されるが、押し出しの後に
  同じ添字が別の面を指しうる（§3.5「indexは現時点の添字であって、再生時に信用してはならない」）。
  これを解くのは `provenance` の空スロット（§6予防2）であり、Aの範囲外。
  **A2も付け替えを実装しないこと**（中途半端な再解決は、壊れていることが分からなくなる分だけ悪い）。
- **MCPツールとしての公開**は F1/F2。ここはコアのAPIまで。
- **UI側（`web/`）** は B。確定選択と提案を見た目で区別して描くのも B。
- **領域の述語**（スケッチの領域を条件で選ぶ）。`select_region` で参照は作れるが、
  `predicates` の `kind` は face / edge だけ。

## テスト一覧とタスクファイル項番の対応（A1）

| 項番（タスクファイル） | テスト |
|---|---|
| 1（種別を保持） | `test_each_kind_of_reference_keeps_its_kind` / `test_shape_refs_have_the_shape_of_the_spec` / `test_regions_can_be_selected_with_a_region_ref` / `test_selecting_a_region_validates_the_sketch_and_the_index` / **`test_regions_go_into_the_selection_like_any_other_kind`** / `test_confirmed_items_rejects_an_unknown_kind` |
| 2（差分編集） | `test_modify_selection_adds_and_removes_without_rebuilding` / `test_remove_wins_when_the_same_reference_is_added_and_removed` / `test_modify_selection_refuses_a_malformed_ref` |
| 3（重複しない） | `test_adding_the_same_reference_twice_does_not_duplicate`（位置も動かないこと込み） / `test_the_same_index_on_two_bodies_is_not_a_duplicate` |
| 4（空にできる） | `test_clear_selection_empties_it` |
| 5（操作をまたいで残る） | `test_selection_survives_an_operation` |
| 6（ログに載らない） | `test_changing_the_selection_does_not_touch_the_operation_log` ＋ 対照の `test_set_visible_still_lands_in_the_log` |
| 7（保存されない） | `test_selection_is_not_saved` |
| 8（提案として区別） | `test_proposed_items_are_distinguishable` |
| 9（**交互操作そのもの**） | `test_the_ai_proposes_the_human_fixes_and_the_ai_reads_it_back` |
| 10（commitで確定） | `test_commit_selection_confirms_without_changing_the_items` / `test_commit_selection_is_idempotent` |
| 11（決めた遷移規則） | `test_human_editing_a_proposal_confirms_it` / `test_a_proposal_cannot_be_fed_to_a_verb` |
| 12（発火する） | `test_changing_the_selection_fires_a_change_event` / `test_every_selection_api_fires_exactly_one_event` |
| 13（描き直すのに足る） | `test_the_event_carries_enough_to_redraw` / `test_the_event_carries_the_client_op_back` |
| 14（既存の上り方向） | `test_selection_events_ride_the_same_up_path_as_set_visible` / `test_selection_is_part_of_the_pushed_state` / `test_the_event_does_not_pretend_to_be_a_verb` |
| 15（述語で面） | `test_faces_can_be_selected_by_normal` ほか |
| 16（述語で辺） | `test_edges_can_be_selected_by_direction_and_length` / `test_length_conditions_are_strict_comparisons` / `test_results_group_by_body_in_document_order` |
| 17（関係述語） | `test_edges_on_the_boundary_of_a_face_can_be_selected` / **`test_the_boundary_excludes_the_rim_of_a_hole`** / `test_the_boundary_of_the_bottom_face_is_a_different_set` / `test_the_relation_predicate_refuses_to_guess_which_body` / `test_the_boundary_of_a_face_is_ready_for_fillet` |
| 18（不可視は対象外） | `test_hidden_bodies_are_not_scanned_by_predicates` / `test_naming_a_hidden_body_explicitly_is_an_error` |
| 19（0件はエラーでない） | `test_zero_matches_is_not_an_error` / `test_the_caller_can_see_the_match_count` |
| 20（不正な述語・不正な参照） | `test_invalid_predicates_are_rejected`（9通り） / `test_an_invalid_predicate_leaves_the_selection_untouched` / **`test_a_malformed_ref_is_refused_without_touching_the_selection`**（5通り） |

## QAで足した契約（2026-08-11・cursor auto のレビュー指摘を反映）

QAは `gpt-5.6-sol-high` を試したが**月次の使用量上限**（8/19リセット）で呼べず、
**cursor の auto 枠**でレビューした。1ラウンド目の判定は**不合格**で、major 2件・minor 6件。
**全件対応済み**（この節の内容がその結果）。

- **major: `select_region` の契約とテストが両立していなかった** … docstringは
  「存在しないスケッチは KeyError・範囲外は ValueError」と書いているのに、テストは
  スケッチも領域も無い空の `Document` で成功することを要求していた。**契約側を正とし**、
  テストを実際のスケッチ（半径20の円＝領域1個）で組み直した。あわせて、M2の
  `region.region_ref` / `region.regions` を使い回す旨を明記した（別形式のdictを作ると
  操作ログの `selection` に2つの形が混ざる）。
- **major: 項番1の「領域」が実際には検証されていなかった** … 参照の形しか見ておらず、
  領域を `modify_selection` へ入れる経路が1本も無かった。差分編集・重複除去・種別での
  絞り込みまで通すテストを足し、`RegionRef` が `kind` キーを持たない問題を
  `ref_kind` / `REGION_KIND` で解いた（**この穴はQAが見つけるまで気づいていない**）。
- minor: 項番7のアサーションが弱い（新しい文書の選択が空、しか見ていなかった）→
  **保存されたJSONが選択を運んでいないこと**を本体の検査にした。
- minor: 述語の順序契約が1Bodyでしか検査されておらず「Body順」が壊れても落ちなかった →
  2Bodyのテストを追加。
- minor: 関係述語が「件数4・長さ30」しか見ておらず、**底面の外周でも緑になりえた**
  （直方体は上下が合同）→ 端点のZまで固定し、底面との排他も対照実験にした。
- minor: 不正な**参照**（不正な述語ではなく）の契約にテストが無かった → 5通り追加し、
  例外の種類を `select_*` と揃えることを明文化。
- minor: `Session` の選択メソッドの**戻り値**が書かれていなかった → イベントそのものを
  返す（`get_selection` だけ `to_dict()`）と明記。
- minor: `client_op` が契約に書いてあるのにテストが無かった → 追加。

2ラウンド目は上の8件の解消を確認したうえで **major 1件・minor 4件**。**全件対応済み**:

- **major: 「外周のみ」を契約に書きながら、穴の無い形でしか検査していなかった** …
  面の**全ワイヤ**を返す誤実装でも「件数4・長さ30・Z=40」で緑になる。1ラウンド目の
  「底面でも緑」と同型の穴を、指摘の側が自分で一般化して見つけている。
  **貫通穴を開けた箱**（duocadのverbだけで作る）で対照を足した。
- minor: 不正な参照の失敗契約が `propose_selection` にしか無かった（`modify_selection` は
  人が触る主経路）→ 両方にかかる契約として明文化し、テストを追加。
- minor: 重複 add したときの**位置**が未固定だった → 「動かさない」と決めて固定。
- minor: 関係述語の `face` が Body局所の添字かどうか書かれておらず2通りに読めた →
  Body局所と決め、可視Bodyが複数なら `body` 必須（無ければ ValueError）とした。
- minor: `server.py` 冒頭の `state()` の形の記述に `selection` が無く、A節の決定と
  食い違っていた → 追記（**A1時点ではまだ載っていない**ことも明記）。

## 実行方法（A）

```
.venv\Scripts\python.exe -m pytest tests/test_selection.py tests/test_predicates.py -q
```

**素の `python` は使わない** — `OCP` を持たない別環境で、収集エラーになる（2026-08-11実測）。

## Aの実装で分かったこと（2026-08-11・`selection-core-logic`）

**契約77件＋QAで足した2件の計79件が緑。全スイート377 passed。** 実装は cursor(auto) へ
委譲し**1巡で通った**（仕様をファイルで渡し、契約が既に `tests/README.md` と
スタブのdocstringに全部書いてあったのが効いた）。

- **面の法線は build123d の `Face.normal_at()` に任せてよい**（実測）。`describe_faces` の
  docstringは当初「`TopAbs_REVERSED` なら反転する」と書いていたが、`normal_at()` は
  **既に向きを織り込んだ外向き**を返し、`kernel.tessellate` の値と一致した。
  自分でもう一度反転すると二重になる。ただし**tessellate 側とは別のコードパス**なので、
  どちらかだけ壊れると述語だけが静かにずれる。`test_faces_can_be_selected_by_normal`
  （`describe_faces` 由来）と `_top_face_index`（tessellate 由来）が同じ面を指すことで
  両経路の一致が毎回検査される。**この対応関係を壊さないこと。**
- **辺の添字を引き当てるのに `kernel.edge_index_of` を足した**（`IsSame` で照合）。
  `edges_of_face` は build123d の `Face.outer_wire().edges()` から辺を得るが、
  **その列挙順が `kernel._edge_map` と一致する保証は無い**ので、形状の同一性で
  kernel側の添字へ引き当てる。`describe_edges` が「build123dの順序を信用しない」と
  書いているのと同じ理由で、ここを横着すると「選んだ辺と違う辺が丸まる」。
- **`edge_indices` の順序は選択順（追加順）であって昇順ではない。** `fillet` は
  `edges` と `radius` のリストを**位置で対応**させるので、途中で並べ替えると
  半径の割り当てが黙ってずれる。別経路の確認でもここを固定した。
- **`evaluate` の実測コスト**: 合成した約27Body・約2900面で 0.3〜0.4秒（QAが計測）。
  `describe_faces` / `describe_edges` はBodyあたり1回に抑えてある。
  伸びしろとして、`kernel.edge_index_of` が呼ぶたびに辺マップを作り直す点と、
  `on_boundary_of_face` だけの述語でも全辺を `describe_edges` する点が残っている
  （**正しさの問題ではない**。実物のPCケースで遅ければここを見る）。

### 別経路の確認（pytestとは別に素のPythonで1巡通した）

30×30×40の直方体（スケッチ→押し出しで作成）に対して:

1. AIが述語 `parallel_to Z and length_gt 20` で辺を提案 → **4本**（`[1, 2, 5, 8]`）
2. **提案のままでは verb に渡せない**ことを確認（`commit_selection() first` で拒否）
3. 人が `modify_selection` で1本足して1本引く → AIが `get_selection` で読み直す →
   `[2, 5, 8, 4]`（**選択順のまま**）、`origin` は `"user"` に遷移
4. **選択の操作で操作ログは1行も増えない**（素材の3行のまま）
5. その選択をそのまま `fillet(r=3.0)` へ → **体積 36000.000 → 35712.876 / 面 6 → 10**
6. AI起点の選択変更が**イベント（1回・`client_op` 込み）と `state_meta()["selection"]` の
   両方**に載ることを確認（`applied` を落とすMCP起点でも画面へ届く経路）

**これが§2.1の交互操作のPython側の一巡**である。UI側は B、MCP側は F でつながる。

### QAで足したもの（2026-08-11・cursor auto のレビュー指摘）

判定は**合格**（major 0・minor 7）。うち3件を実装・文書側で、2件をテストで対応した:

- `on_boundary_of_face` で**可視Bodyが0件**のとき「複数あるので `body` を指定せよ」と
  出していた（0件は複数ではないし、`body` を明示しても解決しない）→ 0件と複数で
  言うことを分けた。
- 範囲外の面添字のメッセージに**有効な範囲が無かった**（`select_*` は `1..count` を出す）
  → 揃えた。
- `describe_faces` の docstring が実装と食い違っていた（上記の法線の件）→ 実測に合わせた。
- `kind_is` の**評価**にテストが無かった（`validate` と `describe_ops` しか見ていない）
  → `test_kind_is_actually_filters_by_the_classified_kind` を追加。
- 「不正な述語で選択を変えない」が **`validate` 段階で落ちる例しか無かった** →
  `validate` を通ってから落ちる例（範囲外の面添字）を
  `test_a_predicate_that_fails_at_evaluation_also_leaves_the_selection_untouched` で追加。
  **「全条件を検査してから評価する」が効いているかは、こちらでしか確かめられない。**

据え置いた minor は上記の性能2件（正しさの問題ではない）。

---

# F — MCPで全verbと述語選択を公開する（2026-08-11追記・テスト先行の段階）

`tests/test_mcp_verbs.py`（操作と問い合わせ）と `tests/test_mcp_selection.py`（選択と述語）が正。
実装計画書 **§0（到達表と「満足に使える」の操作的定義）／§5（API/MCP要件）／§8b の F**。

**このタスク（F1）は契約だけを書き、実装は F2 が入れる。** 契約タスク完了時点で
新規91件のうち **84件が赤・7件が緑**。緑7件は M4 で既に成立している規律の回帰ガード
（境界・ツールの説明とスキーマの有無・実行時エラーをプロトコルエラーにしないこと・
ログのverb名）で、**ツールが3つしか無い今は自動的に満たされるが、28個になっても守られる**
ことを見張る。既存377件は1行も触っていない（実測 376 passed / 1 failed。赤は
`test_extruding_one_part_of_the_pc_case_stays_under_1000ms` の既知のフレークのみで、
着手前から赤い）。

## 何を成立させるためのものか

§0の到達表の **AI列が 1/19**（`set_visible` だけ）である。これは設計ミスの結果で、
M2以降のタスクに毎回「GUI側は触らない」と書いた一方、**AI側へ出すタスクを一度も積まなかった**。
F が埋めるのはその列であり、合格条件は §0 が定義している:

> MCPツールとして呼べ／引数を**述語**または明示的な参照で与えられ／**結果と失敗が構造化して返る**

## 公開するツールは28個

| 群 | ツール |
|---|---|
| 問い合わせ(3) | `describe_document` / `describe_shape` / `measure` |
| 述語の自己記述(1) | `describe_predicates` |
| 選択(5) | `get_selection` / `propose_selection` / `modify_selection` / `commit_selection` / `clear_selection` |
| スケッチ(5) | `sketch_create` / `add_line` / `add_circle` / `add_rectangle` / `add_arc` |
| フィーチャ(6) | `extrude` / `extrude_face` / `fillet` / `chamfer` / `pattern_rectangular` / `pattern_circular` |
| ボディ・パラメータ(3) | `set_visible` / `make_editable_copy` / `set_parameter` |
| 入出力(3) | `import_step` / `export_step` / `export_stl` |
| 履歴(2) | `undo` / `redo` |

操作は19個で、**§0の到達表のAI列19行とちょうど対応する**（表は
`add_line / add_circle / add_rectangle / add_arc` のようにまとめて書かれているので、
展開すると19になる）。`tools/list` の集合は**一致**で検査する（部分集合ではない）——
「19のうち18ある」は §0 の観点では未達だが、部分集合の検査だと緑になる。

**ツール名は操作ログのverb名と同じ綴りにする。** AIは `describe_document` が返すログを読んで
次の一手を決めるので、ログのverb名と呼べるツール名が違うと対応表を推測させることになる。

## 決定1（Fの背骨）— 形は選択から、数値は引数から

**面・辺・領域を引数で受けるツールは1つも無い。** `fillet(radius)` であって
`fillet(edges, radius)` ではない。形は**確定した選択**から取り、数値・名前・パスだけを引数で渡す。
Bodyだけは `body_id` 引数で受ける —— Bodyは選択集合の項目ではないため。

これは §5 の `apply(verb, selection, args)` を verb ごとのツールへ分解した形である。
効き目は3つ:

1. **A1の「提案のままでは verb に渡せない」が全verbへ自動的にかかる。** 形を引数で受ける裏口が
   1つでもあると、その経路だけ提案と確定の区別が素通りになる。
   `test_no_verb_tool_takes_a_shape_argument` がこれを名指しで固定している。
2. **参照の検査をMCP層に書かなくて済む。** `selection._coerce_ref` が既に持っている
   （形が不正なら ValueError・存在しないBodyなら KeyError）。二重に書けば必ず食い違う。
3. **§0の「述語または明示的な参照で与えられ」が1箇所に集まる。** どちらも
   `propose_selection`（`predicate=` / `refs=`）から入り、verbは選択を読むだけになる。

代償は、フィレット1回が3呼び出し（提案→確定→適用）になること。**これは代償ではなく設計の主張**で、
交互操作（AIが提案 → 人が直す → AIが適用）の一巡そのものである。人が居ない場面でも
AIが `commit_selection()` を明示すれば通る（A1の決定4）。

## 決定2 — 成功は封筒に入れて返す

ログに1行積むツールは、次を必ず返す:

    verb           ログに積んだverb名（undo/redo は "undo"/"redo"）
    seq            そのOperationの通し番号
    changed_bodies この操作で形状か可視性が変わったBodyId
    bodies         その変更後の姿。キーは body_id / name / kind / visible /
                   face_count / volume の6つちょうど（`BODY_KEYS`）
    can_undo / can_redo

加えて作ったものの**IDを返す**（`sketch_create`→`sketch_id` / `add_*`→`entity_id` /
`pattern_*`→`pattern_id` / `make_editable_copy`→`body_id` / `import_step`→`body_ids`）。
返さないとAIは次の呼び出しで指せない。

**`changed_bodies` は `Session.body_revision` の呼び出し前後の差から導くこと。**
verbごとに「これはbodyを変える」と数え上げる実装だと、verbが増えたときに静かに漏れる
（漏れてもpytestは通り、**ブラウザだけが古いまま**になる）。版が「経路ではなく状態」から
導かれているのは元々このためである（`Session.body_revision` のdocstring）。
`test_changed_bodies_is_empty_when_only_a_sketch_changed` が、数え上げ実装なら落ちる側の対照。

`export_step` / `export_stl` は**ログに載らない**（文書を変えないのでverbではない）。
そのぶん `{"path", "bytes", "body_ids"}` を返す —— AIは自分でファイルを確かめられないので、
戻り値が唯一の確認手段である。

## 決定3 — 失敗も封筒に入れて返す（例外のテキストを素で投げない）

    {"error": {"code": <6語彙>, "message": <原文>, "tool": <ツール名>, "exception": <型名>, ...}}

を `json.dumps` した本文＋`isError: true`。M4は `f"{type(error).__name__}: {error}"` を
そのまま返していたが、それは §0 の「失敗が構造化して返る」を満たしていない。

| code | いつ | 添えるもの |
|---|---|---|
| `not_found` | `KeyError` / `FileNotFoundError` | — |
| `invalid_argument` | `ValueError` | 選択由来なら `required_kind` / `selection` |
| `selection_not_committed` | 提案のまま verb を呼んだ | `hint`（`commit_selection` を含む）/ `selection` |
| `selection_empty` | 要求した種別の項目が確定選択に無い | `required_kind` / `selection` |
| `invalid_predicate` | 述語が不正 | **`valid_ops` = `describe_ops()`** / `valid_kinds` |
| `internal` | 上記以外 | — |

**`FileNotFoundError` を忘れないこと。** パスの間違いはAIが直せる誤りなので、`internal` に
落とすと直せるものを直さずに諦める（F1で実測: 無いSTEPは `FileNotFoundError` で来る）。

コードを分けるかどうかの基準は**「AIが次に打つ手が変わるか」**である。`selection_empty` と
「違う種別を選んだ」を分けていないのは、どちらも次の手が「辺を選び直す」で同じだから
（区別の材料は `selection.count` で足りる）。逆に `selection_not_committed` を
`invalid_argument` に潰してはいけない —— AIは半径を変えて再試行しはじめる。

### 拒否はコアがする。MCPは名前を付けるだけ

`selection_not_committed` を返すために **`origin` を先に見て弾いてはいけない**。拒否するのは
`selection.confirmed_items`（A1が「区別を実際に効かせている唯一の場所」と定めた場所）だけで、
MCPは**落ちたあとに** `get_selection()` を見てコードを選ぶ。先に弾くと同じ規則が2箇所に生まれ、
片方だけ緩んだときに誰も気づかない。

**この規則は対のテストでしか固定できない**（QA1回目の指摘）:

- `test_no_selection_consuming_tool_accepts_a_proposal`（**形を選択から取る全ツール**を回す）
  … 提案が通ってしまわないこと。ただし**MCPが先に弾いていても緑になる**ので、これだけでは
  「門が1箇所」を主張できない。
- `test_the_gate_lives_in_the_core_not_in_the_mcp_layer` … `selection.confirmed_items` を
  `origin` を見ない版へ差し替えると、MCPは**そのまま通す**こと。通らなければMCP層にも門がある。

前者だけを書いて「これで決定3が守られる」と読むのは誤りである（最初の版がそう書いていた）。

## 決定4 — 選択5ツールは戻り値の形をMCP層で揃える

`Session` 側は変更する4メソッドが**発火したイベントそのもの**
（`{"change","selection","client_op"}`）を返し、`get_selection` だけが `Selection.to_dict()`
（`{"items","origin","count"}`）を返す（A1の決定8）。この差はコアの都合（イベントの形を
1箇所で作る）であって、**AIに見せる理由が無い**。MCPは5つとも `{"selection": {...}}` に揃える。

## 決定5 — 述語の語彙は `describe_predicates` で自己記述する

`predicates.describe_ops()` を**そのまま**返す。`describe_ops` の docstring が
「F1で書き直さずに済むように、opの定義はここ1箇所を正にする」と既に宣言しており、MCP側で
書き直すと**片方だけ増えたopが黙って通る**（スキーマにはあるがバリデータには無い、またはその逆）。

opの一覧だけでは述語を組み立てられないので、`kinds` / `where_is: "and"` / `example` まで出す。
**ORが無いことをAIが知らないと、無い機能を試して失敗する**（A1の決定5: ORが要るときは
2回評価して `modify_selection(add=...)` で足す）。`example` は `predicates.validate()` を
通る本物であることをテストが要求する —— 例が古くなって嘘になるのを防ぐため。

## 決定6 — `measure` は verb ではないので提案のままでも通る

§5 の `measure(selection)`。**`confirmed_items` を経由しない。** AIが自分の提案を測って
確かめてから確定する、という往復がここで閉じる。確定を要求すると、AIは中身を見ずに commit する
動機を持ってしまい、提案と確定の区別が実際に弱くなる。

値は**問い合わせ層のものをそのまま渡す**（`query.describe_faces` / `describe_edges` /
`region.regions`）。測り直すと述語が使う値と食い違いうる。**頂点は測れない**（位置を返す
問い合わせ層の関数がまだ無い）ので `{"measured": false, "reason": ...}` で返す ——
黙って落とすと、AIは「3つ選んだのに2つしか返らない」を自分の誤りと解釈する。

## 決定7 — `describe_document` はスケッチとパラメータ名まで見せる

§5 は「ボディ・**スケッチ**・パラメータの一覧」と定めているが、M4の実装はスケッチを出していない。
スケッチが見えないと `pattern_rectangular(entities=[...])` の引数をAIが作れず、**自分が同じ
会話の中で作った図形のIDしか知らない**（undo/redoや再接続で盲目になる）。

各図形は `params`（解決済みの**数値**）と `parameters`（その数値を持つ**パラメータ名**）の
両方を出す。数値だけだと `set_parameter` の引数が作れず、名前だけだと今の値が分からない。
領域の数え上げ（splitter）は計算なので `include_regions` で任意にする。

## 決定8 — `import_step` はコアのverbそのもの（`load_step` ではない）

起動時の `Session.load_step` は「参照 + 編集用コピー + 参照を隠す」までやるが、それは利便のための
組み合わせであって verb ではない。MCPが出すのは verb の側（参照ボディを足すだけ）で、
足りない1手（`make_editable_copy`）を戻り値の `hint` で必ず知らせる —— 黙って参照ボディだけ
増やすと、AIは編集できない相手に verb を打ち続ける。

> **2026-08-15にMCPの既定を変えた**（この決定そのものは生きている）。`import_step` verbは今も
> 「参照ボディを足すだけ」のコアのverbだが、**MCPツール `import_step` が既定で出すのは
> 新しいverb `import_step_editable` の方**になった。「参照 + 編集用 + 参照を隠す」は
> 利便の組み合わせではなく**1本のverb**になったので、MCPから1呼び出しで出せる。
> 詳細は末尾の「起動オプション `--step` の廃止」節。

## F1で実測した落とし穴 — **OCCTのSTEP書き出しはstdoutを汚す**

STEPを書き出すと、C++側から標準出力へ統計が出る:

    ****        Statistics on Transfer (Write)                 ****
    Step File Name : ...(662 ents)  Write  Done

stdioトランスポートは**stdoutをJSON-RPC専用**にしている（`duocad/mcp_server.py` の
モジュールdocstring:「stdoutを1文字でも汚すとMCPクライアントが接続に失敗する」）ので、
`export_step` を公開した瞬間に接続が壊れる。**Python層の `contextlib.redirect_stdout` では
止まらない**（C++が実fdへ直接書く）ため、ツール実行の間だけ **fd 1 を fd 2 へ振り替える**
（`os.dup2`）必要がある。

検査は `test_exporting_does_not_corrupt_the_stdio_channel` で、**実プロセスを起こす**。
同一プロセス内で `DocumentTools` を叩くテストでは原理的に検出できない（汚れるのは
テストランナーのstdoutであって、JSON-RPCの経路ではないため）。

## 期待値の出どころ（すべて2026-08-11に実測）

30×30×40 の直方体（A1と同じ。立方体にしないのも同じ理由）。

- 述語 `parallel_to Z and length_gt 20` の一致は **4本**（`[1, 3, 5, 7]`）
- 上面（法線·Z > 0.9）は **index 6**、その外周は `[2, 6, 10, 12]`
- 交互操作の一巡（1本足して1本引いて `fillet(r=2.0)`）: 選択順は **`[3, 5, 7, 2]`**（昇順ではない）、
  **面 6 → 10 / 体積 36000.000 → 35872.006**
- スケッチ（30×30の長方形）→ 領域1個（面積900.0）→ `extrude(40)` で **面6・体積36000.0**
- `extrude_face`（上面 +5mm）で 36000 → **40500**（= 30×30×5 の増分）
- 円（半径10）→ `extrude(5)` の体積 **1570.796**、半径を20にすると **6283.185**
- `shelf_attachment_GD.step` の `import_step` は **17個**の参照ボディを返す（単一ではない）

**添字は決め打ちにせず毎回導く**（上面は法線から、辺は端点から）。決め打ちは、分解や
数え方の細部が変わったときにテストが静かに別の実体を見に行く。

## F1で**契約にしなかった**こと（範囲の明示）

- **形が変わったときの添字の付け替え**。A1と同じくFの範囲外（`provenance` の空スロットの仕事）。
  一巡のテストは fillet 後も選択が4件残ることだけを見ており、**同じ辺を指しているとは
  主張していない**。
- **`apply(verb, ...)` という総称ツール**。verbごとのツールにしたので持たない
  （総称にするとスキーマが `args: object` に潰れ、AIから見た型検査が消える）。
- **選択の項目ごとの `origin`**。A1の決定2のまま集合単位。
- **UI側（`web/`）**。確定選択と提案を描き分けるのは B。
- **MCPクライアントからの実接続と実ブラウザでの確認**。F2の検証項目
  （`pytest` では「イベントが1回出て `state` に載る」までしか見られない）。
- **領域の述語**（A1と同じ。`predicates` の `kind` は face / edge だけ）。
- **書き出し先パスの制限**。ローカル専用ツールなので任意のパスへ書ける。

## テスト一覧とタスクファイル項番の対応（F1）

| 項番（タスクファイル） | テスト |
|---|---|
| 1（全操作が呼べる） | `test_every_v1_verb_is_reachable_as_an_mcp_tool` / `test_the_ai_column_of_the_reachability_table_is_complete` / `test_tool_names_match_the_verb_names_in_the_operation_log` |
| 2（引数スキーマ） | `test_every_tool_has_a_description_and_an_object_schema` / `test_every_argument_is_typed_and_documented` / `test_the_argument_of_every_tool_is_fixed` / **`test_no_verb_tool_takes_a_shape_argument`** ＋ 型検査4件（`test_numbers_are_not_accepted_as_strings` ほか） |
| 3（refs と predicate の両方） | `test_propose_selection_accepts_explicit_references` / `test_propose_selection_accepts_a_predicate` / `test_propose_selection_needs_exactly_one_of_them` |
| 4（結果が構造化） | `test_a_verb_reports_what_changed_not_a_sentence` / `test_changed_bodies_is_empty_when_only_a_sketch_changed` / `test_set_visible_still_reports_the_same_shape` ＋ 各verbを1回ずつ成功させる `test_every_sketch_verb_is_reachable_and_returns_its_id` / `test_both_patterns_are_reachable_and_return_their_id` / `test_extrude_can_cut` |
| 5（失敗が機械可読） | `test_a_missing_body_is_reported_as_not_found` / `test_a_bad_value_is_reported_as_invalid_argument` / `test_a_failed_verb_leaves_the_document_untouched` / `test_the_error_says_which_tool_failed` / `test_an_execution_error_is_not_a_protocol_error` / `test_import_step_reports_a_missing_file_as_not_found` |
| 6（不正な述語＋有効なopの一覧） | `test_an_unknown_op_comes_back_with_the_valid_ones` / `test_a_type_error_in_a_predicate_is_also_self_repairable` / `test_an_unknown_predicate_kind_is_reported_the_same_way` / `test_an_invalid_predicate_leaves_the_selection_untouched` ＋ 対照の `test_a_malformed_reference_is_refused_without_touching_the_selection` |
| 7（0件はエラーでない） | `test_zero_matches_is_a_success_that_says_zero` / `test_zero_matches_still_replaces_the_previous_selection` / `test_measure_of_an_empty_selection_is_not_an_error` |
| 8（提案のままでは通さない） | **`test_no_selection_consuming_tool_accepts_a_proposal`（全ツール）** / `test_extrude_also_refuses_a_proposed_region` / **`test_the_gate_lives_in_the_core_not_in_the_mcp_layer`** / `test_a_proposal_cannot_be_fed_to_a_verb_through_mcp` / `test_the_ai_can_execute_its_own_proposal_after_committing` / `test_measure_works_on_a_proposal` / `test_applying_a_verb_with_nothing_selected_says_so` / `test_selecting_the_wrong_kind_is_also_reported_as_empty` |
| 9（**MCP経由だけで一巡**） | **`test_the_whole_interleaved_lap_runs_through_mcp_alone`** |
| 10（選択変更が配信される） | `test_a_selection_change_from_mcp_fires_exactly_one_event` / `test_a_selection_change_from_mcp_reaches_the_browser` / `test_a_verb_applied_through_mcp_reaches_the_browser` / `test_every_mutating_tool_fires_exactly_one_event` / `test_a_read_only_tool_fires_nothing` / `test_a_failed_tool_fires_nothing` |
| 11（UIと同じ履歴） | `test_undo_through_mcp_undoes_the_ui_operation` / `test_redo_through_mcp_replays_it` / `test_undo_with_nothing_to_undo_is_a_structured_failure` / `test_undo_refuses_to_dismantle_the_document_floor` |
| （§5の問い合わせ） | `test_measure_reports_lengths_for_edges` / `test_measure_reports_area_normal_and_center_for_faces` / `test_measure_reports_the_area_of_a_region` / `test_measure_admits_what_it_cannot_measure` / `test_describe_document_lists_sketches_and_their_entities` / `test_region_counts_are_opt_in` |
| （項番外・範囲の維持） | `test_the_mcp_layer_holds_no_geometry_of_its_own` / `test_the_mcp_layer_does_not_reimplement_verbs` / `test_changing_the_selection_through_mcp_does_not_touch_the_log` / `test_the_selection_is_not_saved` / `test_exporting_does_not_corrupt_the_stdio_channel` |

## M4の契約をひとつ退役させた（F1で必要になった変更）

`tests/test_mcp.py` は「**公開するツールは3つだけ**」を集合の一致で固定しており、
`test_unknown_tool_is_invalid_params` は「未知のツール」の例に `propose_selection` を
使っていた。**このままでは F2 が両方を緑にできない**（28個にした瞬間にM4側が赤くなる）ため、
F1で次のように直した。QAの1回目が指摘するまで見落としていた欠陥である:

- `EXPECTED_TOOLS`（3つ・一致） → `M4_TOOLS`（3つ・**部分集合**）。
  `test_tools_list_is_exactly_the_minimal_set` → `test_the_m4_tools_survive`。
  **顔ぶれの上限を決める契約は `tests/test_mcp_verbs.py` にひとつだけ置く** ——
  2ファイルに書くと、片方の更新を忘れた瞬間に実装側が詰む。
- 「未知のツール」の例を `no_such_tool_ever` へ差し替え（実在しえない名前を使う）。
- M4節の散文（本文 `duocad/mcp_server.py — 公開するツールは3つだけ`）に失効の断りを入れた。
  3つの**振る舞い**の記述は現役である。

## 実行方法（F）

```
.venv\Scripts\python.exe -m pytest tests/test_mcp_verbs.py tests/test_mcp_selection.py -q
```

**素の `python` は使わない** —— `OCP` を持たない別環境で収集エラーになる。
`test_exporting_does_not_corrupt_the_stdio_channel` は実プロセスを起こし、既定と違うポート
（18775/18776）を使う。`python -m duocad.server` を開いたままでも衝突しない。

## F2への申し送り

- **`descriptors()` はF1で書いていない。** ツールの説明文と引数スキーマはF2の成果物である
  （説明文はAIがツールを選ぶ唯一の手がかりなので、「何ができるか」ではなく
  「**いつ使うか**」を書く。既存3つの流儀に合わせること）。
  したがってF1完了時点の赤の主因は「未知のツール」であって、スタブの `NotImplementedError`
  ではない（A1のときと違う点）。
- **`_check_arguments` を強くする必要がある。** 今は `boolean` / `string` しか見ておらず、
  `number` / `integer` / `array` / `object` と**型のリスト**（`fillet.radius` は数値と配列の
  両方を取る）に対応していない。**boolを数値として通さない**こと（Pythonでは `True` が
  `int` の一種）。エラーメッセージに**引数名を含める**（テストが名指しを要求する）。
- **`DocumentTools` にF1が入れたスタブがある**（`F2_TOOLS` とメソッド群）。docstringに
  実装時の要点を書いてあるので、それを消さずに中身を埋めること。
- **`tests/test_mcp.py` はF1で3箇所直してある**（上記「M4の契約をひとつ退役させた」）。
  F2はここを触る必要が無い —— 触りたくなったら、それは28個の契約と食い違っている合図である。
- **1つの図形を2つの反復で使い回せない**（コアが ValueError で拒む。F1で実測）。
  `pattern_rectangular` と `pattern_circular` のテストが別々の図形を使っているのはそのため。

## Fの実装で分かったこと（2026-08-12・`duocad-mcp-full-logic`）

**契約91件＋QAで足した2件の計93件が緑。全スイート 470件中469 passed**（赤1件は
`test_extruding_one_part_of_the_pc_case_stays_under_1000ms` の壁時計フレークで、
F1の着手前から赤い。この回に4回計測して 1421.8 / 1127.6 / 1056.5 / 1095.5 ms・閾値1000ms。
claude 16 + Cursor 28 プロセスが同時稼働している負荷環境）。

実装は cursor(auto) へ**4回に分けて**委譲した。1回で全部を頼んだ1回目は
**descriptors 28個と `Session` 側のverbの口までで打ち切られ**、25個のツール本体がスタブのまま
返ってきた（`_revision_snapshot` という書きかけの呼び出しが残り、M4から緑だった
`tests/test_mcp.py` の3件が赤に落ちていた）。以降は
**土台 → 選択7個 → 残り18個＋MCP.md → QA対応**の4回に割って、各回とも委譲元が `pytest` を
回して確認した。**次に大きい委譲をするときも、最初から3〜4回に割る前提で計画すること。**

### `fillet` / `chamfer` が選択のBodyを捨てていた（QAが見つけた・**この節で最も重要**）

確定した辺の参照は `body` と `index` の両方を持つ。実装は `index` だけを取り出して
`Session.fillet(edges, radius, body_id=None)` を呼んでおり、`body_id=None` はコア側で
**アクティブなBody**になる。つまり**選択が指しているのと別の部品が丸まる** —— 27部品の
PCケースなら「同じ添字の別部品の辺」を黙って変える。例外も出ない。

**F1の契約が1Bodyの直方体でしか検査していなかったので素通しした。** 交互操作の一巡を通す
`test_the_whole_interleaved_lap_runs_through_mcp_alone` は緑のままだった。`extrude_face` は
面の参照から `body` を渡していて、**辺の側だけが抜けていた**（同じ形の穴が片方だけ空く典型）。

QAの指摘を受けて `test_a_verb_targets_the_body_the_selection_names`（fillet / chamfer の2件）を
足した。**アクティブでないBodyの辺を選んで丸め、無関係なBodyが変わらないことを見る** ——
対応前は実測で `changed_bodies` が `['other']` になっていた。

**教訓**: 「選択から形を取る」設計では、**参照が運んでいる情報を全部使っているか**を
verbごとに確かめること。添字だけ使って `body` を落とすのは、型でも例外でも検出されない。

### 拒否の判定は型付き例外にした（`SelectionNotCommitted`）

最初の実装は「選択が proposed **かつ例外メッセージに `commit_selection` を含む**」で
`selection_not_committed` を判定していた。理由は正当で、**origin を見るだけでは
「提案中に起きた別の ValueError」（不正な参照など）まで畳んでしまう**（実際に
`test_a_malformed_reference_is_refused_without_touching_the_selection` が落ちた）。

しかしメッセージ文字列への依存は、コア側の文言を変えた瞬間に `invalid_argument` へ落ちる
＝ **決定3が禁じた潰し方**をコードが自分で再現する。QAの結論に従い、
`duocad/selection.py` に **`SelectionNotCommitted(ValueError)`** を足して
`confirmed_items` が送出し、MCPは `isinstance` で名前を付けるだけにした。

**`ValueError` の派生にしてある**ので、A1が書いた `pytest.raises(ValueError)` の契約は
そのまま生きている。**門は依然として `confirmed_items` の1箇所だけ**で、MCPは先回りしない
（`test_the_gate_lives_in_the_core_not_in_the_mcp_layer` がその向きを見張る）。

### 「これで全部」型のアサーションは3箇所にあった

F1のQAが `tests/test_mcp.py` の「ツールは3つだけ」を見つけたが、**同じ主張が
`web/scripts/smoke-mcp.mjs` にもあった**（`if (tools.length !== 3) fail(...)`）。pytestを
全部緑にした後で実機のsmokeを回して初めて発覚し、こちらもM4の3つの部分集合検査へ退役させた。

**上限の契約は1箇所に置く**（今は `tests/test_mcp_verbs.py` の `EXPECTED_TOOLS`）。
実機スクリプトは pytest とは別に走るので、**pytestが全部緑でも実機だけ赤い**という形で残る。

### 別経路の確認（pytestとは別に、実プロセス・実ブラウザで通した）

1. **実プロセスのMCPサーバへstdioで接続**（`python -m duocad.mcp_server` を起こし、
   JSON-RPCを直接書く）。空の文書からMCPだけで箱を作り、`describe_document` →
   述語で辺を4本提案 → **提案のままでは `selection_not_committed`** → `get_selection` →
   `measure`（提案のままでも測れる） → `modify_selection` で1本足して1本引く（`[2,5,8,4]`・
   **選択順のまま**） → `commit_selection` → `fillet(r=2.0)` で **面 6→10 / 体積
   36000.000→35872.006**。**A2 が素のPythonで通した一巡と同じ数字**である。
2. **本物のWebSocket接続**（ブラウザのふり）に、丸めた後の `face_count` と
   **AIが書き換えた選択**の両方が届くことを確認。差分に新しい `geometry` が入っている。
3. `export_step` で32KB書き出した**後も** JSON-RPCが続く（下記のstdout対策が効いている）。
4. **本物のEdge**（`web/scripts/smoke-mcp.mjs`）でPCケース27部品を読み、MCPが26部品を隠すと
   画面が変わり、奥の面が本物のクリックで選べるようになるところまでPASS。

### 実測: OCCTのSTEP書き出しがstdoutを汚す件は本当に効いていた

F1が予測して契約に入れた `os.dup2` によるfd振り替えが無ければ、`export_step` を公開した
時点でMCP接続が壊れていた。**pytestの同一プロセス内テストでは検出できない**ので、
実プロセスを起こす `test_exporting_does_not_corrupt_the_stdio_channel` が唯一の検査である。

### 委譲の記録

- cursor(auto): **6回**（実装4回・QA2回）
- cursor(個別モデル指定): **1回**（`gpt-5.6-sol-high` / **月次上限で失敗**・リセット 2026-08-19）
- QAは2ラウンド。1回目 **不合格**（major 1・minor 6）→ 全件対応 → 2回目 **合格**（指摘なし）
- 実装は cursor、QAも cursor(auto) だが、**契約を書いたのは opus** なので
  「実装者と別のモデルがレビューする」原則は保たれている

### 完了後の状態

**§0の到達表のAI列（19操作）が全部埋まった。** 残るのは人間側（B → C → D → E）と
統合検証（G）である。推奨順序どおり**次は B（コマンド層の骨格：状態機械・複数選択・
数値入力・プレビュー・Esc）**。

**実装計画書 `duocad/docs/IMPLEMENTATION_PLAN.md` の §0 の到達表は
まだ「AI 1/19」のままである**（`reports/` はこのタスクの target_dir 外なので触っていない）。
次にあの文書を触る人が更新すること。

---

# B — コマンド層の骨格（2026-08-13追記。UIタスクなので契約の大半は pytest の外にある）

タスクBの本体は**アプリ層**（引数を集める共通の仕組み）で、`pytest` では完了判定できない。
それでも B はコアとサーバに2つ足したので、**その2つだけは pytest が固定する**
（`tests/test_preview.py`・11件）。

## 何を足したのか

| 足したもの | 場所 | なぜコアなのか |
|---|---|---|
| **投機的実行** `speculative(doc)` | `duocad/speculative.py` | プレビューをブラウザ側の仕掛けにすると、AIが「適用したらどうなるか」を試せない（§2「コアはUIが無くても機能的に完全」） |
| **`Session.preview(verb, args)`** | `duocad/server.py` | 上を使って形だけ測り、文書を元へ戻す |
| WebSocketの選択3コマンド | `duocad/server.py` の `_dispatch` | 既にMCPにあったものをブラウザにも開けただけ。**人が過不足を直す経路が無いと交互操作は片道** |

**verbは1行も変えていない。** 実装計画書§2の図は verb層を純関数と描いているが、実装の verb は
`body.shape` を差し替えて操作ログへ1行積む。全verbを純粋にする書き換えは由来マップと
undoスナップショットまで巻き込む大手術なので、**「元へ戻す」側を1つ置く**方を採った。

## `Session.preview` が固定している契約（`tests/test_preview.py`）

1. **形を返す** — 変わった可視Bodyだけ。`geometry` は `state`/`delta` と同じ形で、
   アプリ層は本物と同じ道具で描ける。**本物を実行したときの体積と一致する**
2. **何も残さない** — 操作ログ・カーソル・形状・体積・選択・`applied_count`・
   テセレーションのキャッシュ。連続で5回投げても同じ
3. **何も発火しない** — `_emit` が `_suppress_events` で止まる。**経路によらず**止まるので、
   MCPから直接呼んでも中間状態は配信されない
4. **失敗しても何も残さない** — 距離0で落としても文書は同一
5. **登録制** — `PREVIEWABLE_VERBS` に無いverbは `ValueError`。ソケットから届いた文字列で
   `Session` の任意のメソッドを呼べる作りにしない。**Bの範囲は `extrude_face` 1つ**で、
   C・Dはここへ1行足す

`preview` は**状態を配信しない**（要求した接続にだけ返る）。したがって `_dispatch` でも
`_apply` で囲まない —— 囲むと、押し出し約1秒の計算中にMCPから届いた**本物の**変更まで
「WebSocket経由だから配信不要」と誤判定して捨てる。

## Escで「文書が一切変わらない」の実体

UIのEscが消すのは**画面の幽霊と打ちかけの数値だけ**である。サーバ側には最初から何も
残っていない（プレビューは投機的実行）。だから `test_preview_leaves_nothing_behind` が
緑であることが、実機のEscの正しさの土台になっている。

## 実機で確認したこと（`pytest` の外・2026-08-13）

`web/scripts/smoke.mjs`（人間側の経路）に4つ足した。**すべて本物のEdgeで PASS**:

- **Shift+クリックで複数選択**（面2つ）→ 選択パネルの件数表示が「面 2面」
- **Ctrl+クリックで1本外す** → 残るのは最初に選んだ面
- **選択パネルの「外す」で1件落とす**（3Dで指せない辺・頂点を人が直す経路と同じもの）
- **距離を打つとプレビューが出る** → **Escで中断すると `applied_count`・操作ログの長さ・
  体積のどれも動かない**

`web/scripts/smoke-mcp.mjs`（AI側の経路）に**検証項番5の一巡**を足した:

> AIが述語で辺4本を提案 → **ブラウザに提案として出る**（枠の色・「AIの提案」の札・承認ボタン）
> → 人がクリックで1本外す → **AIが `get_selection` で読むと外れており `origin=user`**

これが通ったことで、交互操作が**人間側から見える形で**成立している。

## 実機検査で踏んだ落とし穴 — 画面の伸縮がピックをずらす

選択パネルの高さを内容任せ（`max-height`）にしたところ、**面を1つ選んだ瞬間にパネルが伸びて
ビューポートがリサイズされ、カメラのアスペクトと射影が変わった**。その結果、
「クリック前に測った画素」が別の部品の面を指し、2回目のクリックが隣の部品に当たった。
`SelectionPanel` の高さを固定して塞いである。実機スクリプト側も**クリックの直前に射影し直す**
（測った座標を使い回さない）。

## Bで**契約にしなかった**こと（範囲の明示）

- **辺・頂点の3Dハイライト**。ビューポートが持っているのは面ごとの三角形メッシュだけで、
  個々の位相辺のジオメトリを持っていない（面の境界線は `EdgesGeometry` による見た目であって、
  コアの辺の添字とは対応しない）。辺の選択は選択パネルが一覧と件数で見せる。
  **3Dで辺を指せるようにするのは D（フィレット・面取り）の債務**である
- **可視性が変わったときの選択の掃除**。M1はブラウザ側で「見えなくなった面の選択を外す」を
  していたが、選択がコアへ移った以上、勝手に外すとAIが書いた選択をUIが黙って壊す。
  コアが「見えないものは選べない」を入口（`select_*`）で守る現状の契約に任せている
- **C・D・Eのコマンド**。骨格と `extrude_face` の載せ替えまでがBの範囲

---

# C（スケッチのコマンド） — 2026-08-13

**手動操作が一級の操作モードになった回。** スケッチはM2からコアとMCPにあり、AIからは描けたが
**人からは描けなかった**。Cが埋めたのはその片道で、**コアには何も足していない**
（WebSocketに口を開け、描ける形で配信しただけ）。契約は `tests/test_sketch_ui.py`（12件）。

## サーバに足した2つ

1. **`SKETCH_COMMANDS`** — ブラウザから踏めるスケッチのverb（`sketch_create` / `add_line` /
   `add_circle` / `add_rectangle` / `add_arc` ＋ 値の打ち直しの `set_parameter`）。
   `PREVIEWABLE_VERBS` と同じ登録制で、**引数が1つでも欠けたら失敗させる**
   （既定値で補うと、打ち忘れた寸法が黙って0で入る）。
2. **`state.sketches[]`** — 平面の3D枠（`frame`）・図形・領域を**画面に描ける形**で配る。

```
sketches[] = {
  sketch_id, plane,
  frame: {origin, u, v, normal},          # p = origin + u*uAxis + v*vAxis
  entities: [{entity_id, kind, params, refs}],   # refs = パラメータ名（打ち直し用）
  drawn:    [{kind, params}],                    # 反復を展開した描画用
  regions:  [{index, area, centroid, positions, indices}],
}
```

- **`entities` と `drawn` を分ける**。前者は人が値を打ち直す対象、後者は反復を展開した
  「実際に見えるもの」。1つにすると、反復した図形が画面に出ないのに領域だけ増える。
- **`frame` はコアが配る**（`kernel.sketch_plane_frame`）。基準3面の軸の取り方と、面に貼った
  ときの法線反転（`ZReverse`）の扱いをアプリ層に再実装させると必ずずれる。
- **領域はキャッシュする**。`BRepAlgoAPI_Splitter` ＋ テセレーションは重く、`state` は変更の
  たびに全接続へ配る。キャッシュは**内容の署名**（解決済みの図形と平面）と、**貼った面の
  形状オブジェクトの同一性**（`is` で比較。`id()` の整数を控えるとアドレス再利用で偽ヒットする）
  で引くので、`set_parameter` で値が変われば必ず作り直される。

## アプリ層 — Bの骨格に載せた（作り直していない）

- 骨格へ足したのは**点（point）引数**だけ。選択でも数値でもない3つ目の集め方で、
  スケッチ平面のクリックから (u,v) を集める。**選択集合には入れない**
  （線の始点が共有選択に溜まると、次のコマンドが引数として拾う）。
- **スケッチの幽霊はブラウザで描く**（`CommandDef.preview: 'local'`）。押し出しと違って形は
  ブラウザ側で分かっており、投機実行にすると中断しても文書に一度は入る。**そもそも送らない**
  ことで「Escで中断しても文書が一切変わらない」を構造的に成立させた。
- **スケッチモードの既定コマンドは「選ぶ」**。描くコマンドが常に点を待っていると、領域を
  狙ったクリックが図形の頂点になる。**確定しても描くコマンドは張られたまま**で(同じ図形を
  続けて描けるほうが速い)、「選ぶ」へ戻るのは**Escを押したとき**である。
- **モード中は面を拾わない**（`PickEvent.hit` は常に null）。奥のソリッドの面を掴むと、線を
  引くつもりのクリックが選択の置き換えになる。領域が手前・平面上の点がその次。
- **Enterの受け皿を窓に足した**。点だけで引数が揃うコマンド（線分）は数値欄が無く、
  B の時点では確定できなかった（数値欄の `keydown` しか Enter を拾っていなかった）。

## 実機で確認したこと（`pytest` の外・2026-08-13）

`web/scripts/smoke.mjs` に**タスクCの検証1〜5**を足した。**すべて本物のEdgeで PASS**:

- **基準平面（XY）と既存ソリッドの平面状の面の両方**にスケッチを作れる
- **線分・円・長方形・円弧**を手で描け、**数値で寸法を打てる**（円は中心をクリック→半径を打つ）
- **円を線分で分けると領域が2つに割れ、片方を本物のクリックで選べる**（共有選択集合に入る）
- **Escで中断**すると図形も操作ログも増えない
- **置いた後に寸法を打ち直せる**（半径 20→12 で領域の面積 826.2→342.6 mm²）
- **undoで図形が1つずつ消える**

`web/scripts/smoke-mcp.mjs` に**検証6**を足した:

> 人がブラウザで描いた円が、AIの `describe_document` に半径ごとそのまま見えている
> （`include_regions: true` で領域数も一致）。**片方にしか無い状態を作らない。**

## 実機検査で踏んだ落とし穴 — 選択は1往復あとに落ち着く

押し出し・undoの**後継の選び直し**は「サーバの状態が届く → アプリ層が `modify_selection` を
送る → もう一度状態が届く」の**もう1往復**で決まる。体積が戻った瞬間の選択はまだ1つ前の面を
指しており、そこへスケッチを貼ろうとすると `face 34 is cylinder_partial` で失敗した。
実機スクリプトは**平面が1つ選ばれるまで待つ**ようにしてある（`applied` の到着では待たない）。

もう1つ: 画面の伸縮がピックをずらす件（B節）と同じ理由で、**`SketchPanel` の高さも固定**した。

## Cで**契約にしなかった**こと（範囲の明示）

- **押し出し・フィレット・反復のコマンド**（Dの範囲）。領域の選択までがCで、それを引数に
  取るverbはDが載せる
- **図形の削除・移動・拘束**。コアにverbが無い（v1の範囲外）
- **スケッチ平面そのものの描画**。無限平面を描くと奥のソリッドが隠れ、「既存の部品の上に
  描く」が見えなくなる。描くのは図形と領域だけ

---

# D（フィーチャのコマンド） — 2026-08-14

**AIからしか使えないverbが無くなった回。** 領域押し出し・フィレット・面取り・矩形反復・
円形反復はM2〜M3でコアに入り、F2でMCPに開いたが、**ブラウザからは1つも呼べなかった**
（Cのスケッチと同じ片道）。契約は `tests/test_feature_ui.py`（25件）。

## コアに足した1つ — 辺を折れ線で配る

`kernel.tessellate_edges(shape, deviation) -> [{index, kind, length, closed, points}]`。

**面がクリックで選べるのは面ごとの三角形を配っているからで、辺も配らないと人は辺を指せない。**
それまで画面に出ていた輪郭線は three.js の `EdgesGeometry` による見た目であって、**コアの辺の
添字とは対応していなかった**（どの線を指してもフィレットの引数にならない）。ここが無いと
フィレット・面取りは人間側から永久に使えないままになる（`viewport.ts` が「Dの債務」と
書いていたのがこれ）。

- **添字は `_edge_map` の1始まり** = `edge_count` / `edge_of` / `fillet(edges=[...])` と同じ。
  ずれると「指した辺と違う辺が丸まる」という、例外も出ない静かな事故になる。
  `describe_edges`（端点は build123d で測る別経路）と端点が一致することを契約にして、
  **2経路の突き合わせ**にしてある（`describe_faces` の法線が tessellate と突き合わせられて
  いるのと同じ規律）。
- 折れ線は `GCPnts_QuasiUniformDeflection` で `deviation`(mm) 以内。直線は2点、円は分割される
  （弦1本で返すと、円筒の丸い稜線が直線に見えて狙えない）。
- **分割に失敗した辺も端点2点で返す**。落とすとその辺だけ画面から消え、指せない辺になる。

## サーバに足した2つ

1. **`FEATURE_COMMANDS`** — ブラウザから踏めるフィーチャのverb（`extrude` / `fillet` /
   `chamfer` / `pattern_rectangular` / `pattern_circular`）。`SKETCH_COMMANDS` と同じ登録制で、
   **引数が1つでも欠けたら失敗させる**。とくに `mode`（join/cut）を必須にしてある ——
   既定の `"join"` に落ちると**「切り取るつもりで足した」が黙って通り**、しかも形が増えるので
   失敗に見えない。
2. **`get_edges`** → `{"type":"edges", body_id, revision, edges:[...]}`。**`state` にも `delta` にも
   入れない**（要求した接続にだけ返る。`preview` と同じ扱い）。常時配ると27部品(2835面)の
   初回配信に折れ線が丸ごと乗り、M3.5の差分配信で稼いだぶんを使い切る。`revision` を返すのは
   **クライアントが「今の形の辺か」を判定できるようにする**ため（§7「添字は形状変更で壊れる」の辺版）。

`PREVIEWABLE_VERBS` に `extrude` / `fillet` / `chamfer` を足した。**反復は載せていない** ——
安全のためではなく、変わるのがスケッチの図形でソリッドではないので、`preview` の応答
（形が変わったBodyの一覧）が常に空になるため。

## アプリ層 — Bの骨格に載せた（作り直していない）

- 骨格へ足したのは**選択肢（choice）引数**と**図形（entities）引数**の2つ。前者は押し出しの
  結合/切り取り（**既定を持たない**）、後者は反復の対象。
- **図形は共有選択集合に入らない**（コアの `SELECTABLE_KINDS` は面・辺・頂点・領域の4種）。
  MCP側も反復だけは図形IDを引数で受けており、スケッチパネルの一覧が**人間側の対**になる。
- **辺は要求しているコマンドが起動している間だけ出す**。常時出すと(1)27部品ぶんの辺が要り
  (2)辺は面の手前に重なるので面を狙うクリックが辺に取られ、M1の押し出しが遅くなる。
- **奥の辺も見え、奥の辺も指せる**（深度検査をしない）。three.js の線のレイキャストは面による
  遮蔽を見ないので、見た目だけ隠すと「見えない辺が選ばれる」になる。**見えているものと
  指せるものを一致させる**側へ揃えた。
- **失敗しても、やり直せる状態にして返す**。フィレットの失敗は握りつぶさず理由を画面へ出し、
  **選択はコア側にあるので消えず**、打った値も戻す（§7: r=10.0 失敗 / r=10.5 成功 という
  非単調な失敗では、すぐ近くの値で打ち直すのが実際の使い方になる）。
- **成功したときは逆に、丸めた辺の選択を外す**（QA指摘 2026-08-14）。形が変われば辺の並びは
  作り直されるので、残った添字は**別の辺**を指す —— 強調表示のまま残すと、続けて半径を打った
  ときに「選んだ覚えのない辺が丸まる」。**コア側では落とさない**: 「選択は操作をまたいで残る」は
  A1の契約（`test_mcp_selection.py` の交互操作の一巡が fillet 後も選択が残ることを固定している）
  で、AIは次の一手をそのまま打てることを前提にしている。`extrude_face` は後継の添字
  （`after_face_index`）が返るので選び直せるが、**フィレットには辺の後継の対応が無い**ため、
  UIができる正直なことは「外して選び直させる」だけである。
  → **AI側には同じ持ち越しが残っている**（MCPで fillet した後の選択は古い添字のまま）。
  A1の契約と§7が真正面からぶつかる箇所なので、**辺の後継を返すのはコア側の宿題**。
- **領域は姿が変わったら選択を外す**。消えた領域は送信前に止められるが、**分裂は止まらない**
  （添字は1始まりの連番なので、割れても古い添字は範囲内に残り、黙って別の領域を押し出す）。
  面積と重心の印を毎回突き合わせ、変わったら選び直させる。
- **反復にプレビューを出さない**。展開の規則（全周は `360/count`、回転した長方形は4本の線分へ
  落とす）はコアが持っており、幽霊のためにブラウザへ二重実装すると「画面の予想と実際が違う」を
  自作することになる（§2 形の定義は1箇所）。
- **視点を全体へ戻す手（`#view-fit`）を足した**。`lookAtSketch` は図形の大きさに寄るので、
  半径4mmの円を描いてモードを抜けると40mmの板が画面の外にある（M1は視点が動かなかった）。

## 実機で確認したこと（`pytest` の外・2026-08-14）

`web/scripts/smoke-features.mjs`（新設）に**タスクDの検証1〜5**を入れた。**すべて本物のEdgeで
PASS**。**空の文書から始める**のがこのスクリプトの要点で（当時は `--no-step` で起動していた。
**このオプションは2026-08-15に廃止**され、現在は常に空で起動する — 末尾の廃止節）、27部品が居る文書では
`join` の押し出しが既存の部品へ融合してしまい、**最初の板が生まれる経路**を通れない:

- **手だけで板に穴を開けた**（長方形→押し出し 12000mm³ → 円→cut で 11497.35mm³、減りは π×4²×10）
- **辺を2本、本物のクリックで選んで丸めた**（2本目はShift+クリック。面 7→9）
- **失敗する半径（1000mm）では理由が画面に出て、選択2件と打った値がそのまま残る**
- **Escで中断**すると操作ログは伸びず、**選択は残る**（選び直しにならない）
- **undo**でフィレットも反復も1操作として戻る
- **円い辺を面取りして円錐ができた**（面の種別 平面/全周円筒/円錐が手だけで揃った）
- **矩形反復 3×2 = 6個**、**円形反復は全周8個で隣り合う角度が45度**（360/8。誤差 4.5e-6度）

**検証の範囲を正確に書いておく**（QA指摘 2026-08-14。「PASS」の意味を後から誤読しないため）:

- 検証3の文言は「`PC_case_feet.step` と同じ**構成**（平面3 + 全周円筒1 + 円錐1 = 5面）」だが、
  実機で確かめたのは**その3種の面が手だけで作れること**であって、5面の部品そのものは作っていない
  （あの部品は段のぶんの平面をもう1枚持つ形で、現行のverbでは寸法まで再現できない —— これは
  D以前から分かっている制約で、Dで変わっていない）。
- 検証5「**すべてのコマンドで** Esc と undo」は、Escをフィレットで・undoをフィレットと矩形反復で
  確かめた。**状態機械は1つ**（`CommandHost.svelte`）で、Esc も undo もコマンド定義に依らない
  ので代表確認で足りる、という論法に立っている。**コマンドごとに状態機械を作らない**という
  タスクBの決定が、この論法の前提そのものである。

検証6（MCP経由でも一致）は pytest 側にある —— UIが叩く `FEATURE_COMMANDS` 経由の
`Session.fillet` と、AIが叩く `DocumentTools.fillet` が**同じメソッドに集まる**ことを、
面数・体積・操作ログで突き合わせている。

## 実機検査で踏んだ落とし穴

- **辺は「届いた」だけでは指せない**。形が変わると古い辺は捨てられるので、`revision` が今の
  Bodyと一致するまで待つ必要がある。本数だけで待つと**1つ前の形の辺**を見て、そこから拾った
  添字が今の形の別の辺を指す（§7の壊れ方そのもの）。
- **選択は1往復あとに落ち着く**（C節と同じ）。辺をクリックした直後に `selection` を読むと、
  まだ前の状態を見ている。
- **選択肢のボタンはフォーカスを外してから返す**。ボタンにフォーカスが残ると、続けて押す
  Enterを状態機械が（ボタンの操作とみなして）無視するので、「向きを選んでEnter」が確定しない。
- **サーバは長寿命**（文書は接続より長生きする）。`smoke-features.mjs` は空の文書を前提にするので、
  **走らせるたびにサーバを起動し直す**。前回の板が残っていると「最初のソリッドが生まれる」経路を通れない。
- **空の文書で起動する理由**（当時のオプションは `--no-step`。**2026-08-15に廃止**し常に空で起動する）。
  27部品が居る文書では `join` の押し出しが既存の部品へ融合してしまい、
  「対象が無ければ新しいソリッドを作る」というverbの分岐（＝最初の板が生まれる道）が通らない。

## Dで**契約にしなかった**こと（範囲の明示）

- **パラメータ編集パネル・操作履歴パネル・ファイル操作**（Eの範囲）
- **辺ごとに違う半径のフィレット**。コアは受け取れるが、UIから辺ごとに打たせる場面がまだ無い
- **頂点の選択**。v1のverbで頂点を引数に取るものが無い

---

# E（パネルとファイル操作） — 2026-08-14

**作ったものが手元に残るようになった回。** 保存・開く・書き出しはそれまで**MCPからしか
呼べず**、人はブラウザで作れても保存できなかった。あわせて、M3で入れた「名前つきの寸法値」を
人が触れる面(パラメータ編集パネル)と、文書が操作の並びであることが見える面(操作履歴パネル)を
出した。契約は `tests/test_files_ui.py`（14件）。

## コアに足した1つ — `OpLog.redo_count`

`can_undo` / `can_redo` の真偽だけでは、**3回undoした画面と1回undoした画面が同じに見える**。
`entries` はカーソルまでの写しなので、外からは末尾に何本残っているかを知る手段が無かった。

## サーバに足した3つ

1. **ファイル操作の口**（`FILE_COMMANDS` ＋ `open_document`）。**verbかどうかで応答が違う**:
   `import_step` / `make_editable_copy` は文書を変えるので状態配信、`save_document` /
   `export_step` / `export_stl` は変えないので**要求した接続にだけ**結果（書いた先の絶対パスと
   バイト数）を返す。`open_document` は文書を**差し替える**ので全接続へ配る（表からは外して
   個別に扱う）。
2. **`state.parameters`** — 名前つきの寸法値。打ち直しの対象そのもので、配らないと
   パラメータ編集パネルは何も出せない。
3. **`state.redo_count`** — 履歴の位置。

**パスの解決はサーバだけが行う**（相対はサーバの作業ディレクトリ基準）。ブラウザ側でも
正規化すると解釈が2箇所に分かれ、「保存したと言われたファイルが見つからない」が起きる。
書いた先は必ず**絶対パスで応答に載せて**画面へ出す。

## 開くときの2つの落とし穴（どちらも実機で踏んだ）

- **ログを文書へ束ねてから再生する**。`parameters.rebuild` は「その操作までを作り直す」ために
  `document.log` を頭から読むので、`log.replay(doc)` を先に呼ぶと **`set_parameter` を含むログが
  開けない**（理由つきで拒否される）。他のverbだけのログは開けてしまうので気づきにくい。
- **版（revision）の数え上げは捨てない**。Bodyのidは操作ログに記録され再生で同じidが再現しうるので、
  版まで1へ戻すと、そのidを知っているクライアントが「変わっていない」と判断して**前の文書の形を
  描き続ける**。捨てるのは状態（`_revision_state`）だけでよく、次の `body_revision` で必ず1つ上がる。

**開くのに失敗しても今の文書は壊れない**（新しい `Document` の上で再生し切ってから差し替える）。

## アプリ層 — Bの骨格に載せた（ファイル操作も「コマンド」）

タスクEの制約が「ファイル操作もコマンドとして載せる」。ボタンとダイアログを別に作らないので、
**Escで中断できる・引数が揃うまで確定しない・何を待っているか1行で出る**が、押し出しと同じ
仕組みでそのまま効く。骨格へ足したのは**文字列（text）引数**だけ（数値でも選択肢でもない3つ目の
打ち込み）。**確定してもパスは消さない** —— 同じ場所へ保存し直す・書き出し先を1文字直す、が
普通の使い方だから。

## 「AIがやった操作」と「人がやった操作」の区別 — **半分しかできない**

タスクEは「区別できると望ましい（できなければ理由を書く）」。**この画面が出した操作にだけ
印が付く**というのが結論で、理由は**操作ログに主体が入っていない**こと（§3.6の `Operation` は
seq / verb / args / selection / provenance のみ）。本物にするならコアの永続形式を変えることになり、
保存済みのファイル・MCP・再生経路すべてに波及する。したがって印が無い＝AIの操作**ではない**
（再読込より前の自分の操作にも印は無い）。見出しも「自分」だけを立て、他方には何も書いていない。

印の付け方には2つ落とし穴がある（どちらもQA指摘 2026-08-14で直した）:

- **`pendingOp` と突き合わせない**。パラメータの打ち直しや可視性の切り替えは「応答待ち」を
  立てない経路なので、**自分でやったのに印が付かない**（パラメータ編集はEの中心操作そのもの）。
  出した `client_op` を全部控えて照合する。
- **seqは使い回される**。undoで捨てた操作の番号は次に積まれた操作が再び使うので、控えを
  消さずに溜めると**AIが積んだ操作に「自分」が付く**（取りこぼしより悪い）。新しい操作の
  seq を受けたら、その番号以上の控えを先に落とす。文書ごと差し替わったら全部捨てる。

## 一覧の落とし穴 — `flex-direction: column-reverse` で新しい順に並べない

「新しい操作を上に」をCSSの反転でやると、**溢れた分へスクロールで到達できなくなる**
（QA指摘 2026-08-14。headless Edgeで実測: 30行入れても `scrollHeight === clientHeight` で
`scrollTop` が0に固定され、見えるのは最新の約4行だけ）。スクロール容器の main-start 側へ
押し出されたコンテンツは到達不能なオーバーフローになる、というCSSの仕様どおりの挙動である。
**配列を逆順にして通常の `column` で描く**のが正しい。当時の既定 `--step` 起動では開幕から
55件（import 1 + make_editable_copy 27 + set_visible 27）積まれるので、実運用で即座に出た。
（**`--step` は2026-08-15に廃止**。アプリ内importは操作ログ1行なので開幕55件は生じなくなったが、
操作を重ねれば同じ問題に当たるため、この教訓は生きている。）

**検査の教訓**: 実ブラウザのsmokeでも、テキストや data 属性だけ読んでいると素通りする
（この件は最初 PASS のまま抜けた）。一覧UIは**先頭以外の行に実際に到達できるか**まで見る。

## レイアウトの落とし穴 — 高さ固定のパネルは `flex: 0 0 auto` にする

`height: 96px` だけでは足りない。パネルが増えると flex は**全部を等しく押し潰しにかかる**ので、
Eでパネルを2枚足した時点で `SketchPanel` が 96→55px に潰れ、**図形の入力欄が枠の外へ出て
クリックできなくなった**（Cの実機検査が落ちて気づいた。値が `20` のまま `12` を打って `2012` に
なる、という分かりにくい壊れ方をする）。**縮んでよいのはビューポート（`.main`）だけ**である。

## 実機で確認したこと（`pytest` の外・2026-08-14）

`web/scripts/smoke-files.mjs`（新設）に**タスクEの検証1〜5**を入れた。**すべて本物のEdgeで PASS**:

- **穴の径を1箇所打ち直したら、反復で作った3つの穴が全部追随した**（半径2→3で体積 5811.50→5575.88）
- **保存 → 画面を再読込 → 開く で同じ形が戻った**（体積・面数とも一致。操作ログから再生）
- STEP/STL を書き出し（偏差を指定できることを応答で確認）、**STEPを読み込んで
  `make_editable_copy` で編集可能にできた**
- 履歴パネルに位置が出る（「7 件が有効 ・ あと 1 件やり直せる」）。undo/redoで動く

**検査そのものの落とし穴**: 「保存 → 開く → 同じ形」は、**サーバが長寿命**なので開くのが
失敗しても体積が一致して**素通りする**（実際に一度素通りした）。開く前にundoで形を変えてから
開き、戻ることを見ている。あわせて、**画面の通知欄にエラーが出ていないこと**を最後に検査する
（サーバのエラーはブラウザのコンソールには出ないので、見ないと「失敗しているのに緑」になる）。

## Eで**契約にしなかった**こと（範囲の明示）

- **ファイル選択ダイアログ**。ブラウザからサーバのファイルシステムは見えないので、パスは打つ。
  サーバが解決した絶対パスを返すことで「どこへ書いたか」は必ず分かる
- **操作ログの主体（誰が実行したか）の永続化**。上記のとおりコアの契約変更になる
- **履歴の任意の位置へ飛ぶ**（クリックでその操作まで戻る）。undo/redoの回数で足りている

---

# 起動オプション `--step` の廃止と verb `import_step_editable`（2026-08-15）

## 何が問題だったか（本人の指摘）

**同じ「STEPを読む」が起動時とアプリ内で違う意味になっていた。**

| | 何ができたか |
|---|---|
| 起動時 `--step`（`Session.load_step`） | ソリッドごとに 参照ボディ + 編集用コピー + 参照を隠す。読んだ直後から触れる |
| アプリ内・MCP（`io_step.import_step`） | 参照ボディ（読み取り専用）だけ。編集するには `make_editable_copy` が1ボディずつ |

PCケース（27ソリッド）をアプリ内から読むと、面をクリックして「編集可能にする」を**27回**やらないと
触れる状態にならない。**この非対称のせいで起動オプションが消せなかった。**
方向は「起動時にしかできないことを無くす」であって、CLI引数を削ることではない。

## 足したverb（`duocad/verbs/import_editable.py`）

```
import_step_editable(doc, path) -> list[BodyId]        # 戻り値は**編集用ボディ**のID
```

- ソリッドごとに 参照ボディ（`kind="reference"`・**`visible=False`**）と
  編集用ボディ（`kind="solid"`・可視）を作る。名前は `<stem>_N` と `<stem>_N-edit`
  （起動時に `--step` が作っていた名前と同じ）。
- **操作ログは1行**（`verb="import_step_editable"`,
  `args={"path":.., "reference_ids":[..], "body_ids":[..]}`）。undo1回で34個（参照17+編集用17）が
  まとめて消え、redo/replayは記録したIDをそのまま使う。
- 参照を隠すのを `set_visible` の別行にしていない（`load_step` の旧実装との違い）。
  1行の中で `visible=False` を作るので、再生で「隠した」操作が失われる余地がない。
- 失敗（パス無し・壊れたSTEP）は `kernel.read_step` の中で止まり、documentにもログにも何も残らない。
- 契約は `tests/test_import_split.py` の後半6件（`test_editable_import_*` / `test_undo_removes_every_body_of_the_editable_import_and_redo_restores_them` / `test_replay_of_the_editable_import_reproduces_the_same_document`）。

## 既存の経路をどう寄せたか

| 経路 | いま何を呼ぶか |
|---|---|
| `Session(step_path, deviation)` → `Session.load_step` | `import_step_editable` 1本。`_setup_floor_seq`（土台はundo不可）は従来どおり。**Python APIとして残す**（pytestがこの形で土台を作る） |
| WebSocket `{"cmd":"import_step"}`（UIの「STEP読み込み」） | `Session.import_step(path, editable=True)` → 新verb。**通常のverbとしてundoできる** |
| MCPツール `import_step` | 既定で新verb。`editable=false` のときだけコアの `import_step`（参照だけ） |
| コアの verb `import_step` | **そのまま**。保存済みログの再生に要るので消せないし、参照だけ欲しい経路も残す |

## ツール名とverb名が食い違う唯一の場所

MCPツール `import_step` が積むログは `verb="import_step_editable"`。
`test_mcp_verbs.py::test_tool_names_match_the_verb_names_in_the_operation_log` は
「LOGGING_TOOLSの各名がverbとして登録されている」ことしか見ないので通るが、
**ログを読んだAIが同じ綴りのツールを探すと見つからない**という穴は残る。
ツールの説明文とMCP.mdに対応を明記して塞いである（消すには19操作の表とツール名を変える必要があり、
それはこのタスクの範囲を超える）。

## 廃止に伴って直したもの

- `duocad/server.py` / `duocad/mcp_server.py` の `--step` / `--no-step` と `DEFAULT_STEP` 定数。
  PCケースのパスの移し先は **`web/scripts/models.mjs`**（読む側が決める）。
- `web/scripts/smoke.mjs` … 冒頭で自分で「STEP読み込み」を踏んで27部品を入れる。
  **読み込みの後にEscで既定コマンドへ戻す**のを忘れると、以後の押し出しで `#distance` が出ない
  （`CommandHost.commit` はコマンドを既定へ戻さない。2026-08-15に実際に踏んだ）。
- `web/scripts/smoke-mcp.mjs` … 空で起動し、MCPの `import_step` を呼んでから27件を検査する。
- `web/scripts/smoke-files.mjs` / `acceptance.mjs` … 「編集可能にする」の対象を参照ボディから
  **読み込んでできた編集用ボディ**へ。参照は隠れていて `projectFace` が null を返すので指せない。
- UIの `make_editable_copy` コマンドは**ボディの複製**として残っている（UIから参照ボディを指す経路は
  もう存在しない）。
