---
name: occt-step-writer-transfer-status-must-be-checked
description: 運用知見(2026-08-10) — OCCTのSTEP書き出しはWrite()だけでなくTransfer()もIFSelect_ReturnStatusを返す。Transferの戻り値を捨てるとWriteが成功する限り「転送に失敗したSTEP」を成功として通す。読み込み側(ReadFile)の戻り値検査と同じ規律を書き出し側にも適用する
created: 2026-08-10
updated: 2026-08-10
---

# OCCTのSTEP書き出しは Transfer() の戻り値も検査する

`STEPControl_Reader.ReadFile()` の戻り値を検査しないとプロセスごと落ちる件
（[[ocp-stepcontrol-reader-segfault-on-failed-readfile]]）は既知だが、**書き出し側にも
同じ構造の穴がある**。

```python
writer = STEPControl_Writer()
writer.Transfer(shape, STEPControl_StepModelType.STEPControl_AsIs)  # ← 戻り値がある
status = writer.Write(filename)                                      # ← ここだけ見がち
```

`Transfer()` も `Write()` と同じく **`IFSelect_ReturnStatus` を返す**。戻り値を捨てると、
**転送に失敗していても `Write()` が成功する限り成功として通る**。読み込み側と違って
プロセスは落ちないぶん、**壊れた（あるいは中身の無い）成果物が静かに残る**という
より発見しにくい壊れ方をする。

判定はどちらも `IFSelect_ReturnStatus.IFSelect_RetDone` との比較でよい。
`Transfer` が失敗したら `Write()` に進まないこと（ディスクに触らせない）。
`Write` が失敗したときは書きかけのファイルを消す — 0バイトの成果物が「成功」として
残るのを防ぐため。

## 一般化

OCCTは「例外を投げずに黙って失敗する」設計で統一されている（フィレットの `IsDone()=False`、
`ReadFile()` の `IFSelect_RetFail` など）。**戻り値やステータスを返すOCCTの呼び出しは、
一つ残らず検査する**を規律にするのが正しい。個別に「ここは要る／要らない」を判断すると、
今回のように読み込み側だけ固めて書き出し側が抜ける。

関連: [[ocp-stepcontrol-reader-segfault-on-failed-readfile]] / [[occt-stl-relative-deflection-ignores-deviation]]
