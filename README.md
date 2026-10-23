# duocad

**Fusion360と同等以上にモダンでシンプルなUIに、自分が実際に使う手動操作と強力なAI操作を載せ、
両者が同じ選択集合を交互に触れるようにしたモデリングツール。**
差別化はUIでも機能数でもなく **交互操作**（AIが集合を選ぶ → 人が過不足を直す → AIが操作を適用）にある。

このREADMEは開発を再開するときの入口である。単位は mm 固定。

**制作に使うときは [docs/MANUAL.md](docs/MANUAL.md)**（制作用の安定版と開発用の使い分け）。

### 文書の地図

全文書の一覧と要約は **[docs/README.md](docs/README.md)**（目的の文書を探すときはまずここ）。最初に読むのは次の4本:

| 文書 | 読むとき |
|---|---|
| [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md) | 層・verb・保存形式を変えるとき（設計判断と実測） |
| [MCP.md](MCP.md) | AI側から触るとき（**MCPツール一覧の正**と登録手順） |
| [docs/UI.md](docs/UI.md) | 画面の振る舞いを変えるとき |
| [docs/TOPOPT.md](docs/TOPOPT.md) | 形状最適化の数字の根拠・配線を疑うとき |

---

## 0. 設計原則

UIとAI操作の両方に共通する設計原則。verbを足す・直すときはここへ立ち返る。

1. **viewport上で実物をクリックして選択できるものは、別途ボタンを用意しない。**
2. **すべての操作を verb→target→how の順序に統一する。** 起動(verb)→対象(target。クリックまたは
   共有選択集合)→どうするか(how。数値・選択肢・パス)。全コマンドで同じ型（`CommandHost.svelte`）。
   対象を指すID・座標・貼る先の平面名は target 側であり how ではない（`MCP.md`「AIはどこまで自分でやるか」と揃える）。
   新規ボディへの命名のような省略できる付帯入力は target でも how でもない第三分類。
   適用範囲はコマンド行を通るverbに限り、可視性の切替(`set_visible`)・選択集合の編集は対象外。
3. **省けるクリックは徹底的に省く。** Enterが要るのは (1) 対象を複数選べる（個数固定の対象は当たらない）
   (2) howの入力がある（点から導ける寸法欄 `NumberArgSpec.fromPoints` は how ではない）のどちらかだけで、
   どちらでもなければ最後のクリックで即適用。正は `web/src/commands/command.ts` の `needsEnter`、
   顔ぶれは `tests/test_ui_command_wiring.py` が固定する。
4. **エラーはすべてトーストで見せる**（`web/src/Toast.svelte`）。情報（読み込んだ部品数等）は画面に出さず
   `window.__duocad.messages` で読めるだけ。
5. **コメントは、コードに現れない制約だけを書く。** [CLAUDE.md](CLAUDE.md) §コメント。

AI側の対応する規則は [MCP.md](MCP.md)「AIはどこまで自分でやるか」が正。画面側の判断は [docs/UI.md](docs/UI.md)。

---

## 1. 起動（開発サーバー）

**前提**: コマンドは **リポジトリ直下** をカレントにして実行する。

> **`web/dist` を作り直さないとUIの変更は画面に出ない。** Pythonサーバが配信するのは
> `web/src` ではなく `web/dist`（ビルド成果物）。

**立て方は1つ。** MCPとWebSocketと静的配信を1プロセスで兼ねる。**先にアプリを立てて、Claude Codeが後から繋ぎに来る**（1.2）。

**AIから文書を触る道は2本ある**。同じ `Session` を叩く対等なクライアントなので同時に使ってよい:

- **アプリ内AIパネル**（右サイドバー）—— ローカルの llama-server か Claude API（APIキーで従量課金。既定モデル
  `claude-haiku-5-5`）。キーの置き方は §4「AI側」
- **Claude Code**（MCP）—— **サブスク認証のまま使えるのはこちらだけ**。繋ぎ方は1.2

### 1.1 立てる

```powershell
npm run build --prefix web
.\.venv\Scripts\python.exe -m duocad.mcp_server
```

初回や `web/package.json` を更新したときだけ、1行目の前に `npm install --prefix web` を1回足す。

`--open` でブラウザが開く。**ターミナルのstdinが開いている限り生きる**（`Ctrl+C` で終了）。
**開くURLと待ち受けポートは、立った時点で stderr に出る。**

### 1.2 Claude Codeを繋ぐ（登録は要らない）

**`.mcp.json` がリポジトリ直下にあるので、このディレクトリでClaude Codeを起動すれば繋がる。**
承認・再接続・userスコープ登録・Claude Desktop の詳細は [MCP.md](MCP.md)「Claude Code への登録」。

**一度だけ必要なのは、古いstdio登録を消すことだけ**（localスコープの登録は `.mcp.json` を**黙って覆い隠す**）:

```powershell
claude mcp remove duocad -s local
```

**トークンもヘッダも要らない。** `/mcp` は `Origin` を持つリクエストと知らない `Host` を落とすので、URLだけで足りる
（理由は `WebHandler._mcp_forbidden` のdocstring）。

### 1.3 frontendを直しながら見る（Vite dev server）

```powershell
.\.venv\Scripts\python.exe -m duocad.mcp_server   # コア側(WS 8765)
cd web; npm run dev                                 # 画面(5173)
```

`http://localhost:5173/` を開く（`127.0.0.1` だと、`localhost` を IPv6 の `::1` へ先に解決する環境で Vite が繋がらない）。devサーバは `/duocad-config.json` を返さないので、
画面は既定の `ws://127.0.0.1:8765` へ落ちて繋がる（Pythonサーバは立てておくこと）。
このときだけは `web/dist` の作り直しが要らない。
型検査は `npm run check`（`tsc --noEmit` ＋ `svelte-check`。`.svelte` の中も見る）。

---

## 2. UIチートシート

操作手順・キー割当・ボタン文言は各コンポーネントのコメントが正
（`CommandHost.svelte`・`commands/*.ts`・`FilesPanel.svelte`・`catalog.py` など）。
画面の振る舞いの判断（パネル配置・既定状態・設定ダイアログ・測定）は [docs/UI.md](docs/UI.md)、
保存・選択・可視性の判断は [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md)。ここに書くのはコードを読むだけでは分からないことだけ。

- **右サイドバー（`.sidebar`）の幅は常に300px固定**。動くとビューポートの寸法＝カメラの
  アスペクトが変わり、実機検査の座標決め打ちが黙って別の場所を掴む。
- **Enterが要らないコマンド**は `web/src/commands/command.ts` の `needsEnter` が正。
- undo/redoは**キーボードのみ**（Ctrl+Z / Ctrl+Y）。
- **保存は1操作ごとに自動で走る**（`Session._emit` が `autosave()` を呼ぶ）。undo/redo も保存の引き金で、
  ファイルは常に今の文書の鏡。無題の文書は最初の変更で `untitled-<n>.json` が付く。
  **Ctrl+S** は同じ内容を書き直すだけ。ファイルパネルの名前欄は改名（`rename_document`）、別名の複製は一覧の「複製」。
  いま編集しているファイルはサーバが持つ（`state.current_name`）。`#save-state`（未保存）は自動保存が書けなかったときだけ出る。
- **選択パネルは無い**。選択を直す手段は3Dクリックに一本化してある。
- **保存ファイルは自己完結している**。読み込んだSTEPの本文が操作ログへ取り込まれるので、元のSTEPを
  動かしても開ける（元STEPの更新には追随しない）。代償はファイルサイズ。
- **形を変える操作の確定後と undo/redo の後は選択が常に空になる**（添字は形状変更で壊れる）。可視性の切替では飛ばない。
- **「測定」は verb ではない**（操作ログに載らず undo の対象にもならない）。[docs/UI.md](docs/UI.md)「測定」。

---

## 3. 自動検査の走らせ方

### pytest（コア・MCPの契約）

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

サーバもブラウザも要らない。実STEPを読む重いテストは含まない。ただし `tests/eval_*.mjs` を走らせる検査と `tests/test_protocol.py` の型検査は Node 23.6 以上と `web/node_modules` を使うので、先に §1.1 の `npm install --prefix web` を済ませる（無いと素の clone で20件余りが落ちる）。

### 形状最適化の受入検査

途中データの保存範囲・場所・読み方は [docs/TOPOPT_RECORDING.md](docs/TOPOPT_RECORDING.md)。
最終形状の合格条件は [docs/TOPOPT_ACCEPTANCE.md](docs/TOPOPT_ACCEPTANCE.md)
（最終形状は `topopt_checks/check_final_shape.py`、島除去後・平滑化前の6条件は `topopt_checks/check_pre_smoothing.py` で判定。
判定器自体の回帰検査は `tests/test_final_shape.py` / `tests/test_pre_smoothing.py`）。

**以下の数値検査は pytest には入れていない**（毎回の `pytest -q` が重くなる。マーカーで既定から外すと、CIが無いので
誰も回さなくなる）。**強制力は、依頼に書く「受入検査がすべて緑」の一文と、それを回すセッションが担う。**

```powershell
# 二値FEM（要素剛性・節点番号・行列組立・CG/direct・残差検算)の移設前一致
& .\.venv\Scripts\python.exe topopt_checks\check_fem.py

# メッシュ化側の正しさ（合成密度場。SIMPは回さない）
& .\.venv\Scripts\python.exe topopt_checks\check_mesh.py

# ボクセル化側の正しさ（SIMPを回さない）
& .\.venv\Scripts\python.exe topopt_checks\check_voxelize.py

# 面アンカー側の正しさ
& .\.venv\Scripts\python.exe topopt_checks\check_anchor.py

# 塊(設計空間)。分離 + 固定凸包 + 連結性 + 空の条件面(--run <実行フォルダ> で実物の基準実行も)
& .\.venv\Scripts\python.exe topopt_checks\check_design_space.py

# 途中記録。製品入口から1回走らせ、導出1回・全候補の塊一致・表面の再現・最終文書/STLの一致
& .\.venv\Scripts\python.exe topopt_checks\check_trace.py

# von Mises応力・実単位換算・代表応力・最小安全率
& .\.venv\Scripts\python.exe topopt_checks\check_stress.py

# CPU並列化
& .\.venv\Scripts\python.exe topopt_checks\check_parallel.py

# 保存済み最終ソリッドの三角形分割: 境界交差の検出・共有辺再分割・接続6項目・自己交差・float32(実モデル。--compare で標準メッシャーと並べる)
# 入力の `*-final-solid.json` は 2026-10-06 以前の実行記録にだけある（現行の製品は作らない）。--out は既存のデータを上書きするので使い捨ての場所にする
& .\.venv\Scripts\python.exe topopt_checks\check_boundary_meshing.py --brep-json output\optimization_runs\<古い実行>\<連番>-final-solid.json --out $env:TEMP\robust_meshing_final.json --compare

# duocad本体への配線(4条件 -> 塊 -> 二値探索 -> メッシュボディ)
& .\.venv\Scripts\python.exe topopt_checks\check_optimize_wiring.py
```

**終了コードが 0 で、最後の行が `OK:` なら合格**（例外: `check_trace.py` は最後が `PASS:` と `Archive:` の行、`check_boundary_meshing.py` は結果の JSON の行で、どちらも終了コード 0 が合格）。`check_optimize_wiring.py` と `check_trace.py` は完成形検査まで通すので1回に約9分かかる（他の処理で混んだ機械ではもっと）。 何を守っている検査かは
[docs/TOPOPT.md](docs/TOPOPT.md)「受入検査が見ているもの」。

### 実ブラウザのsmoke（本物のクリックとキー入力）

**各スクリプトが自分で `duocad.mcp_server` を立てて、終わったら止める**ので、コマンドを1本ずつ叩くだけでよい。
**毎回まっさらな文書で始まる**。ブラウザは**Chromeを先に見る**（無ければEdge。`DUOCAD_BROWSER` で明示すれば最優先）。
何を smoke に書くか・書かないかは [docs/SMOKE_TESTING.md](docs/SMOKE_TESTING.md)。

**一覧の正は `web/scripts/smoke*.mjs` のファイル名**（各ファイル先頭のコメントが何を見るかの正）。

```powershell
cd web

# 1本だけ（例）
node scripts/smoke.mjs

# 全部（1本でも❌ならそこで終了コード1）
Get-ChildItem scripts/smoke*.mjs | ForEach-Object {
  node $_.FullName
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
```

STEPを読む smoke（`smoke-face-culling.mjs`）は `web/scripts/models.mjs` の `PC_CASE_STEP` が指すパスを使う。
骨格は `web/scripts/harness.mjs` の `runSmoke`（使い捨ての `--models-dir`、OSが選ぶ空きポートなので並列可。
`--port` / `--http-port` / `--timeout` は全本が読む）。

**verb ごとの画面の検査は持たない。** AI側とコアの契約は pytest（`tests/test_mcp_verbs.py` ほか）、
TS の純関数は `tests/eval_*.mjs`、幽霊とコアの確定結果の一致は `tests/test_ghost_agreement.py` が持つ。

---

## 4. アーキテクチャ

**層の図と「外してはいけない3つ」・verbの粒度は [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md)「層の分け方」が正。**

### 中核の決めごと

- **操作ログが唯一の正で、形状はそこから導出されるキャッシュ。** 保存形式はJSON。開くときはログから再生する。
- **選択集合はアプリ層ではなくコアにある。** 出所（AIか人か）を区別しない1つの集合で、人もAIも同じ操作
  （`modify_selection` / `toggle_selection` / `clear_selection`。AIだけ述語を書ける `set_selection`）で触る。
  **verbが食う面・辺・領域は、MCPからもWebSocketからも引数で渡さない** —— この集合から取る。
- **起動中のコマンド（verb と途中の引数）とモードもコアにある**（`Session.workspace`。issue #153）。選択と同じく操作ログに
  載らず undo でも戻らず保存もされない短命の共有状態で、人（画面）も AI も同じ操作（`begin_command` / `set_command_args` /
  `end_command` / `apply_command` / `enter_sketch` / `leave_sketch`）で触る。画面は従うだけで、自分では持たない。
- **verbのメタ情報は1箇所にしか書かない。** `duocad/catalog.py` の `COMMANDS` が唯一の宣言で、WebSocketの許可リストも
  MCPのツール定義も導出。verbを1つ足すときに書くのはverbのモジュールと宣言1エントリの2箇所だけ（手順は `catalog.py` 冒頭docstring）。
- **verbを掛ける経路は1本。** verbのモジュールは純関数と**入口**（`@entry`）を持ち、`Session.apply(verb, args)` が
  入口を名前で引く。WebSocket・MCP・アプリ内AIはどれも `apply` を通るので、実行中の断り・選択のリセット・
  自動保存・配信・引数検査はどの経路でも同じに効く。frontend だけが別に**引数の集め方**を持つ。
- **コアと画面の契約は生成物で持つ。** `web/src/generated/protocol.ts` は `catalog.py` からの生成物
  （`python tools/generate_protocol_ts.py`）。宣言と応答の一致・生成物の鮮度・型検査は `tests/test_protocol.py` が見る。

### ボディの種別 —— B-rep と 三角形メッシュ

`Body.kind` は2つあり、**形の持ち方は種別ごとに1本だけ**（混ぜたBodyは `Body.__post_init__` が弾く）。

| kind | 形 | できること |
|---|---|---|
| `"solid"` | `Body.shape`（`TopoDS_Shape`） | 全部 |
| `"mesh"` | `Body.mesh`（`Mesh` = 頂点＋三角形。不変） | **表示 / STL書き出し / 削除 / 表示の切り替え / 名前の変更だけ** |

**メッシュボディに編集verbを効かせない**（理由は `duocad/meshbody.py` のモジュールdocstring）。断りの文言は
`meshbody.reject` の1箇所、門は `verbs/_common.editable_body` と `verbs/extrude._resolve_target` の2つ。

- **扱いは「名指ししたら断る / 導出した集合からは黙って外す」。** 根拠は「呼び出し側が何を主張したか」——
  面・辺・頂点を指す選択・`predicates` の `body` 明示・保持/除外ソリッドへの割り当ては名指しなので断り、
  `predicates` の `body` 省略と `export_stl` の対象集合は導出なので、前者は黙って外し後者は含める。
- **選択の粒度はボディ単位だけ**（メッシュには面の指紋が無い）。メッシュボディを含むドラッグ一括選択は丸ごと落ちる
  （範囲外の添字と同じ挙動に揃えてある）。
- **STLは混在した文書でも1本に両方書く**（メッシュはそのまま、B-repはその場で `kernel.tessellate`。画面の三角形とビット単位で同じ。ただし球の極・円錐の頂点・角のフィレットに OCCT が出す面積0の三角形だけは除く。メッシュボディの縮退は断る）。
  **STEPはメッシュを含む文書を断る**（ファイルも作らない）。
- **undo / redo に載る。** メッシュボディの追加は操作ログ1行（verb `add_mesh_body`）で三角形は `args` に入る。
  元になったB-repは持たない（持つと再生時に別の形が復元される）。代償は数万三角形でJSONが数MBになること。

`add_mesh_body` はWebSocketには開けていない（`catalog` の `ws="none"`）。入口はMCPと形状最適化のコードだけ。

### 形状最適化を走らせる

条件を置く4 verb（`set_preserve_bodies` / `set_exclude_bodies` / `set_fixtures` / `set_loads`）の続きが
`duocad/optimize_run.py` で、結果は `add_mesh_body` で文書へ入る。詳細は [docs/TOPOPT.md](docs/TOPOPT.md)「設計と画面」。

- **MCPには同期の `run_optimization` を開けていない**（進捗・中断を運べない）。画面は WebSocket の protocol 口
- **メッシュボディは保持・除外に指定できない**（上「ボディの種別」の名指しの線）
- **受入検査**は §3 の `topopt_checks/`。画面からの実行を通す実機 smoke は持たない

### 前提にしないこと

- **複数のブラウザ・複数の人が同じ文書を同時に編集すること。** 想定は「1人が1つのブラウザ」で、`Session` 全体を守る排他は無い。
  形状は壊れない（verbの適用は同期）が、操作ログや選択の順序は意図と違いうる。変えるなら `duocad/net/ws.py` の `Server` に排他を入れるのが先。

### モジュールとimport境界

**`duocad/` 配下で `OCP` を import してよいのはカーネル層（`kernel.py` から切り出した `kernel_*.py` と `mesh_geometry.py`。一覧は下の表）と `topopt/voxelize.py` だけ、`build123d` は `query.py` だけ。**
`tests/test_boundaries.py` がソースを静的走査して検査する（動的importも検出）。

| モジュール | 役割 | OCP | build123d |
|---|---|---|---|
| `duocad/document.py` | `Document` / `Body` のデータモデル | ✗ | ✗ |
| `duocad/oplog.py` | `Operation` / `OpLog`（undo・再生・永続化。verb名の分岐を書かない） | ✗ | ✗ |
| `duocad/sketch.py` | `Sketch` / `Entity` / `Pattern` とスケッチのverb | ✗ | ✗ |
| `duocad/region.py` | 閉領域の検出（領域は描くものではなく計算されるもの） | ✗ | ✗ |
| `duocad/parameters.py` | 名前つきの寸法値とログからの再生 | ✗ | ✗ |
| `duocad/selection.py` | 選択集合（人のクリック側。可視性を含む「何が選べるか」） | ✗ | ✗ |
| `duocad/predicates.py` | 述語による選択（AI側。構造化JSON） | ✗ | ✗ |
| `duocad/speculative.py` | プレビュー（確定前にverbを投機的に走らせて捨てる） | ✗ | ✗ |
| `duocad/verbs/*.py` | verb層（1 verb 1モジュール。純関数と入口） | ✗ | ✗ |
| `duocad/entry.py` | verbの入口の登録表（`@entry`） | ✗ | ✗ |
| `duocad/catalog.py` | **verbの単一宣言** | ✗ | ✗ |
| `duocad/svg_import.py` | SVG → スケッチの図形（svgelements は build123d の推移依存で既に入っている） | ✗ | ✗ |
| `duocad/query.py` | 問い合わせ層のインタフェース | ✗ | **○ここだけ** |
| `duocad/kernel.py` | OCCTを直接叩く層（テセレーションもここ） | **○** | ✗ |
| `duocad/kernel_brep.py` | BRep保存・`inspect_solid`・箱/格子との共通体積 | **○** | ✗ |
| `duocad/kernel_chains.py` | 辺の連なり（フィレット・面取りで1本として振る舞う辺の組）と、フィレットの事前検査 | **○** | ✗ |
| `duocad/kernel_overflow.py` | はみ出し・貫通を吸収するフィレット（丸めの肉を別に作って足し引きする。[docs/FILLET_OVERFLOW.md](docs/FILLET_OVERFLOW.md)）。kernel から呼ばれるだけの一方向 | **○** | ✗ |
| `duocad/mesh_geometry.py` | 三角形境界の接続検査、CADソリッド化、位相の列挙 | **○** | ✗ |
| `duocad/kernel_sketch.py` | スケッチ平面の枠・図形種別ごとの辺・閉領域の検出。kernel を読むだけの一方向 | **○** | ✗ |
| `duocad/kernel_extrude.py` | 押し出し・回転体・ロフトの幾何（`extrude_faces` / `extrude_regions` / `revolve_regions` / `loft_regions`）。kernel を読むだけの一方向 | **○** | ✗ |
| `duocad/kernel_shell.py` | シェル（中空化）の幾何（`shell_solid`）。kernel を読むだけの一方向 | **○** | ✗ |
| `duocad/kernel_thread.py` | ねじ山の幾何（山形・隙間の上下限と既定・`thread_face`。[docs/THREAD.md](docs/THREAD.md)）。kernel を読むだけの一方向 | **○** | ✗ |
| `duocad/kernel_face_edit.py` | 面の削除・面のオフセットの幾何と関門（[docs/FACE_EDIT.md](docs/FACE_EDIT.md)）。kernel を読むだけの一方向 | **○** | ✗ |
| `duocad/kernel_worker.py` | 面の削除・オフセットを常駐の子プロセスで計算する口（形は BinTools の base64 で渡す） | **○** | ✗ |
| `duocad/worker_process.py` | 重い計算を子プロセスで走らせる共通の部品（起動・1行1 JSON の線・時間の上限・取り消し・作り直し。干渉チェックと面の編集が使う） | ✗ | ✗ |
| `duocad/kernel_split.py` | ボディを平面で割る幾何（`split_by_plane`）。kernel を読むだけの一方向 | **○** | ✗ |
| `duocad/kernel_project.py` | ソリッドの辺・面・外形をスケッチ平面へ写す幾何（`edge_entities` / `face_edges` / `outline_entities`）。kernel を読むだけの一方向 | **○** | ✗ |
| `duocad/kernel_interference.py` | 干渉チェックの幾何（2体 General Fuse の像の共有・最短距離・描画用の三角形）。単独で OCP を触る | **○** | ✗ |
| `duocad/interference.py` | 干渉チェックの組み立て（対象の決め方・区分と隙間の判定・時間の上限と部分結果。verb ではない） | ✗ | ✗ |
| `duocad/interference_mesh.py` | 干渉チェックのメッシュ経路（manifold3d。B-rep を 0.01 mm で三角形分割した近似） | ✗ | ✗ |
| `duocad/interference_worker.py` | 干渉チェックの子プロセス（`worker_process` に形の `load` を載せた親側の `WorkerProcess` と、`python -m` で起こす子の本体） | ✗ | ✗ |
| `duocad/kernel_meshing.py` | OCCTの面分割の段階制御と境界折れ線の交差検査（`tessellate` の実体） | **○** | ✗ |
| `duocad/boundary_check.py` | 境界折れ線の交差・接触・重なりの判定（固定εを使わない） | ✗ | ✗ |
| `duocad/mesh_intersections.py` | 三角形メッシュの自己交差 | ✗ | ✗ |
| `duocad/session.py` | `Session`（1つの文書を持つサーバ側の状態。verbを掛ける唯一の口 `apply`。配りものは `view` へ委譲） | ✗ | ✗ |
| `duocad/view.py` | `DocumentView`（配りもの＝状態配信・版・キャッシュ。文書を変えない） | ✗ | ✗ |
| `duocad/paths.py` | `REPO_ROOT` と既定の置き場（`web/dist` / `models`） | ✗ | ✗ |
| `duocad/net/ws.py` | `Server`（WebSocketの配信と `_dispatch`） | ✗ | ✗ |
| `duocad/net/http.py` | `web/dist` の静的配信と `/mcp` | ✗ | ✗ |
| `duocad/net/tools_on_loop.py` | `ToolsOnLoop`（別スレッドからの道具呼び出しをイベントループへ限定） | ✗ | ✗ |
| `duocad/net/interference_runs.py` | 画面からの干渉チェックをワーカースレッドで走らせ、要求した接続にだけ返す | ✗ | ✗ |
| `duocad/server.py` | **入口**（再輸出。`mcp_server.py` とテストはここから名前を取る） | ✗ | ✗ |
| `duocad/mcp_server.py` | MCPサーバ。アプリ層ではなくコアのもう一人のクライアント | ✗ | ✗ |
| `duocad/agent/` | アプリ内AIエージェント（プロバイダのアダプタ＋tool-useループ。UIは持たない） | ✗ | ✗ |

**`duocad/net/` → `duocad/session.py` → `duocad/view.py` の一方向。** 唯一の逆向きは `Server._ensure_ai_loop` が
`build_provider` を入口（`duocad.server`）越しに引くところで、差し替え点を `duocad.server.build_provider` に固定するため
（`tests/test_agent_ws.py` が monkeypatch する。直接importすると差し替えが黙って効かなくなる）。

### frontend（`web/`）

**Svelte 5 + TypeScript + Vite。SvelteKitは使わない。** CSSはバニラ。根拠は [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md)「技術選定の根拠」、
three.js / Svelte / DOM の罠は [docs/WEB_PITFALLS.md](docs/WEB_PITFALLS.md)。

**守るべき唯一のルール: canvasはフレームワークの外に置く。** three.jsが所有するコンテナを渡し、Svelteは二度と中を触らない。
`viewport.ts` を狭いインタフェースにして **three.jsの型を外へ漏らさない**。

| ファイル | 役割 |
|---|---|
| `web/src/App.svelte` | 画面全体の組み立て・選択の規則・キー処理 |
| `web/src/{displaySettings,documentFiles,aiChat,measurement}.svelte.ts` | `App.svelte` から切り出した状態の持ち主。**選択・ピック・コマンドの確定には触らない**かたまりだけ |
| `web/src/viewport.ts` | three.js の囲い（load / onPick / highlight / project …） |
| `web/src/session.ts` | WebSocketクライアント。**WSのURLはサーバが `/duocad-config.json` で教える** |
| `web/src/generated/protocol.ts` | **生成物**。正は `duocad/catalog.py`。手で直さない |
| `web/src/commands/CommandHost.svelte` | コマンド層の骨格（起動→引数収集→プレビュー→Enter/Esc・即適用）。verbごとに作り直さない |
| `web/src/commands/{command,sketch,features,files,moveMode}.ts` | 各コマンドの引数の集め方の定義だけ（Enter要否はここから導出） |
| `web/src/{Browser,Files,Optimize,SelectionInfo}Panel.svelte` | 各パネル（コアが持つものを描き直すだけ） |
| `web/src/Toast.svelte` | エラーの札。`fail(message)` の1本だけ |
| `web/src/AiPanel.svelte` | AIパネル。文書は描かない |
| `web/src/SettingsDialog.svelte` | 設定ダイアログ。native `<dialog>`（非モーダル）。一度決めたら触らない設定だけ |
| `web/src/viewSettings.ts` | 画面設定のlocalStorage。クライアント側だけの設定で、文書の状態は入れない |

### AI側（MCP）

**ツールの一覧は [MCP.md](MCP.md) が正**。表と実際の `tools/list` の一致は `tests/test_doc_code_agreement.py` が機械で見る。

- **面・辺・領域は引数で受けない。選択から取る**（数値・名前・パスだけが引数）。WebSocketのコマンドも同じ形
- **ツール名 == WebSocketのコマンド名 == UIのコマンドID == 操作ログのverb名**。1つのツールを引数で複数のverbに打ち分けない。
  両方向の一致は `test_tool_names_match_the_verb_names_in_the_operation_log` と `tests/test_ui_command_wiring.py` が固定する
- 述語は構造化JSON（`where` はAND。語彙は `describe_predicates` が自己記述する）

公式SDKは入れず、JSON-RPC 2.0 を素で実装。トランスポートは stdio と HTTP（`/mcp`）の2本で、`McpServer.handle()` はどちらも知らない（契約は `tests/test_mcp_http.py`）。

### AI側（アプリ内エージェント・`duocad/agent/`）

アプリの中からAIを選んで繋ぐ（llama-server と Claude API）。MCP経路と共存する。
実測・方向・落とし穴は [docs/AI_INTEGRATION.md](docs/AI_INTEGRATION.md)。設計の要点:

- **道具の定義もシステムプロンプトも第二の場所に書かない** —— `DocumentTools.descriptors()` / `.call()` / `.error_envelope()` と
  `catalog.agent_instructions()` をMCPと共有する
- **暴走は必ず打ち切る**（`max_turns` / `max_tokens` / タイムアウト / 中断フラグ）。応答はストリーミングで受け、
  時間切れと中断は接続を閉じる（llama-server は非streamingだと閉じても生成し続ける。`StreamingTransport`）
- **済んだ依頼は「人の発話 + AIの最終返答」だけを残す**（`AgentLoop._settle_history`）。文脈長に当たったら古い依頼を半分捨てる
- **ツール定義は先に読ませておく**（`ai_prepare`。llama-server の `--slot-save-path` があればスロット保存を使う）
- **文書と選択は先に見ておく** —— `describe_document` / `get_selection` をループが代わりに呼んで履歴へ置く（`AgentLoop._look_first`）
- **同時編集は楽観ロックで守る** —— 版は「文書の世代・ログの本数・適用回数・選択の指紋・可視性の指紋・作業空間の版」の6つ組
  （`DocumentTools.current_seq()`）。可視性も起動中のコマンドも操作ログに載らないので、それぞれの印が要る
- **Claude APIのキーは画面の「設定」で置ける**（`ai_set_api_key`。`duocad_secrets.json` に書く。gitignore済み。画面へ値は返さない）。
  手で置くなら環境変数 `DUOCAD_ANTHROPIC_API_KEY` か同じファイル（`{"DUOCAD_ANTHROPIC_API_KEY": "sk-ant-..."}`）。環境変数が優先。
  **`ANTHROPIC_API_KEY` は読まない** —— その名前をユーザー環境に置くと、同じマシンの Claude Code がサブスクではなくそのキーで課金する。
  モデルは `DUOCAD_ANTHROPIC_MODEL` で差し替える。出力上限は `ANTHROPIC_MAX_TOKENS`（思考も数えられる）
- **キーを渡すのは `anthropic` だけで送り先は公式APIに固定**（`base_url` を拒否）。`openai_compat` には鍵を渡さない
- **llama-server へは思考の設定とサンプリングを送る**（AIパネルの見出しで 思考なし / low / medium / サーバ任せ、既定 low。`THINKING_PAYLOAD`）。打ち切りは `AI_LIMITS` の `openai_compat`（60往復・16,384トークン・2時間）。根拠は [docs/LOCAL_LLM_QWEN38.md](docs/LOCAL_LLM_QWEN38.md)
- 依存は増やしていない（公式SDKを入れず標準ライブラリで素のHTTP）

**WebSocketへの配線**: `duocad/net/ws.py` の `ai_get_status` / `ai_configure` / `ai_set_thinking` / `ai_set_api_key` / `ai_prepare` / `ai_send` / `ai_cancel`。

- **`ai_*` はverbではない**（操作ログに載らずundoの単位でもない）ので、`catalog.COMMANDS` には `category="protocol"`・`ws="special"`・`mcp=False` で宣言する（ソケットが受け取る名前を宣言で尽くすため）が、verb にも MCPツールにもしない。
  `tests/test_ui_command_wiring.py::test_ai_commands_are_declared_but_are_neither_tools_nor_verbs` が固定する
- **`ai_send` はモデルへのHTTPを待たずに返る**（`run_in_executor`）。進捗 `ai_tool_call` と最終 `ai_result` は別イベント
- **`Session` に触るのはイベントループのスレッドだけ** —— `ToolsOnLoop`(`duocad/net/tools_on_loop.py`)が `.call()` 等をイベントループへ投げ返す
- 中断は `ai_cancel`。反映は次のチェックポイントまで遅れうる。「どこまでやったか」は `ai_result.tool_calls`

契約は `tests/test_agent_*.py`（偽プロバイダで決定的に検査）。実機のClaudeは `tests/test_agent_claude_api.py`（課金されるので
`DUOCAD_LIVE_CLAUDE=1` のときだけ）、llama-server は `tools/llm_probe.py` と `tools/llm_tasks.py` で測る。

---

## 5. リポジトリの地図

```
duocad/
  .mcp.json              Claude Code向けのMCP登録（projectスコープ）
  README.md              ← このファイル（入口）
  MCP.md                 MCPサーバの仕様と登録手順
  docs/                  設計判断・形状最適化ほか。索引は docs/README.md
  duocad/                コア（Python）。verbs/ に1 verb 1モジュール。net/ はアプリ層との接点、
                         agent/ はアプリ内AIエージェント。役割は §4 の表
  tests/                 pytest
  web/                   frontend（Svelte 5 + Vite）
    src/                 画面
    scripts/             実ブラウザの検査（smoke*.mjs）と撮影（screenshot.mjs）。共有部品は harness.mjs ほか
    dist/                ビルド成果物（**Pythonサーバが配信するのはこちら**・git管理外）
  .venv/                 Python 3.14（git管理外）
  pyproject.toml         依存はバージョン固定
```

サンプルのSTEPは `web/scripts/models.mjs` の `PC_CASE_STEP` が指す場所（リポ外）。**読み取り専用で扱うこと。**

---

## 6. 既知の落とし穴

### 運用の注意

- **duocadのサーバを2つ同時に立てない**（8765/8766がぶつかり、後から立った方が落ちる）。
  Claude CodeがHTTPで繋ぐ限り起きないが、**stdioの登録が残っているとまだ起きる**（§1.2）。
- **`/mcp`（8766）はブラウザから叩けない。** `Origin` を持つリクエストと `127.0.0.1` / `localhost` 以外の `Host` を403で落とす。
  認証トークンは無い（このPCでコードを実行できる相手はトークンのファイルも読める）。契約は `tests/test_mcp_http.py`。
- **WebSocket(8765)はOriginを検査する。** 繋げるのはduocad自身が配るページ（静的配信と Vite dev server）だけ。
  `Origin` を送らないクライアント（`smoke*.mjs`）は繋がる（理由は `tests/test_ws_origin.py` 冒頭）。
  検査が無いと、別のサイトを開いているだけでそのページが文書を壊し・任意パスへ書き・`ai_send` で課金を消費できた。
- **`web/dist` を作り直さないとUIの変更が画面に出ない**（§1）。`dist` が無くても `mcp_server` は静的配信だけ諦めて起動する
  （理由はstderrに1行出る）。
- **文書は接続より長生きする。** まっさらから試すときはサーバを立て直す。

### 実測で判明している技術的な地雷（コード側は対処済み。数値を疑うとき用）

OCCT・STEP・STL の罠の索引は [docs/CAD_LIBRARY_PITFALLS.md](docs/CAD_LIBRARY_PITFALLS.md)。ここには数値を伴うものだけ残す。

- **OCCTのフィレットは例外を投げずに失敗し、「通ったのに壊れている」帯がある**（20mm立方体・全辺、2026-09-18 実測:
  r=9.9 成功、r=10.0 は `IsDone()=False`、**r=10.5 と 11.0 は `IsDone()` が真なのに `BRepCheck` が偽**で体積が
  4251.51 から 9246.46 へ増える。半径に対して単調ではない）→ verbのラッパで **`IsDone()` / 空でない / 体積>0 / `BRepCheck`** の4つとも通す。
- **STEP往復の体積は bit-exact ではない**（相対 約8e-16）→ 比較は `math.isclose(rel_tol=1e-6)`。

### 現行verbの制約

- **面取りは距離1つ（＝45度）しか取れない。** 非対称な面取り（PCケースの脚の円錐）は寸法まで再現できない。
- 拘束ソルバは持たない（各図形が自分の数値を持つパラメトリック・プリミティブで足りている）。
