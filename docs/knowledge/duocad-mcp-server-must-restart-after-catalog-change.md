---
name: duocad-mcp-server-must-restart-after-catalog-change
description: 運用知見(2026-09-03/04実測) — duocadのcatalog.py/ws.pyを変えたら動いているduocad.mcp_serverプロセスの再起動まで含めて1手(コマンド表はモジュール読み込み時に1回だけ組む)。ただしバックエンドは複数フロントエンドを受ける長寿命プロセスで他セッションの未保存文書が乗っていることがあるため、確認なしに再起動しない
created: 2026-09-03
updated: 2026-09-04
---

# duocad: catalog.py変更はmcp_server再起動まで含めて1手 — ただし共有中の再起動は他者の未保存作業を消しうる

## 再起動しないと新verbは無効のまま(2026-09-03実測)

実UIで `delete` verbを使ったら `ValueError: unknown command: 'delete'` が出た。`duocad/catalog.py` の宣言・`duocad/net/ws.py` の配線・UIコマンドIDの3層は正しく揃っており(`tests/test_ui_command_wiring.py` 12/12 pass)、ソースは壊れていなかった。

原因はプロセス側: `duocad.mcp_server --open` が前日13:40起動のまま生きていた。`delete` を追加したコミット(`c899f7b`)より前に起動したプロセスなので、コマンド表(`ws.py` がモジュール読み込み時に `catalog.ws_table(...)` から1回だけ組み立てる `FEATURE_COMMANDS` 等)が**古いcatalog.pyのまま**メモリに残っていた。

`duocad.mcp_server` はMCP(stdio)とWebSocket(人のUI)を**同一プロセスが両方サーブする**ため、AIから触っていなくても人がUIで新verbを使う瞬間に同じ理由で失敗する。フロントはvite dev serverでHMRが効くので気づきにくいが、**バックエンドはPythonプロセスの生存期間＝コマンド表の鮮度**であり、催促なしに追従しない。

同じ機序(Pythonは起動時にimportしたモジュールを保持する)の別の出方が [[headless-shot-photographs-a-stale-server-when-the-port-is-taken]](自動検査スクリプトが古いサーバを撮って偽の緑を出す)。

## 再起動そのもののリスク(2026-09-04実測)

バックエンドは複数フロントエンド接続を受ける長寿命プロセスで、**文書は接続より長生きする**(readme.mdの既知事項) — 再起動すると、その時点で未保存のまま開かれていた文書の編集内容が、どのクライアント/セッションのものであっても消える。

実例: `move_body` をpreviewable化する変更(`catalog.py`)を反映するには再起動が要ったが、そのプロセスには**別の対話セッションが開いていたと見られる `mounting-plate.json` の未保存編集**(画面上「未保存」表示)が乗っていた。再起動すればその編集は失われる可能性があったため、**再起動せずpytestでの検証のみに留めた**(フロントエンドのみの変更は、Viteのdevサーバーを別途起動して安全に検証できた — `strictPort:true` で、ポートが空いていれば同じ生きたバックエンドへ接続し直せる)。

**手順**: duocadのバックエンドを触るとき(特にAI/無人セッション)は、他に接続しているセッションがいないか・その文書が未保存でないかを先に確認する。確認できない状況では、pytest等の静的検証で済ませ、ライブな実機確認は再起動が安全と分かるまで見送る。
