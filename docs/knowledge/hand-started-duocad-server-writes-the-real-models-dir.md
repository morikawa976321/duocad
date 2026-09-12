---
name: hand-started-duocad-server-writes-the-real-models-dir
description: 落とし穴(2026-09-05・実際に踏んだ) — duocadのサーバを手で立てて画面を触ると、リポジトリ直下の実models/を書き換える。検査スクリプトは--models-dirに使い捨てを渡すが、手起動では既定のまま
created: 2026-09-05
updated: 2026-09-05
---

duocadの画面を目で見るために `python -m duocad.mcp_server` を**手で立てると、保存先は
リポジトリ直下の実 `models/`** になる(`duocad/server.py` の `DEFAULT_MODELS_DIR`)。
実機検査スクリプトは `startServer({modelsDir})` で必ず使い捨てのtempを渡しており
(`web/scripts/server.mjs` に「**実ブラウザ検査は必ず渡すこと**」と明記されている)、
**手で立てるときだけこの防御が外れる**。

実際に踏んだ形: UIの見た目を確かめるために手でサーバを立て、ブラウザから
`mounting-plate.json` を開いてテスト用のSTEP(4.6MB)を読み込んだ。文書は接続より長生きするので、
その状態が残ったまま保存経路を通り、**本人の作業ファイルが161KB→4.9MBになった**
(本人の操作2件はそのまま残り、テスト用の `import_step` が1件追記された形)。
`git diff` で気づくまでコミット対象に紛れていた。

**手で立てるときも `--models-dir` に使い捨てを渡す**のが正しい:

```powershell
.\.venv\Scripts\python.exe -m duocad.mcp_server --models-dir $env:TEMP\duocad-scratch
```

**stdinを閉じない**ことも併せて要る —— stdioでMCPを喋るのでEOFで即終了する
(`tail -f /dev/null | ...` などで開いたままにする)。

もう1つの側面: **8765を掴んだサーバは複数の接続を相手にする**。手で立てたものを消し忘れると、
別セッションや本人のブラウザがそこへ繋いで作業を続けうる。`models/` の差分を見て
「自分が入れた覚えのない操作」が混ざっていたら、それは並行して誰かが触っている印なので、
`git checkout` を向けてはいけない([[parallel-session-git-add-all-race]] と同じ性質の事故)。

関連: [[duocad-viewport-glitch-was-multiple-clients]]
