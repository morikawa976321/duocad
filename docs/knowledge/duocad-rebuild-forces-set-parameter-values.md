---
name: duocad-rebuild-forces-set-parameter-values
description: 落とし穴+修正(2026-08-30) — duocadの`parameters.rebuild`は`set_parameter`の最終値を毎オペレーション後に強制し続けるので、後から積んだverbが同じ名前つき値へ書いた結果が再生のたびに黙って消える。強制範囲をそのseqより前だけに限定して解消
created: 2026-08-30
updated: 2026-08-30
---

# rebuildの「値の強制」は後から書いたverbを黙って消す

## 事実(2026-08-30実測)

`duocad/parameters.py` の `rebuild()` は、範囲内の `set_parameter` を先に走査して最終値の表を作り、
**再生する各オペレーションの直後にその表を当て直していた**。これは「`set_parameter` はログの
上では影響する図形より後ろに載るので、素直に順番どおり再生すると古い値で形ができる」を
避けるための仕組みだった。

ところが `move_sketch_entities`(名前つき値へ直接書くverb)を足すと、**その set_parameter より
後ろに積まれた move の書き込みが、次の rebuild のたびに上書きされて消える**。
症状は例外もログも出ない「移動がなかったことになる」で、再生を挟むまで気づけない。

## 修正

強制を **その名前を最後に書いた `set_parameter` の seq より前のオペレーションだけ**に限定した
(`forced_before`)。当の `set_parameter` を再生し終えた後は自然な順序に任せる ——
通り過ぎた時点で `_do_set_parameter` が同じ値を入れているので、既存の挙動は変わらない。
`tests/test_move_verbs.py::test_a_later_set_parameter_does_not_erase_the_move` が回帰を止める
(修正を戻すと実際に赤くなることを確認済み)。

## 一般化

**「ログの後ろにある宣言的な値を、再生中ずっと強制する」形は、その値を書く別のverbが増えた
瞬間に壊れる。** 強制するなら「まだ再生していない分を先に当てる」に限ること。
