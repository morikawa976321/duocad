---
name: llama-reasoning-budget-fixes-empty-content
description: 実測(2026-08-23) — llama-serverの --reasoning-budget N と --reasoning-budget-message で、思考付きモデルの空返し問題が根治する。思考をNで打ち切っても本文が返るので、max_tokensを大きく取る必要がなくなる
created: 2026-08-23
updated: 2026-08-23
---

# `--reasoning-budget` が空返しの根治策になる

[[local-llm-reasoning-empty-content]]（単発2048・エージェント用途16384の目安）は
「max_tokensを厚く取る」を対処としているが、**これは対症療法であり、
最悪値を捨てる代償を払っている**(実際に1リクエストで7,468トークン・120秒に至った)。

llama-server には専用のフラグがある:

- `--reasoning-budget N` … **N>0で思考のトークン予算**(0で即終了、既定-1は無制限)
- `--reasoning-budget-message MESSAGE` … 予算切れのとき**思考終了タグの直前にMESSAGEを注入**する

2026-08-23、Ornith-1.5-9Bで `--reasoning-budget 512` を入れて実測したところ、
思考が予算に当たっても**本文は正常に返った**(content 1,686文字・`finish_reason: stop`)。
**空返しの原因そのものが消える。**

結果、`max_tokens` を大きく取る理由が無くなり、duocadでは16384→1024へ下げられた
(実タスク20試行で打ち切り0/20・誤り0/20)。

**対処の順序が逆だった** —— 空返しに対して出力上限を上げるのではなく、
思考の側を縛るのが正しい。上限を上げるのは最悪値を捨てることでもある。
