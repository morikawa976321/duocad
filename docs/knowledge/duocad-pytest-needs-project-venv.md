---
name: duocad-pytest-needs-project-venv
description: duocadのpytestは素のpythonだとOCPが無く大量の収集エラーになる。必ず .venv\Scripts\python.exe を使う
created: 2026-08-11
updated: 2026-09-06
---

`C:\Users\tm976\mf\duocad` で `python -m pytest` を走らせると、**全モジュールが
`ModuleNotFoundError: No module named 'OCP'` で収集エラー**になる（件数はOCPをimportする
テストファイルの数で変わる。2026-08-11は19件、2026-09-06は48件）。PATH上の `python` は
`cadquery-ocp-novtk` が入っていない別環境で、**セッションによって実体が違う**
（2026-09-06のtaskbox runnerでは hermes-agent の venv を拾っていた）。

正しい呼び方:

```
cd C:\Users\tm976\mf\duocad
.\.venv\Scripts\python.exe -m pytest -q
```

**「テストが全部壊れている」に見えるが、実際はインタプリタを間違えているだけ**という
形で出る。**`python -m pytest -q 2>&1 | tail` と書くとパイプ末尾の終了コード（0）が返るので、
件数を読まずに終了コードだけ見ると「緑」と誤読する。**2026-08-11のdrainで、タスクの前提条件（着手前にpytestが緑であること）の確認が
これで一度空振りした。`pyproject.toml` の `pythonpath = ["."]` は入っているので、
venvのpythonさえ使えば `-m` 無しの素の `pytest` でも通る。

同種の罠: [[parallel-sessions-inflate-cpu-bound-benchmarks]]（duocadの性能テストが
機械負荷で偽陽性の赤になる件）。**duocadで赤を見たら、まず環境と負荷を疑う。**
