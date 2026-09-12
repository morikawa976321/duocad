---
name: occt-addoptimal-does-not-get-faster-when-repeated
description: 実測(2026-09-07・OCCT) — BRepBndLib::AddOptimal は Add の約2000倍遅く、OCCTが結果を持たないので2回目も同じだけかかる。正確さを取るならキャッシュとセットでしか成立しない
created: 2026-09-07
updated: 2026-09-07
---

`BRepBndLib::AddOptimal` は面のトリムを見るので実寸の bbox を返すが、**トリム曲線を実際に
評価するので遅い**。しかも **OCCT は結果を形状に持たないので、同じ形状を2回測っても
2回目が速くならない**(三角形分割 `BRepMesh` が形状に貼り付いて残るのとは逆)。

実測(2026-09-07・duocad `models/hanger_bar_attachment.json`、11ボディ、温めた後の中央値):

| 測り方 | 11ボディ合計 |
|---|---|
| `BRepBndLib::Add` | 0.09ms |
| `BRepBndLib::AddOptimal` | **178ms**(約2000倍) |

1ボディあたり 18〜60ms(形状による)。この差は「あると嬉しい」の水準ではなく、
**繰り返し呼ぶ経路では差し替えとキャッシュがセットでしか成立しない**。duocad の
`optimize_run.resolution_limits` の定常状態: `Add` 0.56ms / `AddOptimal`+キャッシュ
0.50ms / `AddOptimal` キャッシュ無し **145ms**。

**キャッシュは初回のコストを1つも減らさない。** 27ボディの
`describe_document(include_bbox=True)` は初回で +1.6秒になる。

鍵の取り方には別の罠がある: [[topods-shape-hash-matches-but-eq-is-false]]
