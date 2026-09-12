---
name: shelf-attachment-gd-step-no-longer-exists
description: 事実(2026-08-23確認) — duocadの実測・受入の基準に繰り返し使われてきた 3d-models\shelf_attachment_GD.step (50KB・17ソリッド) は現存しない。残っているのは shelf_attachment.stl だけで、コード内の「これで実測した」というコメントはもう再現できない
created: 2026-08-23
updated: 2026-08-23
---

# `shelf_attachment_GD.step` はもう無い

2026-08-23、taskboxのタスクが実測対象として指していた
`C:\Users\tm976\mf\3d-models\shelf_attachment_GD.step`（50KB・17ソリッド）が
**存在しないことを確認した**。`mf` 配下を検索しても見つからず、残っているのは
`3d-models\shelf_attachment.stl`（STLのみ）である。

**duocadのコード・テスト・実装計画書に「shelf_attachment_GD.step で実測した」という
コメントが複数残っている**（面構成 56/34/22/0/0/0、円筒22面のU範囲が全て正確に360.0度、
`FULL_CYLINDER_DEGREES` のしきい値の根拠など）。これらは**もう再現・再検証できない**。
数値そのものは残るが、疑わしくなったときに確かめる手段が無い。

代わりに使える現存の小さいSTEP（`3d-models` 直下）:

- `monitor_attachment_for_24inch_GD.step` … 81KB・26ソリッド
- `mATX_PC_case_20251122\PC_case_feet.step` … 7.5KB
- `mATX_PC_case_20251122\fan_120mm.step` … 16.7KB

なお、duocadのpytestは2026-08-22に私物STEPへの依存を切ってある
（[[duocad-drops-private-step-test-dependency]]）ので、テストが落ちるわけではない。
効くのは**手元での実測・受入・smoke**と、コメントの根拠のほうである。

## 消えた時刻の窓（2026-08-23追記）

**2026-08-22 22:49 の時点では存在していた。** タスクを積む前の下調べで
`ls -la` が `50480` バイトを返している（同じコマンドで
`PC_case_20251122.step` の 4642352 バイトも同時に取得）。
drainのタスクrunnerが「無い」と判定したのは同日 00:41 前後なので、
**消えたのは 08-22 22:49 〜 08-23 00:41 の約2時間**である。

この窓で動いていたのは (1) 23:22 開始のdrain (2) 並行していた別の有人セッション
（drain実行中にU1〜U4を積んだもの）の2つ。**drainのトレースは削除ではなく
「存在しない」ことへの対処を記録している**（代替として
`monitor_attachment_for_24inch_GD.step` 81KB・26ソリッドへ差し替えて実測を続行）ので、
runnerが消した形跡は無い。原因は未特定。

**ごみ箱に残っていれば復元できる可能性がある**（窓が2時間と短いため）。
