"""探索が確定したマスクから、平滑化で潰れる 1×1 の空隙(管)を埋める。

四方が材料で閉じた空セル(2軸以上で両隣が材料)は Taubin 30回で壁の間隔が 0.1 要素未満まで潰れ、まれに壁どうしが
交差する(docs/TOPOPT_ACCEPTANCE.md「平滑化(Taubin)後の自己交差」の R1・R2。64条件の交差4箇所はすべて管の中)。1軸だけ挟まれた溝は
0.12 要素以上残り交差しないので埋めない。埋めた後の6条件(FEM)は `optimize_run` が `acceptance.inspect` で取り直す。
"""
from __future__ import annotations

import numpy as np

#: 埋める空セルの条件: 両隣が材料である軸がこの数以上(2 = 断面が四方とも閉じた管・窪み。1 は溝で、埋めない)。
TUBE_MIN_SANDWICHED_AXES = 2
#: 埋める反復の上限。埋めると隣の空セルが新たに挟まれることがある(64条件の実測で最大3回)。超えたら止めずに例外にする。
MAX_ROUNDS = 16


def sandwiched_axes(mask: np.ndarray) -> np.ndarray:
    """空セルごとに、その軸の両隣(±1セル)が材料である軸の数 (0..3)。格子の外は材料でない。材料セルは 0。"""
    material = np.asarray(mask, dtype=bool)
    padded = np.pad(material, 1, constant_values=False)
    count = np.zeros(material.shape, dtype=np.int8)
    for axis in range(3):
        low = np.roll(padded, 1, axis=axis)[1:-1, 1:-1, 1:-1]
        high = np.roll(padded, -1, axis=axis)[1:-1, 1:-1, 1:-1]
        count += low & high
    count[material] = 0
    return count


def fill_tubes(mask: np.ndarray, allowed: np.ndarray) -> tuple[np.ndarray, dict]:
    """`allowed`(設計空間。除外は引き切ってある)の中の管セルを、無くなるまで埋める。

    戻り値は `(filled, info)`。`filled` は複製(入力は変えない)、`info` は `dict(cells=埋めた総数, rounds=[各回の数])`。
    `allowed` の外の管(除外ソリッド由来の空隙)は埋めずに残す —— それが潰れて交差すれば `finalize` の入力検査が止める。
    """
    filled = np.array(mask, dtype=bool, copy=True)
    allowed = np.asarray(allowed, dtype=bool)
    if filled.shape != allowed.shape:
        raise ValueError(f'マスクと設計空間の形が違う: {filled.shape} / {allowed.shape}')
    rounds: list[int] = []
    while True:
        tube = (sandwiched_axes(filled) >= TUBE_MIN_SANDWICHED_AXES) & allowed
        count = int(tube.sum())
        if count == 0:
            return filled, dict(cells=int(sum(rounds)), rounds=rounds)
        if len(rounds) >= MAX_ROUNDS:
            raise RuntimeError(f'1×1 の空隙を {MAX_ROUNDS} 回埋めても残る(各回 {rounds}、残り {count} セル)')
        filled |= tube
        rounds.append(count)
