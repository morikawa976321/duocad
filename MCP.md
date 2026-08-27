# duocad の MCP サーバ — AIから文書を触る

**AIが文書を操作するための口**（F / 2026-08-11）。ブラウザとMCPは同じ `Session` を叩く対等な
クライアントで、MCPが変えた内容は開いている画面へ即座に届く。

公開ツールは**28個**。面・辺・領域などの**形は引数で受けず、確定した選択から取る**
（数値・名前・パスだけが引数）。提案の選択はそのまま verb に渡せない — 先に
`commit_selection` が要る。**この1手はAI自身が踏んでよい**（人の承認を待つ規則は
2026-08-22に外した。どこまで自分でやるかは下記「AIはどこまで自分でやるか」）。

## 公開ツール（28）

| 群 | ツール |
|---|---|
| 問い合わせ(3) | `describe_document` / `describe_shape` / `measure` |
| 述語の自己記述(1) | `describe_predicates` |
| 選択(5) | `get_selection` / `propose_selection` / `modify_selection` / `commit_selection` / `clear_selection` |
| スケッチ(6) | `sketch_create` / `add_line` / `add_circle` / `add_rectangle` / `add_arc` / `import_svg` |
| フィーチャ(6) | `extrude` / `extrude_face` / `fillet` / `chamfer` / `pattern_rectangular` / `pattern_circular` |
| ボディ・パラメータ(2) | `set_visible` / `set_parameter` |
| 入出力(3) | `import_step` / `export_step` / `export_stl` |
| 履歴(2) | `undo` / `redo` |

**呼び方の型**（例）:

1. `describe_document` で `body_id` / スケッチ / パラメータ名を知る
2. `propose_selection`（`refs` か `predicate`）→ 必要なら `measure` で確認 → `commit_selection`
   （**AIが自分で踏む**。人に見せて止めるときだけ踏まない）
3. `fillet(radius=…)` / `extrude(distance=…)` など — **辺や領域は引数に書かない**
4. `import_step` は**読んだ時点で編集できるボディ**を返す（`body_ids`。追加の1手は要らない）
5. `import_svg(sketch_id, path)` はSVGの図形を**いま開いているスケッチへ**足す
   （2026-08-28。新しいスケッチは作らない —— 無ければ先に `sketch_create`）。戻り値は
   足した `entity_ids` と `region_count`。そのまま `propose_selection` → `commit_selection`
   → `extrude` へ進める。**領域は1枚のSVGに複数あるのが普通**（文字の内側の穴・離れた図形）
   なので、押し出す前に `describe_document(include_regions=True)` で面積を見て選ぶこと

## AIはどこまで自分でやるか（2026-08-22改訂）

**「提案して人の承認を待つ」を無条件の規則にするのはやめた。** `commit_selection` は
最初から上の一覧（選択5個）に載っており、AIが自分で確定して適用することは機構的に
ずっと可能だった —— 承認を強制していたのは文書とプロンプトだけである。規則は2段になった。

### (1) 確認を待たず適用してよい操作

次の**どちらにも当たらない**操作:

- 対象を**何個でも取る**引数がある（何本の辺で終わりなのかは機械には決められない）
- **how**（寸法・選択肢・パス）の入力がある（打ち終わり・選び終わりも機械には決められない）

**how は「何をどうするか」＝操作の結果を決める値だけ**である（寸法・分岐の選択・出力先のパス）。
`sketch_id` や `body_id` のような**既存の対象を指すID**・点の座標・スケッチを貼る先の平面名
（`base_plane` の `XY`/`XZ`/`YZ`）は、文字列や列挙に見えても**対象（target）の側**であり
how には数えない（手動UIでも `PlaneArgSpec` / `point` は how ではない）。

**どちらでもない付帯入力もある。** 既存の対象を指してもいなければ結果も決めない引数
（新しく作るものへの命名など）で、**省略でき、既定がある**ものである。打ち終わりを
人に聞く必要がそもそも無いので how には数えず、手動UIにも出さない（`ws=False`）——
この種の「省略可の付帯入力」は第1段の判定を変えない。
**いま該当する引数は1つも無い**（唯一の実例だった複製の `name` が2026-08-27に消えた）ので、
ここに書けるのは規則だけである。無い例を作文しないこと。

どちらにも当たらなければ対象は1つに定まり、人に聞くことが残っていない。AIは
`propose_selection` → `commit_selection` → verb を**一息に実行してよい**。

**この基準は手動UIのEnter要否と同一のもの**である（人が押すEnterは「まだ足すのか・
打ち終わったか」を人が言う手であり、AIが確認を待つ理由と同じ）。判定の実物は
`web/src/commands/command.ts` の `needsEnter`、どのverbがどちらかの表は
`tests/test_ui_command_wiring.py`（`ENTER_REQUIRED`）が機械で固定している。
**顔ぶれをここに写さない** —— verbを足した日に古くなるうえ、基準さえ書けばツールの
引数から導ける。

### (2) それ以外の操作 — 止まるか進むかをAIが判断する

提案で止めるか適用まで行くかを、**AIがユーザーのプロンプトから自律的に判断する**。目安:

| 依頼 | AIの動き |
|---|---|
| 対象も数値も定まる具体的な指示（「上面の穴4つを半径2でフィレット」） | `commit_selection` して適用まで行く |
| 曖昧・探索的（「角を丸めたい」「どこを削ればいい?」） | `propose_selection` で止め、何を選んだかを述べて確定してよいか尋ねる |

**迷ったら止める側に倒す** —— 提案は人がクリックで直せるが、適用してしまった形は
`undo` で戻すしかない。

### 止める選択肢を成立させているもの

提案のまま verb を呼ぶとコアが `selection_not_committed` で断る（`duocad/selection.py` の
`confirmed_items`）。**この門があるから「止めた」が本当に止まる**ので、撤去していない。

> AIへ渡す文（MCPの `instructions` とアプリ内AIパネルのシステムプロンプト）は
> `catalog.agent_instructions()` の1本で、この規則もそこに入っている（下記「実装のメモ」）。

## 何が起きるか（このサーバの位置づけ）

```
  MCPクライアント(Claude Code / Claude Desktop)
        │ stdio (JSON-RPC 2.0)
        ▼
  duocad.mcp_server ── Session(文書・操作ログ) ──▶ WebSocket ──▶ ブラウザ(web/)
        ▲                     ▲                                      │
        └─ AIの操作           └── 変更イベント(上り方向) ────────────┘
```

- MCPサーバは**アプリ層ではなく、コアのもう一人のクライアント**。ブラウザとMCPは同じ文書を
  叩く対等なクライアントで、MCPがブラウザを操作することはない。
- したがって操作は**アプリ層を一切通らずに**文書を変える
  （実装計画書§2「コアはUIが無くても機能的に完全」の実証）。
- **AIが文書を変えると、開いているブラウザの表示が自分では何もしていないのに変わる**。
  実体は `Session.add_listener`（`duocad/server.py`）。
- **1プロセスで全部を持つ**（MCP＝stdioと `/mcp`、WebSocket、静的配信）。別プロセスにすると
  「AIが変えた文書」と「人が見ている文書」が別物になる。

## 起動

**先に自分でduocadを立て、MCPクライアントは後から繋ぐ**（2026-08-26）。人間は同じプロセスが
配信する `http://127.0.0.1:8766/` をブラウザで開く。

```
.venv\Scripts\python.exe -m duocad.mcp_server [オプション]
```

| オプション | 既定 | 意味 |
|---|---|---|
| `--port` | 8765 | WebSocket |
| `--http-port` | 8766 | 静的配信（ブラウザで開く）＋ `/mcp`（MCPのHTTPトランスポート） |
| `--deviation` | 0.2 | テセレーションの線形偏差（mm・絶対） |
| `--web-dist PATH` | `web/dist` | 静的配信するディレクトリ |
| `--open` | — | 起動時にブラウザを開く（既定は開かない） |

`web/dist` が無い場合は**静的配信を諦めてstdioのMCPだけで動く**（理由はstderrに出る）。
このとき `/mcp` も同じHTTPサーバに載っているので**一緒に立たない**。先に
`cd web && npm install && npm run build` しておくこと。

**注意**: duocadのサーバを2つ同時に立てない（ポートがぶつかり、後から立った方がWebSocketの
bindで落ちる）。

## トランスポートは2本

`handle()` はトランスポートを知らない（1メッセージの辞書を受け取って返すだけ）ので、
**同じ28ツールがどちらの口からも同じ顔で出る**。契約は `tests/test_mcp_http.py` が固定する。

| | 誰が使う | プロセスの所有者 |
|---|---|---|
| **HTTP** `http://127.0.0.1:8766/mcp` | Claude Code | **duocad自身**（クライアントは後から繋ぐ） |
| **stdio** | Claude Desktop など、stdioしか話せないクライアント | MCPクライアント |

`/mcp` は `Origin` を持つリクエスト（＝ブラウザから来たもの）と、知らない `Host` を403で落とす。
**認証トークンは置いていない** —— このPCでコードを実行できる相手はトークンを書いたファイルも
読めるので、守れる相手が増えないためである（`WebHandler._mcp_forbidden` のdocstringが正）。
サーバ→クライアントのSSEストリームは提供しない（pushするものが無い）ので `GET /mcp` は405。

## Claude Code への登録（要らない）

**リポジトリ直下の `.mcp.json` が登録そのもの**（projectスコープ）。duocadのディレクトリで
Claude Codeを起動すれば繋がる —— 起動時に**cwdの `.mcp.json` だけ**を読む（親は遡らない）。
初回だけ承認を訊かれ、結果は `~/.claude.json` の `enabledMcpjsonServers` に残る
（`claude mcp reset-project-choices` でやり直せる）。

```json
{
  "mcpServers": {
    "duocad": {
      "type": "http",
      "url": "http://127.0.0.1:8766/mcp"
    }
  }
}
```

**一度だけやること**: 古いstdio登録を消す（`claude mcp remove duocad -s local`）。スコープの優先順位は
**local > project > user** で**エントリ全体が入れ替わる**ため、localに残っている限り
`.mcp.json` は黙って覆い隠される。

繋がったかは `claude mcp list`（接続まで見る）。セッション内では `/mcp` でツール一覧が出る。
**duocadが立っていなければ繋がらないだけ**で、Claude Code自体は起動する。HTTPのサーバは
**自動で繋ぎ直す**（初回失敗で最大3回、セッション中の切断で最大5回・指数バックオフ）ので、
duocadを立て直してもClaude Code側の操作は要らない。起動時の待ちは既定30秒（`MCP_TIMEOUT`）。

**起動時に読むSTEPを決める引数は無い**（2026-08-15に `--step` を廃止）—— サーバは常に空の
文書で立ち、STEPは `import_step` で読む。

duocad以外のディレクトリからも使う日だけ、userスコープへ手で登録する
（`claude mcp add --scope user --transport http duocad http://127.0.0.1:8766/mcp`）。
28ツールが無関係なプロジェクトにも出るので既定にはしない。

## Claude Desktop への登録

Claude Desktopはstdioで繋ぐので、**あちらがduocadのプロセスを所有する**（人間用UIの寿命も
Claude Desktopのセッションに縛られる。1.2のHTTPのような後付けの接続はできない）。
`%APPDATA%\Claude\claude_desktop_config.json` に書き、**Claude Desktop を再起動する**
（設定は起動時にしか読まれない）。

```json
{
  "mcpServers": {
    "duocad": {
      "command": "C:\\Users\\tm976\\mf\\duocad\\.venv\\Scripts\\python.exe",
      "args": ["-m", "duocad.mcp_server"],
      "cwd": "C:\\Users\\tm976\\mf\\duocad"
    }
  }
}
```

## 使い方（AIの側の手順）

1. `describe_document` を呼ぶ → `body_id`・スケッチ・パラメータ名を得る
2. 手前の部品を隠すなら `set_visible(body_id, false)`（**人間の画面からも消える**）。
   **可視性は操作ログに載らないので `undo` では戻らない** —— 戻すのは `set_visible(body_id, true)`
3. 対象を `propose_selection` で選び、`commit_selection` してから fillet / extrude 等
   —— **確定を人に頼まない**。提案で止めるかどうかの判断は上記「AIはどこまで自分でやるか」
4. STEPを足すなら `import_step(path)` —— **これ1回で編集できるボディが揃う**
   （戻り値の `body_ids` がそれ。`reference_ids` は隠してある原本）。
   **読み込みのツールはこれ1つだけ**で、参照ボディだけを足す口は無い（2026-08-27）
5. 取り消すのは `undo`（UIと同じ履歴）。**戻せるのは形を変えた操作だけ**で、
   可視性の切り替えと選択の変更は履歴に載っていない

読み込んだSTEPはソリッドごとに「参照ボディ（読み取り専用・隠されている）」と
「編集用ボディ」の対になる。`describe_document` が既定で返すのは**編集用だけ**で、
参照ボディの件数は `reference_count` で知らせる（`include_references=true` で出せる）。
**参照ボディの可視性は切り替えられない**（原本なので。編集用の `body_id` を使う）。

> **ツール名は操作ログのverb名と同じ綴りである**（例外なし。**WebSocketのコマンド名とUIのコマンドIDも同じ**）。ログを読んで次の一手を決めるとき、
> そこに出ている名前をそのまま呼べる。**1つのツールを引数で複数のverbに打ち分けない** ——
> 2026-08-15までは1つの `import_step` ツールが `editable` 引数で2つのverbを打ち分けており、
> **ログに出る方のverbを呼べるツールが存在しない**状態だった（QA指摘で解消）。
> 2026-08-27にSTEP読み込みのverb自体が `import_step` 1本へ統合され対応は1:1へ戻ったが、
> 規律は変わらない（読み込みのverbを増やすなら、同じ綴りのツールも増やす）。
> 一致は `tests/test_mcp_verbs.py::test_tool_names_match_the_verb_names_in_the_operation_log`
> が**両方向で**固定する（片方向だった間にこのずれが緑のまま入り込んだので、集合の一致にした）。

## 実装のメモ

- **依存を増やしていない**。公式SDK（`mcp` パッケージ）は入れず、JSON-RPC 2.0 を
  素で実装している（`duocad/mcp_server.py`）。トランスポートはstdioとHTTPの2本だが、
  `handle()` はどちらも知らないので**プロトコルの実装は1つ**である。使うのは `initialize` / `tools/list` /
  `tools/call` の3メソッドだけ。対応プロトコル版は `2025-06-18` / `2025-03-26` / `2024-11-05`。
- **stdoutに出してよいのはJSON-RPCの行だけ**。起動メッセージ・警告はすべてstderrへ出す。
  OCCTのSTEP書き出しがC++からstdoutへ統計を吐くため、ツール実行中は fd 1 を stderr へ振る。
- **AIへ渡す手順の文（`initialize` の `instructions`）は `catalog.agent_instructions()` の1本**で、
  アプリ内AIエージェント（`duocad/agent/`・README §5）と共有する。手順を2箇所に書くと
  片方だけ直った状態に気づけない。ツール数も宣言から数えるので、verbを足しても古くならない。
- ツールの実行時エラーは JSON-RPC エラーではなく `isError: true` の構造化封筒
  （`not_found` / `invalid_argument` / `selection_not_committed` / `selection_empty` /
  `selection_kind_mismatch` / `invalid_predicate` / `internal`）。
  `selection_empty` は確定選択が**丸ごと空**のとき、`selection_kind_mismatch` は
  **空ではないが要求した種別が無い**とき（例: フィレットを呼んだが面3枚が確定している）。
  後者は実際に確定している種別と件数を `present_kinds` で返すので、選び直す先が引数なしで分かる。
- 契約は `tests/test_mcp.py` / `tests/test_mcp_verbs.py` / `tests/test_mcp_selection.py`。
