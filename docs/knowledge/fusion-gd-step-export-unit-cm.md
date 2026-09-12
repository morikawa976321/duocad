---
name: fusion-gd-step-export-unit-cm
description: Fusion 360のジェネレーティブデザイン結果をSTEPで出すと単位がcmになり、入力(mm)と食い違う
created: 2026-08-25
updated: 2026-08-25
---

Fusion 360のジェネレーティブデザイン結果をSTEPでエクスポートすると、**長さ単位が `SI_UNIT(.CENTI.,.METRE.)`（cm）** で書き出される。同じ設計の入力STEPは `SI_UNIT(.MILLI.,.METRE.)`（mm）なので、入力と出力で単位が食い違う。

実例（2026-08-24、棚受けブラケット）: 入力 `shelf_attachment.step` はmm、GD結果 `shelf_attachment_8.step` はcm。座標をそのままmmとして読むと 11.6 × 15.5 × 51.3 という1/10のサイズに見え、正しくは 60 × 154 × 113 mm だった。

**危険なのは、1/10でもスライサのビルドボリュームに収まってしまうためエラーにならず気づきにくいこと。** 数値だけ見て「GDが想定外に小さいものを出した」と誤読しかけた。

対策: GD結果をスライサやCADへ読み込んだ直後に**実寸を確認する**。STEPを直接調べる場合は `grep -a -o "SI_UNIT([^)]*)" <file>` で単位宣言を先に見る。
