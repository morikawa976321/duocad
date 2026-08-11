# duocad の MCP サーバ — AIから文書を触る

**AIが「前面パネルを隠して」と言えるようにするための口**（M4の最小の先取り・2026-08-11）。
見えない面は選べない以上、**可視性は選択機構の一部**であり、大規模モデル（PCケース27部品・
2835面）で交互操作を成立させる前提になる。

公開しているツールは3つだけ。選択の提案・修正（`propose_selection` / `modify_selection` /
`apply`）はM4の範囲で、**ここには無い**。

| ツール | 何をするか |
|---|---|
| `describe_document` | ボディ一覧（`body_id`・名前・種別・可視性・面数・体積）とパラメータ一覧。**最初にこれを呼ぶ** |
| `set_visible` | `set_visible(body_id, visible)`。コアの verb を叩くだけ（操作ログに載る・undoできる） |
| `describe_shape` | 1ボディの面数・辺数・面の種別内訳・体積・境界箱（読み取りのみ） |

`describe_document` を必ず一緒に出しているのは、**`set_visible` だけを公開してもAIは
どのBodyが存在するか知らないので使えない**ため。

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
- したがって `set_visible` は**アプリ層を一切通らずに**文書を変える
  （実装計画書§2「コアはUIが無くても機能的に完全」の実証）。
- **AIが可視性を変えると、開いているブラウザの表示が自分では何もしていないのに変わる**。
  これが「コアが変更イベントを発火し、アプリ層が購読して描き直す」上り方向で、
  実体は `Session.add_listener`（`duocad/server.py`）。これが無いと交互操作は原理的に
  成立しない（§2「外してはいけない3つ」の2番目）。
- **1プロセスで両方を持つ**（stdioのMCPとWebSocket＋静的配信）。別プロセスにすると
  MCP側が自分の `Document` を持ってしまい、「AIが変えた文書」と「人が見ている文書」が
  別物になる。

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

1. `describe_document` を呼ぶ → `body_id` の一覧を得る
2. どの部品が手前かを知りたければ `describe_document(include_bbox=true)`、
   1つの形を詳しく見たければ `describe_shape(body_id)`
3. `set_visible(body_id, false)` で隠す → **人間の画面からも消える**
4. 元に戻すのは `set_visible(body_id, true)`、またはブラウザ側の Ctrl+Z（undo）

読み込んだSTEPはソリッドごとに「参照ボディ（読み取り専用・隠されている）」と
「編集用コピー」の対になる。`describe_document` が既定で返すのは**編集用だけ**で、
参照ボディの件数は `reference_count` で知らせる（`include_references=true` で出せる）。
両方見せると同じ部品が2つ並び、隠しても画面が変わらない方をAIが掴むため。

## 実装のメモ

- **依存を増やしていない**。公式SDK（`mcp` パッケージ）は入れず、JSON-RPC 2.0 over stdio を
  素で実装している（`duocad/mcp_server.py`）。使うのは `initialize` / `tools/list` /
  `tools/call` の3メソッドだけで、SDKは pydantic・anyio・httpx・starlette・uvicorn を
  連れてくる。pyproject が依存を4つに固定しているローカル専用ツールには重すぎる。
  対応プロトコル版は `2025-06-18` / `2025-03-26` / `2024-11-05`。
- **stdoutに出してよいのはJSON-RPCの行だけ**。起動メッセージ・警告はすべてstderrへ出す
  （stdoutを1行汚すと接続に失敗する）。
- ツールの実行時エラーは JSON-RPC エラーではなく `isError: true` の結果として返す
  （AIが理由を読んでやり直せるようにするため）。引数の型・必須・未知キーは
  実行前に弾く（`visible` に文字列 `"false"` が来ると Python では真になり、
  **黙って逆の操作をする**ため）。
- 契約は `tests/test_mcp.py`、実ブラウザでの確認は `web/scripts/smoke-mcp.mjs`。
