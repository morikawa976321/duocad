---
name: three-shadermaterial-is-always-glsl3
description: 実測(2026-09-03・three 0.185.1) — ShaderMaterialは常に #version 300 es で組まれるのでgl_FragDepth/fwidthがそのまま使える。色空間変換は自分で include する。reversedDepthBufferは@types/threeに無く、自前のgl_FragDepthとは併用できない
created: 2026-09-03
updated: 2026-09-03
---

# three.js 0.185 で自前シェーダーを書くときの実測

`node_modules/three/src/renderers/webgl/WebGLProgram.js` を読んで確かめた事実
(duocadの無限グリッドを書くときに要った)。

- **`RawShaderMaterial` でなければ、three は必ず `#version 300 es` を付ける。** GLSL1流儀
  (`varying` / `gl_FragColor`)で書いてよく、three 側が `#define` で読み替える。**GLSL ES 3.00
  の組み込み(`gl_FragDepth`・`fwidth`・`inverse`)がそのまま使える。**
- **色空間の変換は自分で書く。** 組み込み材質と同じ見え方にするには、フラグメントの最後に
  `#include <colorspace_fragment>` を置く(`linearToOutputTexel` は prefix 側にある)。
  忘れると同じ色コードなのに組み込み材質より暗く出る。`#include` は ShaderMaterial でも
  解決される。
- **フラグメントの prefix に `projectionMatrix` は無い**(`viewMatrix` と `cameraPosition`
  はある)。フラグメントでクリップ座標を作りたければ自分で uniform を渡す。
- **`renderer.reversedDepthBuffer` は実体としては存在するが、`@types/three` 0.185.4 の
  `WebGLRendererParameters` に無い**ので TypeScript からは渡せない。さらに深度の向きと
  範囲の規約を変えるので、**`gl_FragDepth` を自前で書くシェーダーとは併用できない**
  (`z/w * 0.5 + 0.5` が黙って壊れる)。

## TypeScript側の罠

シェーダーをテンプレートリテラルで持つとき、**GLSLのコメントに書いた日本語の中の
バッククォート(`` ` ``)がリテラルを終端させる**。エラーは `TS1443: Module declaration names
may only use ' or " quoted strings` という原因の遠い形で出る。
