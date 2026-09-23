"""内部モデルのハンドルをモデルより長生きさせると落ちることの再現。各条件を子プロセスで1回ずつ回す。

exit_globals / del_context_first が 3221225477 (0xC0000005) で落ち、del_ordered は 0 で返る
(2026-09-23 実測、cadquery-ocp-novtk 7.9.3.1.1)。製品の解放順は duocad/kernel_meshing.py の _Model.close()。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BODY = r'''
import sys, gc
sys.path.insert(0, r"@ROOT@")
from OCP.BRepTools import BRepTools
from OCP.BRep import BRep_Builder
from OCP.TopoDS import TopoDS_Shape
from OCP.Message import Message_ProgressRange
from duocad.kernel_meshing import _parameters
from OCP.BRepMesh import (BRepMesh_ModelBuilder, BRepMesh_EdgeDiscret, BRepMesh_ModelHealer, BRepMesh_ModelPreProcessor,
                          BRepMesh_FaceDiscret, BRepMesh_MeshAlgoFactory, BRepMesh_ModelPostProcessor)
from OCP.IMeshTools import IMeshTools_Context
s = TopoDS_Shape(); BRepTools.Read_s(s, r"@BREP@", BRep_Builder())
c = IMeshTools_Context(); c.SetModelBuilder(BRepMesh_ModelBuilder()); c.SetEdgeDiscret(BRepMesh_EdgeDiscret())
c.SetModelHealer(BRepMesh_ModelHealer()); c.SetPreProcessor(BRepMesh_ModelPreProcessor())
c.SetFaceDiscret(BRepMesh_FaceDiscret(BRepMesh_MeshAlgoFactory())); c.SetPostProcessor(BRepMesh_ModelPostProcessor())
p = _parameters(.2, .35, True)
for n in ('Deflection','Angle','Relative','InParallel','DeflectionInterior','AngleInterior','MinSize','CleanModel'):
    setattr(c.ChangeParameters(), n, getattr(p, n))
c.SetShape(s); assert c.BuildModel() and c.DiscretizeEdges() and c.HealModel() and c.PreProcessModel()
model = c.GetModel()
faces = [model.GetFace(i) for i in range(model.FacesNb())]
edges = [model.GetEdge(i) for i in range(model.EdgesNb())]
pcs = [e.GetPCurve(j) for e in edges for j in range(e.PCurvesNb())]
assert c.DiscretizeFaces(Message_ProgressRange()) and c.PostProcessModel()
MODE = "@MODE@"
if MODE == 'del_ordered':
    del pcs, edges, faces, model, c; gc.collect()
elif MODE == 'del_context_first':
    del c; gc.collect(); del model; gc.collect(); print('still readable:', pcs[0].ParametersNb(), flush=True)
    del faces, edges; gc.collect(); del pcs; gc.collect()
print('done', MODE, flush=True)
'''
brep = str(ROOT / 'tests' / 'fixtures' / 'gap_minimal_solid.brep')
for mode in ('exit_globals', 'del_ordered', 'del_context_first'):
    script = BODY.replace('@ROOT@', str(ROOT)).replace('@BREP@', brep).replace('@MODE@', mode)
    r = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
    print(mode, 'exit', r.returncode, '|', r.stdout.strip().replace('\n', ' / '))
