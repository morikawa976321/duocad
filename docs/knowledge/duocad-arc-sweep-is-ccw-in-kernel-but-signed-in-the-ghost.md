---
name: duocad-arc-sweep-is-ccw-in-kernel-but-signed-in-the-ghost
description: 落とし穴(2026-09-06発見) — duocadの円弧はkernel.pyが(end-start)%360で常に反時計回りに掃くのに、web/src/entityKinds.tsのarcPointsはend-startの符号どおりに描く。end_deg<start_degを送ると幽霊と確定結果が違う弧になる。UI側のbuildが常にstart<=endを出せばよくkernelは触らない
created: 2026-09-06
updated: 2026-09-06
---

duocadの円弧は、**同じ引数を2つの層が別々の規則で読む**(2026-09-06に発見)。

- `duocad/kernel.py`: `sweep_deg = (end_deg - start_deg) % 360.0` —— **必ず反時計回り**に掃く。
- `web/src/entityKinds.ts` の `arcPoints`: `sweep = end - start` —— **差の符号どおり**に描く。

したがって `end_deg < start_deg` を送ると、**確定前の幽霊と確定結果が違う弧になる**。`duocad/sketch.py` の `add_arc` は角度を正規化せずそのまま格納するので、この値は素通りする。

角度を手で打つ人がいない間は露呈しないが、**マウスで掃引を決める入力([[duocad-sketch-dimensions-follow-the-mouse]] の3点円弧)を作ると必ず踏む**。回避はUI側で閉じる —— 時計回りの弧は始点と終点を入れ替えて表せば同じ弧になるので、`build` が常に `start_deg <= end_deg` を出せばよく、kernelを触る必要はない。

同種の向きの非対称は鏡映にもある: [[mirroring-an-arc-swaps-its-endpoints]]。
