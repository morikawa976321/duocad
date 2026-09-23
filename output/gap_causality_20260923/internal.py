import sys,json
from pathlib import Path
OUT=Path(__file__).resolve().parent;ROOT=OUT.parents[1];sys.path[:0]=[str(ROOT),str(OUT)]
from OCP.BRepTools import BRepTools
from OCP.BRep import BRep_Builder
from OCP.TopoDS import TopoDS_Shape
from OCP.BRepMesh import (BRepMesh_IncrementalMesh,BRepMesh_ModelBuilder,BRepMesh_EdgeDiscret,
    BRepMesh_ModelHealer,BRepMesh_ModelPreProcessor,BRepMesh_FaceDiscret,BRepMesh_MeshAlgoFactory,
    BRepMesh_ModelPostProcessor,BRepMesh_FaceChecker)
from OCP.IMeshTools import IMeshTools_Context
from duocad import kernel
from diagnose import load

def context():
 c=IMeshTools_Context();c.SetModelBuilder(BRepMesh_ModelBuilder());c.SetEdgeDiscret(BRepMesh_EdgeDiscret())
 c.SetModelHealer(BRepMesh_ModelHealer());c.SetPreProcessor(BRepMesh_ModelPreProcessor())
 c.SetFaceDiscret(BRepMesh_FaceDiscret(BRepMesh_MeshAlgoFactory()));c.SetPostProcessor(BRepMesh_ModelPostProcessor());return c

rows=[]
for name in sys.argv[1:] or ['minimal_solid_1']:
 if name=='original':s=load();selected=[74485,74486,74499,7491,8272,11842]
 else:
  s=TopoDS_Shape();BRepTools.Read_s(s,str(OUT/(name+'.brep')),BRep_Builder());selected=list(range(1,kernel.face_count(s)+1))
 fm=kernel._face_map(s);BRepTools.Clean_s(s);c=context();mesh=BRepMesh_IncrementalMesh();mesh.SetShape(s)
 pars=mesh.ChangeParameters();pars.Deflection=.2;pars.Angle=.35;pars.Relative=False;pars.InParallel=False
 mesh.Perform(c);model=c.GetModel();print(name,'status',mesh.GetStatusFlags(),mesh.IsDone(),model.FacesNb(),flush=True)
 for i in range(model.FacesNb()):
  df=model.GetFace(i);fi=fm.FindIndex(df.GetFace())
  if fi not in selected:continue
  old=df.GetDeflection();row=dict(shape=name,face=fi,deflection=old,status=df.GetStatusMask(),checks=[])
  for d in [old,.01,.001,1e-5]:
   df.SetDeflection(d);ch=BRepMesh_FaceChecker(df,c.GetParameters());ok=ch.Perform()
   row['checks'].append(dict(deflection=d,checker_accepts=ok))
  df.SetDeflection(old);rows.append(row);print(json.dumps(row),flush=True)
 (OUT/'internal.json').write_text(json.dumps(rows,indent=2))
