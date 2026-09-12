---
name: ocp-stepcontrol-reader-segfault-on-failed-readfile
description: 事実(2026-08-10実測・再現確認済み) — OCP(OCCT)のSTEPControl_Readerは、ReadFile()がIFSelect_RetDone以外を返した(=読み込み失敗)状態のままTransferRoots()を呼ぶと、Python例外にならずプロセスごと落ちる(Windowsで0xC0000005)。パスが無い場合だけでなく中身が壊れたファイルでも起きるので、存在チェックでは防げずReadFile()の戻り値検査が要る
created: 2026-08-10
updated: 2026-08-10
---

# OCPのSTEPファイル読み込み失敗はPython例外ではなくセグフォルトになりうる

`cadquery-ocp-novtk==7.9.3.1.1`（import名は `OCP`）/ `build123d==0.11.1` の環境
（`C:\Users\tm976\mf\minicad\.venv`。`cad-deixis-probe`(2026-08-13に削除済み) と同一バージョン構成）で、
minicadのM0テスト設計中に発見・再現確認した。

## 再現コード

```python
from OCP.STEPControl import STEPControl_Reader
r = STEPControl_Reader()
r.ReadFile(r"存在しないパス.step")   # ここは安全。IFSelect_RetError を返すだけ
r.TransferRoots()                     # ここでプロセスごとセグメンテーション違反
```

- `ReadFile()` 単体は正しく `IFSelect_RetDone` 以外を返す。**ここまではPythonから見て正常**。
- しかし読み込みが失敗した状態のまま `TransferRoots()` を呼ぶと、**Python例外を投げずに
  プロセスごとクラッシュする**。`OneShape()` まで進める前の時点、`TransferRoots()` の
  呼び出しだけで再現した。

## 失敗は2種類あり、どちらでも落ちる（2026-08-10 監査で別プロセスに切り出して確認）

| 与えたもの | `ReadFile()` の戻り値 | `TransferRoots()` を呼ぶと |
|---|---|---|
| 存在しないパス | `IFSelect_RetError` | 落ちる |
| 存在するがSTEPでない中身 | `IFSelect_RetFail`（`**** ERR StepFile : Undefined Parsing` を標準出力に吐く） | 落ちる |

Windowsでの終了コードは **`-1073741819` = `0xC0000005`（アクセス違反）**。
POSIXのSIGSEGV（終了コード139）ではないので、`139` を目印に探しても見つからない。

**「存在するが壊れている」側が実務上は重要** — `Path.exists()` によるガードは前者しか防げず、
壊れたSTEPを掴んだ瞬間に同じ死に方をする。防げるのは `ReadFile()` の戻り値検査だけ。

## なぜ重要か

[[build123d-face-index-breaks-on-fillet]] や `duocad/docs/IMPLEMENTATION_PLAN.md` §7が
警告する「OCCTの操作は例外を投げず黙って失敗する」（フィレットの `IsDone()=False` 例）と同種だが、
**こちらは例外にすらならずプロセスが死ぬ分、実害が大きい**。テストフレームワーク（pytest等）の
実行中にこの経路を踏むと、**そのテストプロセス全体が落ち、失敗の記録すら残らない**。

## How to apply

STEPを読むラッパ関数は、`ReadFile()` の戻り値を**必ず先に検査**し、`IFSelect_RetDone` 以外なら
`TransferRoots()`/`OneShape()` を呼ぶ前に明示的な例外（パス無し=`FileNotFoundError`、
中身が壊れている=`ValueError` 等）を送出すること。**ファイルの存在チェックで代用しない。**
「失敗を黙って通さない」規律をOCP系のコードに実装するときは、フィレットの `IsDone()` チェックと
同様に、**STEP読み込みでも読み込み系メソッドの戻り値チェックを省略しない**のが要点。

関連: [[build123d-face-index-breaks-on-fillet]] / [[build123d-ocp-abstraction-gap-measured]]
