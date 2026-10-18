"""記録された採用形状と入力CADを比較する。元データや最適化ロジックは変更しない。"""
import argparse
import json
from pathlib import Path

import _bootstrap  # noqa: F401
from _documents import open_session
import _record
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt
import check_final_shape as check
from duocad.topopt.trace import load_snapshot


def load_world(run: Path) -> dict:
    """平滑化の前後の頂点(`raw_vertices` / `vertices`)と面を持つ記録。集合演算の前に記録される
    (`final-mesh-world` は集合演算後の最終メッシュで `raw_vertices` を持たない)。"""
    return load_snapshot(next(run.glob('*-pre-boolean-mesh-world.json')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    args = parser.parse_args()
    run = args.run
    out = run/'analysis'
    out.mkdir(exist_ok=True)
    world = load_world(run)
    session = open_session(run / 'input.json')
    doc = session.document
    def save(name, data):
        _record.write(out / name, data)
    preserve = [(bid, doc.get_body(bid).shape) for bid in doc.optimize.preserve_bodies]
    exclude = [(bid, doc.get_body(bid).shape) for bid in doc.optimize.exclude_bodies]
    rows = []
    for stage, vertices in [('raw', world['raw_vertices']), ('smooth', world['vertices'])]:
        bbox = BRepPrimAPI_MakeBox(gp_Pnt(*vertices.min(0)), gp_Pnt(*vertices.max(0))).Shape()
        for bid, shape in preserve:
            row = dict(stage=stage, body=bid, name=doc.get_body(bid).name,
                       keep_volume_mm3=check.kernel.volume(shape),
                       **check.boolean_remainder(shape, bbox, check.BOPAlgo_Operation.BOPAlgo_CUT))
            rows.append(row)
    save('preserve_bbox_witness.json', dict(method='CAD minus mesh bounding box: lower bound on missing preserve volume, not complete intersection test', bodies=rows))
    print(json.dumps(rows), flush=True)
    from duocad import mesh_geometry
    original = mesh_geometry.BOPAlgo_CheckerSI
    class RecordingChecker:
        def __init__(self):
            self.inner = original()
        def __getattr__(self, name):
            return getattr(self.inner, name)
        def Perform(self):
            self.inner.Perform()
            ds = self.inner.DS()
            rows = []
            for pair in ds.Interferences():
                shapes = [ds.Shape(i) for i in pair.Indices()]
                rows.append(dict(types=[str(s.ShapeType()) for s in shapes],
                                 bounds=[check.kernel.bounding_box(s) for s in shapes]))
            save('intersections.json', rows)
    mesh_geometry.BOPAlgo_CheckerSI = RecordingChecker
    _, components = check.topology(world['vertices'], world['faces'])
    check.make_material(world['vertices'], world['faces'], components)
    mesh_geometry.BOPAlgo_CheckerSI = original
    print('Final interference locations saved; checking raw mesh against all CAD conditions', flush=True)
    report = check.evaluate(world['raw_vertices'], world['faces'], preserve, exclude,
                            progress=lambda text: print(text, flush=True), check_constraints=True)
    save('acceptance_raw.json', report)
    print(json.dumps(report, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
