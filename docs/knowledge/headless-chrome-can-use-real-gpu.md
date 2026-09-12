---
name: headless-chrome-can-use-real-gpu
description: headless Chrome(new)はこのPCで実GPU(RTX 5060 Ti/D3D11)を掴める。--use-angle=swiftshaderを外せば済み、CPUは7分の1になる。ただし検査の所要時間は変わらない
created: 2026-08-22
updated: 2026-08-22
---

「headlessだからGPUが使えずCPU描画になる」は**この環境では誤り**。2026-08-22に実測した。

`--headless=new` で起動したChromeのWebGL RENDERER文字列:

- `--use-angle=swiftshader --enable-unsafe-swiftshader` あり（duocadの現状）→
  `ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device ...), SwiftShader driver)` / MAX_SAMPLES 4
- 引数なし（`--headless=new` だけ）→ `ANGLE (NVIDIA, NVIDIA GeForce RTX 5060 Ti (0x00002D04) Direct3D11 ...)` / MAX_SAMPLES 8
- `--use-angle=d3d11` 明示、`--enable-unsafe-swiftshader` だけ残す、のどちらでも **D3D11（実GPU）**

**`--enable-unsafe-swiftshader` を残したままでも実GPUを掴む**（この引数は「掴めなかったときソフトGLへ落ちてよい」の許可であって、ソフトGLの強制ではない）。強制しているのは `--use-angle=swiftshader` の方だけ。

負荷差（1280×860・MSAA・毎フレーム2万インスタンス描画の合成負荷、6秒間、gpu-processのCPU実測）:

| | gpu-process CPU | fps |
|---|---|---|
| SwiftShader | **1463%**（14論理コア中の約15コア相当≒飽和） | 0.7 |
| D3D11 | **6%** | 36 |

**この合成負荷の倍率は実アプリに当てはまらなかった**（同日、duocadへ適用してから実測）。
検査の所要時間は**ほぼ変わらない**（smoke.mjs 163→150秒 / smoke-toast 15→15秒 / acceptance 16→15秒）。
検査の律速はOCCT/Python側とWebSocket往復であって描画のfpsではなく、14コアあるので描画が8コアを
占めても律速スレッドは大きくは待たされない。**変わるのはCPU消費だけ**（`smoke.mjs` 1本、chrome全プロセスの累積CPU）:

| | chrome累積CPU | 平均コア数 | 所要 |
|---|---|---|---|
| SwiftShader | 1550秒 | 9.5 | 163秒 |
| D3D11 | 215秒 | 1.4 | 150秒 |

合否も同一（6本中5本PASS、赤い `smoke-features` は両方とも同じ文言で落ちる）。

**教訓**: 合成ベンチの倍率をそのまま実タスクの改善幅として語らない。律速が別にあると、描画を50倍
速くしても全体は1割も動かない。今回は「検査が速くなる」と一度言ってから実測で覆した。

**由来**: duocadのこの引数は 2026-08-10 の `2f6bbfa`（M1）で入った。当時の検査ブラウザは **Edge headless** で、
そのときの定番レシピをそのまま持ってきたもの。2026-08-15 に Chrome 優先へ切り替えた（`391d599`）ときも引数は素通りした。

**適用済み**: duocad `web/scripts/browser.mjs` から `--use-angle=swiftshader` を外し、起動時にRENDERER文字列を
`GL :` の行へ出すようにした（2026-08-22。フォールバック許可の `--enable-unsafe-swiftshader` は残置）。

**残る不確実性**: taskbox drain はタスクスケジューラ（LogonType=Interactive、ユーザーのセッションで実行）から
起動するので実GPUを掴めるはずだが、そこは未実測。**次のdrainのログで `GL :` の行を見れば分かる**
（黙ってソフトGLへ落ちても検査は通ってしまい、CPUを食うだけの状態が見えないので、この行を置いた）。

[[headless-webgl-checks-burn-cpu]]
