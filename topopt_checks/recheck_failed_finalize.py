r"""検証材料64条件のうち最終処理が平滑化の交差で落ちた4条件を、空隙埋めの入った製品経路で回し直す(#95 の残り)。

    .venv/Scripts/python.exe topopt_checks/recheck_failed_finalize.py F2000_h2.0_w2
    .venv/Scripts/python.exe topopt_checks/recheck_failed_finalize.py F2000_h2.0_w2 --stages   # finalize の段ごとの自己交差

`verify_tube_fill.run_one`(空隙埋め → 6条件の取り直し → 平滑化 → finalize)をそのまま使い、3点だけ差し替える:
- 探索の代表応力を検証材料を作った当時の 99.5% 点に戻す(`_percentile_rule`)。今の部材域 Q_V では保存マスクが
  6条件に通らず、空隙埋めの前に no_feasible_found で止まる。
- 完成形検査 `final_check.inspect` を「測らなかった」にする。hanger では 1/3 格子で約 20 GB を使い、問いは finalize が
  通るかであって完成形の強度ではない。
- 記録先を `output/smoothing_corpus/tube_fill_records_finalize4/` にして 2026-10-07 の記録を上書きしない。

`--stages` は finalize の入力を受け取った所で、集合演算・as_original・簡略化(1e-5 / 1e-4 / 1e-3)・float32 で潰れる辺の
潰し込みの各段の自己交差を float64 と float32 で数え、`<記録先>/<条件名>.stages.json` に書いてから本物の finalize を呼ぶ。
"""
import argparse
import json
import sys
from unittest.mock import patch

import _bootstrap  # noqa: F401

import manifold3d
import numpy as np

import verify_tube_fill as v
from _percentile_rule import percentile_rule
from duocad.mesh_geometry import weld_exact
from duocad.mesh_intersections import triangle_intersections
from duocad.topopt import final_check
from duocad.topopt import finalize as fz

RECORDS = v.OUT / "tube_fill_records_finalize4"


def skipped_final_check(final_vertices, final_faces, *, min_width, **_):
    return dict(safety_factor=None,
                safety=dict(status="unknown", reason="skipped_in_verification", safety_factor=None),
                conditions=dict(status="unknown", reason="skipped_in_verification"), material_cells=0,
                neck=dict(events=[], required_mm=float(min_width["requested_mm"])))


def _count(vertices, faces, f32):
    vv = np.asarray(vertices, dtype=np.float64)
    if f32:
        vv = vv.astype(np.float32).astype(np.float64)
    return triangle_intersections(vv, faces)


def _mesh(solid):
    m = solid.to_mesh64()
    p = np.asarray(m.vert_properties, dtype=np.float64)[:, :3]
    return weld_exact(p[np.asarray(m.tri_verts, dtype=np.int64)])


def staged_finalize(name):
    real = fz.finalize

    def run(vertices, faces, preserve, exclude, progress=None):
        vertices, faces = np.asarray(vertices, dtype=np.float64), np.asarray(faces)
        rec = dict(input=_count(vertices, faces, False)["counts"], bodies=[])
        solids = dict(preserve=[], exclude=[])
        for role, shapes in (("preserve", preserve), ("exclude", exclude)):
            for i, shape in enumerate(shapes):
                bv, bf, _ = fz.tessellate_body(shape)
                rec["bodies"].append(dict(role=role, index=i, bbox=[bv.min(0).round(3).tolist(), bv.max(0).round(3).tolist()]))
                solids[role].append(fz._manifold(f"{role}-{i}", bv, bf))
        result = manifold3d.Manifold.batch_boolean([fz._manifold("smoothed", vertices, faces), *solids["preserve"]],
                                                   manifold3d.OpType.Add)
        if solids["exclude"]:
            result = result - manifold3d.Manifold.batch_boolean(solids["exclude"], manifold3d.OpType.Add)
        raw = _mesh(result)
        rec["boolean_f64"], rec["boolean_f32"] = _count(*raw, False)["counts"], _count(*raw, True)["counts"]
        for tol in (1e-5, 1e-4, 1e-3):
            sv, sf = _mesh(result.as_original().simplify(tol))
            cv, cf, collapsed = fz.collapse_float32_slivers(sv, sf)
            final = _count(cv, cf, True)
            rec[f"simplify_{tol:g}"] = dict(simplified_f64=_count(sv, sf, False)["counts"], final_f32=final["counts"],
                                            collapsed_edges=collapsed,
                                            pairs_centre_mm=[cv[cf[[a, b]]].reshape(-1, 3).mean(0).round(3).tolist()
                                                             for a, b, _ in final["pairs"][:20]])
        RECORDS.mkdir(parents=True, exist_ok=True)
        (RECORDS / f"{name}.stages.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        return real(vertices, faces, preserve, exclude, progress=progress)

    return patch.object(fz, "finalize", run)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("name")
    ap.add_argument("--stages", action="store_true")
    a = ap.parse_args()
    v.RECORDS = RECORDS
    with patch.object(final_check, "inspect", skipped_final_check), percentile_rule():
        if a.stages:
            with staged_finalize(a.name):
                return v.run_one(a.name)
        return v.run_one(a.name)


if __name__ == "__main__":
    sys.exit(main())
