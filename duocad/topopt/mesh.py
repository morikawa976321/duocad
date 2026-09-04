"""密度場(ボクセル)を滑らかな三角形メッシュへ変換する。

パイプライン(この順序で。段を飛ばさない):

0. **ガウシアン平滑化+塗り直し**(2026-09-05追加) — 密度場の**コピー**を
   `scipy.ndimage.gaussian_filter` で均し、保持マスク(`keep_mask`)を渡されていれば
   ρ=1へ、除外・設計空間外マスク(`zero_mask`)を渡されていればρ=0へ塗り直す。
   marching cubesの**前**に密度場側でうねりを消すのがこの段の目的 — Taubin(メッシュ側の
   低域通過)は中周波(k≈0.25〜0.5)の除去効率が悪く、marching cubes出力に残ったうねりを
   目立たせたまま反復数を増やしても取り切れない(伝達関数の実測。詳細は
   `docs/TOPOPT.md`「密度場側のガウシアン平滑化」)。ぼかすと保持・除外・設計空間の外が
   すべてにじむので、ぼかした**後**に塗り直す — `docs/TOPOPT.md`「パッシブ要素は密度フィルタ
   の後で上書きする」(設計変数フィルタのにじみ対策)と同型の問題への、同じ手当て。
   マスクを渡さなければ塗り直しはしない(`mesh.py`のCLIと`check_mesh.py`は片持ち梁の
   密度場を直接渡すので保持・除外の概念自体を持たない)。
1. **浮島の除去** — 二値化した場で `scipy.ndimage.label` を取り、最大連結成分だけを残す。
   連続場のほうを直接ゼロで塗り、後段(marching cubes)には二値化していない連続値を渡す。
   **段0の出力(平滑化+塗り直し後の場)に対して行う** — 塗り直しで保持ソリッドが
   本体から浮いた孤立領域になっていたら、ここで拾われずに消える(2026-09-05時点では
   保持ソリッドは通常、荷重伝達のため本体と連結しているはずという前提。孤立浮島に
   なる保持の扱いは別タスク)。
2. **ゼロの縁を1層足す** — 材料が設計空間の端に接しているとメッシュが開き、水密でない
   (=スライサに通らない)STLになる。
3. **marching cubes** — `skimage.measure.marching_cubes` に連続値の密度場を渡す。
   **ここに二値化した配列を渡してはいけない**。二値化すると辺上の全ての端点が0/1になり、
   線形補間が必ず中点(0.5)に落ちて半セルずれた階段が戻ってくる — この経路の価値は
   「補間で面の位置が要素サイズより細かく決まる」ことにあるので、ここを間違えると
   marching cubesを使う意味が消える。`check_mesh.py` の検査5がこれを機械で見張る。
4. **Taubin平滑化** — 自前実装(scipyの疎行列でラプラシアンを1度作るだけ)。
   `pyvista` / `vtk` / `pymeshlab` は使わない。段0が中周波のうねりを密度場側で
   ほぼ消しているので、ここでの反復数は「格子ファセット(marching cubesの階段状の
   高周波成分)を取る」だけで足りる(下記 `DEFAULT_ITERS` 参照)。
5. **STL書き出し** — 自前のバイナリSTLライタ(依存追加なし)。

`faces` の巻き順は3の直後に**外向き規約**(`(v1-v0)x(v2-v0)` が外向き法線)へ揃える。
以後この配列を受け取る側 — STL書き出し・ブラウザ描画(`probe_topopt/server.py` → three.js) — は
どちらも入れ替えを行わない。

**応力・安全率は段0より前(平滑化していない密度場)の上で計算されている。** 出力する形状
(平滑化後)と評価した形状(平滑化前)は厳密には一致しない — σ=1要素程度なら差は小さいはずだが、
測ってはいないので「小さい」とは言い切らない(利用者への留保は `docs/TOPOPT.md` 参照)。

新規依存は `scikit-image`(marching cubes)だけ。インストールコマンドは
`docs/TOPOPT.md` の「ファイル」節を参照。
"""

from __future__ import annotations

import argparse
import struct
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import scipy.sparse as sp
from scipy import ndimage
from scipy.spatial import cKDTree
from skimage.measure import marching_cubes

_ROOT = Path(__file__).resolve().parent

# Taubin平滑化の既定値(タスク指定)。level は等値の太さを決める設計の摘みなので
# 定数で埋め込まず、常に引数として外に出す。
DEFAULT_LEVEL = 0.5
DEFAULT_LAMBDA = 0.5
DEFAULT_MU = -0.53
# 2026-09-05: 30 → 12(密度場側のガウシアン平滑化(下記 DEFAULT_SMOOTH_SIGMA)が中周波の
# うねりを引き受けたので、Taubin側は格子ファセット(k≥1)を取るだけで足りる。
# k≥1 の伝達関数 (1-λk)(1-μk)=0.765 の12乗で 0.765^12 ≈ 4.0% まで落ちる)。
# **これ自体は未検証の決め打ち値** — 2026-09-05に本人判断で、探索・比較をせずに
# そのまま採用した。実測の根拠は無い(下の DEFAULT_SMOOTH_SIGMA と同じ扱い)。
DEFAULT_ITERS = 12
DEFAULT_SPACING = (1.0, 1.0, 1.0)  # [mm]。1要素=1mmという既存の描画規約(probe_topopt/web/index.html の
# BoxGeometry(1,1,1))に合わせた。probe_topoptに他の物理スケールの定義は無い。
DEFAULT_PAD = 1
# 密度場ガウシアン平滑化のσ[要素]。**2026-09-05に本人判断で未検証のまま決め打ちした値。
# 実測の根拠は無い**(このモジュールの他の既定値 — λ/μ/level — はいずれも実測か伝達関数の
# 計算で裏付けがあるが、σとTaubin反復数(上のDEFAULT_ITERS)の2つだけはこの原則の例外。
# 探索・比較表は作らない、というのが本人の明示指示 — `docs/TOPOPT.md` 参照)。
DEFAULT_SMOOTH_SIGMA = 1.0


# ---------------------------------------------------------------------------
# 0. ガウシアン平滑化 + 保持/除外の塗り直し
# ---------------------------------------------------------------------------
def smooth_density(
    rho: np.ndarray,
    *,
    sigma: float | None = DEFAULT_SMOOTH_SIGMA,
    keep_mask: np.ndarray | None = None,
    zero_mask: np.ndarray | None = None,
) -> np.ndarray:
    """密度場の**コピー**をガウシアンで均し、保持・除外/設計空間外を塗り直す。

    `rho` は書き換えない — `Result.rho` を平滑化の入力にそのまま渡しても、
    このコピーだけが変わる(応力・安全率・体積の評価は呼び出し側が別に持つ
    `rho` の上でそのまま行われる。段を跨いで書き換えると出力用の場の上で
    評価が行われることになる)。

    `sigma` が真値(0でない)なら `scipy.ndimage.gaussian_filter` を1回掛ける。
    `keep_mask` を渡せばその要素をρ=1へ、`zero_mask` を渡せばその要素をρ=0へ、
    **平滑化の後に**塗り直す(にじみの向きは保持→材料が増える側、除外→減る側で
    非対称なので、順序が逆だと塗り直しの意味が無い)。どちらも渡さなければ
    塗り直しはしない。両方に含まれる要素があれば `keep_mask` が勝つ(塗り直しの
    適用順で決まる — `keep_mask` を先に書くと後の `zero_mask` が上書きしてしまうので、
    実装は `zero_mask` を先に適用し `keep_mask` を後に適用する)。
    """
    field = np.array(rho, dtype=np.float64, copy=True)
    if sigma:
        field = ndimage.gaussian_filter(field, sigma=sigma)
    if zero_mask is not None:
        field[np.asarray(zero_mask, dtype=bool)] = 0.0
    if keep_mask is not None:
        field[np.asarray(keep_mask, dtype=bool)] = 1.0
    return field


# ---------------------------------------------------------------------------
# 1. 浮島の除去
# ---------------------------------------------------------------------------
def remove_islands(rho: np.ndarray, threshold: float = DEFAULT_LEVEL) -> np.ndarray:
    """二値化(`rho > threshold`)した場の最大連結成分だけを残す。

    **返すのは連続値の場**(二値化した配列そのものではない)。連結成分の判定にだけ
    二値化を使い、生き残った領域の値はもとの連続値のまま返す。
    """
    binary = rho > threshold
    if not binary.any():
        return np.zeros_like(rho)
    labels, n_labels = ndimage.label(binary)
    if n_labels <= 1:
        return rho.copy()
    counts = np.bincount(labels.ravel())
    counts[0] = 0  # 背景(ラベル0)は連結成分として数えない
    largest = int(np.argmax(counts))
    out = np.zeros_like(rho)
    mask = labels == largest
    out[mask] = rho[mask]
    return out


def pad_field(rho: np.ndarray, width: int = DEFAULT_PAD) -> np.ndarray:
    """ゼロで `width` 層だけ縁を足す。材料が設計空間の端に接していても水密なメッシュになる。"""
    return np.pad(rho, width, mode="constant", constant_values=0.0)


# ---------------------------------------------------------------------------
# 3. marching cubes
# ---------------------------------------------------------------------------
def run_marching_cubes(
    rho_padded: np.ndarray,
    *,
    level: float = DEFAULT_LEVEL,
    spacing: tuple[float, float, float] = DEFAULT_SPACING,
    pad_width: int = DEFAULT_PAD,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """`rho_padded`(連続値・パッド済み)から (verts, faces, interpolation_ratios) を返す。

    座標は「パディングしていない元の格子」の**セル中心規約**に戻す。`rho[ex]` は要素 `ex` の
    **中心**の値であり(`probe_topopt/web/index.html` のボクセル描画も `ex+0.5-nelx/2` でこの規約に
    合わせている)、パッド済み配列の添字 `i` は元の添字 `ex = i - pad_width` に対応する。
    `marching_cubes` は添字 `i` をそのまま `i*spacing` の位置とみなして頂点を置くので、
    それを中心規約の座標 `(ex+0.5)*spacing = (i-pad_width+0.5)*spacing` へ戻すには
    `(pad_width-0.5)*spacing` を引けばよい。**`pad_width*spacing` を引くと0.5要素ぶん
    ずれる**(2026-08-30 QAで発見・修正。engine `marching-cubes-cell-center-half-offset`)。
    `level` は引数(等値の太さを決める設計の摘み。定数で埋め込まない)。

    **`interpolation_ratios` はオフセットを引く前(marching_cubes直後の生座標)で計算する。**
    生座標は「2軸が整数格子点・1軸だけが端点間の線形補間値」という
    `edge_interpolation_ratios` の前提どおりに整数*spacingへ乗っている。オフセット後の
    座標は半要素ぶん(整数ではなく半整数)ずれるため、offset適用後に計算すると
    軸判定が丸ごと反転し「常に0.5」という誤った結果になる
    (2026-08-30、セル中心オフセットの修正と同時に実測して発見)。

    **巻き順もここで直す。** `skimage.measure.marching_cubes` が返す巻き順は、
    `(v1-v0)x(v2-v0)` を外向き法線とする一般的な規約(STL/OCCT/three.js)に対して逆向き
    (実測: 単純な直方体で発散定理の符号付き体積が負になる)。列を `[0, 2, 1]` へ
    入れ替えて**生成側で**規約に合わせ、以後 `faces` を受け取る側は全て外向きを
    前提にしてよいことにする。**書き出し直前で直してはいけない** — 2026-08-30の対策が
    それで、消費者がSTLだけのうちは正しかったが、`faces` を直接ブラウザへ送る経路
    (`probe_topopt/server.py` → three.js の `computeVertexNormals`)が増えた時点で、そちらが
    内向き法線のまま残った(2026-09-02発見。engine
    `winding-fix-at-export-only-leaks-to-other-consumers`)。`check_mesh.py` の検査9が
    これを機械で見張る。
    """
    verts, faces, _normals, _values = marching_cubes(rho_padded, level=level, spacing=spacing)
    faces = faces[:, (0, 2, 1)]
    interpolation_ratios = edge_interpolation_ratios(verts, spacing=spacing)
    offset = np.asarray(spacing, dtype=float) * (pad_width - 0.5)
    verts = verts - offset
    return verts, faces, interpolation_ratios


# ---------------------------------------------------------------------------
# 4. Taubin平滑化(自前)
# ---------------------------------------------------------------------------
def build_adjacency(n_verts: int, faces: np.ndarray) -> sp.csr_matrix:
    """三角形の辺から頂点隣接行列(重みなし・対称・対角0)を1度だけ作る。"""
    i0, i1, i2 = faces[:, 0], faces[:, 1], faces[:, 2]
    rows = np.concatenate([i0, i1, i1, i2, i2, i0])
    cols = np.concatenate([i1, i0, i2, i1, i0, i2])
    data = np.ones(rows.size, dtype=np.float64)
    a = sp.coo_matrix((data, (rows, cols)), shape=(n_verts, n_verts)).tocsr()
    a.data[:] = 1.0  # 同じ辺が複数三角形で重複しても重みは1のまま(隣接の有無だけを見る)
    a.sum_duplicates()
    a.data[:] = 1.0
    return a


def _discrete_laplacian(n_verts: int, faces: np.ndarray) -> sp.csr_matrix:
    """離散ラプラシアン `L = D^-1 A - I`。`L @ v` は「近傍の平均 - 自分」。"""
    a = build_adjacency(n_verts, faces)
    deg = np.asarray(a.sum(axis=1)).ravel()
    d_inv = np.zeros_like(deg)
    nz = deg > 0
    d_inv[nz] = 1.0 / deg[nz]
    d_inv_mat = sp.diags(d_inv)
    return d_inv_mat @ a - sp.eye(n_verts, format="csr")


def taubin_smooth(
    verts: np.ndarray,
    faces: np.ndarray,
    *,
    lam: float = DEFAULT_LAMBDA,
    mu: float = DEFAULT_MU,
    n_iter: int = DEFAULT_ITERS,
) -> np.ndarray:
    """Taubin(λ|μ)平滑化。`|μ| > λ` でないと通過帯域周波数 `1/λ + 1/μ` が負になり、
    ただのLaplacianへ退化して形が縮む(タスクの必須条件)。
    """
    if not (mu < 0.0 and abs(mu) > lam):
        raise ValueError(f"|mu| > lam でなければならない(lam={lam}, mu={mu})")
    lap = _discrete_laplacian(verts.shape[0], faces)
    v = verts.astype(np.float64).copy()
    for _ in range(n_iter):
        v = v + lam * (lap @ v)
        v = v + mu * (lap @ v)
    return v


def laplacian_smooth(
    verts: np.ndarray,
    faces: np.ndarray,
    *,
    lam: float = DEFAULT_LAMBDA,
    n_iter: int = DEFAULT_ITERS,
) -> np.ndarray:
    """素のLaplacian平滑化(戻すμパスが無い)。Taubinとの比較専用 —
    `check_mesh.py` の検査4(「Taubinと名乗っているが実はLaplacian」の検出)がこれを使う。
    """
    lap = _discrete_laplacian(verts.shape[0], faces)
    v = verts.astype(np.float64).copy()
    for _ in range(n_iter):
        v = v + lam * (lap @ v)
    return v


# ---------------------------------------------------------------------------
# 形状の指標
# ---------------------------------------------------------------------------
def mesh_volume(verts: np.ndarray, faces: np.ndarray) -> float:
    """発散定理による閉曲面の体積(符号は問わず絶対値を返す)。"""
    tri = verts[faces]
    v0, v1, v2 = tri[:, 0], tri[:, 1], tri[:, 2]
    signed = np.einsum("ij,ij->i", v0, np.cross(v1, v2)).sum() / 6.0
    return float(abs(signed))


def face_normals(verts: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """正規化していない面法線(大きさ=2×面積)。"""
    tri = verts[faces]
    return np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])


def vertex_normals(verts: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """面積重み付けの頂点法線(正規化済み)。"""
    fn = face_normals(verts, faces)
    vn = np.zeros_like(verts)
    for k in range(3):
        np.add.at(vn, faces[:, k], fn)
    norms = np.linalg.norm(vn, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return vn / norms


def edge_interpolation_ratios(
    verts: np.ndarray, spacing: tuple[float, float, float] = DEFAULT_SPACING
) -> np.ndarray:
    """marching cubesが辺上に置いた頂点について、辺上の内分比(0〜1)を返す。

    marching cubesの頂点は必ず格子の辺(隣接2節点を結ぶ線分)の上にある。3軸のうち
    2軸は整数格子点に一致し、残り1軸だけが端点間を線形補間した値になる。**その軸の
    小数部**が内分比(0.5なら中点)。二値化した場を渡すと、辺の両端が必ず0/1になり
    線形補間が常に中点へ落ちるので、この分布が0.5に集中する(検査5が見ているもの)。
    """
    idx = verts / np.asarray(spacing, dtype=float)
    frac = idx - np.floor(idx)
    dist_to_int = np.minimum(frac, 1.0 - frac)
    axis = np.argmax(dist_to_int, axis=1)
    rows = np.arange(verts.shape[0])
    return frac[rows, axis]


def min_wall_thickness(
    verts: np.ndarray,
    faces: np.ndarray,
    *,
    k: int = 20,
    normal_dot_max: float = -0.3,
) -> float:
    """最小肉厚の近似値。

    各頂点について、法線がほぼ逆向き(内積 < `normal_dot_max`)な近傍点との距離の
    最小値を取る — 薄い壁の表と裏はほぼ逆向きの法線で近接しているので、この値が
    壁の厚みの近似になる(3Dプリントの薄肉検出でよく使われる考え方)。
    """
    if verts.shape[0] < 2:
        return float("inf")
    normals = vertex_normals(verts, faces)
    tree = cKDTree(verts)
    kk = min(k + 1, verts.shape[0])
    dist, idx = tree.query(verts, k=kk)
    dist = np.atleast_2d(dist)[:, 1:]
    idx = np.atleast_2d(idx)[:, 1:]
    dots = np.einsum("ij,ikj->ik", normals, normals[idx])
    candidate = np.where(dots < normal_dot_max, dist, np.inf)
    per_vertex_min = candidate.min(axis=1)
    finite = per_vertex_min[np.isfinite(per_vertex_min)]
    if finite.size == 0:
        return float("inf")
    return float(finite.min())


# ---------------------------------------------------------------------------
# 5. STL書き出し(自前バイナリライタ。新規依存なし)
# ---------------------------------------------------------------------------
def write_stl(path: str | Path, verts: np.ndarray, faces: np.ndarray) -> None:
    """バイナリSTL(80バイトヘッダ + uint32三角形数 + 三角形ごとに法線+3頂点+attr2バイト)。

    **`faces` の巻き順はここでは触らない。** 外向き規約へ合わせるのは
    `run_marching_cubes` の仕事(そちらのdocstring)であり、ここで再度 `[0,2,1]` へ
    入れ替えると二重反転してSTLだけが裏返る。書き出す法線も `faces` の巻き順から
    そのまま計算する。
    """
    verts32 = np.asarray(verts, dtype=np.float32)
    faces = np.asarray(faces, dtype=np.int64)
    tri = verts32[faces]
    normals = np.cross(
        tri[:, 1].astype(np.float64) - tri[:, 0].astype(np.float64),
        tri[:, 2].astype(np.float64) - tri[:, 0].astype(np.float64),
    )
    lens = np.linalg.norm(normals, axis=1, keepdims=True)
    lens[lens == 0.0] = 1.0
    normals32 = (normals / lens).astype(np.float32)

    dtype = np.dtype(
        [
            ("normal", "<f4", 3),
            ("verts", "<f4", (3, 3)),
            ("attr", "<u2"),
        ]
    )
    records = np.zeros(len(faces), dtype=dtype)
    records["normal"] = normals32
    records["verts"] = tri
    records["attr"] = 0

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        # 80バイトのヘッダは利用者のスライサーまで届く。**"solid" で始めないこと**
        # (ASCII STL と誤判定される)。
        f.write(b"duocad topopt: marching cubes + Taubin smoothing".ljust(80, b"\0")[:80])
        f.write(struct.pack("<I", len(faces)))
        f.write(records.tobytes())


# ---------------------------------------------------------------------------
# パイプライン全体
# ---------------------------------------------------------------------------
@dataclass
class MeshResult:
    rho_clean: np.ndarray
    verts_raw: np.ndarray
    faces: np.ndarray
    verts_smooth: np.ndarray
    volume_raw: float
    volume_smooth: float
    seconds: float
    level: float
    spacing: tuple[float, float, float]
    lam: float
    mu: float
    n_iter: int
    interpolation_ratios: np.ndarray
    smooth_sigma: float  # 段0で実際に使ったσ(0.0なら平滑化なし)。診断・検査用。


def build_surface(
    rho: np.ndarray,
    *,
    level: float = DEFAULT_LEVEL,
    spacing: tuple[float, float, float] = DEFAULT_SPACING,
    lam: float = DEFAULT_LAMBDA,
    mu: float = DEFAULT_MU,
    n_iter: int = DEFAULT_ITERS,
    pad_width: int = DEFAULT_PAD,
    smooth_sigma: float | None = DEFAULT_SMOOTH_SIGMA,
    keep_mask: np.ndarray | None = None,
    zero_mask: np.ndarray | None = None,
) -> MeshResult:
    """密度場から「ガウシアン平滑化+塗り直し→浮島除去→パディング→marching cubes→
    Taubin平滑化」までを一度に行う(モジュールdocstringの段0〜4)。

    `smooth_sigma` は既定で有効(`DEFAULT_SMOOTH_SIGMA`)。0 か None を渡すと段0の
    ガウシアンを切れる。`keep_mask` / `zero_mask` は `rho` と同じ形の bool 配列
    (保持ソリッド / 除外ソリッド・設計空間の外)。渡さなければ塗り直しはしない
    — 呼び出し側(`mesh.py` CLI・`check_mesh.py`)が保持・除外の概念を持たない
    片持ち梁の密度場を直接渡す経路がそれに当たる。
    """
    t0 = time.perf_counter()
    rho_smoothed = smooth_density(rho, sigma=smooth_sigma, keep_mask=keep_mask, zero_mask=zero_mask)
    rho_clean = remove_islands(rho_smoothed, threshold=level)
    padded = pad_field(rho_clean, width=pad_width)
    verts_raw, faces, interpolation_ratios = run_marching_cubes(
        padded, level=level, spacing=spacing, pad_width=pad_width
    )
    verts_smooth = taubin_smooth(verts_raw, faces, lam=lam, mu=mu, n_iter=n_iter)
    seconds = time.perf_counter() - t0
    return MeshResult(
        rho_clean=rho_clean,
        verts_raw=verts_raw,
        faces=faces,
        verts_smooth=verts_smooth,
        volume_raw=mesh_volume(verts_raw, faces),
        volume_smooth=mesh_volume(verts_smooth, faces),
        seconds=seconds,
        level=level,
        spacing=spacing,
        lam=lam,
        mu=mu,
        n_iter=n_iter,
        interpolation_ratios=interpolation_ratios,
        smooth_sigma=float(smooth_sigma or 0.0),
    )


def _cli() -> int:
    import simp  # ここでのみ要る(SIMPを回して実データを作るのはCLI用途だけ)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--level", type=float, default=DEFAULT_LEVEL)
    parser.add_argument("--lam", type=float, default=DEFAULT_LAMBDA)
    parser.add_argument("--mu", type=float, default=DEFAULT_MU)
    parser.add_argument("--iters", type=int, default=DEFAULT_ITERS)
    parser.add_argument(
        "--smooth-sigma",
        type=float,
        default=DEFAULT_SMOOTH_SIGMA,
        help="密度場ガウシアン平滑化のσ[要素]。0で段0を切る(未検証の決め打ち既定 — モジュールdocstring参照)",
    )
    parser.add_argument("--out", default=str(_ROOT / "output" / "optimized.stl"))
    args = parser.parse_args()

    p = simp.PRESET
    print(
        f"SIMP最適化を実行中(プリセット {p['nelx']}x{p['nely']}x{p['nelz']} / "
        f"{p['max_iter']}反復)..."
    )
    t0 = time.perf_counter()
    result = simp.run_preset()
    simp_seconds = time.perf_counter() - t0
    print(f"  完了: {len(result.history)}反復 / {simp_seconds:.1f}秒")

    mr = build_surface(
        result.rho,
        level=args.level,
        lam=args.lam,
        mu=args.mu,
        n_iter=args.iters,
        smooth_sigma=args.smooth_sigma,
    )
    write_stl(args.out, mr.verts_smooth, mr.faces)
    out_path = Path(args.out)
    size_kb = out_path.stat().st_size / 1024.0

    print(f"  三角形数: {len(mr.faces)}")
    print(f"  頂点数: {len(mr.verts_smooth)}")
    print(f"  体積(平滑化前): {mr.volume_raw:.3f}")
    print(f"  体積(平滑化後): {mr.volume_smooth:.3f} "
          f"(変化 {100 * (mr.volume_smooth / mr.volume_raw - 1):+.2f}%)")
    thickness_before = min_wall_thickness(mr.verts_raw, mr.faces)
    thickness_after = min_wall_thickness(mr.verts_smooth, mr.faces)
    print(f"  最小肉厚(平滑化前): {thickness_before:.4f}")
    print(f"  最小肉厚(平滑化後): {thickness_after:.4f}")
    print(f"  メッシュ処理時間: {mr.seconds:.2f}秒")
    print(f"  STL: {out_path} ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
