"""密度場を三角形メッシュへ変換する。

保持・除外の塗り直し → 最大連結成分の抽出 → 外周パディング → marching cubes
→ Taubin平滑化の順に処理する。STL書き出しは write_stl を使う。
入力の密度場は変更しない。保持と除外が重なる場合は保持を優先する。
面の巻き順は外向きに揃え、描画・書き出し側では反転しない。

応力・安全率は入力の密度場で評価する。島除去・表面平滑化後のメッシュを
再解析した値ではない。実測と制限は docs/TOPOPT.md を参照。"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from . import trace
import scipy.sparse as sp
from scipy import ndimage
from scipy.spatial import cKDTree
from skimage.measure import marching_cubes

# Taubin平滑化の既定値(タスク指定)。level は等値の太さを決める設計の摘みなので
# 定数で埋め込まず、常に引数として外に出す。
DEFAULT_LEVEL = 0.5
DEFAULT_LAMBDA = 0.5
DEFAULT_MU = -0.53
# 30の根拠(docs/TOPOPT.md「メッシュ化(marching cubes + Taubin平滑化)」):
# 反復30で k≥1 の残りが 0.765^30 ≈ 0.03%、Taubin による体積変化は
# −0.003%(12反復の −0.21% より小さい)、最小肉厚は縮まない。
DEFAULT_ITERS = 30
DEFAULT_SPACING = (1.0, 1.0, 1.0)  # [mm]。1要素=1mmという既存の描画規約に合わせた値。
# **`optimize_run` はここへ実spacingを渡さない**(既定の要素添字空間のまま呼ぶ)。
# 実寸への変換は呼び出し側の `optimize_run._mesh_to_world`
# (`grid.origin + verts * grid.spacing`)が一括して行う契約 —— ここで実spacingを渡すと
# 二重に掛かってメッシュが格子の外へ大きく外れる(`build_surface_from_mask` の
# docstring・`optimize_run._run` のコメントに同じ注意)。
DEFAULT_PAD = 1
def apply_density_masks(
    rho: np.ndarray,
    *,
    keep_mask: np.ndarray | None = None,
    zero_mask: np.ndarray | None = None,
) -> np.ndarray:
    """入力をコピーし、除外を0、保持を1にする。両マスクが重なる場合は保持が優先。"""
    field = np.array(rho, dtype=np.float64, copy=True)
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
    **中心**の値であり(画面側のボクセル描画 —— `web/src/viewport.ts` の `buildDebugVoxels` ——
    もこの規約に合わせてある。**変換点はあそこ1箇所だけ**で、サーバは素の添字を送る)、
    パッド済み配列の添字 `i` は元の添字 `ex = i - pad_width` に対応する。
    `marching_cubes` は添字 `i` をそのまま `i*spacing` の位置とみなして頂点を置くので、
    それを中心規約の座標 `(ex+0.5)*spacing = (i-pad_width+0.5)*spacing` へ戻すには
    `(pad_width-0.5)*spacing` を引けばよい。**`pad_width*spacing` を引くと0.5要素ぶん
    ずれる**。
    `level` は引数(等値の太さを決める設計の摘み。定数で埋め込まない)。

    **`interpolation_ratios` はオフセットを引く前(marching_cubes直後の生座標)で計算する。**
    生座標は「2軸が整数格子点・1軸だけが端点間の線形補間値」という
    `edge_interpolation_ratios` の前提どおりに整数*spacingへ乗っている。オフセット後の
    座標は半要素ぶん(整数ではなく半整数)ずれるため、offset適用後に計算すると
    軸判定が丸ごと反転し「常に0.5」という誤った結果になる。

    **巻き順もここで直す。** `skimage.measure.marching_cubes` が返す巻き順は、
    `(v1-v0)x(v2-v0)` を外向き法線とする一般的な規約(STL/OCCT/three.js)に対して逆向き
    (実測: 単純な直方体で発散定理の符号付き体積が負になる)。列を `[0, 2, 1]` へ
    入れ替えて**生成側で**規約に合わせ、以後 `faces` を受け取る側は全て外向きを
    前提にしてよいことにする。**書き出し直前で直してはいけない** —— STL書き出しの
    直前でだけ直すと、`faces` を直接ブラウザへ送る経路(`duocad/optimize_run.py` →
    `add_mesh_body` → `web/src/viewport.ts`)は内向き法線のまま残る。`check_mesh.py` の検査9が
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
    for iteration in range(n_iter):
        v = v + lam * (lap @ v)
        trace.snapshot('taubin-lambda', iteration=iteration+1, vertices=v)
        v = v + mu * (lap @ v)
        trace.snapshot('taubin-mu', iteration=iteration+1, vertices=v)
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

    **返すのは最小値1つで、これだけでは1要素幅の筋(チェッカーボード)の有無を弁別できない** ——
    階段状メッシュの局所ノイズが逆向き法線の対を作り、1点でも薄い値を出せば最小値がそこへ
    張り付く(実測: 筋あり 0.0133要素 / 筋なし 0.0797要素で、どちらも「1要素よりはるかに薄い」)。
    弁別が要るなら `per_vertex_min` の分布の下側の裾(壁厚<1要素の頂点の割合。実測 17.29% 対
    0.79%)を見る(docs/TOPOPT.md「メッシュ化(marching cubes + Taubin平滑化)」)。平均・中央値では埋もれる。
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


@trace.stage('surface')
def build_surface(
    rho: np.ndarray,
    *,
    level: float = DEFAULT_LEVEL,
    spacing: tuple[float, float, float] = DEFAULT_SPACING,
    lam: float = DEFAULT_LAMBDA,
    mu: float = DEFAULT_MU,
    n_iter: int = DEFAULT_ITERS,
    pad_width: int = DEFAULT_PAD,
    keep_mask: np.ndarray | None = None,
    zero_mask: np.ndarray | None = None,
) -> MeshResult:
    """保持・除外を塗り直し、浮島除去・等値面抽出・Taubin平滑化を行う。

    keep_mask / zero_mask は rho と同じ形の bool 配列。省略したマスクは適用しない。"""
    t0 = time.perf_counter()
    masked = apply_density_masks(rho, keep_mask=keep_mask, zero_mask=zero_mask)
    rho_clean = remove_islands(masked, threshold=level)
    padded = pad_field(rho_clean, width=pad_width)
    trace.snapshot('surface-fields', masked=masked, rho_clean=rho_clean, padded=padded)
    verts_raw, faces, interpolation_ratios = run_marching_cubes(
        padded, level=level, spacing=spacing, pad_width=pad_width
    )
    trace.snapshot('marching-cubes', vertices=verts_raw, faces=faces,
                   interpolation_ratios=interpolation_ratios)
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
    )


@trace.stage('surface-from-mask')
def build_surface_from_mask(
    mask: np.ndarray,
    *,
    level: float = DEFAULT_LEVEL,
    spacing: tuple[float, float, float] = DEFAULT_SPACING,
    lam: float = DEFAULT_LAMBDA,
    mu: float = DEFAULT_MU,
    n_iter: int = DEFAULT_ITERS,
    pad_width: int = DEFAULT_PAD,
) -> MeshResult:
    """確定済みの二値マスクを直接メッシュ化する(計画書§3・§8段階5の唯一の入口)。

    `build_surface` と違い `apply_density_masks`(保持/除外の塗り直し)・`remove_islands`
    (浮島除去)を**しない**。呼び出し側が渡すマスクは既に `acceptance.inspect` の6条件に
    合格し、`binary_search.resolve_islands` で単一連結成分に絞られている確定マスクである
    ——ここでもう一度どちらかを掛けると、探索が確定した形状を検査を経ずに書き換えることになる
    (計画書§3「候補ごとのメッシュ生成・STL出力は廃止する」と表裏、確定後の1回だけがこの入口)。
    """
    t0 = time.perf_counter()
    rho = np.asarray(mask, dtype=np.float64)
    padded = pad_field(rho, width=pad_width)
    trace.snapshot('surface-fields', masked=rho, rho_clean=rho, padded=padded)
    verts_raw, faces, interpolation_ratios = run_marching_cubes(
        padded, level=level, spacing=spacing, pad_width=pad_width
    )
    trace.snapshot('marching-cubes', vertices=verts_raw, faces=faces,
                   interpolation_ratios=interpolation_ratios)
    verts_smooth = taubin_smooth(verts_raw, faces, lam=lam, mu=mu, n_iter=n_iter)
    seconds = time.perf_counter() - t0
    return MeshResult(
        rho_clean=rho,
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
    )


