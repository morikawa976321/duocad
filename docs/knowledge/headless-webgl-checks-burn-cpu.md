---
name: headless-webgl-checks-burn-cpu
description: duocadのheadless検査中はChromeのGPUプロセスが8コア級のCPUを食う。SwiftShader(ソフトGL)+MSAA+無条件rAFループの積であり、異常ではない。SwiftShader強制は自前の引数由来で、外せば実GPUを掴める(headless-chrome-can-use-real-gpu)
created: 2026-08-22
updated: 2026-08-22
---

duocadの `web/scripts/smoke*.mjs` / `acceptance.mjs` を回している間、Chromeの
**gpu-process 1本だけで約800%（14論理コア中の約8コア）** を持っていく。2026-08-22 15:31 起動の
T3実行中に実測（起動から約160秒で累積CPU 1319秒）。**taskbox drainの暴走ではなく、検査の設計どおりの消費。**

内訳（3つの積であって、単独の原因ではない）:

- `web/scripts/browser.mjs` が `--use-angle=swiftshader --enable-unsafe-swiftshader` で起動する。
  この指定によりWebGLは**CPUによるソフトウェアラスタライズ**になり（headlessだからではない — [[headless-chrome-can-use-real-gpu]]）、SwiftShaderは
  全コアにスレッドを広げる。→ 1コアではなく8コア級になる理由。
- `web/src/viewport.ts` の `tick()` が `controls.update()` → `renderer.render()` → `requestAnimationFrame`
  を**無条件で**繰り返す（変化が無くても毎フレーム描く）。puppeteerは既定で
  `--disable-background-timer-throttling` を付けるので、**画面が無くても60fpsで回り続ける**。
- `WebGLRenderer({ antialias: true })`（MSAA）。実GPUではほぼ無料だがソフトGLでは最も高い部類。

**判断材料**: 消費を下げたいなら、検査時だけ①MSAAを切る ②描画をon-demand化（変化時のみ `render()`）
のどちらか。①は検査の見た目を変え、②はアプリ本体の挙動変更で、どちらも検査の意味に影響する。
検査は数分で終わるので放置してよい、と当初は考えた。**この結論は同日中に覆った** —
実GPUを掴めることが実測で分かったので、正しい手当ては「`--use-angle=swiftshader` を外す」である
（[[headless-chrome-can-use-real-gpu]]）。MSAAとrAFループは、実GPUなら費用がほぼ無い。

[[headless-chrome-can-use-real-gpu]]
