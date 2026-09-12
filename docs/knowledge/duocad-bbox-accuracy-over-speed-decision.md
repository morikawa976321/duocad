---
name: duocad-bbox-accuracy-over-speed-decision
description: 判断(2026-09-07) — duocad の bbox は初回 +1.6秒を払ってでも実寸を返す側(AddOptimal)を採る。キャッシュの対象は bbox だけ、寿命は文書の切り替えまで
created: 2026-09-07
updated: 2026-09-07
---

duocad の bbox の取り方について、ユーザーが決めたこと(2026-09-07):

- **`BRepBndLib::Add` から `AddOptimal` へ差し替える。** 理由として本人が挙げたのは
  「29%のボディの寸法が最大9倍狂っているほうが害が大きい」。初回のコスト
  (27ボディの `describe_document(include_bbox=True)` で +1.6秒)は**承知の上で受け入れる**
  ——「遅くなったから戻す」を後から言わない、と明示している。
- **キャッシュの対象は bbox だけ。** 汎用の「形状キャッシュ」にはしない。
- **キャッシュの寿命は文書の切り替えまで。** 文書を差し替えたら捨てる。
  **グローバルな辞書1個で持たない**(長いセッションで古い形状が積み上がるため)。

実装は `Document` が `BoundingBoxCache` を1つ持つ形になった。文書の差し替えは
`Session._replace_document` が新しい `Document` を作るので、後始末のコードは要らない
(`view.DocumentView.reset()` は呼び忘れうる口を持つが、こちらは持たない)。

コスト側の実測は [[occt-addoptimal-does-not-get-faster-when-repeated]]。
