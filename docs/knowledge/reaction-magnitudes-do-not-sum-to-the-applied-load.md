---
name: reaction-magnitudes-do-not-sum-to-the-applied-load
description: 実測(2026-09-11・duocad topopt) — 拘束面ごとの反力ベクトルの和は荷重の合力の符号反転に厳密一致する(相対誤差8.6e-14)が、「大きさ」の合計は荷重の合力を超える(取付点どうしが引き合う分が乗る。hanger_test 3面で0.97/0.97/1.90、合計3.85)。fraction(各面の反力の大きさ/荷重の合力の大きさ)は合計1を期待して読むと必ず間違える
created: 2026-09-11
updated: 2026-09-11
---

# 反力の「大きさ」の合計は荷重を超える(2026-09-11実測)

duocad topoptの反力の内訳(2026-09-11実装・実測)。

全グループの反力**ベクトル**の和は荷重の合力の符号を反転したものに厳密に一致する(剛体並進がKの零空間にあることから従う。実測の相対誤差8.6e-14)。しかし**大きさの和はそれ以上になる** — 取付点どうしが引き合っていれば打ち消し合うぶんが乗る。

hanger_test(拘束面3枚)の実測では、荷重の合力を1としたときの各面の受け持ちが**0.97 / 0.97 / 1.90(合計3.85)**。1枚だけで荷重の1.9倍を受け持っている。

`fraction`(= 各面の反力の大きさ / 荷重の合力の大きさ)は「割合」という名前だが**合計が1になる量ではない**。

関連: [[reaction-constraint-forces-a-load-path-at-any-epsilon]] / [[reaction-constraint-needs-one-adjoint-per-group]] / [[hanger-test-is-the-verification-model]]
