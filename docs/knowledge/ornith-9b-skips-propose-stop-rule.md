---
name: ornith-9b-skips-propose-stop-rule
description: 運用知見(2026-08-22) — Ornith-1.5-9Bはduocadのpropose→commit→verbプロトコルのうち「曖昧な依頼はproposeで止めて尋ねる」規則を守れず、寸法を自分で捏造してverbまで適用してしまう。止める判断はプロンプトではなく実装側でガードすべき。ガードは2026-08-23に実装済み
created: 2026-08-22
updated: 2026-08-23
---

# Ornith-1.5-9Bはproposeで「止まる」側を守れない

2026-08-22実測。`agent_instructions()` は「曖昧・探索的な依頼なら
propose_selection で止めて確定してよいかを尋ねる(止めたら verb は呼ばない)」
「迷ったら止める側に倒す」と明記している。

「なんかこのモデル、角が尖ってて危ないから、いい感じに処理しといて」という
最大限に曖昧な依頼に対し、モデルは止まらず **R1.5mm を自分で決めて fillet まで適用した**。
`fillet` は how の入力(radius)を持つので規則(2)に該当し、止まるのが正しい。

これは指示文の読み落としではなく、**「進める」方向への一貫した偏り**と見るべき。
9Bクラスに「やらない判断」を期待するのは分が悪い。

**対処: プロンプトで守らせようとせず、duocad側の実装でガードする。**
how の入力を持つ verb は、ユーザー発話に数値が明示されていなければ
commit_selection 以降を拒否する、という機械的な条件で止められる。
プロトコル違反の代償は「undoでしか戻せない適用」なので、モデルの裁量に置かない。

**続報(2026-08-23)**: このガードは実装済み。設計判断と実測(曖昧依頼で適用0/3・寸法明示の依頼は3/3完走で誤発火なし)は [[guard-the-discipline-the-model-cannot-keep]]。

関連: [[ornith-9b-clears-duocad-tool-protocol]] / [[mock-backend-makes-the-model-look-broken]]（当初観測した「ID捏造」「多段操作の破綻」は模擬バックエンドの産物で、実物では再現しなかった）
