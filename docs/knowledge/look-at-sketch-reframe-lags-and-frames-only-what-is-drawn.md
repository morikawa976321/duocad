---
name: look-at-sketch-reframe-lags-and-frames-only-what-is-drawn
description: 実機検査の落とし穴2件(2026-09-06実測) — 既存スケッチへ再入場した直後、window.__duocad.activeSketchは同期的に更新されるがlookAtSketch()のカメラ再フレームは数フレーム遅れ、直後のprojectSketchPointがまれにnullを返す(settle()を挟む)。また「正面から見る」はその時点で見えているものに視野を合わせるので、後から離れた座標を押すと画面外になりnullを返す(入った直後に検査で使う図形を全部描く順序にする)
created: 2026-09-06
updated: 2026-09-06
---

# 「正面から見る」の視野は遅れて反映され、描いてある範囲にしか合わない

どちらも症状は `projectSketchPoint` / `projectRegion` が `null` を返すことで、UIの不具合と紛らわしい。

## 1. 再入場直後のカメラ再フレームは数フレーム遅れる

`BrowserPanel` の「スケッチ(...)」行をクリックして**既存のスケッチへ再入場**すると、`window.__duocad.activeSketch` はそのクリックの反応として同期的に更新されるが、内容へカメラを合わせ直す `viewport.lookAtSketch()` の効果(カメラ位置・frustum半径)は**数フレーム遅れて反映される**(2026-09-06実測)。

`page.waitForFunction(() => window.__duocad.activeSketch === id)` が解決した直後に `projectSketchPoint(u, v)` を呼ぶと、**まれに `null`**(前のモードの視点のまま、狙った図形がまだ画面外)。再現率は低く、連続で流すと通ったり落ちたりする —— 「たまに落ちるテスト」に見えて実際はカメラのタイミング競合。

**対策**: `activeSketch` の一致を確認した後、射影ヘルパを呼ぶ前に数フレーム待つ(既存の `settle()` ヘルパで十分。`smoke-hover.mjs` 検証6で採用)。`acceptance.mjs` が同種の再入場後に `setTimeout(500)` を挟んでいたのも恐らく同じ理由(当時は原因を明記していない)。

## 2. 視野はそのとき存在する図形にしか合わない

`#sketch-look`(正面から見る)は押した時点で見えているものに視野を合わせる。「板だけがあるスケッチへ入り直して `#sketch-look` を押し、そのあと原点付近の (0,-20) を押す」順序にしたところ、その点が画面の外にあり `projectSketchPoint` が null を返して落ちた(板は原点から60mm離れた所にあった)。

**回避は順序の組み替え**: スケッチへ入った直後に、その検査で使う図形を全部描いてしまう。描画そのものは視野を動かさないので、最初に合わせた視野のまま最後まで押せる。後から離れた場所を足す構成にすると、視野合わせの後で座標が画面外へ出る。
