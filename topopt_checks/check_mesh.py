"""marching cubes + Taubin平滑化(`mesh.py`)の受入検査。

    .\\.venv\\Scripts\\python.exe topopt_checks\\check_mesh.py

**このファイルが合否そのものである。** タスクの完了条件1「`check_mesh.py` が緑」を、
下記10項目の assert で判定する。1つでも落ちればプロセスは非ゼロで終わる。

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
8. **座標系の整合(セル中心規約)** — 既知の位置に置いた合成密度場で、面のバウンディング
   ボックスが期待どおりの位置にあるかを見る。1〜7はどれも並進不変(体積・水密性・分布の
   形は位置のズレでは変わらない)なので、**全軸を一律に半セルずらす**類のバグを検出できない
   (2026-08-30にQAが発見: `rho[ex]`はセル`ex`の中心の値なのに、パディング除去のオフセット
   計算がそれを踏まえておらず、marching cubesの面が設計空間に対して常に半要素ずれていた)。
9. **面の向き(巻き順が外向き)** — 閉曲面の向きは「全部外向き」か「全部内向き」の
   2通りしかないので、(a)向きが全面で一貫している(共有辺を2枚の三角形が必ず逆向きに
   たどる＝同じ有向辺が2度現れない)ことと、(b)発散定理の符号付き体積が正であること、の
   2つで確定する。(a)だけだと全体反転を、(b)だけだと一部反転の打ち消し合いを見逃す。
   閾値は無い。1〜8で巻き順を見るのは検査7だけで、それは**STL経路しか通らない**ため、
   `mr.faces` を直接使う消費者(`probe_topopt/server.py`→three.js描画)が内向きのまま残る事故を
   捕まえられなかった(2026-09-02発見。engine
   `winding-fix-at-export-only-leaks-to-other-consumers`)。
10. **保持/除外の塗り直し(2026-09-05追加)** — 密度場側のガウシアン平滑化(`mesh.py`段0)
    は保持ソリッド・除外ソリッド・設計空間の外をにじませるので、その塗り直しが
    実際に効いているかを合成場で見る。塗り直し**後**の場そのもの(保持=1・除外=0が
    厳密に保たれているか)と、できたメッシュの幾何(`voxelize.occupancy`で焼き直し、
    保持マスクの要素がすべて内側に入り、除外マスクの要素に1つも材料が入っていないか)
    の両方を見る。**極端なσ(3.0要素)で1回だけ確認する**(調整値の最適化はしない —
    タスク本文の指示)。水密・面の向きもこの合成場で保たれることを併せて確認する。

実行には片持ち梁を1回回す(`bench.MESH_MATERIAL` = 35x14x14・15反復。約11秒)。**ここだけ
プリセット(20x8x8)を使わない** —— 理由は `bench.py` の該当コメント。8番目の検査だけは
高速な合成密度場で行うので、この所要時間には影響しない。
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

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
from duocad.topopt import OUTPUT_DIR
from duocad.topopt import bench
from duocad.topopt import mesh
from duocad.topopt import simp
from duocad.topopt import voxelize as vx

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
    p = bench.MESH_MATERIAL
    print(
        f"[0] 片持ち梁を最適化 "
        f"({p['nelx']}x{p['nely']}x{p['nelz']} = {p['nelx'] * p['nely'] * p['nelz']}要素, "
        f"最大{p['max_iter']}反復。**プリセットより細かい** —— 理由は bench.MESH_MATERIAL)"
    )
    t0 = time.perf_counter()
    result = simp.run_preset(**p)
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
    # `mr.rho_clean` は**段0(ガウシアン平滑化+塗り直し)を経た後**の場(`mesh.build_surface`
    # の実装順序どおり)。既定で段0が有効なので、ここは常に「marching cubesが実際に
    # 受け取った場」と比べていることになる — 平滑化の有無に関わらず同じ式で書ける。
    #
    # **閾値は5%から12%へ広げた**(2026-09-05、ガウシアン平滑化が既定で入ったことに
    # 伴う見直し。検査は消さない — 削るのではなく許容差を取り直す)。二値に近い場では
    # 「等値0.5の面が囲む体積 ≒ sum(rho)」がほぼ厳密に成り立つが、ガウシアンで均すと
    # SIMPの密度フィルタ由来の遷移域(すでに二値ではない)がさらに広がり、この近似が
    # 緩む。実測(このファイルの既定シナリオ、σ=1要素・DEFAULT_SMOOTH_SIGMA): 平滑化前
    # 3.42% → 平滑化後 8.21%。12%はこの実測に安全余裕を足した値で、level取り違えや
    # 半セルずれ(検査8が別途見る観点)のような本物のバグ(実測30%超)は依然として
    # 捕まえる。
    expected = float(mr.rho_clean.sum())  # 要素体積=1(spacing=(1,1,1))なので係数1
    actual = mr.volume_raw
    rel = abs(actual - expected) / expected
    check(
        "marching cubes直後の体積がsum(rho)と一定範囲内で一致(ガウシアン平滑化込みで12%)",
        rel < 0.12,
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
    # **`mr.verts_raw` から再計算しない。** `verts_raw` はセル中心規約へ半要素オフセット
    # 済みで、整数格子点の前提(`edge_interpolation_ratios`が軸を判定する根拠)が崩れて
    # 全頂点が「軸誤判定→常に0.5」になる(2026-08-30、オフセット修正と同時に実測して発見)。
    # `mr.interpolation_ratios` は `mesh.run_marching_cubes` がオフセット適用前
    # (marching_cubes直後の生座標、整数格子点のまま)に計算済みの値を使う。
    ratios = mr.interpolation_ratios
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
    """既知の位置に置いた合成密度場で、marching cubesが返す面のバウンディングボックスが
    「セル中心」規約(`rho[ex]` はセル `ex` の中心の値。`probe_topopt/web/index.html` のボクセル描画も
    `ex+0.5-nelx/2` でこの規約)どおりの位置にあるかを見る。

    上の1〜7はどれも並進不変(体積・水密性・分布の形は位置のズレでは変わらない)なので、
    **全軸を一律に半セルずらす**類のバグはこれが無いと素通りする(2026-08-30にQAが発見:
    パディング除去のオフセット計算が `pad_width*spacing` になっており、正しくは
    `(pad_width-0.5)*spacing`。差の0.5要素ぶん、面が常に設計空間に対してずれていた)。

    このぶんだけは実データ(SIMP結果)を使わず、位置が最初から分かっている合成密度場
    (直方体ブロック1つ、z軸方向は設計空間の端(0)に接するように置いてパディング境界の
    処理も一緒に確認する)で見る — 高速かつ期待値が数式で厳密に出せるため。

    **`smooth_sigma=0` でガウシアン平滑化(段0)を切る。** ここが見たいのは座標変換
    (半セルオフセット)だけで、平滑化とは無関係な関心事。しかもこの合成場は
    6x5x4の格子に2x2x2のブロック1つという極小サイズで、既定のσ(=1要素)を掛けると
    ブロックの山がほぼ均されて0.5を割り込み、`marching_cubes`が等値面を見つけられず
    例外になる(2026-09-05実測)。平滑化の効き自体は検査10(σ=3.0の極端値まで)が
    別に見る。
    """
    print("\n[8] 座標系の整合(既知の位置に置いた合成密度場のバウンディングボックス)")
    lo = (2, 1, 0)
    hi = (4, 3, 2)
    rho = np.zeros((6, 5, 4))
    rho[lo[0] : hi[0], lo[1] : hi[1], lo[2] : hi[2]] = 1.0

    mr_synth = mesh.build_surface(rho, smooth_sigma=0)
    bbox_min = mr_synth.verts_raw.min(axis=0)
    bbox_max = mr_synth.verts_raw.max(axis=0)
    expected_min = np.array(lo, dtype=float)
    expected_max = np.array(hi, dtype=float)

    err_min = float(np.abs(bbox_min - expected_min).max())
    err_max = float(np.abs(bbox_max - expected_max).max())
    # 許容0.25要素: 正しい実装なら誤差はほぼ0(浮動小数点誤差のみ)、報告されたバグは
    # 0.5要素ぶんずれるので、その中間に閾値を置けばどちらも明確に判定できる。
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
    tri = verts[faces]
    signed = float(
        np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() / 6.0
    )
    check(
        "発散定理の符号付き体積が正(閉曲面が外向きに巻かれている)",
        signed > 0.0,
        f"符号付き体積={signed:.3f}",
    )

    # 符号付き体積は総和なので、一部の三角形だけ裏返っていても打ち消し合いで正のまま
    # になりうる。向きが全面で一貫していることは有向辺で判定できる —— 向きの揃った
    # 閉多様体では、有向辺 (a,b) はちょうど1本、その逆向き (b,a) もちょうど1本になる。
    # **重心方向との内積では判定できない**: トポロジー最適化の結果は星形ではないので、
    # 正しく外向きでも重心から見て後ろを向く面が普通にある。
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
# 10. 保持/除外の塗り直し(ガウシアン平滑化がにじませたぶんの手当て)
# ---------------------------------------------------------------------------
def check_mask_repaint() -> None:
    """合成密度場(実データではなく、位置が分かっている合成ブロック)で塗り直しを確かめる。

    **本体ブロック**(x:2..14 / y:2..12 / z:2..12、ρ=1)に対して:
    - **保持(keep_mask)**: x:10..18 のスパイク。本体の内側(10..14)から外側(14..18)へ
      連続してまたぐように置く — `keep_mask` は塗り直しで常にρ=1へ戻されるので、
      **`keep_mask`自身が連結していれば、本体側とσ次第で薄れても浮島にならない**
      (境界ちょうどで本体とキープを接するだけだと、極端なσで境界の物理密度が0.5を
      割り込み、`remove_islands`が浮島として消してしまう。実測で確認して置き方を
      決めた — 2026-09-05)。
    - **除外(zero_mask)**: x:6..9 / y:5..8 / z:5..8 の小さな穴。本体の内部に、
      どの壁からも4要素以上離して開ける(平滑化ののち周りの材料がにじみ込む先)。

    σ=3.0(タスク指定の極端値。最適値探索はしない)でガウシアン平滑化+塗り直しを掛け、
    (a) marching cubesへ渡す直前の場で保持=1・除外=0が厳密に保たれているか、
    (b) できたメッシュを`voxelize.occupancy`で同じ格子へ焼き直し、保持マスクの要素が
        すべて内側(occupied)・除外マスクの要素が1つも内側でないか、
    (c) その合成場でも水密・面の向きが保たれているか(検査1・9と同じ観点)、
    を見る。
    """
    print("\n[10] 保持/除外の塗り直し(σ=3.0の極端値。合成密度場)")
    shape = (24, 14, 14)
    rho = np.zeros(shape)
    rho[2:14, 2:12, 2:12] = 1.0
    rho[6:9, 5:8, 5:8] = 0.0  # 除外の下にある「穴」(既定でも0だが明示)

    keep_mask = np.zeros(shape, dtype=bool)
    keep_mask[10:18, 5:9, 5:9] = True  # 本体を貫くスパイク(内側4要素 + 外側4要素)
    zero_mask = np.zeros(shape, dtype=bool)
    zero_mask[6:9, 5:8, 5:8] = True

    mr = mesh.build_surface(rho, keep_mask=keep_mask, zero_mask=zero_mask, smooth_sigma=3.0)

    # (a) 場そのもの
    keep_ok = bool(np.all(mr.rho_clean[keep_mask] == 1.0))
    check("塗り直し後の場: 保持マスクがすべてρ=1", keep_ok, f"keep要素={int(keep_mask.sum())}")
    zero_ok = bool(np.all(mr.rho_clean[zero_mask] == 0.0))
    check("塗り直し後の場: 除外マスクがすべてρ=0", zero_ok, f"zero要素={int(zero_mask.sum())}")

    # (b) メッシュを同じ格子へ焼き直して幾何で確認
    soup = vx.TriangleSoup(
        positions=mr.verts_smooth.astype(np.float64),
        triangles=mr.faces.astype(np.int64),
        face_index=np.zeros(len(mr.faces), dtype=np.int64),
    )
    grid = vx.Grid(origin=(0.0, 0.0, 0.0), spacing=1.0, shape=shape)
    occupied, occ_stats = vx.occupancy(soup, grid)
    check(
        "保持マスクの要素がすべて出力の内側にある(食われていない)",
        bool(occupied[keep_mask].all()),
        f"内側 {int(occupied[keep_mask].sum())}/{int(keep_mask.sum())}",
    )
    check(
        "除外マスクの要素に材料が1つも入っていない",
        not bool(occupied[zero_mask].any()),
        f"侵入 {int(occupied[zero_mask].sum())}/{int(zero_mask.sum())}",
    )
    RESULTS["mask_repaint_unbalanced_columns"] = occ_stats.get("unbalanced_columns")

    # (c) 水密・面の向き(検査1・9と同じ観点をこの合成場でも)
    verts, faces = mr.verts_raw, mr.faces
    degenerate = int(
        np.sum(
            (faces[:, 0] == faces[:, 1]) | (faces[:, 1] == faces[:, 2]) | (faces[:, 0] == faces[:, 2])
        )
    )
    used = np.zeros(len(verts), dtype=bool)
    used[faces.ravel()] = True
    edge_count: dict[tuple[int, int], int] = {}
    for a, b, c in faces:
        for u, v in ((a, b), (b, c), (c, a)):
            key = (int(u), int(v)) if u < v else (int(v), int(u))
            edge_count[key] = edge_count.get(key, 0) + 1
    bad_edges = int(np.sum(np.array(list(edge_count.values())) != 2))
    check(
        "水密(縮退三角形・孤立頂点・共有辺2枚がこの合成場でも保たれる)",
        degenerate == 0 and bool(np.all(used)) and bad_edges == 0,
        f"縮退={degenerate} 孤立頂点={int(np.sum(~used))} 2枚でない辺={bad_edges}",
    )
    tri = verts[faces]
    signed = float(np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() / 6.0)
    check("面の向き(符号付き体積が正)がこの合成場でも保たれる", signed > 0.0, f"符号付き体積={signed:.3f}")


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
    check_bounding_box_alignment()
    check_face_winding(mr)
    check_mask_repaint()

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
