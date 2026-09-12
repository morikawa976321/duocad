---
name: ornith-9b-clears-duocad-tool-protocol
description: 実測(2026-08-22) — Ornith-1.5-9B(Q4_K_M・RTX 5060 Ti・67 tok/s)は実duocadの29ツールで、スケッチ作成→円→矩形パターンの複合操作を5手で完走した。長文脈(40ボディからの条件抽出)も通る。9Bでもduocadのエージェント土台に足りる
created: 2026-08-22
updated: 2026-08-22
---

# Ornith-1.5-9Bは実duocadの29ツールプロトコルを完遂できる

2026-08-22、llama-server(build 10549, Q4_K_M, 生成67 tok/s)に実duocadの
`DocumentTools` を直結して実測。

- **複合操作を5手で完走**: 「XY平面にスケッチ→原点に直径8mmの円→X20mm×4・Y15mm×3で配列」
  を `describe_document → sketch_create → add_circle → pattern_rectangular → describe_document`
  で完了。直径→半径の変換も正しく、**エラーゼロ・IDの捏造ゼロ**。
- **長文脈も通る**: 40ボディ+8スケッチ+24パラメータ(約7,800字)を読ませ
  「kind=solidのうち体積最大」を特定させる課題を完璧に通した(参照ボディ5個を全部除外)。

つまり[[local-llm-drives-duocad-tool-protocol]]で27Bについて確認した「ローカルLLMが
CADエージェント土台に足りる」は、**9Bまで下げても成立する**。
ラップトップ(dGPU無し)への移行余地がここで確認できた。

残る弱点は能力ではなく規律の側 = [[ornith-9b-skips-propose-stop-rule]]。

注意: 当初これを「6手を超えると破綻する」と記録しかけたが、それは模擬バックエンドの
産物だった = [[mock-backend-makes-the-model-look-broken]]。
