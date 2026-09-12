---
name: fov-default-change-exposed-a-duplicate-hardcoded-camera-distance
description: 落とし穴(2026-09-05実測) — duocadの`viewport.ts`起動時カメラ位置が`fitDistance`と同じ計算を`diagonal * 1.2`という決め打ち定数で重複実装しており、旧既定FOV(45度)でしか一致しない値だった。FOV_DEFAULTを45→30へ変えたら、この重複だけが取り残されて実機検査が原因不明のpick失敗で落ちた
created: 2026-09-05
updated: 2026-09-05
---

duocadの`web/src/viewport.ts`で、カメラが画面に収まる距離を計算する`fitDistance(radius)`
(FOVとアスペクトから動的に距離を出す関数)とは別に、**`createViewport`冒頭の起動時カメラ
位置だけが`diagonal * 1.2`という決め打ち定数を独自に持っていた**(空文書でも原点まわりが
見えるようにする初期姿勢)。

この定数は`fitDistance`が存在する前の名残で、旧既定FOV=45度のとき
`radius / tan(22.5°) ≈ radius * 2.4142`(半径ベースでは`diagonal * 1.2071`)と**偶然ほぼ
一致していた**(差0.6%)ため、何年も気づかれずに残っていた。

[[duocad-fov-ui-reduced-to-binary]]でFOV_DEFAULTを45→30へ変えたとき、この重複定数だけを
直さないままにしたところ、`diagonal * 1.2`は正しい値(30度なら`diagonal * 1.8660`)から
**55%もずれ**、起動直後の空文書や「まだ内容の無いスケッチ」(`lookAtSketch`が半径0のとき
「前の距離のまま」に倒す経路)でカメラが想定より寄ったまま固まった。

**症状は原因を一切名指ししない形で出た**: 実機検査`smoke-move.mjs`が
「線分そのもの(辺)をクリックで選べない(タイムアウト)」で落ちた。クリック座標自体は
`window.__duocad.projectSketchPoint(u,v)`で毎回動的に(その時点のカメラで)計算しているので
理論上FOVに依存しないはずだが、実際にはこの起動時カメラのズレが上流の状態に影響していた。
`git stash`でpristineコードに戻すと2回連続green、戻すと2回連続redという再現性テストで
初めて「FOV_DEFAULTの変更そのものが原因」と確定できた。

**教訓**: FOVやカメラ距離の既定値を変える前に、`grep -rn "diagonal \* [0-9]"`のような
「`fitDistance`を経由しない距離の決め打ち」が他に無いかを確認すること。1箇所を直しても、
同じ値を独自に再計算している場所が残っていれば、その場所だけ静かに古い前提のまま取り残される。

修正: 起動時カメラ位置も`fitDistance(diagonal / 2)`を通すように統一(`frameToContent`と
同じ式)。duocad `fa65b79`。

関連: [[duocad-fov-ui-reduced-to-binary]] / [[first-red-run-is-not-always-your-own-change]]
