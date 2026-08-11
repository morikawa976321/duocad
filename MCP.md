# duocad の MCP サーバ — AIから文書を触る

**AIが文書を操作するための口**（F / 2026-08-11）。ブラウザとMCPは同じ `Session` を叩く対等な
クライアントで、MCPが変えた内容は開いている画面へ即座に届く。

公開ツールは**28個**。面・辺・領域などの**形は引数で受けず、確定した選択から取る**
（数値・名前・パスだけが引数）。提案の選択はそのまま verb に渡せない — 先に
`commit_selection` が要る。

## 公開ツール（28）

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

**呼び方の型**（例）:

1. `describe_document` で `body_id` / スケッチ / パラメータ名を知る
2. `propose_selection`（`refs` か `predicate`）→ 必要なら `measure` で確認 → `commit_selection`
3. `fillet(radius=…)` / `extrude(distance=…)` など — **辺や領域は引数に書かない**
4. `import_step` のあとは戻り値の `hint` どおり `make_editable_copy` してから編集する

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
- **1プロセスで両方を持つ**（stdioのMCPとWebSocket＋静的配信）。別プロセスにすると
  「AIが変えた文書」と「人が見ている文書」が別物になる。

## 起動

MCPクライアントが**このプロセスを起動する**（自分で先に立てておく必要はない）。人間は
プロセスが配信する `http://127.0.0.1:8766/` をブラウザで開く。

```
.venv\Scripts\python.exe -m duocad.mcp_server [オプション]
```

| オプション | 既定 | 意味 |
|---|---|---|
| `--step PATH` | `PC_case_20251122.step` | 起動時に読むSTEP |
| `--no-step` | — | STEPを読まずに空で起動する |
| `--port` | 8765 | WebSocket |
| `--http-port` | 8766 | 静的配信（ブラウザで開くのはこちら） |
| `--deviation` | 0.2 | テセレーションの線形偏差（mm・絶対） |
| `--web-dist PATH` | `web/dist` | 静的配信するディレクトリ |
| `--open` | — | 起動時にブラウザを開く（既定は開かない） |

`web/dist` が無い場合は**静的配信だけを諦めてMCPは動く**（理由はstderrに出る）。先に
`cd web && npm install && npm run build` しておくと画面も出る。

**注意**: `python -m duocad.server`（M1からのWebSocketサーバ）と同時に起動すると
ポートがぶつかる。**MCP経由で使うときは `duocad.mcp_server` だけを起動する**
（こちらがWebSocketと静的配信を兼ねる）。

## Claude Code への登録

プロジェクト直下（`C:\Users\tm976\mf\duocad`）で：

```
claude mcp add duocad -- C:\Users\tm976\mf\duocad\.venv\Scripts\python.exe -m duocad.mcp_server
```

登録できたかは `claude mcp list`（接続まで見る）で確認する。セッション内では `/mcp` で
ツール一覧が出る。**パスは絶対で書く** — `claude` を起動したディレクトリが
`duocad` とは限らないため。STEPを変えたいときは末尾に `--step <path>` を足す。

設定ファイルを直接書く場合（`.mcp.json` / `~/.claude.json` の `mcpServers`）：

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

## Claude Desktop への登録

`%APPDATA%\Claude\claude_desktop_config.json` に上と同じ `mcpServers` の項目を書き、
**Claude Desktop を再起動する**（設定は起動時にしか読まれない）。

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
2. 手前の部品を隠すなら `set_visible(body_id, false)`（**人間の画面からも消える**）
3. 対象を `propose_selection` で選び、`commit_selection` してから fillet / extrude 等
4. STEPを足したら `import_step` → `make_editable_copy`（戻り値の `hint` を見る）
5. 取り消すのは `undo`（UIと同じ履歴）

読み込んだSTEPはソリッドごとに「参照ボディ（読み取り専用・隠されている）」と
「編集用コピー」の対になる。`describe_document` が既定で返すのは**編集用だけ**で、
参照ボディの件数は `reference_count` で知らせる（`include_references=true` で出せる）。

## 実装のメモ

- **依存を増やしていない**。公式SDK（`mcp` パッケージ）は入れず、JSON-RPC 2.0 over stdio を
  素で実装している（`duocad/mcp_server.py`）。使うのは `initialize` / `tools/list` /
  `tools/call` の3メソッドだけ。対応プロトコル版は `2025-06-18` / `2025-03-26` / `2024-11-05`。
- **stdoutに出してよいのはJSON-RPCの行だけ**。起動メッセージ・警告はすべてstderrへ出す。
  OCCTのSTEP書き出しがC++からstdoutへ統計を吐くため、ツール実行中は fd 1 を stderr へ振る。
- ツールの実行時エラーは JSON-RPC エラーではなく `isError: true` の構造化封筒
  （`not_found` / `invalid_argument` / `selection_not_committed` / `selection_empty` /
  `invalid_predicate` / `internal`）。
- 契約は `tests/test_mcp.py` / `tests/test_mcp_verbs.py` / `tests/test_mcp_selection.py`。
