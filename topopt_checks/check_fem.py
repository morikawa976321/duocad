"""二値FEM(`duocad/topopt/fem.py`)の移設前一致検査。

    .\\.venv\\Scripts\\python.exe topopt_checks\\check_fem.py

**この検査の存在理由**: `docs/PLAN_BINARY_STRESS_SEARCH.md` §8 段階1「FEM分離」は
`simp.py` にあった要素剛性・節点番号・行列組立・CG/direct/MG・残差検算を `fem.py` へ
移すだけの作業で、計算そのものを変えてはならない。ここでは二値の棒・梁・空セル領域を
含む4題材について、**移設前の実装で採った基準値**(変位・コンプライアンス・反力・
von Mises 応力のSHA-256とスカラー値)と、いまの実装の出力を突き合わせる。

基準値の採取手順(再現する場合): `git stash` で本ファイルを含む段階1の変更点を戻し、
移設前の `simp.py` に対して同じ題材を解いて基準値を採り、`git stash pop` で戻して
このファイルの期待値と比較する。要素剛性・DOF番号・組立・線形解法のどこかが
移設で変わっていれば、SHAかスカラー値のどちらかが必ず動く。

`fem.Model` は SIMP のペナルティ引数 `penal` を持つが、ここでは全題材を ρ∈{0,1} の
二値(`active_mask` で空セルを組み立てから除外)にしてあるので `rho**penal` は
0/1のどちらでも `penal` に依らない —— 二値FEMとしての振る舞いだけを見ている。
"""

from __future__ import annotations

import hashlib
import sys
import time

import numpy as np

# **Windowsの既定コンソール(cp932)で落ちないようにする。**
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
from duocad.topopt import fem, simp, stress

FAILURES: list[str] = []

# 移設前の `simp.py`(このタスクの着手基準コミット)で採った基準値。
# 採取に使った題材の定義は `_make_case` / `_run_case` と1対1。
BASELINE = {
    "rod_solid_cg": {
        "grid": (16, 4, 4),
        "void_blocks": [],
        "solver": "cg",
        "threads": 1,
        "void_cells": 0,
        "u_sha": "f7182a1cec94fb99ce3d23ffb536c76e331dd2e5bf877cbcb57c65b59c1e8d7c",
        "compliance": 66.34665868500139,
        "cg_iterations": 108,
        "reaction_sha": "8100a5826c8b9ac900726f3e0619f23daf4c21a27179c934fe5d683b321bcbca",
        "von_mises_sha": "aef999b072ffc64889bd1372a9ec789313ce89692e8271f96c813655c75f0acc",
        "representative_mpa": 0.9810916712161777,
        "raw_max_mpa": 0.9810917083269399,
        "free_dofs": 1200,
        "fixed_dofs": 75,
    },
    "beam_void_interior_mg": {
        "grid": (16, 6, 6),
        "void_blocks": [(6, 10, 2, 4, 2, 4)],
        "solver": "mg",
        "threads": 1,
        "void_cells": 16,
        "u_sha": "f98711698742fa041b0280f1688018a3d94ad6b66a5d9a80ca8f3ac64f1c3971",
        "compliance": 16.862912993949756,
        "cg_iterations": 16,
        "reaction_sha": "616d1acfd2050c126274ac53ced302708d640e81e0cb9ac8da10f9409d04df55",
        "von_mises_sha": "95ea62b8baa34f1c19e695db38845ebc50fb8849938c04edf1cfaa10bd45396d",
        "representative_mpa": 2.5222538558242835,
        "raw_max_mpa": 2.52477154353815,
        "free_dofs": 2343,
        "fixed_dofs": 156,
    },
    "beam_void_surface_direct": {
        "grid": (12, 6, 6),
        "void_blocks": [(4, 8, 0, 2, 0, 6)],
        "solver": "direct",
        "threads": 1,
        "void_cells": 48,
        "u_sha": "42125d0c12c9b033b90e63ff4a717f09fecd8f4819d27ed7b735b935de654a4b",
        "compliance": 14.569613168075428,
        "cg_iterations": 0,
        "reaction_sha": "27d9d94f87dfdc4e6c0921e91e2cb828126009f8fe78ad1e5e19d430fbd90458",
        "von_mises_sha": "b18ece3f902afc10b8c59acf404bf66aac2a446a678f3fb4a4ca90b8e9ac96a4",
        "representative_mpa": 4.696393561325111,
        "raw_max_mpa": 4.701231762848346,
        "free_dofs": 1638,
        "fixed_dofs": 273,
    },
    "beam_multi_void_mg_threads": {
        "grid": (20, 8, 8),
        "void_blocks": [(2, 5, 1, 3, 1, 3), (12, 16, 4, 7, 4, 7)],
        "solver": "mg",
        "threads": 4,
        "void_cells": 48,
        "u_sha": "3df15e5e41e9fc7b81a0294df7be3e4948ea726552b46c8aad635964e201db88",
        "compliance": 12.242055466859082,
        "cg_iterations": 23,
        "reaction_sha": "2550c17608f0448a100937e89a6a5d511b11d817e0402e071bc199d42a75e4f0",
        "von_mises_sha": "d8a8333256d611e39a233821a93e4da63d9d2e0b23826978584aa650748faa5e",
        "representative_mpa": 3.4552324738385196,
        "raw_max_mpa": 4.243306641488558,
        "free_dofs": 4818,
        "fixed_dofs": 285,
    },
}


def check(name: str, ok: bool, detail: str) -> None:
    """1件の検査結果を出力する。**落ちても即死せず全件を出してから終わる。**"""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def _sha(arr: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(arr).tobytes()).hexdigest()


def _make_model(nelx: int, nely: int, nelz: int, void_blocks: list[tuple[int, ...]]):
    """棒(全材料)・梁(void_blocksで空洞を開けた形)を二値の `active_mask` で作る。"""
    void = np.zeros((nelx, nely, nelz), dtype=bool)
    for (x0, x1, y0, y1, z0, z1) in void_blocks:
        void[x0:x1, y0:y1, z0:z1] = True
    extra_fixed = fem._floating_dofs(nelx, nely, nelz, void) if void.any() else None
    model = fem.Model(
        nelx, nely, nelz,
        active_mask=(~void) if void.any() else None,
        extra_fixed_dofs=extra_fixed,
    )
    return model, void


def run_case(name: str, spec: dict) -> None:
    nelx, nely, nelz = spec["grid"]
    model, void = _make_model(nelx, nely, nelz, spec["void_blocks"])
    check(
        f"{name}: void セル数が期待どおり",
        int(void.sum()) == spec["void_cells"],
        f"{int(void.sum())} / 期待 {spec['void_cells']}",
    )
    check(
        f"{name}: 自由/固定自由度数が期待どおり",
        int(model.free.size) == spec["free_dofs"] and int(model.fixed.size) == spec["fixed_dofs"],
        f"free={model.free.size} fixed={model.fixed.size} / 期待 free={spec['free_dofs']} fixed={spec['fixed_dofs']}",
    )

    rho = np.ones((nelx, nely, nelz), dtype=float)
    penal = 3.0  # 二値(active_maskで空を除外済み)なので rho**penal は penal に依らない

    with fem.collect_solve_stats() as stats:
        u = model.solve(rho, penal, solver=spec["solver"], threads=spec["threads"])

    check(
        f"{name}: 変位 U が移設前とビット一致",
        _sha(u) == spec["u_sha"],
        f"solver={spec['solver']} threads={spec['threads']}",
    )
    check(
        f"{name}: CG反復数が移設前と一致(解法経路が変わっていない)",
        stats["cg_iterations"] == spec["cg_iterations"],
        f"{stats['cg_iterations']} / 期待 {spec['cg_iterations']}",
    )

    c, _ce = model.compliance(rho, penal, u)
    check(
        f"{name}: コンプライアンス c=F·U が移設前と一致",
        c == spec["compliance"],
        f"{c!r} / 期待 {spec['compliance']!r}",
    )

    groups = [simp.ReactionGroup(name="fixed", dofs=model.fixed)]
    reactions = simp.reaction_forces(model, rho, penal, u, groups)
    check(
        f"{name}: 反力 R=(Ku-f)|_fixed が移設前とビット一致",
        _sha(reactions) == spec["reaction_sha"],
        f"合力 {reactions.sum(axis=0).tolist()}",
    )

    scale = stress.UnitScale.for_model(model, element_mm=1.0, load_n=1.0, youngs_modulus_mpa=2000.0)
    field = stress.element_stress(model, u).scaled(scale)
    check(
        f"{name}: von Mises 応力場が移設前とビット一致",
        _sha(field.von_mises) == spec["von_mises_sha"],
        "element_stress().scaled()",
    )
    rep = stress.representative_stress(field, rho, rho_min=0.5, percentile=99.5)
    check(
        f"{name}: 代表応力(99.5%点)・素の max が移設前と一致",
        rep.value == spec["representative_mpa"] and rep.raw_max == spec["raw_max_mpa"],
        f"代表={rep.value!r} max={rep.raw_max!r}",
    )


def main() -> int:
    started = time.perf_counter()
    print("=" * 72)
    print("check_fem.py : 二値FEM(fem.py)の移設前一致検査")
    print("=" * 72)
    for name, spec in BASELINE.items():
        print(f"\n[{name}] grid={spec['grid']} void_blocks={spec['void_blocks']} "
              f"solver={spec['solver']} threads={spec['threads']}")
        run_case(name, spec)

    print("\n" + "=" * 72)
    if FAILURES:
        print(f"NG: {len(FAILURES)}件が不合格 / {time.perf_counter() - started:.1f}秒")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"OK: 全検査に合格 / {time.perf_counter() - started:.1f}秒")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
