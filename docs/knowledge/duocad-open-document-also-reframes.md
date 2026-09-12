---
name: duocad-open-document-also-reframes
description: 判断+実装(2026-08-30) — duocadで文書を開く(open_document)ときもimport_stepと同じくビューポートを全体が入る視点へ合わせ直す。new_document(空文書)は対象外
created: 2026-08-30
updated: 2026-08-30
---

duocadでSTEPインポート時の再フレーミングを実装したタスク(`20260830-1534-duocad-reframe-on-import`)
で、「文書のスケールが不連続に変わるのは import_step と open_document だけ」という
タスク記載の前提を**実機で検証**し、`open_document` にも同じ再フレーミングを足した。

**実測手順**: 小さな立方体を保存(`small.json`)→新規文書→大きな板を保存(`big.json`)→
`small.json` を開く→`big.json` を開く、という順で `window.__duocad.cameraPose()` を見た。
修正前は `big.json` を開いてもカメラが `small.json` のときの位置のままで、外接箱の8頂点の
半分以上が画面外に投影された。`viewport?.frameAll()` を `open_document` の応答分岐
(`applied.change === 'document' && applied.name !== null`)に足すと、カメラが実際に
`big.json` の中心へ動き、全頂点が画面内に収まった。

**`new_document`(`applied.name === null`)は対象外にした** —— 空文書には合わせる内容が
無く、`frameToContent()` は箱が空のまま何もしないので実害は無いが、完了条件の文言
「インポートとopen_documentだけ」に忠実に、対象を開いた場合だけへ絞った。

**再フレーミングを他の通常編集(押し出し・フィレット等)へは広げていない** —— 2026-08-23の
「全体ボタン削除」裁定は撤回していない。

関連: [[duocad-history-panel-removed-from-ui]](画面のボタンを削らずに自動で解決する方針の同系統)
