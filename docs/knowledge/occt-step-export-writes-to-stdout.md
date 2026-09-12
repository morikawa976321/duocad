---
name: occt-step-export-writes-to-stdout
description: OCCTのSTEP書き出しはC++側からstdoutへ統計を吐く。stdio越しのプロトコル(MCP等)を同じプロセスで喋らせると接続が壊れ、Pythonのredirect_stdoutでは止まらない
created: 2026-08-12
updated: 2026-08-12
---

`kernel.write_step`（OCCTのSTEPライタ）を呼ぶと、標準出力へ次のような統計が出る（2026-08-12にduocadで実測）:

```
****        Statistics on Transfer (Write)                 ****
Step File Name : ...(662 ents)  Write  Done
```

**同じプロセスでstdio越しのプロトコルを喋らせていると、これで接続が壊れる。** MCPは1行1メッセージのJSON-RPCをstdoutでやり取りするので、JSON以外の行が1つでも混ざるとクライアント側がパースに失敗する。

**Pythonの `contextlib.redirect_stdout` では止まらない。** OCCTはC++から実ファイルディスクリプタへ直接書くため、`sys.stdout` を差し替えても素通りする。止めるには `os.dup2` でfd 1をfd 2（stderr）へ振り替える必要がある。

**同一プロセス内のテストでは検出できない。** 汚れるのはテストランナーのstdoutであってプロトコルの経路ではないので、実プロセスを起こして「stdoutの全行がJSON-RPCとしてパースできる」ことを見るしかない。

読み込み（`read_step`）は静かで、書き出しだけが喋る。STLの書き出しも同じ疑いで扱うこと。
