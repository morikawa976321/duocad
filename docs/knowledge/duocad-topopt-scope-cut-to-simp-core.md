---
name: duocad-topopt-scope-cut-to-simp-core
description: 判断(2026-08-29) — duocadに載せるGenerative Design機能の範囲を確定。複数outcome・クラウド・製造制約・メッシュ→B-rep復元をすべて落とし、線形静解析FEA+SIMPだけを作る。出力はメッシュ止まりで3Dプリント直行
created: 2026-08-29
updated: 2026-08-31
---

# duocadのGDは「FEA+SIMP」だけに絞る

## 発端（2026-08-29・志向）

Fusion360からduocadへの移行中、Fusion360のGenerative Design的な機能をduocadでもやりたい、と本人が表明した。実装方針についての本人の明言: **フルスクラッチも「楽しそう」だが「ちょっと難易度が高い」** — 既存ライブラリを土台にする方向を望んでいる。GDは実際に使っていた機能である（[[fusion-gd-step-export-unit-cm]] にSTEP出力の実績）。

## 範囲の確定（2026-08-29・判断）

Fusion 360のGenerative Designを5要素（①線形静解析FEA ②SIMP密度法トポロジー最適化 ③製造方法別の幾何制約 ④複数outcome同時探索+クラウド課金 ⑤結果メッシュ→T-spline変換）に分解して示したところ、本人が範囲を確定した:

- **④は不要。**
- **③は現時点では不要。**
- **⑤も不要** — 「現段階では3DP直行を前提にメッシュ出力で問題ない」（本人明言）。裏付けは [[no-oss-mesh-to-editable-brep]]。

作るのは **①+② だけ**。

あわせて、実装上の3難所（(1)境界条件をCAD面に紐づけるUI (2)数分かかるジョブを同期前提のアプリに載せる非同期化 (3)結果を編集可能CADに戻せないこと）のうち、**(1)と(2)は「難しいが解決可能な問題なので無視する」と本人が判断した**（＝検証段階では境界条件をコードにプリセットし、同期実行のままでよい）。

検証の進め方も確定: **duocadルートに検証用フォルダを作り、そこで行う**（実体は `duocad/probe_topopt`）。まず形状最適化が正しく動くことを確認する。画面は**ビューポートとボタン1つだけ**で、押すと最適化の様子が見られる（最終結果だけでも可）。初期形状・荷重条件はすべてAI側でプリセットする。

関連: [[cad-migration-scope-shifted-to-app]] / [[topopt-probe-includes-face-anchoring]] / [[duocad-optimize-mode-contract]]
