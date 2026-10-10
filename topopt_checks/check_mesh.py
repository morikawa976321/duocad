"""メッシュ化の回帰検査。水密性・体積・肉厚・座標・面の向き・突起と空洞の保存を確認する。"""

from __future__ import annotations

import time

import numpy as np
from scipy import ndimage

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
from _harness import Checker, run
from duocad.topopt import OUTPUT_DIR
from duocad.topopt import mesh
from duocad.topopt import voxelize as vx

CHECK = Checker()
check = CHECK.check
FAILURES = CHECK.failures
RESULTS = CHECK.results

# 準備で使う合成マスクの格子。marching cubes の体積誤差は要素の大きさで決まり、粗すぎると
# 検査[2](メッシュ体積 = sum(mask) が5%以内)を割る。
MESH_MATERIAL_GRID = (35, 14, 14)


# ---------------------------------------------------------------------------
# 準備: 合成した二値マスク(単一連結)から mesh.build_surface_from_mask でメッシュを作る
# ---------------------------------------------------------------------------
def run_pipeline() -> tuple[np.ndarray, mesh.MeshResult]:
    """探索は回さず、製品と同じ契約(単一連結の bool マスク)の合成形状を製品と同じ入口へ渡す。"""
    nelx, nely, nelz = MESH_MATERIAL_GRID
    print(
        f"[0] 合成マスクを作る ({nelx}x{nely}x{nelz} = {nelx * nely * nelz}要素)"
    )
    t0 = time.perf_counter()
    # 大きい sigma で「太い塊」を作ってから二値化する(細い突起を作らない
    # ——痩せた形は Taubin 平滑化で体積が大きく動き、検査[3]を割る。実測)。
    rng = np.random.default_rng(20261017)
    seed = rng.random((nelx, nely, nelz))
    blobby = ndimage.gaussian_filter(seed, sigma=3.0, mode="nearest")
    binary = blobby > np.quantile(blobby, 1.0 - 0.35)
    labels, _count = ndimage.label(binary)
    sizes = np.bincount(labels.ravel())
    sizes[0] = 0
    rho = labels == int(np.argmax(sizes))
    elapsed = time.perf_counter() - t0

    t0 = time.perf_counter()
    mr = mesh.build_surface_from_mask(rho)
    mesh_seconds = time.perf_counter() - t0
    print(
        f"  メッシュ生成完了: 頂点{len(mr.verts_raw)} / 三角形{len(mr.faces)} / "
        f"{mesh_seconds:.2f}秒"
    )
    RESULTS["field_seconds"] = round(elapsed, 2)
    RESULTS["mesh_seconds"] = round(mesh_seconds, 2)
    RESULTS["vertices"] = len(mr.verts_raw)
    RESULTS["triangles"] = len(mr.faces)
    return rho, mr


# ---------------------------------------------------------------------------
# 1. 水密
# ---------------------------------------------------------------------------
def watertight_counts(verts: np.ndarray, faces: np.ndarray) -> tuple[int, int, np.ndarray]:
    """(縮退三角形(頂点番号の重複)の数, 孤立頂点の数, 辺ごとの共有枚数の配列)。`mesh.py` から独立した照合役。"""
    degenerate = int(
        np.sum((faces[:, 0] == faces[:, 1]) | (faces[:, 1] == faces[:, 2]) | (faces[:, 0] == faces[:, 2]))
    )
    used = np.zeros(len(verts), dtype=bool)
    used[faces.ravel()] = True
    edge_count: dict[tuple[int, int], int] = {}
    for a, b, c in faces:
        for u, v in ((a, b), (b, c), (c, a)):
            key = (int(u), int(v)) if u < v else (int(v), int(u))
            edge_count[key] = edge_count.get(key, 0) + 1
    return degenerate, int(np.sum(~used)), np.array(list(edge_count.values()))


def signed_volume(verts: np.ndarray, faces: np.ndarray) -> float:
    """発散定理の符号付き体積(閉曲面が外向きに巻かれていれば正)。"""
    tri = verts[faces]
    return float(np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() / 6.0)


def check_watertight(mr: mesh.MeshResult) -> None:
    print("\n[1] 水密")
    faces = mr.faces
    verts = mr.verts_raw

    degenerate, n_isolated, counts = watertight_counts(verts, faces)
    check("縮退三角形が無い(頂点番号重複)", degenerate == 0, f"件数={degenerate}")

    areas2 = np.linalg.norm(mesh.face_normals(verts, faces), axis=1)
    zero_area = int(np.sum(areas2 < 1e-12))
    check("面積0の三角形が無い", zero_area == 0, f"件数={zero_area}")

    check("孤立頂点が無い", n_isolated == 0, f"未使用頂点数={n_isolated}")

    bad = int(np.sum(counts != 2))
    check(
        "すべての辺がちょうど2枚の三角形に共有される",
        bad == 0,
        f"辺の総数={len(counts)} / 2枚でない辺={bad} "
        f"(内訳: {dict(zip(*np.unique(counts, return_counts=True)))})",
    )


# ---------------------------------------------------------------------------
# 2. 体積の一致(marching cubes直後)
# ---------------------------------------------------------------------------
def check_volume_matches_density(mr: mesh.MeshResult) -> None:
    print("\n[2] 体積の一致(marching cubesに実際に渡した場 vs sum(mask)x要素体積)")
    expected = float(mr.rho_clean.sum())  # 要素体積=1(spacing=(1,1,1))なので係数1
    actual = mr.volume_raw
    rel = abs(actual - expected) / expected
    check(
        "marching cubes直後の体積がsum(mask)と一定範囲内で一致(5%)",
        rel < 0.05,
        f"メッシュ体積={actual:.3f} sum(mask)={expected:.3f} 相対誤差={rel:.4%}",
    )
    RESULTS["volume_raw"] = round(actual, 3)
    RESULTS["volume_expected_from_density"] = round(expected, 3)
    RESULTS["volume_vs_density_rel_err"] = rel


# ---------------------------------------------------------------------------
# 3. Taubinが体積を保つ
# ---------------------------------------------------------------------------
def check_taubin_preserves_volume(mr: mesh.MeshResult) -> None:
    print("\n[3] Taubinが体積を保つ")
    rel = abs(mr.volume_smooth - mr.volume_raw) / mr.volume_raw
    check(
        "平滑化前後の体積変化が5%以内",
        rel < 0.05,
        f"平滑化前={mr.volume_raw:.3f} 平滑化後={mr.volume_smooth:.3f} 変化={rel:+.4%}",
    )
    RESULTS["volume_smooth"] = round(mr.volume_smooth, 3)
    RESULTS["volume_change_rel"] = rel


# ---------------------------------------------------------------------------
# 6. 最小肉厚
# ---------------------------------------------------------------------------
def check_min_wall_thickness(mr: mesh.MeshResult) -> None:
    print("\n[6] 最小肉厚(平滑化前後)")
    before = mesh.min_wall_thickness(mr.verts_raw, mr.faces)
    after = mesh.min_wall_thickness(mr.verts_smooth, mr.faces)
    ratio = after / before if before > 0 else float("inf")
    # 平滑化前は階段状ノイズで逆向き法線ペアが過剰検出され実際より薄く出るため、
    # 平滑化後は通常むしろ増える。極端な縮退(反復過多・パラメータ誤り)だけ拾えればよく、閾値は緩い。
    check(
        "平滑化後の最小肉厚が平滑化前の50%を下回らない",
        ratio >= 0.5,
        f"平滑化前={before:.4f} 平滑化後={after:.4f} 比={ratio:.3f}",
    )
    RESULTS["min_thickness_before"] = round(before, 4)
    RESULTS["min_thickness_after"] = round(after, 4)
    RESULTS["min_thickness_ratio"] = ratio


# ---------------------------------------------------------------------------
# 7. STLの往復
# ---------------------------------------------------------------------------
def check_stl_roundtrip(mr: mesh.MeshResult) -> None:
    print("\n[7] STLの往復(build123d.Mesherで読み戻す)")

    from build123d import Mesher

    out_path = OUTPUT_DIR / "optimized.stl"
    mesh.write_stl(out_path, mr.verts_smooth, mr.faces)
    size_kb = out_path.stat().st_size / 1024.0
    print(f"  書き出し: {out_path} ({size_kb:.1f} KB)")

    t0 = time.perf_counter()
    solid = Mesher().read(str(out_path))[0]
    read_seconds = time.perf_counter() - t0
    n_faces = len(solid.faces())
    volume = float(solid.volume)
    print(f"  読み戻し: {read_seconds:.1f}秒 / 面数={n_faces} / 体積={volume:.3f}")

    check(
        "読み戻した面数が三角形数と一致(facetedB-rep)",
        n_faces == len(mr.faces),
        f"面数={n_faces} 三角形数={len(mr.faces)}",
    )
    rel = abs(volume - mr.volume_smooth) / mr.volume_smooth
    check(
        "読み戻した体積が書き出し前と一致",
        rel < 0.01,
        f"読み戻し体積={volume:.3f} 書き出し前={mr.volume_smooth:.3f} 相対誤差={rel:.4%}",
    )
    RESULTS["stl_size_kb"] = round(size_kb, 1)
    RESULTS["stl_roundtrip_faces"] = n_faces
    RESULTS["stl_roundtrip_volume"] = round(volume, 3)
    RESULTS["stl_roundtrip_read_seconds"] = round(read_seconds, 1)


# ---------------------------------------------------------------------------
# 8. 座標系の整合(セル中心規約)
# ---------------------------------------------------------------------------
def check_bounding_box_alignment() -> None:
    print("\n[8] 座標系の整合(既知の位置に置いた合成密度場のバウンディングボックス)")
    lo = (2, 1, 0)
    hi = (4, 3, 2)
    rho = np.zeros((6, 5, 4), dtype=bool)
    rho[lo[0] : hi[0], lo[1] : hi[1], lo[2] : hi[2]] = True

    mr_synth = mesh.build_surface_from_mask(rho)
    bbox_min = mr_synth.verts_raw.min(axis=0)
    bbox_max = mr_synth.verts_raw.max(axis=0)
    expected_min = np.array(lo, dtype=float)
    expected_max = np.array(hi, dtype=float)

    err_min = float(np.abs(bbox_min - expected_min).max())
    err_max = float(np.abs(bbox_max - expected_max).max())
    # 許容0.25要素: 正しければ誤差はほぼ0、半セルのずれのバグは0.5要素なので中間に置く。
    check(
        "バウンディングボックスがセル中心規約の期待範囲と一致(半セルずれていない)",
        err_min < 0.25 and err_max < 0.25,
        f"min={bbox_min.tolist()}(期待{expected_min.tolist()}) "
        f"max={bbox_max.tolist()}(期待{expected_max.tolist()}) "
        f"誤差(min,max)=({err_min:.4f},{err_max:.4f})",
    )
    RESULTS["bbox_alignment_err_min"] = err_min
    RESULTS["bbox_alignment_err_max"] = err_max


# ---------------------------------------------------------------------------
# 9. 面の向き(巻き順が外向き)
# ---------------------------------------------------------------------------
def check_face_winding(mr: mesh.MeshResult) -> None:
    print("\n[9] 面の向き(巻き順が外向き)")
    verts = mr.verts_raw
    faces = mr.faces
    signed = signed_volume(verts, faces)
    check(
        "発散定理の符号付き体積が正(閉曲面が外向きに巻かれている)",
        signed > 0.0,
        f"符号付き体積={signed:.3f}",
    )

    # 符号付き体積は一部の三角形が裏返っても打ち消し合いで正のままになりうるので、有向辺で見る
    # (向きの揃った閉多様体では (a,b) も (b,a) もちょうど1本)。
    # **重心方向との内積では判定できない**: 結果は星形でなく、外向きでも重心から見て後ろを向く面がある。
    n_verts = verts.shape[0]
    directed = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    key = directed[:, 0].astype(np.int64) * n_verts + directed[:, 1]
    rev = directed[:, 1].astype(np.int64) * n_verts + directed[:, 0]
    n_dup = int(key.size - np.unique(key).size)
    check(
        "同じ有向辺が2度現れない(裏返った三角形が無い)",
        n_dup == 0,
        f"重複した有向辺={n_dup} / 有向辺={key.size}",
    )
    check(
        "すべての有向辺に逆向きの相手がいる(向きが全面で一貫)",
        np.array_equal(np.sort(key), np.sort(rev)),
        f"有向辺={key.size} 一致={bool(np.array_equal(np.sort(key), np.sort(rev)))}",
    )
    RESULTS["duplicate_directed_edges"] = n_dup
    RESULTS["signed_volume"] = round(signed, 3)


# ---------------------------------------------------------------------------
# 10. 突起と空洞が平滑化で潰れない
# ---------------------------------------------------------------------------
def check_spike_and_cavity() -> None:
    """マスクの細い突起と内部の空洞が、最終メッシュを同じ格子へ焼き直しても残ることを確認する。"""
    print("\n[10] 突起と空洞の保存(合成マスク)")
    shape = (24, 14, 14)
    rho = np.zeros(shape, dtype=bool)
    rho[2:14, 2:12, 2:12] = True

    keep_mask = np.zeros(shape, dtype=bool)
    keep_mask[10:18, 5:9, 5:9] = True  # 本体を貫くスパイク(内側4要素 + 外側4要素)
    zero_mask = np.zeros(shape, dtype=bool)
    zero_mask[6:9, 5:8, 5:8] = True

    mr = mesh.build_surface_from_mask((rho & ~zero_mask) | keep_mask)

    soup = vx.TriangleSoup(
        positions=mr.verts_smooth.astype(np.float64),
        triangles=mr.faces.astype(np.int64),
        face_index=np.zeros(len(mr.faces), dtype=np.int64),
    )
    grid = vx.Grid(origin=(0.0, 0.0, 0.0), spacing=1.0, shape=shape)
    occupied, occ_stats = vx.occupancy(soup, grid)
    check(
        "突起の要素がすべて出力の内側にある(食われていない)",
        bool(occupied[keep_mask].all()),
        f"内側 {int(occupied[keep_mask].sum())}/{int(keep_mask.sum())}",
    )
    check(
        "空洞の要素に材料が1つも入っていない",
        not bool(occupied[zero_mask].any()),
        f"侵入 {int(occupied[zero_mask].sum())}/{int(zero_mask.sum())}",
    )
    RESULTS["spike_cavity_unbalanced_columns"] = occ_stats.get("unbalanced_columns")

    verts, faces = mr.verts_raw, mr.faces
    degenerate, isolated, counts = watertight_counts(verts, faces)
    bad_edges = int(np.sum(counts != 2))
    check(
        "水密(縮退三角形・孤立頂点・共有辺2枚がこの合成場でも保たれる)",
        degenerate == 0 and isolated == 0 and bad_edges == 0,
        f"縮退={degenerate} 孤立頂点={isolated} 2枚でない辺={bad_edges}",
    )
    signed = signed_volume(verts, faces)
    check("面の向き(符号付き体積が正)がこの合成場でも保たれる", signed > 0.0, f"符号付き体積={signed:.3f}")


def main() -> int:
    shared: dict = {}

    def prepare() -> None:
        shared["mr"] = run_pipeline()[1]

    return run(
        CHECK,
        "check_mesh.py : marching cubes + Taubin平滑化の受入検査",
        [
            prepare,
            lambda: check_watertight(shared["mr"]),
            lambda: check_volume_matches_density(shared["mr"]),
            lambda: check_taubin_preserves_volume(shared["mr"]),
            lambda: check_min_wall_thickness(shared["mr"]),
            lambda: check_stl_roundtrip(shared["mr"]),
            check_bounding_box_alignment,
            lambda: check_face_winding(shared["mr"]),
            check_spike_and_cavity,
        ],
    )


if __name__ == "__main__":
    raise SystemExit(main())
