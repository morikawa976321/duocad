# 安定版と開発版

同じPCで開発(develop)と実制作の両方をする。実制作が開発の途中の状態(書きかけのコード・
ビルド・依存の入れ替え・無人タスク)に巻き込まれないよう、**実制作は別cloneの安定版で行う**。

## 配置

| | 安定版(制作用) | 開発版 |
|---|---|---|
| コード | `%USERPROFILE%\mf\duocad-stable`(`main` のclone) | `mf\duocad`(develop) |
| 実行環境 | 専用の `.venv` / `node_modules` / `web/dist` | 開発用のもの |
| 制作データ | `%USERPROFILE%\mf\duocad-stable\data\{models,export,logs}`(git の外) | リポジトリ直下の `models/` `export/` |
| ポート(WS / HTTP) | **8775 / 8776** | 8765 / 8766(既定) |
| MCP(Claude Code) | 安定版のフォルダで開いたセッション → 8776(local スコープ登録) | `.mcp.json` → 8766 |

使う側の見え方(起動・画面・札・アドレス)は [MANUAL.md](MANUAL.md) の表。

**開発版が既定のポートを持ち、安定版がずれる**のは、開発側が既定のポートで立て・繋ぐ習慣を変えずに済ませるため。追跡している `.mcp.json`(8766)は両方のcloneに
入るが、安定版のフォルダでは local スコープの登録(8776)が優先される(local > project > user)。
**登録が消えると、安定版のフォルダのセッションは 8766 へ繋ぐ**(開発版が立っていればそれを操作してしまう)ので、
`install.ps1` は登録の後に `claude mcp get duocad` で 8776 を指すことを確かめ、違えば止まる。
逆向き(開発版のセッションが安定版を操作する)は起きない。

安定版のフォルダには `install.ps1` が `CLAUDE.local.md`(未追跡)を置き、そこで開いたセッションに
「コードを触らない・MCPで操作する・CLAUDE.md の開発の規律は適用しない」と伝える。CLAUDE.md の
「安定版は読むだけ」は開発用チェックアウトのエージェント向けで、clone ごと安定版にも入るため。

## 導入(1回だけ)

develop の変更が main に入ってから、開発用のチェックアウトで:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\stable\install.ps1 -ImportModelsFrom models
```

**pushもPRもせずに今のdevelopから始めるとき**は `-Commit (git rev-parse HEAD)` を足す。開発用のチェックアウトから
そのコミットを取り込み、安定版のローカルの `main` をそこに置く(追跡先は `origin/main` のまま)。mainより先にいる間は
「取り込むものが無い」扱いで、次に develop → main がマージされた時点(マージコミットでそのコミットがmainの祖先になる)から
更新ボタンが出る。**mainへのマージをスカッシュにすると祖先にならず、「更新できない」のままになる**(その場合は導入し直す。**先に `data\` を退避する** ——「制作データの扱い」)。

clone・`uv sync --frozen`・`npm ci`・ビルド・データフォルダ作成・`models/*.json` の写し(上書きしない)・
スタートメニュー登録・MCPの local スコープ登録までを行う。やり直しても壊れない(cloneが既にあれば触らない)。

## 起動と終了

- **起動**: スタートメニューの「duocad」(`pythonw -m duocad.launcher`)。サーバが立っていなければ
  窓の無いプロセスで立て(初回は十数秒)、Edgeの `--app=` ウィンドウで開く。立っていれば窓を開くだけ。
- **窓を閉じてもサーバは残る**(MCPクライアントが窓と無関係に繋ぐため)。止め方は [MANUAL.md](MANUAL.md)「サーバを止める」。
- ログ: `data\logs\server.log`(5MBで1世代回す)。
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

元の版への復元にも失敗したときは、トーストがそう言う(`last-update-result.json` の `restored: false`)。

ボタンが出ない・押せない条件(`duocad/channel.py`):

- 安定版のcloneが `main` 以外の枝にいる / 未コミットの変更がある →「更新できない」の札(理由はツールチップ)
- `git fetch` に失敗した(オフライン等)→ 何も出ない
- 形状最適化・AIの実行が走っている / 自動保存が書けていない文書がある → 押すとトーストで断る
  (実行器で走る処理があるとサーバが終われず、60秒後の強制終了で結果を捨てるため)

更新元にタグは打たない。mainはPR経由でしか入らない動作確認済みの枝なので(CLAUDE.md)、そこでもう1段の関門を足さない。

## 安定版で出さない機能

**形状最適化は安定版に出さない**(凍結。本人決定 2026-10-13、[TOPOPT_CONVERGENCE.md](TOPOPT_CONVERGENCE.md) 9節)。開発版では今までどおり使える。
コードは main にも入ったままで、起動の `--channel stable` で隠す(`channel.HIDDEN_IN_STABLE`):

- AIの道具(MCP とアプリ内AI)から、条件を置く5つ(`set_preserve_bodies` / `set_exclude_bodies` / `set_fixtures` / `set_loads` /
  `clear_loads`)を外す。説明文の道具の数も外した後の数になる。
- WebSocket の形状最適化の口(上の5つと、条件の点検・実行・中断・仕上げ・ステップの7つ)を断る。どれが隠れるかは `catalog.Command.feature`。
- 画面は、接続のたびにサーバが送る `optimize_status` の `available` を読み、「形状最適化」ボタンと設定の GPU の欄を出さず、
  ステップの取り直しも頼まない。口を断るのと同じサーバが答えるので、`/duocad-config.json` の取得に失敗しても食い違わない。
- 最適化の条件や結果のボディが入った文書も、そのまま開いて使える(条件は文書に残り、見えないだけ)。

凍結を解くときは `HIDDEN_IN_STABLE` から外し、`tests/topopt/` を既定の pytest へ戻す(`tests/conftest.py` の `FROZEN_TOPOPT`)。
凍結中は既定の実行で回していないので、戻す前に `pytest --topopt` を回し、共有コードの変更で壊れた分を直す。

**アプリ内AIは Claude API だけにする**(本人指示 2026-10-14)。ローカルAI(llama-server、`openai_compat`)は機能 `local_ai` として
同じ `HIDDEN_IN_STABLE` で隠す:

- `ai_status.providers` が `["anthropic"]` だけになり、APIキーが無くても Claude から始まる(画面は「APIキーが未設定」と言う)。
- `ai_configure` で `openai_compat` を選ぶと断る。
- 選ぶものが無いので、AIパネルの「設定」(プロバイダとエンドポイントの欄)を出さない。キーはヘッダーの「設定」から置く。
- MCP(Claude Code から操作する口)は関係しない —— 隠すのはアプリ内AIの相手だけ。

## 巻き戻し(手動)

直前の更新の前のコミットは `duocad-stable\.git\duocad-previous` に残る。実行するコマンドは
[MANUAL.md](MANUAL.md)「前の版に戻す」（`update.ps1 -Rollback`）。動いているサーバは8776から探して**すぐ強制終了する**(止まるよう頼む口は無い。文書は自動保存済みなので、
失うのは走っている形状最適化・AIの途中だけ)。戻り先は**成功した更新のときだけ**書き換わる。巻き戻した後も main は先にあるので、更新ボタンは出続ける。

## 制作データの扱い

- 安定版のファイルを開発版で直接開かない。保存形式を開発版で変えると、開発版で書いたファイルを
  安定版で開けなくなる。開発版で再現したいときは `duocad-stable\data\models` から写して使う。
- 制作データは安定版のcloneの中の `data\` に置き、`.gitignore` の `/data/` で git から外す。
  更新が走らせる `git merge --ff-only`・`git reset --hard`(巻き戻しと失敗時の復元)は追跡外のファイルに触れないので、
  退避しなくても残る。ただし **git は無視されたファイルを黙って上書きする**ので、取り込む先のコミットが `data/` の下か `data` という
  ファイルを(大文字小文字を問わず)追跡していたら `update.ps1` が取り込む前に止まる(`git add -f` で入れない限り起きない)。
- **安定版のフォルダで `git clean -x`(`-X`)を走らせない。** 無視されたファイルとして `data\` を消す
  (`-x` の無い `git clean -fd` は無視されたファイルを残す)。
- **安定版のフォルダを消して導入し直すときは、先に `data\` を clone の外へ退避し、導入後に戻す。** 消すと制作データも消える。
- バックアップの対象には自分で入れること(git は持たない)。
