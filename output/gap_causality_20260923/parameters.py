import sys,json,time
from pathlib import Path
OUT=Path(__file__).resolve().parent;ROOT=OUT.parents[1];sys.path[:0]=[str(ROOT),str(OUT)]
import numpy as np
from OCP.BRepTools import BRepTools
from OCP.BRep import BRep_Builder
from OCP.TopoDS import TopoDS_Shape,TopoDS
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.IMeshTools import IMeshTools_Parameters
from duocad import kernel
from duocad.mesh_geometry import topology
from diagnose import load
name=sys.argv[1] if len(sys.argv)>1 else 'minimal_solid_1'
if name=='original':s=load()
else:
 s=TopoDS_Shape();BRepTools.Read_s(s,str(OUT/(name+'.brep')),BRep_Builder())
fm=kernel._face_map(s);rows=[]
for force,interior in [(False,.2),(False,.001),(True,.2),(True,.01),(True,.001),(True,.0001)]:
 BRepTools.Clean_s(s);p=IMeshTools_Parameters();p.Deflection=.2;p.Angle=.35;p.Relative=False;p.InParallel=False
 p.DeflectionInterior=interior;p.ForceFaceDeflection=force;p.ControlSurfaceDeflection=False
 t=time.perf_counter();mesh=BRepMesh_IncrementalMesh(s,p);tri=[];missing=[]
 for i in range(1,fm.Extent()+1):
  v,f=kernel._face_triangles(TopoDS.Face_s(fm.FindKey(i)))
  if not f:missing.append(i);continue
  tri.append(np.asarray(v).reshape(-1,3)[np.asarray(f).reshape(-1,3)])
 v,inv=np.unique(np.concatenate(tri).reshape(-1,3),axis=0,return_inverse=True);f=inv.reshape(-1,3)
 row=dict(shape=name,force=force,interior=interior,control_surface=False,status=mesh.GetStatusFlags(),done=mesh.IsDone(),missing=missing,seconds=time.perf_counter()-t,topology=topology(v,f)[0])
 rows.append(row);(OUT/(name+'_parameters.json')).write_text(json.dumps(rows,indent=2));print(json.dumps(row),flush=True)
 np.savez_compressed(OUT/f'{name}_force{force}_interior{interior}.npz',vertices=v,faces=f)
