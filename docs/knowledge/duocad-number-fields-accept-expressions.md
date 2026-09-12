---
name: duocad-number-fields-accept-expressions
description: duocadの数値入力欄が数式を受け付けるようになった。三角関数は度、関数セットはユーザーが選んだ範囲
created: 2026-09-06
updated: 2026-09-06
---

duocadの数値入力欄(コマンドの引数欄・STL偏差・形状最適化の最小部材幅)が**数式を受け付ける**ようになった(2026-09-06)。実装は `web/src/expression.ts` の再帰下降パーサで、`eval`/`Function` は使わない。

## ユーザーが決めたこと

- **三角関数の引数は「度」**。`sin(30)` は 0.5。逆三角(`asin`/`acos`/`atan`/`atan2`)の戻り値も度。選択肢はラジアン・「度を既定＋ラジアン版を別名」もあったが度を選んだ。
- **関数セット**は「四則・単項マイナス・括弧・`sqrt`・`sin`/`cos`/`tan`」に加えて **`pi`・`asin`/`acos`/`atan`/`atan2`・`^`(べき乗)・`abs`** まで。提案した推奨セットのうち **`hypot`・`min`・`max`・`round`・`floor`・`ceil` は入れないと明示的に除外した**。`log`/`exp`/剰余も入れない。

## 併せて決まった仕様(実装側の判断)

- **末尾のゴミを拒否する**。`Number.parseFloat('50mm')` は 50 を、`'3 4'` は 3 を返すが、式評価は理由を返して拒む。
- 入力は NFKC 正規化する(日本語IMEの全角対策)。関数名は大文字小文字を問わない。
- **文書パラメータの参照(`width/2` のような変数)は入れていない**。`set_parameter` はMCP専用でUI側にパラメータ欄が無く、「式を保存して再評価する」話になるため別件として残した。次に伸ばすならここ。

関連: [[duocad-ui-interaction-principles]]
