---
name: run-coroutine-threadsafe-self-deadlock
description: 落とし穴(2026-08-20) — asyncio.run_coroutine_threadsafe(...).result() を、対象イベントループ自身のスレッドから呼ぶと必ず固まる。テストが実行器スレッドを挟まず直接呼ぶ経路で顕在化した
created: 2026-08-20
updated: 2026-08-20
---

# `run_coroutine_threadsafe(...).result()` はイベントループ自身のスレッドから呼ぶと固まる

2026-08-20、duocad(タスクH・同時編集の安全性)で遭遇。`.result()` はコルーチンの完了を
**同期的に待つ**が、待っている間もイベントループ自身は「完了を待たれている」側であって
「進める」側ではない。呼び出しスレッド＝ループのスレッドだと、ループは自分を待っている
コードに制御を返してもらえないと1ステップも進めない——**自分で自分を待つ**古典的な
デッドロックになる。

**具体例**: duocadの `_AiToolsOnLoop`(`duocad/server.py`)は「文書に触るのはイベントループの
スレッドだけ」という不変条件を守るため、実行器スレッドから来た呼び出しを
`asyncio.run_coroutine_threadsafe(_once(), self._loop).result()` でループへ投げ返す。
本番経路(`Server._ai_start`)は `AgentLoop.run()` を必ず `run_in_executor` で実行器スレッドへ
逃がすので問題は起きない。だが `tests/test_agent_ws.py` の1本(base_urlの深層防御を見る検査)は
`asyncio.run(scenario())` の**コルーチンの中から** `AgentLoop.run()` を直接(実行器を挟まず)呼んでいた
——このテストはツール呼び出しが0件の応答だけを使っていたため `.call()`(同じ`_run_on_loop`経由)が
一度も走らず、何年も気づかれずにいた。**楽観ロックの追加で毎ターン`current_seq()`(同じ経路)を
呼ぶようにした瞬間に初めて顕在化**し、`pytest` がCPU使用率ほぼ0のまま数分単位で止まった
(=典型的なデッドロックの見分け方: プロセスは生きているのにCPUが動かない)。

**直し方**: 呼び出し側で「いま自分がそのループのスレッド上にいるか」を
`asyncio.get_running_loop()`(無ければ`RuntimeError`→None)で確かめ、一致するなら
`run_coroutine_threadsafe` を使わず直接呼ぶ。一致しないときだけ本来の投げ返しをする。
これは`_run_on_loop`のようなラッパの**恒久的な安全策**として足してよい——本番経路の挙動は
変えず、テストのような「同じスレッドから直接呼ぶ」経路も安全にする。

**見分け方の一般形**: バックグラウンドプロセスが**進んでいるように見えて実は同じ地点で
止まっている**とき(出力が伸びない・CPUが動かない)、まず疑うべきは「遅い」ではなく
「イベントループ関連のデッドロック」。`Get-Process python | Select CPU` を2回、時間を空けて
取り、数値が動いていなければ確定に近い。

関連: [[llm-agent-thinking-runaway-on-contradictory-task]](こちらは思考の暴走で「遅い」、
本件はデッドロックで「止まる」——症状は似るが原因も直し方も別)
