"""duocad.topopt — 形状最適化(SIMP トポロジー最適化)のコア。

2026-09-05 に実験場 `probe_topopt/` から移設し、**同じ日に実験場そのものを削除した**
(中身は `03bf794` までの履歴にある)。移設したときの規律は**依存の向きは実験場 → 本体の
一方向**で、本体からは一切参照しないことだった。

**この規律は移設先でも生きている。** このパッケージから**検証側**
(`topopt_checks/` / `web/scripts/`)を import しないこと —— 製品が検査の道具に依存すると、
道具を捨てるにも直すにも製品が巻き添えになる(実験場を消せたのはこの向きを守っていたため)。

層をまたぐ契約:
    simp        — 数値の中心。**`bench` も `perf` も import しない**(単体で持ち出せる状態を保つ)
    voxelize    — B-rep → 格子。`duocad.kernel` 越しに OCP を呼ぶ
    anchor      — CADの面 → 格子の節点
    design_space— 4条件 → 塊(設計空間)の導出と端から端までの実行
    stress      — von Mises 応力・安全率(後処理)
    safety_loop — 安全率を入力に材料量を二分探索する外側ループ
    mesh        — marching cubes + Taubin平滑化 + STL書き出し
    bench / perf— 受入検査の題材の大きさ(bench)と性能の基準線(perf)

**サブモジュールをここで import しない。** `import duocad` は verb 登録のために
`duocad/__init__.py` から連鎖するので、ここで scipy / skimage / build123d を引き込むと
pytest 全体が毎回それを払うことになる。呼ぶ側が
`from duocad.topopt import design_space` のように名指しする。
"""
from pathlib import Path

# 成果物(STL・測定JSON)の置き場。**呼び出し側の作業ディレクトリに依存させない** ——
# 相対パスで書くと runner が別のディレクトリから回したときにゴミが落ちる
# (2026-09-04に `taskbox/queue/output/` へ実際に落ちた)。
OUTPUT_DIR = Path(__file__).resolve().parent / "output"

__all__ = ["OUTPUT_DIR"]
