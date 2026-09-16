"""duocad.topopt — 形状最適化(SIMP トポロジー最適化)のコア。

**このパッケージから検証側**(`topopt_checks/` / `web/scripts/`)を import しないこと ——
製品が検査の道具に依存すると、道具を捨てるにも直すにも製品が巻き添えになる。

層をまたぐ契約:
    simp        — 数値の中心。**`bench` も import しない**(単体で持ち出せる状態を保つ)
    fem         — 二値FEMの格子・剛性・線形解法(要素剛性・節点番号・行列組立・CG/direct/MG・残差検算)
    voxelize    — B-rep → 格子。`duocad.kernel` 越しに OCP を呼ぶ
    anchor      — CADの面 → 格子の節点
    design_space— 4条件 → 塊(設計空間)の導出と端から端までの実行
    stress      — von Mises 応力・安全率(後処理)
    material    — 許容応力・ヤング率・印刷低減係数の唯一の定義
    acceptance  — 島除去後・平滑化前の6条件(幾何・CAD・境界条件・FEM)。合否の型
    safety_loop — 安全率を入力に材料量を二分探索する外側ループ
    mesh        — marching cubes + Taubin平滑化 + STL書き出し
    bench       — 受入検査の題材の大きさ

**サブモジュールをここで import しない。** `import duocad` は verb 登録のために
`duocad/__init__.py` から連鎖するので、ここで scipy / skimage / build123d を引き込むと
pytest 全体が毎回それを払うことになる。呼ぶ側が
`from duocad.topopt import design_space` のように名指しする。

**この規律の退行を検査するなら、見るのは `skimage` と `duocad.topopt` であって scipy ではない。**
`import duocad` だけなら numpy も scipy も来ないが、`import duocad.view` で numpy と scipy が来る
(2026-09-05 実測)ので、scipy は指標にならない(`tests/test_optimize_backend.py` 8)。
"""
from pathlib import Path

# 成果物(STL・測定JSON)の置き場。**呼び出し側の作業ディレクトリに依存させない** ——
# 相対パスで書くと runner が別のディレクトリから回したときにゴミが落ちる
# (実際に `taskbox/queue/output/` へ落ちたことがある)。
OUTPUT_DIR = Path(__file__).resolve().parent / "output"

__all__ = ["OUTPUT_DIR"]
