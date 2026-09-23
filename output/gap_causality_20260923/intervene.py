import sys,json,time
from pathlib import Path
OUT=Path(__file__).resolve().parent;ROOT=OUT.parents[1];sys.path[:0]=[str(ROOT),str(OUT)]
import numpy as np
from OCP.BRepTools import BRepTools
from OCP.BRep import BRep_Builder
from OCP.TopoDS import TopoDS_Shape,TopoDS
from OCP.BRepMesh import (BRepMesh_IncrementalMesh,BRepMesh_ModelBuilder,BRepMesh_EdgeDiscret,
    BRepMesh_ModelHealer,BRepMesh_ModelPreProcessor,BRepMesh_FaceDiscret,BRepMesh_MeshAlgoFactory,
    BRepMesh_ModelPostProcessor,BRepMesh_FaceChecker)
from OCP.IMeshTools import IMeshTools_Context
from OCP.Message import Message_ProgressRange
from duocad import kernel
from duocad.mesh_geometry import topology

rows=[]
for threshold in (.2,.01,.001,.0001):
 s=TopoDS_Shape();BRepTools.Read_s(s,str(OUT/'minimal_solid_1.brep'),BRep_Builder())
 seed=BRepMesh_IncrementalMesh(s,.2,False,.35,False);p=seed.ChangeParameters()
 c=IMeshTools_Context();c.SetModelBuilder(BRepMesh_ModelBuilder());c.SetEdgeDiscret(BRepMesh_EdgeDiscret())
 c.SetModelHealer(BRepMesh_ModelHealer());c.SetPreProcessor(BRepMesh_ModelPreProcessor())
 c.SetFaceDiscret(BRepMesh_FaceDiscret(BRepMesh_MeshAlgoFactory()));c.SetPostProcessor(BRepMesh_ModelPostProcessor())
 for name in dir(p):
  if name.startswith('_') or callable(getattr(p,name)):continue
  try:setattr(c.ChangeParameters(),name,getattr(p,name))
  except (TypeError,AttributeError):pass
 c.ChangeParameters().CleanModel=False;BRepTools.Clean_s(s);c.SetShape(s);c.BuildModel();c.DiscretizeEdges()
 fm=kernel._face_map(s);model=c.GetModel();r=dict(threshold=threshold,faces=[])
 for i in range(model.FacesNb()):
  df=model.GetFace(i);fi=fm.FindIndex(df.GetFace())
  if fi==1:df.SetDeflection(threshold)
  old=df.GetDeflection();checker=BRepMesh_FaceChecker(df,c.GetParameters())
  r['faces'].append(dict(face=fi,deflection_before=old,accepts_before=checker.Perform()))
 c.HealModel()
 for i in range(model.FacesNb()):
  df=model.GetFace(i);r['faces'][i].update(deflection_after=df.GetDeflection(),status_after=df.GetStatusMask())
 c.PreProcessModel();c.DiscretizeFaces(Message_ProgressRange());c.PostProcessModel()
 tri=[];untri=[]
 for i in range(1,fm.Extent()+1):
  vv,ii=kernel._face_triangles(TopoDS.Face_s(fm.FindKey(i)))
  if not ii:untri.append(i);continue
  tri.append(np.asarray(vv).reshape(-1,3)[np.asarray(ii).reshape(-1,3)])
 v,iv=np.unique(np.concatenate(tri).reshape(-1,3),axis=0,return_inverse=True);f=iv.reshape(-1,3)
 r['untriangulated']=untri;r['topology']=topology(v,f)[0]
 rows.append(r);print(json.dumps(r),flush=True)
 np.savez_compressed(OUT/f'intervention_{threshold}.npz',vertices=v,faces=f)
 (OUT/'intervention.json').write_text(json.dumps(rows,indent=2))
