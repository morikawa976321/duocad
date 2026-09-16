"""保存済みの平滑化後表面だけを製品と同じブーリアン処理へ渡す。SIMPは実行しない。"""
import argparse
from pathlib import Path
from types import SimpleNamespace

import _bootstrap  # noqa: F401
import numpy as np
from duocad.session import Session
from duocad.topopt import trace
from duocad.topopt.finalize import finalize, solid_mesh
from duocad.verbs import add_solid_body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    args = parser.parse_args()
    candidates = list(args.run.glob('*-pre-boolean-mesh-world.json'))
    world = trace.load_snapshot(candidates[-1] if candidates else next(args.run.glob('*-final-mesh-world.json')))
    session = Session(None, .2, models_dir=args.run)
    session.open_document('input.json')
    doc = session.document
    with trace.recording(doc, root=Path('output/finalization_checks')) as recorder:
        print('Archive:', recorder.path, flush=True)
        shape = finalize(world['vertices'], world['faces'],
                         [doc.get_body(b).shape for b in doc.optimize.preserve_bodies],
                         [doc.get_body(b).shape for b in doc.optimize.exclude_bodies],
                         progress=lambda message: print(message, flush=True))
        vertices, faces = solid_mesh(shape)
        grid = SimpleNamespace(**world['grid'])
        trace.final_mesh((world['raw_vertices']-grid.origin)/grid.spacing,
                         (world['vertices']-grid.origin)/grid.spacing, world['faces'], grid,
                         final_vertices=vertices, final_faces=faces)
        body_id = add_solid_body(doc, shape, 'boolean-finalized')
        recorder.document(doc, 'document_after')
        recorder.snapshot('result', dict(state='done', body_id=body_id, source_run=str(args.run)))
        recorder.finish('done')
        print('PASS:', body_id, flush=True)


if __name__ == '__main__':
    main()
