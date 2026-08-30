"""marching cubes + Taubin平滑化(`mesh.py`)の受入検査。

    ..\.venv\Scripts\python.exe check_mesh.py

**このファイルが合否そのものである。** タスクの完了条件1「`check_mesh.py` が緑」を、
下記7項目の assert で判定する。1つでも落ちればプロセスは非ゼロで終わる。

検査の構成:

1. **水密** — すべての辺がちょうど2枚の三角形に共有される。孤立頂点・縮退三角形が無い。
2. **体積の一致** — marching cubes直後(平滑化前)のメッシュ体積が `sum(rho_clean) × 要素体積`
   と数%以内で一致する(等値0.5の面としてまともか)。
3. **Taubinが体積を保つ** — 平滑化の前後で体積変化が5%以内。
4. **Taubinが素のLaplacianより縮まない** — 同じ反復数・同じ開始点で素のLaplacianを回し、
   Laplacianのほうが明確に大きく縮むことを確認する
   (「Taubinと名乗っているが実はLaplacian」の検出)。
5. **補間が効いている(先に二値化していない)** — marching cubesが辺上に置いた頂点の
   内分比の分布を見る。二値化した場を渡すと内分比は必ず厳密に0.5になり分布のばらつきが
   0になるので、ばらつき(標準偏差)がそれよりずっと大きいことを確認する。
6. **最小肉厚** — 平滑化の後も、最小肉厚が平滑化前の一定割合を下回らない
   (細い部材を食っていないこと)。
7. **STLの往復** — 書き出したSTLを `build123d.Mesher().read()` で読み戻し、
   三角形数(=面数、facetedB-rep)と体積が一致する。

実行には`simp.py`のプリセット(40x16x16・60反復)を1回回す(約1.5〜2分)。
"""

from __future__ import annotations

import json
import sys
import time

import numpy as np

# Windowsの既定コンソール(cp932)で記号1つ落ちるだけで検査が1件も走らず異常終了する
# (check_simp.py で2026-08-29に踏んだのと同じ対策)。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import mesh
import simp

FAILURES: list[str] = []
RESULTS: dict[str, object] = {}


def check(name: str, ok: bool, detail: str) -> None:
    """1件の検査結果を出力する。落ちても即死せず全件出してから終わる。"""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    if not ok:
        FAILURES.append(f"{name}: {detail}")


# ---------------------------------------------------------------------------
# 準備: プリセットのSIMPを1回回し、そこから mesh.build_surface でメッシュを作る
# ---------------------------------------------------------------------------
def run_pipeline() -> tuple[simp.Result, mesh.MeshResult]:
    p = simp.PRESET
    print(
        f"[0] プリセットの片持ち梁を最適化 "
        f"({p['nelx']}x{p['nely']}x{p['nelz']} = {p['nelx'] * p['nely'] * p['nelz']}要素, "
        f"最大{p['max_iter']}反復)"
    )
    t0 = time.perf_counter()
    result = simp.run_preset()
    elapsed = time.perf_counter() - t0
    print(f"  SIMP完了: {len(result.history)}反復 / {elapsed:.1f}秒")

    t0 = time.perf_counter()
    mr = mesh.build_surface(result.rho)
    mesh_seconds = time.perf_counter() - t0
    print(
        f"  メッシュ生成完了: 頂点{len(mr.verts_raw)} / 三角形{len(mr.faces)} / "
        f"{mesh_seconds:.2f}秒"
    )
    RESULTS["simp_seconds"] = round(elapsed, 1)
    RESULTS["mesh_seconds"] = round(mesh_seconds, 2)
    RESULTS["vertices"] = len(mr.verts_raw)
    RESULTS["triangles"] = len(mr.faces)
    return result, mr


# ---------------------------------------------------------------------------
# 1. 水密
# ---------------------------------------------------------------------------
def check_watertight(mr: mesh.MeshResult) -> None:
    print("\n[1] 水密")
    faces = mr.faces
    verts = mr.verts_raw

    # 縮退三角形(頂点番号の重複)
    degenerate = int(
        np.sum(
            (faces[:, 0] == faces[:, 1])
            | (faces[:, 1] == faces[:, 2])
            | (faces[:, 0] == faces[:, 2])
        )
    )
    check("縮退三角形が無い(頂点番号重複)", degenerate == 0, f"件数={degenerate}")

    areas2 = np.linalg.norm(mesh.face_normals(verts, faces), axis=1)
    zero_area = int(np.sum(areas2 < 1e-12))
    check("面積0の三角形が無い", zero_area == 0, f"件数={zero_area}")

    used = np.zeros(len(verts), dtype=bool)
    used[faces.ravel()] = True
    n_isolated = int(np.sum(~used))
    check("孤立頂点が無い", n_isolated == 0, f"未使用頂点数={n_isolated}")

    edge_count: dict[tuple[int, int], int] = {}
    for a, b, c in faces:
        for u, v in ((a, b), (b, c), (c, a)):
            key = (int(u), int(v)) if u < v else (int(v), int(u))
            edge_count[key] = edge_count.get(key, 0) + 1
    counts = np.array(list(edge_count.values()))
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
    print("\n[2] 体積の一致(平滑化前 vs sum(rho)x要素体積)")
    expected = float(mr.rho_clean.sum())  # 要素体積=1(spacing=(1,1,1))なので係数1
    actual = mr.volume_raw
    rel = abs(actual - expected) / expected
    check(
        "marching cubes直後の体積がsum(rho)と数%以内で一致",
        rel < 0.05,
        f"メッシュ体積={actual:.3f} sum(rho)={expected:.3f} 相対誤差={rel:.4%}",
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
# 4. Taubinが素のLaplacianより縮まない
# ---------------------------------------------------------------------------
def check_taubin_vs_laplacian(mr: mesh.MeshResult) -> None:
    print("\n[4] Taubin vs 素のLaplacian(同じ反復数・同じ開始点)")
    verts_lap = mesh.laplacian_smooth(mr.verts_raw, mr.faces, lam=mr.lam, n_iter=mr.n_iter)
    vol_lap = mesh.mesh_volume(verts_lap, mr.faces)
    shrink_lap = mr.volume_raw - vol_lap
    shrink_taubin = mr.volume_raw - mr.volume_smooth
    check(
        "Laplacianの方が明確に大きく縮む",
        shrink_lap > shrink_taubin + 0.01 * mr.volume_raw,
        f"Laplacian収縮={shrink_lap:.3f}(体積{vol_lap:.3f}) "
        f"Taubin収縮={shrink_taubin:.3f}(体積{mr.volume_smooth:.3f})",
    )
    RESULTS["volume_plain_laplacian"] = round(vol_lap, 3)


# ---------------------------------------------------------------------------
# 5. 補間が効いている(先に二値化していない)
# ---------------------------------------------------------------------------
def check_interpolation_not_binarized(mr: mesh.MeshResult) -> None:
    print("\n[5] 補間が効いている(内分比の分布)")
    ratios = mesh.edge_interpolation_ratios(mr.verts_raw, spacing=mr.spacing)
    std = float(ratios.std())
    frac_exact_half = float(np.mean(np.abs(ratios - 0.5) < 1e-6))
    # 参考: 二値化した場(rho>0.5の0/1配列)を渡すと、辺の両端が必ず0/1になり
    # 線形補間は厳密に0.5に落ちる(std=0, frac_exact_half=1.0)。continuousな場では
    # SIMPが密度をほぼ0/1へ押し込む(penal=3)ため0.5付近が多めではあるが、
    # フィルタ半径ぶんの遷移域があるので分布に明確な広がりが残る。
    check(
        "内分比のばらつき(標準偏差)が0でない(二値化なら常に0)",
        std > 0.03,
        f"std={std:.4f}(0.5ちょうどの割合={frac_exact_half:.2%}, n={len(ratios)})",
    )
    RESULTS["interpolation_ratio_std"] = std
    RESULTS["interpolation_ratio_frac_exact_half"] = frac_exact_half


# ---------------------------------------------------------------------------
# 6. 最小肉厚
# ---------------------------------------------------------------------------
def check_min_wall_thickness(mr: mesh.MeshResult) -> None:
    print("\n[6] 最小肉厚(平滑化前後)")
    before = mesh.min_wall_thickness(mr.verts_raw, mr.faces)
    after = mesh.min_wall_thickness(mr.verts_smooth, mr.faces)
    ratio = after / before if before > 0 else float("inf")
    # 平滑化前(marching cubes直後)は階段状の局所ノイズで「近接する逆向き法線ペア」が
    # 過剰検出され、実際より薄く出る傾向がある。平滑化するとノイズが消えてこの値は
    # 通常むしろ増える。ここで守りたいのは「平滑化が部材を食っていないか」で、
    # 極端な縮退(反復過多・パラメータ誤り)だけを検出できればよいので閾値は緩めに取る。
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
    from pathlib import Path

    from build123d import Mesher

    out_path = Path(__file__).resolve().parent / "output" / "optimized.stl"
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


def main() -> int:
    print("=" * 72)
    print("check_mesh.py : marching cubes + Taubin平滑化の受入検査")
    print("=" * 72)
    _result, mr = run_pipeline()
    check_watertight(mr)
    check_volume_matches_density(mr)
    check_taubin_preserves_volume(mr)
    check_taubin_vs_laplacian(mr)
    check_interpolation_not_binarized(mr)
    check_min_wall_thickness(mr)
    check_stl_roundtrip(mr)

    print("\n" + "=" * 72)
    print("MEASURED " + json.dumps(RESULTS, ensure_ascii=False, default=float))
    print("=" * 72)
    if FAILURES:
        print(f"NG: {len(FAILURES)}件が不合格")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("OK: 全検査に合格")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
