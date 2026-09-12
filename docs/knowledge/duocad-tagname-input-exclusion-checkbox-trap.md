---
name: duocad-tagname-input-exclusion-checkbox-trap
description: duocadのキー入力除外判定で「tagName===INPUT」を全部除外すると、チェックボックス(type=checkbox)まで巻き込んで既存の実機検査が壊れる罠(2026-08-23実測)
created: 2026-08-23
updated: 2026-08-23
---

# tagName===INPUTだけでの除外はチェックボックスを巻き込む

duocad `web/src/App.svelte` の `onWindowKeydown`(Ctrl+Z/Ctrl+Yの文書undo/redo)を、
`CommandHost.onKeydown` のEnter除外(`['INPUT','TEXTAREA','BUTTON'].includes(tagName)`)と
同じ書き方で「除外するのは編集中のフィールドだけ」に直そうとしたとき、素直に
`['INPUT','TEXTAREA'].includes(target.tagName)` にすると、**パーツ一覧の可視性チェック
ボックス(`input type="checkbox"`)にフォーカスが残った状態でのCtrl+Zがundoしなくなり、
既存の実機検査(`web/scripts/smoke.mjs`)が無限待ちでFAILする。**

原因: チェックボックスもtagNameは`INPUT`だが、「打ち間違いを直す」の対象ではない
(テキストを編集していない)。正しい判定は**tagNameではなくinput typeで見る**:

```js
const isTextEditingField =
  target?.tagName === 'TEXTAREA' ||
  (target?.tagName === 'INPUT' && !['checkbox', 'radio'].includes(target.type))
```

**一般化できる教訓**: 「INPUT/TEXTAREAを除外」という書き方は、そのプロジェクトに
checkbox/radio型のINPUTが存在する時点で暗黙に間違っている可能性がある。既存コードの
除外判定(ここでは`CommandHost.onKeydown`)を「同じ書き方に揃える」よう指示された場合も、
**用途が違えば除外対象の集合まで機械的に一致させてよいとは限らない**——Enterの除外は
「フォームを誤って送信させない」目的でBUTTON/チェックボックス問わず全INPUTを除外して
正しいが、Ctrl+Zの除外は「テキスト編集中のブラウザ標準undoを奪わない」目的なので、
テキストを持たない入力要素(checkbox/radio/button)は除外しない方が正しい。**目的の違いを
確認してから判定式を移植すること。**

検出経路: 変更後に自分でstash/rebuildして負のコントロール(バグを再現させた版)を走らせ、
既存smokeテストが壊れることで発見した。**「同じ書き方に揃えよ」という指示があっても、
既存の実機検査群を必ず再実行して確認する**(タスク20260823-0048-duocad-u2-ctrlz-input-fields)。
