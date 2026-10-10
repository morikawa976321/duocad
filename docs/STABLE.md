# 安定版と開発版

同じPCで開発(develop)と実制作の両方をする。実制作が開発の途中の状態(書きかけのコード・
ビルド・依存の入れ替え・無人タスク)に巻き込まれないよう、**実制作は別cloneの安定版で行う**。

## 配置

| | 安定版(制作用) | 開発版 |
|---|---|---|
| コード | `%USERPROFILE%\mf\duocad-stable`(`main` のclone) | `mf\duocad`(develop) |
| 実行環境 | 専用の `.venv` / `node_modules` / `web/dist` | 開発用のもの |
| 制作データ | `%USERPROFILE%\mf\duocad-data\{models,export,logs}` | リポジトリ直下の `models/` `export/` |
| ポート(WS / HTTP) | **8775 / 8776** | 8765 / 8766(既定) |
| 画面 | Edgeのアプリウィンドウ。ヘッダーに更新ボタン(更新があるときだけ) | ヘッダー左端に「開発版」の札、窓のタイトルに `[開発版]` |
| MCP(Claude Code) | 安定版のフォルダで開いたセッション → 8776(local スコープ登録) | `.mcp.json` → 8766 |
| 起動 | Win → `duocad` → Enter | 従来どおり(README 1章) |

**開発版が既定のポートを持ち、安定版がずれる**のは、開発側が既定のポートで立て・繋ぐ習慣を変えずに済ませるため。追跡している `.mcp.json`(8766)は両方のcloneに
入るが、安定版のフォルダでは local スコープの登録(8776)が優先される(local > project > user)。
**登録が消えると、安定版のフォルダのセッションは 8766 へ繋ぐ**(開発版が立っていればそれを操作してしまう)ので、
`install.ps1` は登録の後に `claude mcp get duocad` で 8776 を指すことを確かめ、違えば止まる。
逆向き(開発版のセッションが安定版を操作する)は起きない。

安定版のフォルダには `install.ps1` が `CLAUDE.local.md`(未追跡)を置き、そこで開いたセッションに
「コードを触らない・MCPで操作する・CLAUDE.md の開発の規律は適用しない」と伝える。CLAUDE.md の
「安定版に触らない」は開発用チェックアウトのエージェント向けで、clone ごと安定版にも入るため。

## 導入(1回だけ)

develop の変更が main に入ってから、開発用のチェックアウトで:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\stable\install.ps1 -ImportModelsFrom models
```

**pushもPRもせずに今のdevelopから始めるとき**は `-Commit (git rev-parse HEAD)` を足す。開発用のチェックアウトから
そのコミットを取り込み、安定版のローカルの `main` をそこに置く(追跡先は `origin/main` のまま)。mainより先にいる間は
「取り込むものが無い」扱いで、次に develop → main がマージされた時点(マージコミットでそのコミットがmainの祖先になる)から
更新ボタンが出る。**mainへのマージをスカッシュにすると祖先にならず、「更新できない」のままになる**(その場合は導入し直す)。

clone・`uv sync --frozen`・`npm ci`・ビルド・データフォルダ作成・`models/*.json` の写し(上書きしない)・
スタートメニュー登録・MCPの local スコープ登録までを行う。やり直しても壊れない(cloneが既にあれば触らない)。

## 起動と終了

- **起動**: スタートメニューの「duocad」(`pythonw -m duocad.launcher`)。サーバが立っていなければ
  窓の無いプロセスで立て(初回は十数秒)、Edgeの `--app=` ウィンドウで開く。立っていれば窓を開くだけ。
- **窓を閉じてもサーバは残る**(MCPクライアントが窓と無関係に繋ぐため)。止めるときはタスクマネージャーで
  コマンドラインに `--channel stable` を含む python を止める。
- ログ: `duocad-data\logs\server.log`(5MBで1世代回す)。
- 同じポートで別のduocad(開発版)が動いていたら、ランチャーは開かずにその旨を出す。

## 更新

ヘッダーの「更新 N」ボタン(`origin/main` に新しいコミットがあるときだけ出る。ツールチップに一覧)。
押すと:

1. サーバが `tools/stable/update.ps1` を一時フォルダへ写して切り離して起動し、自分は終了する
   (**Windowsでは動いている `.venv` の python.exe を置き換えられない**ので、更新はサーバの外で行う)
2. スクリプトが `git merge --ff-only origin/main` → `uv sync --frozen`(設定で GPU を選んだか、GPU の部品が入っている clone では
   `--extra gpu` も。[TOPOPT_GPU.md](TOPOPT_GPU.md))→(`package-lock.json` が変わったときだけ)
   `npm ci` → `npm run build`
3. `duocad.launcher --no-window` で立て直す。画面は再接続を待って自動で読み直し、結果をトーストで出す

**失敗したら元のコミットへ戻して立て直す**(トーストに理由とログの場所。uv・npm が見つからない等でも、
スクリプトは最後に必ず立て直す)。実測(依存の変化なし): 取り込みからビルドまで4秒、巻き戻しを含めて7秒。

**Python の版を上げる更新**(例: 3.11 → 3.14)は、サーバ(uv の venv の python)が子へ渡す `PYTHONHOME` を
更新スクリプトが引き継ぐと `uv sync` が落ちる。`duocad/channel.py` と `update.ps1` が外しているが、**外す前の版の
サーバから押した更新では落ちる**(元の版へ戻る)。そのときは duocad から起動していない普通の PowerShell で `update.ps1` を手で走らせる
(`-ServerPid` を省くと `-HttpPort` からサーバを探して止める):

```
powershell -NoProfile -ExecutionPolicy Bypass -File $env:USERPROFILE\mf\duocad-stable\tools\stable\update.ps1 -RepoRoot $env:USERPROFILE\mf\duocad-stable -LogDir $env:USERPROFILE\mf\duocad-data\logs -DataDir $env:USERPROFILE\mf\duocad-data
```
元の版への復元にも失敗したときは、トーストがそう言う(`last-update-result.json` の `restored: false`)。

ボタンが出ない・押せない条件(`duocad/channel.py`):

- 安定版のcloneが `main` 以外の枝にいる / 未コミットの変更がある →「更新できない」の札(理由はツールチップ)
- `git fetch` に失敗した(オフライン等)→ 何も出ない
- 形状最適化・AIの実行が走っている / 自動保存が書けていない文書がある → 押すとトーストで断る
  (実行器で走る処理があるとサーバが終われず、60秒後の強制終了で結果を捨てるため)

更新元にタグは打たない。mainはPR経由でしか入らない動作確認済みの枝なので(CLAUDE.md)、そこでもう1段の関門を足さない。

## 巻き戻し(手動)

直前の更新の前のコミットは `duocad-stable\.git\duocad-previous` に残る。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File $env:USERPROFILE\mf\duocad-stable\tools\stable\update.ps1 -RepoRoot $env:USERPROFILE\mf\duocad-stable -LogDir $env:USERPROFILE\mf\duocad-data\logs -DataDir $env:USERPROFILE\mf\duocad-data -Rollback
```

動いているサーバは8776から探して**すぐ強制終了する**(止まるよう頼む口は無い。文書は自動保存済みなので、
失うのは走っている形状最適化・AIの途中だけ)。戻り先は**成功した更新のときだけ**書き換わる。巻き戻した後も main は先にあるので、更新ボタンは出続ける。

## 制作データの扱い

- 安定版のファイルを開発版で直接開かない。保存形式を開発版で変えると、開発版で書いたファイルを
  安定版で開けなくなる。開発版で再現したいときは `duocad-data\models` から写して使う。
- `duocad-data` はリポジトリの外なので `git clean` では消えない。バックアップの対象には自分で入れること。
