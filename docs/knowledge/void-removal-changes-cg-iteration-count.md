---
name: void-removal-changes-cg-iteration-count
description: 実測(2026-09-08) — SIMPの組立からvoid要素を外すと未知数も形も変わらないが、CGの反復数は動く。bracket +19% / 安全率探索 -5.5% と符号が題材で変わる
created: 2026-09-08
updated: 2026-09-08
---

duocad の `simp` で **void要素(ρ=0固定)を組み立てから外す**と、`_floating_dofs` が既に
浮いた節点を拘束へ回しているので**未知数は1つも減らず**、最終形状も同じになる
(2026-09-08 の5測定点で ρ>=0.5 の一致率 100.0000%・コンプライアンス相対差 3e-6〜3e-5)。

**しかし K そのものは変わる**(emin 媒質の寄与が消える)ので、**CG の反復数が動く**:

| 題材 | 現行 | void除去 | 変化 |
|---|---|---|---|
| bracket n=40 | 20.9 回/solve | 24.9 | **+19%** |
| hanger 5.37mm | 35.0 | 36.5 | +4% |
| hanger 8.19mm / 6.59mm / tilted n=40 | — | — | 変化なし |
| hanger rmin=8mm の完全な安全率探索(447 solve) | 16,145 回(合計) | 15,263 | **-5.5%** |

**符号が題材で変わる。** bracket では反復数の増加が組み立ての節約をほぼ食い潰し、
全体は 98.2%(他の題材は 79〜85%)にしかならなかった。

したがって **「void除去は数値的に無害」は形の話であって、費用の話ではない。**
組み立て方を変える案を評価するときは、秒数だけでなく **CG反復数を必ず並べて見ること**
(反復数が再現性のある物差しである件は [[cg-iteration-count-is-the-reproducible-benchmark-yardstick]])。
void除去を main へ入れた後の実測は [[void-removal-measured-on-main]]。
