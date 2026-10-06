"""保存済みの平滑化後表面だけを製品と同じ最終処理(メッシュ集合演算)へ渡す。SIMPは実行しない。

    .venv/Scripts/python.exe topopt_checks/replay_finalization.py output/optimization_runs/<run-id>

記録の `pre-boolean-mesh-world`(無ければ `final-mesh-world`、それも無ければ `finalize-input`)の
頂点・三角形と、`input.json` の保持・除外ボディを `finalize` に渡し、`output/finalization_checks/` に
各段階の検査(`smoothed-mesh-checks` / `cad-meshes` / `mesh-boolean` / `final-mesh-checks`)と、
成功時の文書(メッシュボディ)・`final.stl` を保存する。
"""
import argparse
from pathlib import Path
from types import SimpleNamespace

import _bootstrap  # noqa: F401
from _documents import open_session
from duocad.topopt import trace
from duocad.topopt.finalize import finalize
from duocad.verbs import add_mesh_body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    args = parser.parse_args()
    candidates = (list(args.run.glob('*-pre-boolean-mesh-world.json'))
                  or list(args.run.glob('*-final-mesh-world.json')))
    if candidates:
        world = trace.load_snapshot(candidates[-1])
    else:
        # finalize が失敗した実行は trace.final_mesh まで届かず world snapshot が無い
        # (optimize_run.py は finalize 成功後にしか書かない)。finalize への入力(既にワールド座標の
        # vertices/faces)を代わりに読む。'grid'・'raw_vertices' は無いので末尾の final_mesh 記録は省く。
        world = trace.load_snapshot(next(args.run.glob('*-finalize-input.json')))
    session = open_session(args.run / 'input.json')
    doc = session.document
    with trace.recording(doc, root=Path('output/finalization_checks')) as recorder:
        print('Archive:', recorder.path, flush=True)
        vertices, faces = finalize(world['vertices'], world['faces'],
                                   [doc.get_body(b).shape for b in doc.optimize.preserve_bodies],
                                   [doc.get_body(b).shape for b in doc.optimize.exclude_bodies],
                                   progress=lambda message: print(message, flush=True))
        if 'grid' in world:
            grid = SimpleNamespace(**world['grid'])
            trace.final_mesh((world['raw_vertices']-grid.origin)/grid.spacing,
                             (world['vertices']-grid.origin)/grid.spacing, world['faces'], grid,
                             final_vertices=vertices, final_faces=faces)
        body_id = add_mesh_body(doc, vertices.ravel().tolist(), faces.ravel().tolist(), name='mesh-finalized')
        recorder.document(doc, 'document_after')
        recorder.snapshot('result', dict(state='done', body_id=body_id, source_run=str(args.run),
                                         triangle_count=int(len(faces)), vertex_count=int(len(vertices))))
        recorder.finish('done')
        print('PASS:', body_id, int(len(faces)), 'triangles', flush=True)


if __name__ == '__main__':
    main()
