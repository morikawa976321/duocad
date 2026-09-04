"""受入検査スクリプトから `duocad.topopt` を import できるようにする。

**この11本は pytest に入れない**(通しで約200秒。`tests/` へ入れると `pytest -q` が毎回
それだけ重くなる。マーカーで既定から外す案は採らない —— duocad に CI が無いので、既定から
外した瞬間に誰も回さなくなる)。したがって素の `python check_*.py` で走る必要があり、
そのときリポジトリ直下は sys.path に入っていない(入るのはこのスクリプトの置き場だけ)。

**`duocad/` の外に置いてある**のは `tests/` と同じ理由で、`tests/test_boundaries.py` が
`duocad/` 配下を再帰的に走査して「OCP/build123d を直接 import してよいのは kernel.py /
query.py だけ」を強制するからである。検査は合成ソリッドを build123d で組むので、
`duocad/` の中に置くとその境界を名指しで免除させることになる(境界の設計意図は
「サブパッケージを作って逃げられない」ことなので、逃げ道を作らない)。

各検査は本体の import より前に `import _bootstrap` を1行置く。
"""
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
