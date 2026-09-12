---
name: topopt-default-threads-capped-at-half-cores
description: probe_topoptのCPU並列化の既定スレッド数を全論理コアから総コアの半分(下限1)へ変更した判断と根拠
created: 2026-09-05
updated: 2026-09-05
---

2026-09-05本人判断: probe_topoptのCPU並列化(`simp.PARALLEL_THREADS`)の既定を、それまでの
`os.cpu_count()`(全論理コア)から**総コアの半分(下限1)**へ変更した。「問題サイズによる
コア数上限か総コアの半分か、小さいほうを上限とする」という規則。

理由(本人): 配線後はduocad本体が同じマシン上でユーザー操作と並行して回るので、既定が
全論理コアを占有すると、最適化が分オーダーで走る間フロントエンド自体も応答しなくなる。
probe単体で回すだけなら困らないが、既定が本体まで持っていかれると困る。

明示指定(環境変数 `TOPOPT_THREADS` / `optimize(threads=N)` / `Model.solve(threads=N)`)は
絞らない — この判断は「既定値」だけに適用され、`perf.py --threads 14` のような明示的な
全コア指定は従来どおり通る。

性能面でも既定を下げて損はしていない(実測、`20260904-1415`): 4題材合計n=44で
t=4 78.9秒 / t=8 69.9秒(最速)/ t=14 73.5秒。半分(このマシンでは7)はt=8のすぐ隣で、
全コア(t=14)より遅くない。

実装は `duocad/probe_topopt/simp.py` の `_default_thread_count()` の末尾1行のみ
(taskbox `20260904-1558`)。

関連: t=14がt=8より遅かった原因(Intel Core Ultra=Arrow LakeのP/Eコア混在でEコアが
帯域を食い合うだけの可能性)は**未検証の仮説のまま**で、この既定はその切り分けを
先送りするための実用値。本体側の既定を決めるとき(taskbox `20260904-1704` の範囲)にPコア数を数えるべきかを別途測って決める。

関連: [[scipy-csr-matvec-releases-gil-sparse-sparse-does-not]](並列化の中身)
