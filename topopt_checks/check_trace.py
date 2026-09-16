"""製品入口から診断記録を作り、単一導出・全候補の塊一致・途中表面の再現・最終JSON/STLの一致を検査する。"""
import json
from pathlib import Path
import tempfile

import numpy as np
import _bootstrap  # noqa: F401
import check_optimize_wiring as wiring
from duocad.session import Session
from duocad.topopt import trace, design_space, safety_loop, mesh
from check_final_shape import read_stl


def verify_archive(path):
    path=Path(path)
    events=[json.loads(line) for line in (path/'events.jsonl').read_text(encoding='utf-8').splitlines()]
    snapshots=[e for e in events if e['kind']=='snapshot']
    def records(name):
        return [trace.load_snapshot(path/e['file']) for e in snapshots if e['name']==name]
    manifest=json.loads((path/'manifest.json').read_text(encoding='utf-8'))
    assert manifest['status']=='done'
    result=records('result')[0]
    evaluations=records('safety-evaluation-output')
    assert len(evaluations)==result['evaluations']
    # **塊の導出は実行を通して1回だけ**で、旧方式(参照 run・候補ごとの切り出し)の段階は出ない。
    derived=records('derive-output')
    assert len(derived)==1 and len(records('derive-masks'))==1
    assert not [e for e in snapshots if e['name'].startswith(('reference','derive-bbox','derive-candidate'))]
    assert derived[0]['stats']['derivation']==design_space.DERIVATION
    mask_hash=derived[0]['stats']['mask_hash']
    assert design_space.mask_digest(derived[0]['mask'])==mask_hash
    masks=records('derive-masks')[0]
    assert np.array_equal(masks['connected'],derived[0]['mask'])
    # **全候補が同じ塊(同じマスク・同じ格子)を使っている。**
    candidates=records('candidate-input')
    assert len(candidates)==result['evaluations']
    for candidate in candidates:
        assert candidate['space']['stats']['mask_hash']==mask_hash
        assert np.array_equal(candidate['space']['mask'],derived[0]['mask'])
        assert candidate['space']['grid']==derived[0]['grid']
    assert records('boundary-conditions')
    for phase in [e['child'] for e in events if e['kind']=='begin' and e['name']=='simp']:
        counts={name:sum(e['phase']==phase and e['name']==name for e in snapshots)
                for name in ('iteration-input','iteration-analysis','iteration-output')}
        assert counts['iteration-input']>=counts['iteration-analysis']>=counts['iteration-output']
        if any(e['kind']=='end' and e['phase']==phase for e in events):
            assert len(set(counts.values()))==1
        else:
            assert any(e['kind']=='error' and e['phase']==phase for e in events)
    surface_inputs=records('surface-input');surface_outputs=records('surface-output')
    assert len(surface_inputs)==len(surface_outputs)
    for inputs,output in zip(surface_inputs,surface_outputs):
        replay=mesh.build_surface(**inputs)
        assert np.array_equal(replay.verts_raw,output['verts_raw'])
        assert np.array_equal(replay.verts_smooth,output['verts_smooth'])
        assert np.array_equal(replay.faces,output['faces'])
    assert len(records('taubin-mu'))==sum(i['n_iter'] for i in surface_inputs)
    assert len(records('taubin-lambda'))==len(records('taubin-mu'))
    assert len(records('stress-output'))==sum(e[0]=='ok' for e in evaluations)
    final=records('final-mesh-world')[0]
    saved=Session(None,.2,models_dir=path)
    saved.open_document('document_after.json')
    body=saved.document.get_body(result['body_id'])
    from duocad.topopt.finalize import solid_mesh
    from duocad.kernel_brep import inspect_solid
    vertices, faces = solid_mesh(body.shape)
    assert np.allclose(vertices,final['vertices'],rtol=0,atol=1e-12)
    assert np.array_equal(faces,final['faces'])
    assert inspect_solid(body.shape)['watertight']
    assert inspect_solid(body.shape)['single_solid']
    assert records('preserve-union') and records('final-solid')
    v,f=read_stl(path/'final.stl')
    assert np.array_equal(v[f],final['vertices'][final['faces']])
    assert (path/'source.zip').is_file() and list((path/'cad').glob('*.step'))
    print('PASS: full product archive, single derivation shared by every candidate, all iteration stages, surface replay, final document/STL equality')
    print('Archive:',path)


def main():
    with tempfile.TemporaryDirectory(prefix='duocad-trace-check-') as temporary:
        original_search=safety_loop._SEARCH_STL_PATH
        try:
            safety_loop._SEARCH_STL_PATH=Path(temporary)/'search.stl'
            session,left,right,result,events,ids,shapes,_=wiring.run_once()
            wiring.check_end_to_end(session,result,ids,shapes)
            wiring.check_placement(session,left,right,result)
            wiring.check_progress(events)
            wiring.check_cancel()
            assert not wiring.FAILURES, wiring.FAILURES
            verify_archive(result['diagnostic_path'])
        finally:
            safety_loop._SEARCH_STL_PATH=original_search


if __name__=='__main__': main()
