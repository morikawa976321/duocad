# 密度場側ガウシアン平滑化 — 前後比較スクリーンショット(2026-09-05)

`duocad/topopt/mesh.py` に足した段0(ガウシアン平滑化+保持/除外の塗り直し。
`docs/TOPOPT.md`「密度場側のガウシアン平滑化」節)の効果を目で確認するための画像。
**調整値(σ・Taubin反復数)の探索はしていない** — 既定値(σ=1要素、既定で有効)を
入れる前と後を比べただけ。

## 見方

各PNGは左右に並んだ1組:

- **左 = before** — このタスクより前の既定(ガウシアン平滑化オフ・Taubin 30反復)
- **右 = after** — このタスクの既定(ガウシアンσ=1要素+保持/除外の塗り直し・Taubin 12反復)

**同じ密度場(同じ最適化結果の`rho`)から作った2本**(最適化を2回回して比べていない)。
カメラ位置・画角・光源・背景はbefore/after・4題材・4方向すべてで共通(three.jsの
`PerspectiveCamera(fov=45)` / `HemisphereLight` + `DirectionalLight(2,3,1)` /
背景色 `#0a0a12`。`probe_topopt/web/index.html` の既定表示と同じ光源設定)。

## 題材と方向

題材は `design_space.CASES`(`docs/TOPOPT.md`「性能の基準線」節の4題材と同じ):
`bracket` / `plate` / `branching` / `tilted`。各題材につき4枚:

| サフィックス | 内容 |
|---|---|
| `_iso` | 3/4俯瞰(既存の`probe_topopt/web/index.html`の既定視点と同じ向き) |
| `_side` | ほぼ真横(輪郭のシルエットが見える角度。本人が問題に気づいたのもこの種の視点) |
| `_threeq` | もう1つの3/4視点(iso とは別角度) |
| `_zoom` | isoと同じ向きで距離だけを詰めた拡大(部品全体を1000pxに収めた絵では
  中周波のうねりが見えないため) |

各最適化は`max_iter=40`(既定の`derive_default`=案5の導出。`docs/TOPOPT.md`
「塊の導出を案5へ倒した」節と同じ入口)。条件面が空になった題材は無し。

## 見えること(2026-09-05時点の観測。本人の判断材料であって結論ではない)

- **`bracket` / `branching`**: 見た目の差は微妙(この解像度・この視点では、うねりの
  縮小がはっきり分かるのは `_zoom` 程度)。
- **`plate` / `tilted`(除外の穴・薄いリブを持つ題材)**: **差が非常に大きい。**
  `plate_iso.png` は before で連続した1枚の傾斜面だったリブ群が、after では
  複数の細い枝へ裂けて穴が空いている。`tilted_zoom.png` は before で
  ギザギザの細い隙間だった除外トンネルの縁が、after では滑らかな1つの穴へ変わり、
  トンネル内部に張っていた薄い膜状の材料(before側の縞模様)が消えている。
  **これは単なる「見た目が滑らかになった」ではなく、薄い部材がガウシアンで両側から
  食われて構造が変わりうることの実例**(タスク本文の罠「ただし細い部材は両側から
  食われる」がここで実際に起きている)。σ=1要素は未検証の決め打ちなので、
  薄肉設計ではこの効果を承知した上でσを下げる調整が要るかもしれない。
- `tilted_zoom.png` の after 側、左下に小さな孤立した三角形の破片が写っている
  (`remove_islands`は最大連結成分だけを残すので、目視での見落としでなければ
  細い橋を介して本体とつながっているはず)。深追いはしていない —— 気になれば
  `duocad/topopt/output/` に同題材のSTLを吐いて確認すること。

## 生成方法(再現用。コミットしていない使い捨てツール)

1. `design_space.CASES[name].derive_space()` → `design_space.run_topopt(space, max_iter=40)`
   で4題材をそれぞれ実際に最適化し、`result["result"].rho`(平滑化前の生のSIMP密度場)と
   `result["solid"]` / `result["void"]`(保持+アンカー材料 / 除外∪設計空間外のマスク)を得る。
2. 同じ`rho`から `mesh.build_surface(rho, smooth_sigma=0, n_iter=30)`(before)と
   `mesh.build_surface(rho, keep_mask=solid, zero_mask=void)`(after、全て既定値)を作る。
3. `probe_topopt/web/vendor/three` の three.js を読む最小限のHTMLページ(puppeteer-core経由の
   headless Chrome。`web/scripts/shot.mjs`と同じ借用の仕方)で、固定カメラ・固定光源のまま
   before/afterそれぞれをレンダリングしてPNGへ。
4. Pillow(`duocad`の`.venv`にscikit-image経由で既に入っている推移依存)で左右に結合。

このスクリプト一式は使い捨てで、`duocad`本体にも`probe_topopt`にもコミットしていない
(セッションのスクラッチ領域に置いた)。撮り直すときは上の手順を再実装すること。
