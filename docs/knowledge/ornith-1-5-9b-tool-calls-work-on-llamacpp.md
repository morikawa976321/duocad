---
name: ornith-1-5-9b-tool-calls-work-on-llamacpp
description: 事実(2026-08-22) — Ornith-1.5-9B Q4_K_Mはllama.cpp build 10549でツール呼び出しと思考分離が動作。RTX 5060 Tiで生成68.2 tok/s、プリフィル1314 tok/s
created: 2026-08-22
updated: 2026-08-22
---

# Ornith-1.5-9Bはllama.cppでツール呼び出しが動く(実測)

モデルカードはvLLM / SGLangでのツールパースしか明記していないが、
**llama.cppでも動作することを実測で確認した**(2026-08-22)。ローカルLLMの
候補選定で最大の不確定要素だった箇所。

## 環境

- llama-server build 10549 (commit b2e5e9b28)、CUDA 13ビルド
- ornith-ai/Ornith-1.5-9B-GGUF:Q4_K_M (5.63GB、単一ファイル)
- RTX 5060 Ti 16GB、-ngl 99 で全層GPU、-c 32768、KVはf16
- **--jinja が必須**(無いとツール定義が渡らない)

## 実測値 (299トークン投入 / 87トークン生成)

- 生成 **68.2 tok/s**
- プリフィル **1,314 tok/s** (299トークンを227ms)

プリフィルがこの速度なら、29ツール定義込みの数千トークンのプロンプトでも
TTFTは1-2秒に収まる。duocadの対話用途でTTFTは当面ボトルネックにならない。

## 確認できたこと

- tool_callsが正しいnameとargumentsで返る
- reasoning_contentに思考が分離される
- finish_reasonがtool_callsになる ([[local-llm-reasoning-empty-content]])

1ツールでの成功であり、**duocadの29ツールから紛らわしい対(フィレット/チャンファー等)を
選び分けられるかは別途検証が要る**。
