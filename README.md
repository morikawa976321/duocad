# duocad

**Fusion360と同等以上にモダンでシンプルなUIに、自分が実際に使う手動操作と強力なAI操作を載せ、
両者が同じ選択集合を交互に触れるようにしたモデリングツール。**
差別化はUIでも機能数でもなく **交互操作**（AIが集合を選ぶ → 人が過不足を直す → AIが操作を適用）にある。

このREADMEは開発を再開するときの入口である。単位は mm 固定。

### 文書の地図（このREADMEは入口。詳細は下記が正）

| 文書 | 何が書いてあるか | 読むとき |
|---|---|---|
| [docs/README.md](docs/README.md) | **docs/ の索引**（配下の全文書一覧と要約） | 目的の文書を探すとき |
| [docs/INITIAL_PLAN.md](docs/INITIAL_PLAN.md) | **最初期の実装計画**（なぜ・当初スコープ・到達表。「企画の前提」がなぜ） | 起点を辿るとき |
| [MCP.md](MCP.md) | **MCPツール一覧の正**と登録手順 | AI側から触るとき |
| [docs/TOPOPT.md](docs/TOPOPT.md) | **形状最適化**（実測の記録・設計と画面・受入検査の意味） | 数字の根拠・topopt配線を疑うとき |
| [docs/TOPOPT_SPEED.md](docs/TOPOPT_SPEED.md) | **形状探索の時間短縮**（1回のFEM評価の内訳・候補評価の費用） | 探索を速くしたいとき |
| [docs/TOPOPT_SMOOTHING_INTERSECTIONS.md](docs/TOPOPT_SMOOTHING_INTERSECTIONS.md) | **平滑化後の自己交差が起きるボクセルの形**（64条件の全数集計・管の潰れと交差の規則・合成での再現） | 平滑化の自己交差・潰れを疑うとき |
| [docs/TOPOPT_TARGET_FACTOR.md](docs/TOPOPT_TARGET_FACTOR.md) | **探索に渡す目標安全率 = 実際の目標 × 2.4** の決定・根拠・実測（細分すると目標を割る問題の受け方） | 探索時の目標・報告の「探索の格子での値」を疑うとき |

---

## 0. 設計原則

UIとAI操作の両方に共通する設計原則。verbを足す・直すときはここへ立ち返る。

1. **viewport上で実物をクリックして選択できるものは、別途ボタンを用意しない。**
   実例はスケッチの貼る先クリック（`CommandHost.svelte`）——基準平面(XY/XZ/YZ)もボタンではなく
   viewport上のグリッドを直接クリックして指す。
2. **すべての操作を verb→target→how の順序に統一する。**
   コマンドを起動し(verb)、対象を指し(target。クリックまたは共有選択集合から)、
   どうするかを打つ(how。数値・選択肢・パス)。全コマンドで同じ型（`CommandHost.svelte`）。
   **対象を指すID・点の座標・貼る先の平面名は、文字列や列挙に見えても target 側であり
   how ではない**（`MCP.md`「AIはどこまで自分でやるか」と揃える。「名前はすべて対象」
   という言い方はしない — 新規ボディへの命名のような**省略できる付帯入力**は target でも
   how でもない第三分類である）。
   **適用範囲はコマンド行を通るverbに限る**。可視性の切替(`set_visible`)・選択集合の編集は直接操作であり、
   この型の対象外である。
3. **省けるクリックは徹底的に省く。** Enterが要るのは次のいずれかに当てはまる操作だけで、
   どちらでもなければ最後のクリックで即適用される（正は `web/src/commands/command.ts`
   の `needsEnter`。即適用になる顔ぶれは `tests/test_ui_command_wiring.py` が固定する）:
   - (1) **対象を複数選べる**（辺を何本でも・図形を何個でも）。**個数が固定の対象（測定の
     「ちょうど2つ」）は当たらない** —— 何個で終わりかが機械に分かる
   - (2) **howの入力がある**（数値・選択肢・パス）。ただし**点から導ける寸法欄**
     （`NumberArgSpec.fromPoints`。スケッチの長さ・幅・高さ・半径）は how ではない ——
     空欄のままでも集めた点から値が決まるので、打ち終わりを人が言う必要がない。

4. **エラーはすべてトーストで見せる**（`web/src/Toast.svelte`）。**いったんエラーのみ**を
   寄せてあり、情報（読み込んだ部品数・書き出したパス等）は`note()`が内部状態として
   保持するだけで画面には出さない（`window.__duocad.messages` で読める）。
5. **コメントは、コードに現れない制約だけを書く。** 詳細は [CLAUDE.md](CLAUDE.md) §コメント（4種の基準）。

AI側の対応する規則は [MCP.md](MCP.md)「AIはどこまで自分でやるか」が正。

---

## 1. 起動（開発サーバー）

**前提**: 以下のコマンドは **リポジトリ直下** をカレントにして実行する（`cd web` などはそこからの相対パス）。

> **`web/dist` を作り直さないとUIの変更は画面に出ない。** Pythonサーバが配信するのは
> `web/src` ではなく `web/dist`（ビルド成果物）。

**立て方は1つ。** MCPとWebSocketと静的配信を1プロセスで兼ねるので、手で触るだけの日も
AIに頼む日も同じもので立てる。**先にアプリを立てて、Claude Codeが後から繋ぎに来る**（1.2）。

**AIから文書を触る道は2本ある**。どちらもこの1つの立て方の上に乗り、同じ `Session` を叩く
対等なクライアントなので**同時に使ってよい**:

- **アプリ内AIパネル**（画面右サイドバー）—— ローカルの llama-server（キー不要）。
  APIキーでのClaude利用はできない（Claudeを使う道は下のMCP経路）
- **Claude Code**（MCP）—— **サブスク認証のまま使えるのはこちらだけ**。繋ぎ方は1.2

### 1.1 立てる

リポジトリ直下で、次の2行を順に実行する:

```powershell
npm run build --prefix web
.\.venv\Scripts\python.exe -m duocad.mcp_server
```

初回や `web/package.json` を更新したときだけ、上の1行目の前に
`npm install --prefix web` を1回足す。

`--open` でブラウザが開く。**ターミナルのstdinが開いている限り生きる**（`Ctrl+C` で終了）。
**ブラウザで開くURLと待ち受けているポートは、立った時点で stderr に出る。**

### 1.2 Claude Codeを繋ぐ（登録は要らない）

**`.mcp.json` がリポジトリ直下にあるので、このディレクトリでClaude Codeを起動すれば繋がる**
（起動時に**cwdの `.mcp.json` だけ**を読む。親ディレクトリは遡らない）。初回だけ「このプロジェクトの
MCPサーバを承認するか」と訊かれ、承認は `~/.claude.json` の `enabledMcpjsonServers` に残る
（やり直すなら `claude mcp reset-project-choices`）。

**一度だけ必要なのは、古いstdio登録を消すことだけ**:

```powershell
claude mcp remove duocad -s local
```

スコープの優先順位は **local > project > user** で、**エントリ全体が入れ替わる**（フィールドは
マージされない）。古い登録はlocalスコープなので、消すまで `.mcp.json` を**黙って覆い隠す**。

- **duocadが立っていなくてもClaude Codeは起動する**（そのサーバが `✘ Failed to connect` になるだけ）。
  繋がったかは `claude mcp list`（接続まで見る）、セッション内なら `/mcp`
- **HTTPのサーバはClaude Codeが自動で繋ぎ直す**（初回失敗で最大3回、セッション中の切断で最大5回・
  指数バックオフ）。**duocadを立て直してもClaude Code側は放っておいてよい** —— stdioには無かった性質
- 起動時の待ちは既定30秒（`MCP_TIMEOUT` にミリ秒を入れて変更する）
- **トークンもヘッダも要らない。** `/mcp` は `Origin` を持つリクエスト（＝ブラウザから来たもの）と
  知らない `Host` を落とすので、URLだけで足りる。理由は `WebHandler._mcp_forbidden` のdocstring
- **stdioの口も残してある** —— Claude Desktopのようにstdioしか話せないクライアント用（[MCP.md](MCP.md)）

duocad以外のディレクトリからも使いたい日だけ、手でuserスコープへ登録する:

```powershell
claude mcp add --scope user --transport http duocad http://127.0.0.1:8766/mcp
```

**既定にはしない** —— duocad の MCP が無関係なプロジェクトにも出てくるため。

### 1.3 frontendを直しながら見る（Vite dev server）

```powershell
.\.venv\Scripts\python.exe -m duocad.mcp_server   # コア側(WS 8765)
cd web; npm run dev                                 # 画面(5173)
```

`http://127.0.0.1:5173/` を開く。devサーバは `/duocad-config.json` を返さないので、
画面は既定の `ws://127.0.0.1:8765` へ落ちて繋がる（Pythonサーバは立てておくこと）。
このときだけは `web/dist` の作り直しが要らない（Viteが `web/src` を直に配る）。
型検査は `npm run check`（`tsc --noEmit` ＋ `svelte-check`）。**`.svelte` の中も見る**。

---

## 2. UIチートシート

**読むとき**: UI操作(クリック/ピック/選択/undo)・保存まわり・サイドバー寸法を変更するとき、または実機検査の座標がずれたとき。

操作手順・キー割当・ボタン文言は各コンポーネントのコメントが正
（`CommandHost.svelte`・`commands/*.ts`・`FilesPanel.svelte`・`catalog.py` など）。
ここに書くのは、コードを読むだけでは分からないことだけ。

- **右サイドバー（`.sidebar`）の幅は常に300px固定**。動くとビューポートの寸法＝カメラの
  アスペクトが変わり、実機検査の座標決め打ちが黙って別の場所を掴む。
- 通常モードで右サイドバーに `AiPanel` だけが残り空きの帯になるのは**意図した見た目**。
- 設定ダイアログは**非モーダル**（`show()`）で、**開いている間もビューポートは触れる**。
  FOVは見ながら決める設定なので、背後を
  inert にすると「見る」だけが残って「回して確かめる」が死ぬ。`showModal()` がただで
  付けていた Esc と重なり順は自前で持ち直してある（`SettingsDialog.svelte`）。
- **Enterが要らないコマンド**は `web/src/commands/command.ts` の `needsEnter` が正（顔ぶれは `tests/test_ui_command_wiring.py` が固定する）。
- undo/redoは**キーボードのみ**（Ctrl+Z / Ctrl+Y）。
- **保存は1操作ごとに自動で走る**（2026-09-16、issue #5。`Session._emit` の verb 応答が
  `autosave()` を呼ぶ）。undo/redo も保存の引き金で、ファイルは常に今の文書の鏡（redo の
  山は残さない）。無題の文書は**最初の変更で `untitled-<n>.json` が自動で付く**。明示の
  保存ボタンは無く、**Ctrl+S** は同じ内容を書き直すだけ（ブラウザの保存ダイアログを止める）。
  ファイルパネルの名前欄は**いま編集しているファイルの改名**（`rename_document`。既存の
  名前は拒否）で、別名の複製は一覧の行の「複製」。**いま編集しているファイルはサーバが持ち**
  （`state.current_name`）、画面はその写しを出す＝リロードしても名前が残る。
- `#save-state`（未保存）は**自動保存が書けなかったときだけ**出る（`state.saved`）。
  可視性の切り替えはファイルに載らないので書き直しも起きない。
- **選択パネルは無い**。選択を直す手段は3Dクリックに一本化してある。
- **保存ファイルは自己完結している**。読み込んだSTEPは本文がそのまま操作ログへ
  取り込まれるので、元のSTEPを移動・改名・削除しても、別PCへ持っていっても開ける
  （Fusionの「挿入」と同じで、元STEPを後から更新しても追随はしない）。代償はファイルサイズが大きくなる。
- **形を変える操作（押し出し・丸め・面取り・反復・図形追加・パラメータ変更）の確定後、
  undo/redoの後は選択が常に空になる**。添字は形状変更で壊れるので、後継を
  当てにいくのではなく選び直させる。可視性の切替（手前を隠す）では選択は飛ばない。
- **「測定」は verb ではない**（issue #10。操作ログに載らず undo の対象にもならない）。頂点・直線の辺・平らな面を
  2つ指すとその場で測る（Enter 不要・素のクリックで2つ目を足せる）。結果の札は**測った選択が変わるまで**残り、
  Esc・次のコマンドの起動・モード切替で消える。判断の理由は [docs/UI.md](docs/UI.md)「測定」。

---

## 3. 自動検査の走らせ方

**読むとき**: 変更後に検査を選んで実行するとき(pytest・形状最適化受入・実ブラウザsmokeの3小節から該当するものだけ)。

### pytest（コア・MCPの契約）

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

サーバもブラウザも要らない。実STEPを読む重いテストは含まない。

### 形状最適化の受入検査

製品の最適化は途中データを自動保存する。[保存範囲・場所・読み方](docs/TOPOPT_RECORDING.md)。

最終形状の水密・単一ソリッドの2条件と、平滑化後の保持・除外ブーリアン処理は
[最終形状の合格条件](docs/TOPOPT_ACCEPTANCE.md)に従って `topopt_checks/check_final_shape.py` で判定する。
この判定器自体の小形状による回帰検査は `tests/test_final_shape.py` にある。

島除去後・平滑化前の6条件(境界・単一ソリッド・保持/除外・荷重欠落・安全率)は
[平滑化前の受入検査](docs/TOPOPT_PRE_SMOOTHING_CHECKS.md)に従って `topopt_checks/check_pre_smoothing.py` で判定する。
検査ロジック本体は `duocad/topopt/acceptance.py` にあり、この判定器自体の回帰検査は `tests/test_pre_smoothing.py` にある。

**以下の最適化の数値検査は pytest には入れていない。** `tests/` へ入れると `pytest -q` が毎回
それだけ重くなる。マーカーで既定から外す案も採らない —— duocad に CI が無いので、既定から
外した瞬間に誰も回さなくなる(`pytest -q` が緑で返るため、無人の runner は「緑だ」と
報告して先へ進む)。**強制力は、タスクに書く「受入検査がすべて緑」の一文と runner が担う。**

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
& .\.venv\Scripts\python.exe topopt_checks\check_boundary_meshing.py --brep-json output\finalization_checks\<run>\000021-final-solid.json --out output\gap_causality_20260923\robust_meshing_final.json --compare

# duocad本体への配線(4条件 -> 塊 -> 二値探索 -> メッシュボディ)
& .\.venv\Scripts\python.exe topopt_checks\check_optimize_wiring.py
```

**どれも最後の行が `OK:` で、終了コードが 0 なら合格。** 何を守っている検査かは
[docs/TOPOPT.md](docs/TOPOPT.md)「受入検査が見ているもの」に書いてある。

### 実ブラウザのsmoke（本物のクリックとキー入力）

**サーバを立てる手順は要らない**。**各スクリプトが自分で `duocad.mcp_server` を立てて、
終わったら止める**ので、コマンドは1本ずつ叩くだけでよい。**毎回まっさらな文書で始まる**。

ブラウザは**Chromeを先に見る**（無ければEdge。`DUOCAD_BROWSER` で明示すればそれが最優先）。

**smoke が見るのは、人が使っていても気づけない壊れ方だけ**（見た目が同じまま値だけ違う・黙って別のものが
変わる・タイミングでしか出ない競合・性能と粗さ）。ボタンが出ない・クリックがずれる・強調が出ないといった、
使えばすぐ気づく壊れ方は守らない（[docs/SMOKE_TESTING.md](docs/SMOKE_TESTING.md)「何を smoke に書くか」）。

**一覧の正は `web/scripts/smoke*.mjs` のファイル名**（各ファイル先頭のコメントが何を見るかの正）。
番号付きの固定リストは持たない —— 増減のたびにREADMEが古くなるため。

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

**骨格は全本共通**（`web/scripts/harness.mjs` の `runSmoke`）: 使い捨ての `--models-dir` でサーバを立て、
ブラウザを開き、終わったら止めて temp を消す。ポートは走らせるたびに OS へ空きを選ばせるので
複数本を並列に回してよい。`--port` / `--http-port`（固定したいとき）と `--timeout` は全本が読む。

既に立てているサーバへ繋ぐ `--url` は、`runSmoke` に `attach: true` を渡した本だけが受ける
（その場合スクリプトはサーバを立てず、止めもしない）。いまの本はどれも対応していない。

**verb ごとの画面の検査は持たない。** AI側（MCP）とコアの契約は pytest（`tests/test_mcp_verbs.py` ほか）、
TS の純関数は `tests/eval_*.mjs`、幽霊とコアの確定結果の一致は `tests/test_ghost_agreement.py` が持つ。

---

## 4. アーキテクチャ

**読むとき**（小節ごと）:
- 中核の決めごと／前提にしないこと／モジュールとimport境界 — 実装タスクでは前提として毎回読む
- ボディの種別(B-rep/三角形メッシュ) — ボディの種類分岐に触るとき
- 形状最適化を走らせる — topopt配線・画面・詳細は [docs/TOPOPT.md](docs/TOPOPT.md)「設計と画面」。README §4 は要点のみ
- frontend(`web/`) — `web/`配下のコードを変更するとき(ファイルの役割・canvas/three.jsの境界を確認する)
- AI側(MCP) — MCPツールを追加・変更するとき
- AI側(アプリ内エージェント) — アプリ内AIエージェントの挙動を変更するとき

**層の図と「外してはいけない3つ」は [docs/INITIAL_PLAN.md](docs/INITIAL_PLAN.md) §2 に書いてある（最初期の計画）。**

### 中核の決めごと

- **操作ログが唯一の正で、形状はそこから導出されるキャッシュ。** 保存形式はJSON（SQLiteは使わない）。
  開くときはログから再生する。undo・再生・AIの履歴読み・git差分が全部これに乗る。
- **選択集合はアプリ層ではなくコアにある。** 永続／双方向（AIが書ける）／種別を保持／差分編集可能／
  **出所（AIか人か）を区別しない1つの集合**で、人もAIも同じ操作（`modify_selection` / `toggle_selection` / `clear_selection`。AIだけ述語を書ける `set_selection`）で触る。ここをUIの状態にすると交互操作が原理的に不可能になる。
  **verbが食う面・辺・領域は、MCPからもWebSocketからも引数で渡さない**——
  どちらもこの集合から取るので、「対象集合の指定方法」は1つしかない。
- **verbのメタ情報は1箇所にしか書かない。** 名前・引数・カテゴリ・プレビュー可否・
  MCPのスキーマは `duocad/catalog.py` の `COMMANDS` が唯一の宣言で、WebSocketの許可リストも
  MCPのツール定義も導出である。**verbを1つ足すときに書くのは、verbのモジュールと宣言1エントリの
  2箇所だけ**（手順の正は `catalog.py` 冒頭docstring）。
- **verbを掛ける経路は1本。** verbのモジュールは純関数（対象を引数で受ける）と**入口**
  （`@entry`。対象を選択から取って純関数へ渡す）を持ち、`Session.apply(verb, args)` が入口を名前で
  引いて呼ぶ。WebSocket・MCP・アプリ内AIはどれも `apply` を通るので、実行中の断り・選択のリセット・
  自動保存・配信はどの経路でも同じに効き、verbごとの配線は無い。届いた引数の検査（必須・未知・型）も
  宣言からの導出で、WebSocketとMCPに同じものが効く。frontendだけは別に持つ ——
  TS側が持つのは**引数の集め方**（どのクリックを待つか・入力欄のid）で、宣言には無い情報だから。
- **コアと画面の契約は生成物で持つ。** コマンド名・分類・WebSocket が受け取る引数・verb が応答に載せる値
  （`Command.returns`）・状態配信のキーは `catalog.py` の宣言で、`web/src/generated/protocol.ts` はそこからの
  生成物（`python tools/generate_protocol_ts.py`）。`commands/*.ts` の定義は `CommandDef<'fillet'>` のように
  verb 名を付けて書き、`id` と `build` の引数が宣言どおりであることを型検査が見る。宣言と実際の応答の一致・
  生成物の鮮度・型検査は `tests/test_protocol.py` が `pytest -q` の中で見る。
- **verbの粒度の定義は [docs/INITIAL_PLAN.md](docs/INITIAL_PLAN.md) §2「verbの粒度」が起点。**

### ボディの種別 —— B-rep と 三角形メッシュ

`Body.kind` は2つある。**形の持ち方は種別ごとに1本だけ**で、混ぜたBodyは作れない
（`Body.__post_init__` が弾く）——「`body.shape is None`」で分岐している既存の経路が
「どちらの意味か」を判定できなくなるため。

| kind | 形 | できること |
|---|---|---|
| `"solid"` | `Body.shape`（`TopoDS_Shape`） | 全部 |
| `"mesh"` | `Body.mesh`（`Mesh` = 頂点＋三角形。不変） | **表示 / STL書き出し / 削除 / 表示の切り替えだけ** |

**メッシュボディに編集verbを効かせない**（理由は `duocad/meshbody.py` のモジュールdocstring）。

断りは1箇所（`meshbody.reject`）に文言を持ち、**必ず「次に何ができるか」を書く**。
断りが入る門は2つだけ: `verbs/_common.editable_body`（面・辺を取るverb全部）と
`verbs/extrude._resolve_target`（領域の押し出しだけ専用の解決関数を持つ）。

決めたこと3つ:

- **メッシュボディの扱いは「名指ししたら断る / 導出した集合からは黙って外す」。**
  この1本の線で全部の門が決まる —— `select_face`/`edge`/`vertex`・`predicates` の `body` 明示・
  `optimize.face_fingerprint`・`sketch` の貼る先・**保持/除外ソリッドへの割り当て**は
  **名指し**なので断り、
  `predicates` の `body` 省略（可視Body全部）と `export_stl` の対象集合は**導出**なので、
  前者は黙って外し後者は含める。判断の根拠は「呼び出し側が何を主張したか」である ——
  名指しは「このボディには面がある」という誤った主張なので断る。導出された集合から外すのは
  不可視Bodyを外しているのと同じで、主張と矛盾しない。**ここで断ると、最適化結果のメッシュが
  文書に1つあるだけで述語による選択が丸ごと使えなくなる。**
- **選択の粒度はボディ単位だけ。** メッシュには面の指紋（選択の安定性の仕組み）が無いので、
  `select_face` / `select_edge` / `select_vertex` はメッシュボディを断る（`kind="body"` を使う）。
  **その結果、メッシュボディを含むドラッグ一括選択は丸ごと落ちる** —— 範囲外の添字を渡した
  ときと同じ挙動で、わざとそろえてある（黙って読み飛ばすと「囲んだのに入っていないものがある」
  が見えなくなる）。
- **STLは混在した文書でも1本に両方書く。** メッシュの三角形は**テッセレーションを通さず
  そのまま**、B-repはその場で `kernel.tessellate` して同じファイルへ。B-repの三角形は
  **画面が土台の粗さで配るものとビット単位で同じ**（`io_step._triangles_of`）で、STL専用に
  測り直す経路（OCCT純正ライタ）は持たない。**STEPはメッシュを含む文書を断る**
  （境界表現の形式に三角形を書く先が無い。ファイルも作らない）。
- **undo / redo に載る。** メッシュボディの追加は操作ログ1行（verb `add_mesh_body`）で、
  三角形は `args` にそのまま入る。**元になったB-repを args に持たない** —— 持つと、元の
  ボディをその後に編集した文書を再生したときに別の形のメッシュが復元される。代償として
  **数万三角形のメッシュを足したモデルのJSONは数MBになる**。

`add_mesh_body` は**WebSocketには開けていない**（`catalog` の `ws="none"`）。画面から三角形を
手で打つ操作は無く、入口はMCPと、形状最適化の結果を入れるコード側だけである。

### 形状最適化を走らせる

条件を置く4 verb（`set_preserve_bodies` / `set_exclude_bodies` / `set_fixtures` / `set_loads`）の
**続き**が `duocad/optimize_run.py` である。結果は既存の `add_mesh_body` だけで文書へ入る
（元のボディは消さず・形も差し替えない）。

**設計・解像度・画面・デバッグ・撮影の詳細は [docs/TOPOPT.md](docs/TOPOPT.md)「設計と画面」が正。**
README に残すのはアーキテクチャ上の要点だけ:

- **MCPには同期の `run_optimization` を開けていない**（進捗・中断を運べない）。画面は WebSocket の
  protocol 口（`OptimizePanel.svelte` → `optimize_conditions` / `optimize_resolution` / …）
- **メッシュボディは保持・除外に指定できない**（上「ボディの種別」の名指しの線）
- **受入検査**は §3 の `topopt_checks/`。画面からの実行を通す実機 smoke は持たない（壊れれば使ってすぐ気づく）

### 前提にしないこと

- **複数のブラウザ・複数の人が同じ文書を同時に編集すること。**
  WebSocketは複数接続へ配信する作りだが、想定するのは「1人が1つのブラウザで開いている」
  ことだけで、同時編集の経路に `Session` 全体を守る排他は無い。形状が壊れることはない
  （verbの適用は同期でawaitを挟まない）が、操作ログや選択状態が意図と違う順序で反映され
  うる。**この前提を変えるなら `duocad/net/ws.py` の `Server` に排他を入れるのが先。**

### モジュールとimport境界

**`duocad/` 配下で `OCP` を import してよいのはカーネル層の6本(`kernel.py` / `kernel_brep.py` / `kernel_meshing.py` / `kernel_sketch.py` / `kernel_extrude.py` / `mesh_geometry.py`)と `topopt/voxelize.py` だけ、
`build123d` は `query.py` と `topopt/` の3本だけ。**
`tests/test_boundaries.py` がソースを再帰的に静的走査して機械的に検査する（動的importも検出）。
**verbがカーネルを直接叩き始めるのを防ぐ唯一の安全網。**

| モジュール | 役割 | OCP | build123d |
|---|---|---|---|
| `duocad/document.py` | `Document` / `Body` のデータモデル | ✗ | ✗ |
| `duocad/oplog.py` | `Operation` / `OpLog`（undo・再生・永続化。**verb名の分岐を書かない**＝レジストリ） | ✗ | ✗ |
| `duocad/sketch.py` | `Sketch` / `Entity` / `Pattern` とスケッチのverb | ✗ | ✗ |
| `duocad/region.py` | 閉領域の検出（`BRepAlgoAPI_Splitter`）。領域は**描くものではなく計算されるもの** | ✗ | ✗ |
| `duocad/parameters.py` | 名前つきの寸法値・`set_parameter`(公開面には無い。ログからの再生用)・ログからの再生 | ✗ | ✗ |
| `duocad/selection.py` | 選択集合（人のクリック側。可視性を含む「何が選べるか」） | ✗ | ✗ |
| `duocad/predicates.py` | **述語による選択（AI側）**。構造化JSONでDSL文字列ではない | ✗ | ✗ |
| `duocad/speculative.py` | プレビュー（確定前にverbを投機的に走らせて捨てる） | ✗ | ✗ |
| `duocad/verbs/*.py` | verb層（1 verb 1モジュール。純関数と、対象を選択から取る入口） | ✗ | ✗ |
| `duocad/entry.py` | verbの入口の登録表（`@entry`）。`Session.apply` がここから名前で引く | ✗ | ✗ |
| `duocad/catalog.py` | **verbの単一宣言**（名前・引数・カテゴリ・プレビュー可否・MCPスキーマ）。下の2つが持つ表はここからの導出 | ✗ | ✗ |
| `duocad/svg_import.py` | SVG → スケッチの図形（**パーサはbuild123dの推移依存で既に入っている** svgelements） | ✗ | ✗ |
| `duocad/query.py` | 問い合わせ層のインタフェース | ✗ | **○ここだけ** |
| `duocad/kernel.py` | OCCTを直接叩く層（テセレーションもここ） | **○** | ✗ |
| `duocad/kernel_brep.py` | 同じカーネル層。生成CADのBRep保存・ブーリアン・最終ソリッドの幾何検査と、箱・格子との共通体積(形状最適化の受入検査) | **○** | ✗ |
| `duocad/mesh_geometry.py` | 同じカーネル層。三角形境界の接続検査と、向き・空洞を保ったCADソリッド化。位相の列挙と compound の定型(`subshapes` / `indexed_map` / `compound`)もここ | **○** | ✗ |
| `duocad/kernel_sketch.py` | 同じカーネル層。スケッチ平面の枠(`face_frame` / `sketch_plane_frame` / `offset_plane_frame`)、図形種別ごとの辺と2D bbox(`_KERNEL_ENTITY_KIND_TABLE`)、閉領域の検出(`sketch_regions`)。kernel を読むだけの一方向 | **○** | ✗ |
| `duocad/kernel_extrude.py` | 同じカーネル層。押し出しの幾何(`extrude_faces` = 既存ソリッドの同一平面の面群を join/cut/new、`extrude_regions` = スケッチ領域を join/cut/new)。kernel を読むだけの一方向 | **○** | ✗ |
| `duocad/kernel_meshing.py` | 同じカーネル層。OCCTの面分割を段階制御し、面分割の直前に境界折れ線の交差を検査して共有辺を再分割する(`tessellate` の実体) | **○** | ✗ |
| `duocad/boundary_check.py` | 境界折れ線(pcurve の離散点列)の交差・接触・重なりの判定。固定εを使わず、確定しない組だけ整数演算 | ✗ | ✗ |
| `duocad/mesh_intersections.py` | 三角形メッシュの自己交差(正当な共有辺・共有頂点の接触を除く)。最終成果物の検査 | ✗ | ✗ |
| `duocad/session.py` | `Session`（1つの文書を持つサーバ側の状態。verbを掛ける唯一の口 `apply`・選択・保存/開く。**配りものは持たず `view` へ委譲**） | ✗ | ✗ |
| `duocad/view.py` | `DocumentView`（**配りもの**＝状態配信・版・テセレーション/辺/頂点/体積/スケッチのキャッシュ。文書を変えない） | ✗ | ✗ |
| `duocad/paths.py` | `REPO_ROOT` と、そこから出る既定の置き場（`web/dist` / `models`） | ✗ | ✗ |
| `duocad/net/ws.py` | `Server`（WebSocketの配信と `_dispatch`）＋ソケットから踏めるコマンドの表 | ✗ | ✗ |
| `duocad/net/http.py` | `web/dist` の静的配信と `/mcp`（MCPのHTTPトランスポート） | ✗ | ✗ |
| `duocad/net/tools_on_loop.py` | `ToolsOnLoop`（別スレッドからの道具呼び出しをイベントループへ限定する覆い） | ✗ | ✗ |
| `duocad/server.py` | **入口**（再輸出。`mcp_server.py` とテストはここから名前を取る） | ✗ | ✗ |
| `duocad/mcp_server.py` | MCPサーバ。**アプリ層ではなくコアのもう一人のクライアント** | ✗ | ✗ |
| `duocad/agent/` | **アプリ内AIエージェント**（プロバイダのアダプタ＋tool-useループ。UIは持たない） | ✗ | ✗ |

**`duocad/net/` → `duocad/session.py` → `duocad/view.py` の一方向**（`session.py` から `net/` を参照せず、`view.py` から `session.py` を参照しない）。`DocumentView` が配る封筒のうち**持ち主が `Session` の値**（`can_undo` / `applied_count` / 「いまプレビュー中か」）は、`Session._view_inputs()` が引数で渡す（`view` 側に旗を複製しない）。唯一の逆向きは `Server._ensure_ai_loop` が `build_provider` を**入口（`duocad.server`）越しに引く**ところで、これはプロバイダの差し替え点を `duocad.server.build_provider` に固定するため（`tests/test_agent_ws.py` がそこを monkeypatch する。直接importすると差し替えが黙って効かなくなる）。呼び出し時まで遅らせてあるのでimport時の循環にはならない。

### frontend（`web/`）

**Svelte 5 + TypeScript + Vite。SvelteKitは使わない**（2026年の破壊的変更はほぼ全てSvelteKit側で、
使わなければ年1回ではなく5年周期の側に乗る）。CSSはバニラ（CSS変数）。

**守るべき唯一のルール: canvasはフレームワークの外に置く。** three.jsが所有するコンテナを渡し、
Svelteは二度と中を触らない。また `viewport.ts` を狭いインタフェースにして
**three.jsの型を外へ漏らさない**（最もchurnの激しい依存を `viewport.ts` とその兄弟モジュールに閉じる）。

| ファイル | 役割 |
|---|---|
| `web/src/App.svelte` | 画面全体の組み立て・選択の規則・キー処理 |
| `web/src/{displaySettings,documentFiles,aiChat,measurement}.svelte.ts` | **`App.svelte` から切り出した状態の持ち主**（画角・原点ギズモ・断面／ファイル操作と一覧／AIとの会話／測定の結果）。必要なものを引数で受け取り、読み口と操作を返す。**選択・ピック・コマンドの確定には触らない**かたまりだけを置く |
| `web/src/viewport.ts` | three.js の囲い（load / onPick / highlight / project …） |
| `web/src/session.ts` | WebSocketクライアント。**WSのURLはサーバが `/duocad-config.json` で教える**（ポートを推測しない） |
| `web/src/generated/protocol.ts` | **生成物**（コマンド名と分類・WebSocket の引数・`Applied`・状態のキー）。正は `duocad/catalog.py`。手で直さない |
| `web/src/commands/CommandHost.svelte` | **コマンド層の骨格**（起動→引数収集→プレビュー→Enter/Esc、**および即適用の発火**）。verbごとに作り直さない |
| `web/src/commands/{command,sketch,features,files,moveMode}.ts` | 各コマンドの引数の集め方の定義だけ（**Enter要否はここから導出**＝`needsEnter`）。`command.ts` に共通の枠組み（`ArgSpec` の種別・`when` によるモード依存の必須判定） |
| `web/src/{Browser,Files,Sketch,Parameters}Panel.svelte` | 各パネル（コアが持つものを描き直すだけ）。**選択パネルは無い**（選択を直すのは3Dクリックだけ） |
| `web/src/Toast.svelte` | **エラーの札**。`fail(message)` の1本だけで「控え(`errorLog`)に積む」と「札を出す」を両方やる。札だけ出す口は公開しない |
| `web/src/AiPanel.svelte` | **AIパネル**。プロバイダ選択・対話・ツール呼び出しの進捗・中断。文書は描かない。**`#ai-collapse`(`BrowserPanel.svelte`と同じ形)で畳める** |
| `web/src/SettingsDialog.svelte` | **設定ダイアログ**。native `<dialog>`（非モーダル、`show()`）。**一度決めたら触らない設定だけ**（いまは正投影のチェックボックス1つ。画角は正投影/透視30度の2値で、スライダーは無い）。ビューポートを覆わず暗くもしない |
| `web/src/viewSettings.ts` | **画面設定のlocalStorage**。入れてよいのはクライアント側だけの設定で、**文書の状態は入れない**。読み書きとも例外を投げない |

### AI側（MCP）

**ツールの一覧は [MCP.md](MCP.md) が正**（ここに写すと、増減のたびに2箇所を直すことになる）。
表と実際の `tools/list` の**顔ぶれの一致**は `tests/test_doc_code_agreement.py` が機械で見る。

設計として押さえるのはこの4点:

- **面・辺・領域は引数で受けない。選択から取る**（数値・名前・パスだけが引数）。
  選択はそのままverbに渡せる（確定の手は無い）。**WebSocketのコマンドも
  同じ形**。`sketch_create` も MCPと同じく `base_plane` だけを取り、省略時は選択の面
  （ちょうど1枚）に貼る
- **ツール名 == WebSocketのコマンド名 == UIのコマンドID == 操作ログのverb名**。
  AIは `describe_document` のログを読んで次の一手を決めるので、綴りが違うと対応表を
  推測させることになる。**1つのツールを引数で複数のverbに打ち分けない**のはこの規律の帰結
- 述語は構造化JSON: `{"kind":"edge","where":[{"op":"parallel_to","axis":"Z"},{"op":"length_gt","value":20}]}`
  （`where` はAND。**語彙は `describe_predicates` が自己記述する**ので、ここには写さない）
- 名前の一致は両方向のテストが固定する
  （`test_tool_names_match_the_verb_names_in_the_operation_log` と
  `tests/test_ui_command_wiring.py`）

公式SDKは入れず、JSON-RPC 2.0 を素で実装している（依存を増やしていない）。**トランスポートは
stdioとHTTP（`/mcp`）の2本**で、`McpServer.handle()` はどちらも知らない —— 1メッセージの辞書を
受け取って返すだけなので、口が増えても中身は増えない（契約は `tests/test_mcp_http.py`）。

### AI側（アプリ内エージェント・`duocad/agent/`）

**AIから文書を触る道は2本ある**（§1 と同じ）。`duocad/agent/` は**先にアプリが立っていて、アプリの中から
AIを選んで繋ぐ**（今あるのは llama-server の1つ）。**MCP経路は残す** —— Claude Codeから
触る道とアプリ内の道は共存する。どちらも「先にアプリが立っている」という同じ向きに揃っている。

- **道具の定義を第二の場所に書かない** —— `DocumentTools.descriptors()` を渡し、`.call()` で実行し、
  失敗は `.error_envelope()` に畳む（**語彙の列挙の正は [MCP.md](MCP.md)「実装のメモ」の1箇所**）。
  MCP経路と顔ぶれ・挙動・失敗の形が同一である
- **システムプロンプトも二重に持たない** —— `catalog.agent_instructions()` の1本をMCPの
  `instructions` とエージェントの両方が読む（ツール数も宣言から数える）
- **暴走は必ず打ち切る** —— `max_turns` / `max_tokens` / 1リクエストのタイムアウト / 外から立てる中断フラグ。
  打ち切った理由は構造化して返す（矛盾した指示を渡すと**思考**が膨らみ、1リクエストが600秒返らなかった実測がある）。
  **待ち時間切れと中断は接続を閉じる** —— 応答はストリーミングで受ける。llama-server は非streamingの
  リクエストを、閉じても最後まで生成し続けるからである（`StreamingTransport`）
- **済んだ依頼は「人の発話 + AIの最終返答」だけを残す** —— ツールの往復は次の依頼の頭で捨てる（量の大半を
  占め、文書が変わったあとは古い）。相手の文脈長に当たったら古い依頼を半分まとめて捨ててやり直す
  （`AgentLoop._settle_history` / `_drop_oldest_half`）
- **ツール定義は先に読ませておく** —— 画面が繋がった時点で `ai_prepare` が届き、system + tools を相手の
  キャッシュへ載せる。llama-server を `--slot-save-path <dir>` 付きで立てていれば、その状態を保存して次回は
  復元で済ませる（`OpenAICompatProvider.prepare`）
- **読む側と書く側の時間を結果に載せる** —— llama-server の `timings` を往復ぶん足して `ai_result.usage` へ入れ、
  パネルに1行出す
- **文書と選択は先に見ておく** —— モデルが依頼のたびに最初に呼ぶ `describe_document` / `get_selection` を、
  ループが代わりに呼んで「呼んで結果が返った」形で履歴へ置く（`AgentLoop._look_first`。人の発話には何も足さない）。
  往復の実測と、発話に添える方式を採らなかった理由は [docs/AI_INTEGRATION.md](docs/AI_INTEGRATION.md)「往復回数の実測」。
  測り直すときは `python tools/llm_tasks.py`
- **同時編集は楽観ロックで守る** —— モデルへの1往復を送る前に文書の版
  （`DocumentTools.current_seq()`）を控え、応答が返ったとき版がずれていたら、そのツール呼び出しは
  実行せず「文書が変わったので選び直せ」と返す。形状を変えるverbの確定後は選択が全リセットされる
  （`Session._reset_selection_if_needed`）ので大半は元から `selection_empty` で止まっていたが、
  `set_visible` のように選択を経由しないverbはこの網に掛からず、楽観ロックがその穴を埋める
  （`fillet`/`chamfer`/`extrude` は対象ボディの可視性も実行時に見直す）。
  **版は「ログの本数・選択の指紋・可視性の指紋」の3つ組** —— 可視性は操作ログに
  載らないので、指紋を持たないと「AIが考えている間に人がボディを隠した」が版に出ない
- **鍵を持つ層ではない。** `build_provider` はプロバイダへ
  鍵を渡さないので、`base_url` をどこへ向けても飛ぶのはキーの無いリクエストだけになる。
  **鍵を要るプロバイダを足すなら、`Server._ai_configure` に `base_url` の拒否を書き戻すこと**
- 依存は増やしていない（公式SDKを入れず標準ライブラリで素のHTTP）

契約は `tests/test_agent_loop.py`（偽プロバイダで決定的に検査）と、`tests/test_agent_streaming.py`（受け方と
打ち切り）・`tests/test_agent_prepare.py`（ウォームアップ）・`tests/test_agent_history.py`（履歴）。実機は
`tests/test_agent_llama_server.py` の1本だけで、**8080にllama-serverが居なければskipする**。
実測と数値は [docs/AI_INTEGRATION.md](docs/AI_INTEGRATION.md)。

**WebSocketへの配線。** `duocad/net/ws.py` が `ai_get_status` / `ai_configure` / `ai_prepare` /
`ai_send` / `ai_cancel` の5つを持ち、画面右サイドバーの `web/src/AiPanel.svelte` から叩く。

- **`ai_*` はverbではない**。文書を直接は変えない(操作ログに載らずundoの単位でもない)ので
  `duocad/catalog.py` の `COMMANDS`(「MCPツール名==WSコマンド名==UIコマンドID==verb名」の
  一致規律)には入れない。対応するMCPツールも作らない(AIがAIを起動する口は要らない)。
  除外は `tests/test_ui_command_wiring.py::test_ai_commands_are_not_verbs` が固定する
- **`ai_send` はモデルへのHTTPリクエスト(数十秒かかりうる)を待たずに返る** ——
  `AgentLoop.run()` を `run_in_executor` で実行器スレッドへ逃がし、進捗
  (`ai_tool_call`)と最終応答(`ai_result`)は別イベントとして後から届く
  (`Server._ai_start` のdocstring)
- **`Session` に触るのは依然イベントループのスレッドだけ** —— 実行器スレッドから
  `DocumentTools` を直接叩くと、WebSocket/MCPと同時に2スレッドが文書を書き換えうる。
  `Server._AiToolsOnLoop` が `.call()` / `.error_envelope()` / `.current_seq()` を1回ずつ
  `asyncio.run_coroutine_threadsafe` でイベントループへ投げ返し、この不変条件を保つ
  (`mcp_server.py` 冒頭の「既知の割り切り」を作り直さずに済む形)
- **AIがツールを呼んで文書を変えると、通常のverb配信に乗って開いている画面が変わる**
  (`applied` は付かない。MCPが変えたときと同じ扱い)
- 中断は `ai_cancel`。走っているHTTPリクエスト自体は切れないので、反映は次の
  チェックポイント(モデルへ投げる前・ツール1件ごと)まで遅れうる —— 「どこまでやったか」は
  `ai_result.tool_calls` に残る

契約は `tests/test_agent_ws.py`(偽プロバイダで `duocad.server.build_provider` を差し替え、
実キー・実HTTPなしで決定的に検査)。

---

## 5. リポジトリの地図

```
duocad/
  .mcp.json              Claude Code向けのMCP登録（projectスコープ。これがあるので登録作業が要らない）
  README.md              ← このファイル（入口）
  MCP.md                 MCPサーバの仕様と登録手順
  docs/                  INITIAL_PLAN(最初期の計画) / TOPOPT
  duocad/                コア（Python）。verbs/ に1 verb 1モジュール
                         session.py=1つの文書を持つサーバ側の状態／view.py=その配りもの
                         （状態配信・版・キャッシュ。session.py が1つ持つ）
                         net/ はアプリ層との接点（ws.py=WebSocket／http.py=静的配信と/mcp／
                         tools_on_loop.py）。server.py はその入口で、再輸出と起動の番人だけ
                         agent/ はアプリ内AIエージェント（プロバイダ＋ループ・UIなし）
  tests/                 pytest。README.md はモジュール単位のAPI契約リファレンス
  web/                   frontend（Svelte 5 + Vite）
    src/                 画面
    scripts/             実ブラウザの検査（smoke*.mjs）と撮影（screenshot.mjs:
                         空の文書とモデルを読んだ画面を --tag 付きで撮る。見た目の前後比較用）。共有部品は
                         harness.mjs（骨格）／server.mjs（サーバの起動・停止）／browser.mjs（ブラウザ選択・起動）／
                         pick.mjs（指す手）／mcp-client.mjs（MCPクライアント）／args.mjs／models.mjs（STEPのパス）
    dist/                ビルド成果物（**Pythonサーバが配信するのはこちら**・git管理外）
  .venv/                 Python 3.11（git管理外）
  pyproject.toml         依存はバージョン固定（ローカル専用なので上げなければ壊れない）
```

サンプルのSTEPは `web/scripts/models.mjs` の `PC_CASE_STEP` が指す場所（リポ外の読み取り専用ディレクトリ）。**読み取り専用で扱うこと。**

---

## 6. 既知の落とし穴

### 運用の注意

- **duocadのサーバを2つ同時に立てない**（8765/8766がぶつかり、後から立った方がWebSocketのbindで落ちる）。
  **Claude CodeがMCPをHTTPで繋ぐ限りはこれを起こさない**（クライアントはduocadを起動しない）
  —— ただし**stdioの登録が残っているとまだ起きる**（§1.2）。
- **`/mcp`（8766）はブラウザから叩けない。** `Origin` を持つリクエストと、
  `127.0.0.1:<port>` / `localhost:<port>` 以外の `Host` を403で落とす。**認証トークンは無い** ——
  このPCでコードを実行できる相手はトークンを書いたファイルも読めるので、守れる相手が増えない
  （下のWebSocketのOrigin検査と同じ線引き）。契約は `tests/test_mcp_http.py`。
- **WebSocket(8765)はOriginを検査する。** 繋げるのはduocad自身が配るページだけ
  ——静的配信（`--http-port`、既定8766）と Vite dev server（5173）を、`127.0.0.1` / `localhost`
  の両方の綴りで許可する。**`Origin` ヘッダを送らないクライアント（`smoke*.mjs` などの素のWS）は
  今までどおり繋がる**（意図した許可。理由は `tests/test_ws_origin.py` 冒頭）。
  WebSocketは同一オリジンポリシーの対象外なので、検査が無いと**duocadを立てたまま別のサイトを
  開いているだけで**、そのページが文書を読み・壊し・`export_step` で任意のパスへ書き・`ai_send` で
  課金を消費できた。`--http-port` を既定から変えると許可リストもそれに追随する。
- **`web/dist` を作り直さないとUIの変更が画面に出ない**（§1 参照）。
  `dist` が無くても `mcp_server` は**静的配信だけ諦めて起動する**ので、
  「立ったのに画面が出ない」という形で出る（理由はstderrに1行出ている）。
- **文書は接続より長生きする。** ブラウザを閉じても消えないので、まっさらから試すときはサーバを立て直す。
- **読み込んだSTEPはそのまま編集できる。**

### 実測で判明している技術的な地雷（コード側は対処済み。数値を疑うとき用）

- **OCCTのフィレットは例外を投げずに失敗し、しかも「通ったのに壊れている」帯がある**
  （20mm立方体・全辺。2026-09-18 実測: r=3.0 と r=9.9 は成功、**r=10.0 は `IsDone()=False`**、
  **r=10.5 と r=11.0 は `IsDone()` が真なのに `BRepCheck` が偽** —— 体積が 4251.51(r=9.9) から
  9246.46 へ**増えて**おり、削ったはずが増えている。r=30.0 は `IsDone()=False`。
  **半径に対して単調ですらない**）→ verbのラッパで毎回 **`IsDone()` / 空でない / 体積>0 /
  `BRepCheck`** の4つとも通す。`IsDone()` だけでは 10.5〜11.0 の帯を素通しする。
- **面の添字は形状変更で壊れる**（同じ面がフィレットで i=5→9。ただし穴の径変更等では動かない＝**散発的に静かに間違う**）
  → 生の添字を保存・再利用しない。由来スロットを day1 から用意してある。
- **STEP往復の体積は bit-exact ではない**（相対 約8e-16 の1ULP級）→ 比較は `math.isclose(rel_tol=1e-6)`。
- **STLの偏差は絶対値（`isRelative=False`）で渡す** → 相対にすると偏差を1/20にしてもメッシュが1バイトも変わらない。
- **STEP読み込みは `ReadFile()` の戻り値を検査してから `TransferRoots()`** → 怠るとPython例外ではなく
  **プロセスごと落ちる**（0xC0000005）。
- **OCCTのSTEP書き出しはC++からstdoutへ統計を吐く** → MCPのJSON-RPCへ割り込んで接続が死ぬので、
  起動時に fd 1 を fd 2 へ振り替えてある（`_claim_stdout`）。

### 現行verbの制約

- **面取りは距離1つ（＝45度）しか取れない。** PCケースの脚の円錐は半径方向4mm・軸方向22mmの
  非対称な面取りなので、**面の種類は作れても寸法までは再現できない**。
- 拘束ソルバは持たない（各図形が自分の数値を持つパラメトリック・プリミティブで足りている）。
