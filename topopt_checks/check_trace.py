"""製品入口から診断記録を作り、単一導出・全候補の塊一致・途中表面の再現・最終JSON/STLの一致を検査する。"""
from pathlib import Path

import numpy as np
import _bootstrap  # noqa: F401
from _documents import open_session
import check_optimize_wiring as wiring
from duocad.topopt import trace, design_space, mesh
from check_final_shape import read_stl


def verify_archive(path):
    path=Path(path)
    archive=trace.RunArchive(path)
    assert not archive.incomplete_event_line
    records=archive.records
    assert archive.manifest['status']=='done'
    result=records('result')[0]
    # **塊の導出は実行を通して1回だけ**(`derive`ステージがちょうど1回しか始まらない)。
    # 二値探索(`find_best_feasible`)は同じ `design_space_mask` の上で候補を作って検査する
    # だけで、候補ごとに塊を作り直さない。
    assert len(archive.begins('derive'))==1
    derived=records('derive-output')
    assert len(derived)==1 and len(records('derive-masks'))==1
    assert derived[0]['stats']['derivation']==design_space.DERIVATION
    mask_hash=derived[0]['stats']['mask_hash']
    assert design_space.mask_digest(derived[0]['mask'])==mask_hash
    masks=records('derive-masks')[0]
    assert np.array_equal(masks['connected'],derived[0]['mask'])
    assert records('resolved-conditions')
    # **候補中はメッシュ化を呼ばない**(段階5の契約)。`build_surface_from_mask` は
    # 確定した採用マスクに対して1回だけ呼ばれる——旧方式の候補ごとのメッシュ化は無い。
    assert len(archive.begins('surface-from-mask'))==1
    surface_inputs=records('surface-from-mask-input')
    surface_outputs=records('surface-from-mask-output')
    assert len(surface_inputs)==len(surface_outputs)==1
    for inputs,output in zip(surface_inputs,surface_outputs):
        replay=mesh.build_surface_from_mask(**inputs)
        assert np.array_equal(replay.verts_raw,output['verts_raw'])
        assert np.array_equal(replay.verts_smooth,output['verts_smooth'])
        assert np.array_equal(replay.faces,output['faces'])
    assert len(records('taubin-mu'))==sum(i['n_iter'] for i in surface_inputs)
    assert len(records('taubin-lambda'))==len(records('taubin-mu'))
    assert len(records('stress-output'))>=1
    final=records('final-mesh-world')[0]
    saved=open_session(path/'document_after.json')
    body=saved.document.get_body(result['body_id'])
    # 結果は `finalize` が合格させた三角形のメッシュボディ(docs/TOPOPT.md「結果は編集可能ソリッドとして持たない」)。
    # 文書の三角形と記録の final-mesh-world が一致し、最終処理の各段階の検査は finalize 段階の snapshot が持つ。
    assert body.kind=='mesh' and body.mesh is not None
    vertices=np.asarray(body.mesh.positions).reshape(-1,3)
    faces=np.asarray(body.mesh.indices).reshape(-1,3)
    assert np.array_equal(vertices,final['vertices'])
    assert np.array_equal(faces,final['faces'])
    assert records('smoothed-mesh-checks')[-1]['failed']==[]
    assert records('cad-meshes') and records('mesh-boolean')
    final_checks=records('final-mesh-checks')[-1]
    assert final_checks['failed']==[] and final_checks['solids']==1
    v,f=read_stl(path/'final.stl')
    assert np.array_equal(v[f],final['vertices'][final['faces']])
    assert (path/'source.zip').is_file() and list((path/'cad').glob('*.step'))
    print('PASS: full product archive, single derivation shared by every candidate, single final meshing, surface replay, finalize checks, final document/STL equality')
    print('Archive:',path)


def main():
    session,left,right,result,events,ids,shapes,_=wiring.run_once()
    wiring.check_end_to_end(session,result,ids,shapes)
    wiring.check_placement(session,left,right,result)
    wiring.check_progress(events)
    wiring.check_cancel()
    assert not wiring.FAILURES, wiring.FAILURES
    verify_archive(result['diagnostic_path'])


if __name__=='__main__': main()
