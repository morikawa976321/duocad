---
name: llama-n-predict-is-a-default-not-a-cap
description: 実測(2026-08-23) — llama-serverの --n-predict は既定値であって上限ではない。クライアントがmax_tokensを明示するとそちらが勝つ(--n-predict 1024のサーバへ16384を送ると3,520トークン返った)。出力長の保証はクライアント側にしか置けない
created: 2026-08-23
updated: 2026-08-23
---

# `--n-predict` はサーバ側の上限ではない

2026-08-23、llama-server(build 10549)で実測。

| リクエスト | 結果 |
|---|---|
| `max_tokens` 指定なし | 1024トークンで `length` |
| **`max_tokens: 16384`** | **3,520トークン**返った(56秒) |
| `max_tokens: 300` | 300トークンで `length` |

**`--n-predict N` は既定値であり、クライアントが `max_tokens` を明示すればそちらが勝つ。**
サーバ側に置けば「クライアントが何を送っても安全」と考えたが、それは成り立たない。

**出力長の保証はクライアント側の値にしか置けない。**
サーバのフラグは、クライアントが何も指定しなかったときの既定として意味がある。

なお `--reasoning-budget N`(N>0で思考のトークン予算)は別で、こちらは実測で機能した =
[[llama-reasoning-budget-fixes-empty-content]]。
