---
name: duocad-ui-interaction-principles
description: duocadのUI設計指針3件(実物クリックできるものにボタンを作らない/verb→target→howの順序統一/省けるクリックは徹底的に省く)とEnter要否の判定規則。8b(AI提案→人承認)の中心性の崩壊を受け入れる
created: 2026-08-22
updated: 2026-08-22
---

# duocad UI設計指針3件とEnter規則(2026-08-22本人決定)

**指針**:

1. **viewport上で実物をクリックして選択できるものは、別途ボタンを用意しない**
2. **すべての操作を verb→target→how の順序に統一する**
3. **省けるクリックは徹底的に省く**(スケッチ操作のEnterはじめ)

**Enter要否の判定規則**(指針3の具体化): 次のいずれかに当てはまる操作はEnterで適用する —
(1) targetが複数選択可能 (2) howの入力(数値・選択肢・パス)が要る。
**どちらにも当てはまらなければEnterなしで即適用**(最後のクリックが確定を兼ねる)。

**8bの崩壊を受け入れる**: 実装計画書§8bの中心機構「AIが選択を提案→人が承認してverb起動」は
この統一で中心性を失う。AIの動作規則:
- **Enter不要の操作** — 選択の承認なしにAIが直接実行してよい
- **Enter必要の操作** — 選択提案が可能(可能というだけであり、適用までしてもよい。
  提案で止めるか適用までするかは**AIがプロンプトから自律的に判断する**)

機構面ではMCPに `commit_selection` が既にあり、AIの直接適用は最初から可能だった。
変わるのは方針・プロンプト・文書の側。

関連: [[duocad-sketch-start-is-two-steps-by-pointing]]、[[cad-manual-ai-division-principle]]、
[[duocad-intentional-duplications]](指針1と衝突する可能性 — 領域の一覧クリックは要再検討)
