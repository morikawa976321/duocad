---
name: local-llm-reasoning-empty-content
description: 運用知見 — 推論付きローカルLLMをOpenAI互換APIで使うと思考の打ち切りでcontentが空になる(HTTP 200のまま)。llama-serverでは--reasoning-budgetが根治策で、max_tokens積み増し(単発2048/エージェント16384)は対症療法。故障との判別はfinish_reason
created: 2026-07-20
updated: 2026-08-25
---

# 推論付きローカルLLMは「成功したのに空返し」する

**症状**: 推論(thinking)付きモデルは答えより先に思考を `reasoning_content` へ吐き、そのあとに `content` を書く。そのため**トークン予算が足りないと思考の途中で打ち切られ、HTTP 200・例外なしのまま `content` が空文字で返る**。`finish_reason` だけが `"length"` になる。エラーではないので、雑に書くと「静かに全件の出力が空になる」という形で失敗が表に出ない。

## 対処

**根治策は思考の側を縛ること**: llama-serverの **`--reasoning-budget N`**（思考のトークン予算）が空返しを根治すると実測で確定している（[[llama-reasoning-budget-fixes-empty-content]]。duocadでは max_tokens 16384→1024 へ下げて打ち切り0/20）。**同フラグが使える環境ではこちらが正しい。**

**対症療法は max_tokens を積むこと**（root fixが使えない環境向け）:

- **単発判定は2048**。1024では日本語200字程度の要約でも5件中1件が思考中に打ち切られ、2048で5/5成功した（思考は1500〜2000字書くことがある）。
- **エージェント用途（ツールを何手も呼ぶ）は16384**。Ornith-1.5-9B(Q4_K_M)でduocadの29ツールを使った実測では、max_tokens=3072 だと1〜3手目は正常でも**4〜5手目で `finish_reason: length` かつ content 空**になって停止した（手数が進んで文脈が伸びるほど思考が長くなるため）。16384にすると2件とも完走。**空返しに見えていたものは能力の限界ではなく単なる打ち切りだった** — 単発の目安をそのまま持ち込むとモデルの能力を誤って低く見積もる。
- **思考を止めるパラメータは効かなかった**: `"chat_template_kwargs": {"thinking": false}` も `"reasoning_effort": "none"` も思考を抑止できなかった（llama.cpp b9673）。

## tool_callsのcontent空は正常（故障との判別はfinish_reason）

OpenAI互換API仕様では、アシスタントがテキストではなく**ツール呼び出しを返した場合、contentが空になるのは正常動作**。本ノートの故障と症状が同じなので、content空だけでは区別できない。**必ずfinish_reasonを見る**:

| | 正常(ツール呼び出し) | 故障(思考の打ち切り) |
|---|---|---|
| finish_reason | tool_calls | length |
| tool_calls | あり | なし |
| content | 空 | 空 |

思考モデルでは `reasoning_content` に思考が分離されて入るので、そこが埋まっていることも正常側の傍証になる。

## バッチ処理に組み込むときの一般則

①**`content` が空の応答を成功扱いしない** ②`finish_reason == "length"` かつ content 空を検知して警告を出す ③トークン予算は思考ぶんを見込む。**API呼び出しが成功したことと、使える出力が返ったことは別物として扱う。**

参考実測: 生成速度は約47 tok/s（RTX 5060 Ti）、1記事あたり9〜17秒で、思考ぶんの時間がまるごと乗る。

関連: [[news-scraping-status]] / [[ornith-1-5-9b-tool-calls-work-on-llamacpp]]
