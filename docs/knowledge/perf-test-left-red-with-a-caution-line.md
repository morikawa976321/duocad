---
name: perf-test-left-red-with-a-caution-line
description: 判断(2026-08-14) — 負荷で赤に振れるduocadの性能テストは直さず注意書きで凌ぐと決定。ただし当のテストは2026-08-22に私物STEP依存として削除済みで、kernel時間の回帰を機械で見る手段は現在無い
created: 2026-08-14
updated: 2026-08-28
---

# 負荷で赤になる性能テストは、直さず注意書きで凌ぐ（2026-08-14）

`duocad/tests/test_incremental_update.py::test_extruding_one_part_of_the_pc_case_stays_under_1000ms`
が、コード無変更のままマシンの混雑で閾値1000msを超えていた（単独5回で 942 / 961 / 973 / 1076 / 1164 ms）。

- **選択**: いったん放置する。**閾値を上げる・テストを消すはしない。**
- **対処**: 解決するまでのあいだ、**無人drainが「性能回帰」と誤診しないための一行注意書き**を
  ①テストのdocstring（失敗が出る場所）②`duocad/tests/README.md`（duocadのタスクが必読にしている契約文書）
  の2箇所に入れた（duocad `018ba23`）。内容は「この1本だけが赤いなら負荷。コードを直さず、
  閾値も上げず、タスクを `failed/` へ送らず、報告に負荷由来と書いて進める」。
- **理由**: 実害は測定値そのものではなく、**赤が狼少年化して本物の性能回帰を見落とすこと**。
  それは注意書きで足りる。

**現状（2026-08-28に実測で確認）**: 上の `tests/test_incremental_update.py` は**もう存在しない**。
[[duocad-drops-private-step-test-dependency]]（2026-08-22）の判断で私物STEP依存のテストごと
削除された。したがって上記の対処はすでに空振りであり、**duocadのkernel時間の回帰を機械で見る手段は
現在ひとつも無い**。根治案（最小値採用／`-m perf` で分離／分解前との比に変える／廃止）は
そもそも測る対象が消えたので、必要なら合成形状で測り直すところからになる。

**一般化（こちらは今も生きている）**: 環境依存で揺れるテストは、**閾値をいじるより「揺れることを
失敗の現場に書く」ほうが安い**。閾値を緩めると測っている意味が消えるが、注意書きは測定を保ったまま
誤診だけを防ぐ。

関連: [[duocad-v1-reached]] / [[tight-perf-test-thresholds-false-positive-under-drain]] / [[duocad-drops-private-step-test-dependency]]
