"""テストが使うSTEPのパス — **1箇所にまとめる**(2026-08-15)。

15のテストファイルが `Path(r"C:\\Users\\tm976\\mf\\3d-models")` を各自で書いていた
(JS側の検査は先に `web/scripts/models.mjs` へ寄せてあったので、Python側だけ残っていた)。
モデルを動かしたときに直す場所が1つで済む、というだけでなく、
**「どのモデルが何面・何ソリッドか」という実測値の置き場**でもある。

いずれも**読み取り専用**。テストはここから読むだけで、書き換えない。
"""
from pathlib import Path

#: 実物のSTEPを置いてある場所(リポジトリの外。3Dプリント用の設計データそのもの)。
MODELS = Path(r"C:\Users\tm976\mf\3d-models")

#: 棚取付具。M0から使っている基準の形状。
#: **17個の独立したソリッド**(ネジ・スペーサー類の集まり)で、面56 = 平面34 + 全周円筒22、
#: 体積 91018.2223598035 mm³、辺66(重複除去)。2026-08-10にOCPで直接実測。
SHELF = MODELS / "shelf_attachment_GD.step"
SHELF_SOLIDS = 17

#: PCケース(mATX)。**27部品・2835面**。規模の側の基準。
PC_CASE = MODELS / "mATX_PC_case_20251122" / "PC_case_20251122.step"
PC_CASE_SOLIDS = 27
PC_CASE_FACES = 2835

#: PCケースの脚。**5面**(平面3 + 全周円筒1 + 円錐1)で、実物として最小。
PC_CASE_FEET = MODELS / "mATX_PC_case_20251122" / "PC_case_feet.step"

#: 積層プレート。74面・単一ソリッド(spline 8面はフィレットの副産物)。
STACKABLE_PLATE = MODELS / "stackable_plate" / "plate_1by1_24.step"
