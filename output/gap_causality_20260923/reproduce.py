import sys,json,time
from pathlib import Path
OUT=Path(__file__).resolve().parent;ROOT=OUT.parents[1];sys.path[:0]=[str(ROOT),str(OUT)]
import numpy as np
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox,BRepPrimAPI_MakeCylinder,BRepPrimAPI_MakeHalfSpace
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepTools import BRepTools
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Pnt,gp_Dir,gp_Ax2,gp_Vec,gp_Pln
from duocad import kernel
from duocad.kernel_brep import boolean,is_valid
from duocad.mesh_geometry import topology
from duocad.session import Session
from diagnose import load

def stats(shape,dev):
 p=kernel.tessellate(shape,dev)
 tri=np.concatenate([np.asarray(f['positions']).reshape(-1,3)[np.asarray(f['indices'],dtype=int).reshape(-1,3)] for f in p['faces'] if f['indices']])
 v,inv=np.unique(tri.reshape(-1,3),axis=0,return_inverse=True);f=inv.reshape(-1,3)
 r,_=topology(v,f)
 return dict(untriangulated=p['untriangulated'],faces=p['face_count'],**r)

run=ROOT/'output/optimization_runs/20260921T185659-b6799e00457d'
session=Session(None,.2,models_dir=run);session.open_document('input.json');doc=session.document
preserve=doc.get_body(doc.optimize.preserve_bodies[3]).shape
source=load();face=TopoDS.Face_s(kernel._face_map(source).FindKey(74485));plane=BRepAdaptor_Surface(face).Plane()
normal=np.array(kernel._face_normal(face));center=np.array(kernel._face_props(face)[1])
rows=[]
for shift in (0.,.01,-.01):
 pl=gp_Pln(plane.Position());pl.Translate(gp_Vec(*(normal*shift)))
 half=BRepPrimAPI_MakeHalfSpace(BRepBuilderAPI_MakeFace(pl).Face(),gp_Pnt(*(center-normal))).Solid()
 box=BRepPrimAPI_MakeBox(gp_Pnt(-3.5,-5.,-63.5),gp_Pnt(-.5,-4.,-60.5)).Solid()
 mat=BRepAlgoAPI_Common(box,half).Shape()
 for radius in (2.6,2.599,2.59,2.5):
  exclude=BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0,-5,-60),gp_Dir(0,1,0)),radius,5).Solid()
  result,diag=boolean(mat,[preserve,exclude],'cut')
  name=f'local_shift{shift}_radius{radius}'
  BRepTools.Write_s(result,str(OUT/(name+'.brep')))
  row=dict(name=name,radius=radius,plane_shift=shift,valid=is_valid(result),volume=kernel.volume(result),diagnostics=diag)
  try:
   row['mesh']=[dict(deviation=d,**stats(result,d)) for d in (.2,.01,.001)]
  except Exception as e:row['exception']=str(e)
  rows.append(row);(OUT/'reproduction.json').write_text(json.dumps(rows,indent=2));print(json.dumps(row),flush=True)
