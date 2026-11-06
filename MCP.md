# duocad の MCP サーバ — AIから文書を触る

**AIが文書を操作するための口。** ブラウザとMCPは同じ `Session` を叩く対等なクライアントで、MCPが変えた内容は開いている画面へ
即座に届く（実体は `Session.add_listener`、`duocad/server.py`）。MCPサーバはアプリ層を一切通らずに文書を変える
（[docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md)「層の分け方」）。**1プロセスで全部を持つ**（MCP＝stdioと `/mcp`、WebSocket、
静的配信）—— 別プロセスにすると「AIが変えた文書」と「人が見ている文書」が別物になる。

面・辺・領域などの**形は引数で受けず、選択から取る**（数値・名前・パスだけが引数）。選択に出所の区別は無く、人もAIも同じ操作
（`modify_selection` / `toggle_selection` / `clear_selection`。AIだけ述語を書ける `set_selection`）で付け外しし、そのまま verb に渡せる。
`toggle_selection` は画面の素のクリックと同じ（あれば外し、無ければ足す）。

## 公開ツール

| 群 | ツール |
|---|---|
| 問い合わせ | `describe_document` / `describe_shape` / `check_interference` / `measure` —— `measure` は選択の各項目の寸法に加え、**ちょうど2項目なら** `between`（点・直線・平面のあいだの距離。平行でない線・面どうしは角度）を返す。画面の「測定」と同じ計算。`check_interference` は見えているボディどうしの干渉（重なり・接触・離れ）を調べ、`clearance`（mm）でははめ合いの隙間も見る読み取り専用の問い合わせで、重い計算は子プロセスで時間の上限つき |
| 述語の自己記述 | `describe_predicates` |
| 選択 | `get_selection` / `set_selection` / `modify_selection` / `toggle_selection` / `clear_selection` —— 人（ブラウザ）が使う選択操作と同じ集合に、AIだけが述語を書ける `set_selection` を足したもの |
| スケッチ | `sketch_create` / `sketch_create_offset` / `add_point` / `add_line` / `add_circle` / `add_ellipse` / `add_rectangle` / `add_arc` / `import_svg` / `move_sketch_entities` / `mirror_sketch_entities` / `envelope_regions` / `project_to_sketch` / `project_outline` —— `project_to_sketch` は選択のソリッドの辺・面（外周と穴）を、`project_outline` は選択が属するボディを法線方向から見た外形（影の境界。外周と貫通穴）を、`sketch_id` のスケッチの平面へ写した普通の図形として足す（元の形に追従しない。点に潰れる辺は図形にせず、全部潰れると失敗）。`envelope_regions` は選択の複数の領域を、へこみに沿って包む**単一の領域線**（線分・円弧・円・楕円・3次ベジエ。正確な図形に直せない辺は誤差 0.001 mm 以下のベジエで近似）を同じスケッチへ足す。`radius`（埋める半径）と `offset`（広げる量・負で内へ）は省略で 0。1つの領域にならないと失敗して何も変わらない |
| フィーチャ | `extrude` / `revolve` / `loft` / `fillet` / `chamfer` / `shell_body` / `thread` / `delete_faces` / `offset_faces` / `boolean_body` / `pattern_rectangular` / `pattern_circular` / `move_body` / `mirror_body` / `split_body` / `delete` / `delete_items` / `rename` |
| 可視性 | `set_visible` / `set_sketch_visible` —— ボディ/スケッチで対。どちらも操作ログに載らず undo では戻らない。ボディを隠すと、そのボディのボディ・面・辺・頂点の選択項目は選択から外れ、戻しても選択は戻らない。**名前つき寸法の打ち直し（`set_parameter`）は公開していない**（人間側に口が無いため。口ができたら人とAIの両方へ同時に出す。`describe_document` はパラメータ名と値を返す） |
| 作業空間 | `get_command` / `begin_command` / `set_command_args` / `end_command` / `apply_command` / `enter_sketch` / `leave_sketch` —— **起動中の verb と途中の引数・モードを人の画面と共有する**(issue #153)。`begin_command(verb)` で起動すると人の画面にその verb が起動し、対象の候補(辺など)が見える。人が画面で起動した verb と打ちかけの引数は `describe_document` / `get_command` の `command` に見え、`set_command_args` で引数を足し、`apply_command` で確定する(人の Enter と同じ)。`end_command` は人の Esc。起動・引数・畳む・モードの操作は操作ログに載らず undo では戻らない（`apply_command` が確定した形の変更はログに載り、undo で戻る）。スケッチを作ると誰が作ってもそのスケッチへ入り、`enter_sketch` / `leave_sketch` で出入りする |
| 形状最適化 | `set_preserve_bodies` / `set_exclude_bodies` / `set_fixtures` / `set_loads` / `clear_loads` / `enter_optimize` / `leave_optimize` —— 保持/除外ソリッド・拘束面・荷重面の条件を置く（ボディと拘束は集合代入。荷重は大きさ・向きを面ごとに持つので上書き代入で、外すのは `clear_loads`）。**安定版には出さない**（凍結中。docs/STABLE.md「安定版で出さない機能」） |
| 入出力 | `import_step` / `export_step` / `export_stl` / `add_mesh_body` —— `export_*` の書き出し先は拡張子が形式（`.step`/`.stp`・`.stl`）と一致するものに限り、保存済みの文書（`models` の `*.json`）は上書きしない。最後の1つは**計算で作った三角形メッシュ**をボディとして足す（表示とSTL書き出しだけ。編集verbは断られる） |
| 履歴 | `undo` / `redo` |

> **ツール名は操作ログのverb名と同じ綴りである**（例外なし。WebSocketのコマンド名とUIのコマンドIDも同じ）。ログに出ている名前を
> そのまま呼べる。**1つのツールを引数で複数のverbに打ち分けない。** 一致は
> `tests/test_mcp_verbs.py::test_tool_names_match_the_verb_names_in_the_operation_log` が両方向で固定する。

## 呼び方の型

1. `describe_document` で `body_id` / スケッチ / パラメータ名を知る。手前の部品を隠すなら `set_visible(body_id, false)`
   （人間の画面からも消える。**可視性は操作ログに載らないので `undo` では戻らない** —— 戻すのは `set_visible(body_id, true)`）
2. `set_selection`（`refs` か `predicate`）→ 必要なら `measure` で確認
   - `measure` は**測るだけ**（verb ではない）。頂点・直線の辺・平らな面・スケッチの点・スケッチの線分・領域を2つ置くと `between.distance`
     （直線・平面は無限に延ばす。スケッチの線分は直線、領域はそのスケッチの平面）、平行でない線・面どうしは
     `between.angle_deg` が返る。測れない組（円い辺・曲面・ボディそのもの・線分以外のスケッチの図形まるごと）は
     `between_error` に理由が、`between_error_items` に測れない項目の添字が入り、各項目の寸法はそのまま読める
   - `sketch_create_offset(distance, base_plane=?)` は `sketch_create` の兄弟。`base_plane` 省略時は選択の面（ちょうど1枚）を基準に
     し、その法線方向へ `distance`（mm・正の向きが法線）離れた平面にスケッチを貼る。基準の基準（offsetのoffset）は作れない。
     `distance` は絶対値 1e6 mm 以下（置いた値。実測では 1e6 は通り、1e12 で図形の追加が失敗し、1e100 は通るので境目は一定でない。
     超えると距離と上限を示して断る。再生は断らない）
   - 図形の追加で断られるもの: 隠したスケッチへの `add_point` / `add_line` / `add_circle` / `add_ellipse` / `add_rectangle` / `add_arc`・`import_svg`・
     `project_to_sketch`・`project_outline`（先に `set_sketch_visible(sketch_id, true)`。redo・再生は通る）、掃引が 0 より大きく 1e-6 度未満の円弧
     （置いた値。実測では 1e-8 度の掃引が全周の円になる。0→360 は全周。SVG の同じ弧は取り込まずに捨てる）
   - `mirror_sketch_entities` は、軸の上に乗って鏡映できない図形を外して残りを鏡映し、外した数を応答の `warning` で返す（全部が軸上なら断る。
     ログの `targets` には残した図形だけが載る）
   - どちらも戻り値に**スケッチの枠 `frame`**（`origin` / `u` / `v` / `normal`）を返し、図形の座標 (u, v) はこの枠で測る
     （3D位置 = `origin + u*frame.u + v*frame.v`）。基準平面なら世界座標のままだが、**面を基準にすると原点も軸の向きも面ごとに違う**
     （面の中心とは限らない。世界の点 P は `u = (P - origin)·frame.u`、`v = (P - origin)·frame.v`）。世界座標のつもりで描くと、
     エラーにならずに形が別の場所にできる（[Issue #125](https://github.com/morikawa976321/duocad/issues/125)）
3. `fillet(radius=…)` / `extrude(distance=…)` など —— **辺や領域は引数に書かない**
4. `import_step(path)` は**読んだ時点で編集できるボディ**（ソリッドごとに1つ、`kind="solid"`）を返す（`body_ids`。追加の1手は要らない）。
   **読み込みのツールはこれ1つだけ**
5. `import_svg(sketch_id, path)` はSVGの図形を**いま開いているスケッチへ**足す（新しいスケッチは作らない —— 無ければ先に
   `sketch_create`）。戻り値は足した `entity_ids`・`region_count`・`size_mm`（幅・高さ mm）・`skipped`（落とした文字・画像・非表示の図形の数の文。無ければ空）。単位は mm（in・pt・pc は換算、物理単位が無ければ 1px=1mm）。**領域は1枚のSVGに複数あるのが普通**（文字の内側の穴・離れた
   図形）なので、押し出す前に `describe_document(include_regions=True)` で面積を見て選ぶ
6. 取り消すのは `undo`（UIと同じ履歴）。**戻せるのは形を変えた操作だけ**で、可視性の切り替え・選択の変更・起動中のコマンドは履歴に載っていない
7. 人と一緒に1つの verb を進めるなら `begin_command(verb)` → `set_selection` で止める（半径と確定は人に残す）。人が起動した verb は
   `get_command` に見えるので、引数を `set_command_args` で足し、確定は `apply_command`。`no_command` は起動中のコマンドが無いときの失敗

## AIはどこまで自分でやるか

**選択だけで止めるか verb まで進めるかは、人からの指示の文面から AI がその都度判断する**（本人の決定）。固定の規則（「曖昧なら止める」
等）は置かない。選択だけで止めたときは**何を選んだかを述べる**。人は画面でその選択を見て、そのまま採用するか、AIに直しを頼むか、
自分でトグルして直す。起動中の verb と途中の引数も同じ共有物（`begin_command` / `set_command_args` / `apply_command`。issue #153）で、
AI が起動して対象まで選び how と確定を人に残す、人が起動したものを AI が埋める、のどちらの向きもできる。確定まで進めるかも指示の文面から判断する。AIへ渡す文（MCPの `instructions` とアプリ内AIパネルのシステムプロンプト）は `catalog.agent_instructions()` の
1本で、この方針もそこに入っている。

## 起動

**先に自分でduocadを立て、MCPクライアントは後から繋ぐ。** 人間は同じプロセスが配信する `http://127.0.0.1:8766/` をブラウザで開く。
duocadのサーバを2つ同時に立てない（後から立った方がWebSocketのbindで落ちる）。

```
.venv\Scripts\python.exe -m duocad.mcp_server [オプション]
```

| オプション | 既定 | 意味 |
|---|---|---|
| `--port` | 8765 | WebSocket |
| `--http-port` | 8766 | 静的配信（ブラウザで開く）＋ `/mcp`（MCPのHTTPトランスポート） |
| `--deviation` | 0.2 | **画面の**テセレーションの線形偏差（mm・絶対。空の文書で、ズーム連動値が来るまでの土台。ボディのある文書は、来るまでの粗さを文書の外接対角から決める（`resolution.document_resolution`。[docs/VIEWPORT.md](docs/VIEWPORT.md)「可変解像度と面カリング」）。STL書き出しは見ない＝`io_step.STL_DEVIATION` 固定） |
| `--web-dist PATH` | `web/dist` | 静的配信するディレクトリ |
| `--open` | — | 起動時にブラウザを開く（既定は開かない） |

`web/dist` が無い場合は**静的配信を諦めてstdioのMCPだけで動く**ので `/mcp` も一緒に立たない。先に `cd web && npm install && npm run build`。
**起動時に読むSTEPを決める引数は無い** —— サーバは常に空の文書で立ち、STEPは `import_step` で読む。

## トランスポートは2本

`handle()` はトランスポートを知らない（1メッセージの辞書を受け取って返すだけ）ので、同じツール集合がどちらの口からも同じ顔で出る。
契約は `tests/test_mcp_http.py` が固定する。

| | 誰が使う | プロセスの所有者 |
|---|---|---|
| **HTTP** `http://127.0.0.1:8766/mcp` | Claude Code | **duocad自身**（クライアントは後から繋ぐ） |
| **stdio** | Claude Desktop など、stdioしか話せないクライアント | MCPクライアント |

`/mcp` は `Origin` を持つリクエスト（＝ブラウザから来たもの）と、知らない `Host` を403で落とす。**認証トークンは置いていない** ——
このPCでコードを実行できる相手はトークンを書いたファイルも読めるので、守れる相手が増えない（`WebHandler._mcp_forbidden` のdocstringが正）。
サーバ→クライアントのSSEストリームは提供しない（pushするものが無い）ので `GET /mcp` は405。

## Claude Code への登録

**リポジトリ直下の `.mcp.json` が登録そのもの**（projectスコープ）。duocadのディレクトリでClaude Codeを起動すれば繋がる ——
起動時に**cwdの `.mcp.json` だけ**を読む（親は遡らない）。初回だけ承認を訊かれ、結果は `~/.claude.json` の `enabledMcpjsonServers`
に残る（`claude mcp reset-project-choices` でやり直せる）。

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

- 古いstdio登録が local スコープに残っていると `.mcp.json` は黙って覆い隠される（優先順位は **local > project > user** で
  エントリ全体が入れ替わる）。消すのは `claude mcp remove duocad -s local`。
- 繋がったかは `claude mcp list`（接続まで見る）、セッション内では `/mcp`。**duocadが立っていなければ繋がらないだけ**でClaude Code自体は
  起動する。HTTPのサーバは自動で繋ぎ直す（初回失敗で最大3回、切断で最大5回・指数バックオフ）ので、duocadを立て直してもClaude Code側の
  操作は要らない。起動時の待ちは既定30秒（`MCP_TIMEOUT`）。
- duocad以外のディレクトリからも使う日だけ userスコープへ手で登録する
  （`claude mcp add --scope user --transport http duocad http://127.0.0.1:8766/mcp`）。無関係なプロジェクトにも出るので既定にしない。

## Claude Desktop への登録

Claude Desktopはstdioで繋ぐので、**あちらがduocadのプロセスを所有する**（人間用UIの寿命もClaude Desktopのセッションに縛られ、後付けの
接続はできない）。`%APPDATA%\Claude\claude_desktop_config.json` に書き、**Claude Desktop を再起動する**（設定は起動時にしか読まれない）。

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

## 実装のメモ

- **依存を増やしていない**。公式SDK（`mcp` パッケージ）は入れず、JSON-RPC 2.0 を素で実装している（`duocad/mcp_server.py`）。使うのは
  `initialize` / `tools/list` / `tools/call` の3メソッドだけ。対応プロトコル版は `2025-06-18` / `2025-03-26` / `2024-11-05`。
- **stdoutに出してよいのはJSON-RPCの行だけ**。起動メッセージ・警告はすべてstderrへ出す。OCCTのSTEP書き出しがC++からstdoutへ統計を吐く
  ため、stdioの待ち受けを始める時点で fd 1 を stderr へ振り替える（ツール実行中だけ囲む形では、人がブラウザから書き出す経路を取り逃す。
  `_claim_stdout`）。
- **AIへ渡す手順の文（`initialize` の `instructions`）は `catalog.agent_instructions()` の1本**で、アプリ内AIエージェント
  （`duocad/agent/`・docs/DEVELOPMENT.md §4）と共有する。ツール数も宣言から数えるので、verbを足しても古くならない。
- **verb の成功封筒**は `verb`・`seq`・`changed_bodies`・`bodies`・`can_undo`・`can_redo` に、入口が返したもの（作ったものの ID・解決後の数値など）を足す。
  `changed_bodies` は形の版の前後差で導く（粗さだけが変わったボディは含めない）。次のキーは該当するときだけ付く:
  - `warning` —— 断らずに確定した注意（離れたツールの結合で離れた塊が入った・切り取りで1ボディに離れた塊が入った・鏡映で図形を外した・
    推奨の外のねじの隙間）。画面では警告のトーストになる。再生・やり直し・プレビューでは出ない。
  - `discarded_redo` —— 取り消したあとの新しい操作で捨てたやり直しの件数（適用の前後のやり直せる件数の差）。
  - `dropped_args` —— 作業空間で構えたコマンドの引数のうち、形が変わったボディの面・辺・頂点を指していたため外した引数名。`undo` / `redo` の封筒には載らない。

  引数・戻り値の追加: `boolean_body` に `keep_tool`（既定 false。true でツールのボディを文書に残し、応答の `removed_bodies` は空。画面の選択と同じ。
  ログには真のときだけ積み、引数の無い保存済みの文書の再生はツールを消す）。`thread` の応答に `diameter`（面の呼び径＝半径の2倍を小数3桁に丸めた値）と `pitch`。
  `export_step` / `export_stl` の戻り値に `overwrote`（書く前に同名のファイルがあったか）。`fillet` の半径と `chamfer` の距離は 1e-6 mm 以上
  （新しい操作の入口だけ。再生は断らない）。
- **辺の述語 `parallel_to` の許容は、単位方向の内積が 1 から 1e-6 以内（傾きで約0.08度）。** 根拠の実測（回転した箱を STEP に書き、`CARTESIAN_POINT` と
  `DIRECTION` を小数2〜8桁に丸めて読み直した、平行な辺の 1−内積 の最大）: 小数2桁 1.4e-5、3桁 2.1e-7、4桁 6e-10。傾きが 1度の辺は 1.5e-4、0.1度は 1.5e-6。
  3桁の最大の約5倍に置いた（置いた値）。丸めの粗い STEP の平行な辺は外れうる。
- ツールの実行時エラーは JSON-RPC エラーではなく `isError: true` の構造化封筒
  （`not_found` / `invalid_argument` / `selection_empty` /
  `selection_kind_mismatch` / `invalid_predicate` / `busy` / `document_changed` / `no_command` / `internal` / `dimension_not_given`）。
  `dimension_not_given` は**アプリ内AIの経路だけ**の語（`duocad/agent/loop.py` が、人が量を言っていないのにモデルが埋めた寸法を実行前に
  止めるときに返す。MCP にはこのガードが無い）。`busy` は形状最適化の実行中に文書を変えるツールを呼んだとき（選択・表示の切り替え・
  書き出しは通る）。`selection_empty` は選択が**丸ごと空**のとき、`selection_kind_mismatch` は**空ではないが要求した種別が無い**とき
  （例: フィレットを呼んだが面3枚が選ばれている）で、実際に選ばれている種別と件数を `present_kinds` で返す。
- **人は画面で文書を切り替えられ、AIには知らせない**（issue #102）。`describe_document` は今のファイル名 `current_name` と世代
  `generation`（開く・新規・閉じるで進む）を返す。MCP は最後に `describe_document` で見た世代を覚え、違っていれば
  `describe_document` 以外のツールを `document_changed` で止める（解くのは `describe_document` だけ。まだ一度も見ていなければ照合しない）。
  **覚える場所はトランスポートごとに1つ**で、HTTP に2クライアントを繋ぐと片方の確認でもう片方も通る（`Mcp-Session-Id` 未実装。
  2本目を繋ぐときに要る）。アプリ内AIは別の仕組みで、実行中の切り替えで中断する（`docs/AI_INTEGRATION.md`）。
- 契約は `tests/test_mcp.py` / `tests/test_mcp_verbs.py` / `tests/test_mcp_selection.py`。
