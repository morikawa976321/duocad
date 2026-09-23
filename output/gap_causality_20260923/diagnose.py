import json, sys, time
from pathlib import Path
OUT=Path(__file__).resolve().parent; ROOT=OUT.parents[1]
sys.path.insert(0,str(ROOT))
import numpy as np
from OCP.BRepTools import BRepTools
from OCP.BRep import BRep_Builder,BRep_Tool
from OCP.TopoDS import TopoDS_Shape,TopoDS
from OCP.TopExp import TopExp,TopExp_Explorer
from OCP.TopAbs import TopAbs_EDGE,TopAbs_FACE
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
from OCP.TopLoc import TopLoc_Location
from OCP.BRepAdaptor import BRepAdaptor_Curve,BRepAdaptor_Surface
from OCP.GCPnts import GCPnts_AbscissaPoint
from OCP.Geom2d import Geom2d_TrimmedCurve
from OCP.Geom2dAPI import Geom2dAPI_InterCurveCurve
from duocad import kernel

BREP=Path(r'C:\Users\tm976\AppData\Local\Temp\claude\C--Users-tm976-mf-duocad\777b2e63-8a37-48c2-90e9-bbf7fb26f7a9\scratchpad\final.brep')
def load():
 s=TopoDS_Shape();BRepTools.Read_s(s,str(BREP),BRep_Builder());return s
def xy(p):return [p.X(),p.Y()]
def xyz(p):return [p.X(),p.Y(),p.Z()]
def crosses(a,b):
 hits=[]
 for i,(p,q) in enumerate(zip(a[:-1],a[1:])):
  v=q-p
  for j,(r,s) in enumerate(zip(b[:-1],b[1:])):
   w=s-r;det=v[0]*w[1]-v[1]*w[0]
   if det==0:continue
   d=r-p;t=(d[0]*w[1]-d[1]*w[0])/det;u=(d[0]*v[1]-d[1]*v[0])/det
   if 0<t<1 and 0<u<1:hits.append(dict(segments=[i,j],fraction=[float(t),float(u)],point=(p+t*v).tolist()))
 return hits

def main():
 s=load();fm=kernel._face_map(s);t=time.perf_counter();payload=kernel.tessellate(s,.2)
 print('meshed',time.perf_counter()-t,flush=True)
 adj=TopTools_IndexedDataMapOfShapeListOfShape();TopExp.MapShapesAndAncestors_s(s,TopAbs_EDGE,TopAbs_FACE,adj)
 rows=[]
 for fi in [74485,74486,74499,7491,8272,11842]:
  f=TopoDS.Face_s(fm.FindKey(fi)); loc=TopLoc_Location();tri=BRep_Tool.Triangulation_s(f,loc)
  BRepTools.Write_s(f,str(OUT/f'face_{fi}.brep'))
  edges=[];curves=[];exp=TopExp_Explorer(f,TopAbs_EDGE)
  while exp.More():
   e=TopoDS.Edge_s(exp.Current());exp.Next();c=BRepAdaptor_Curve(e);pc=BRep_Tool.CurveOnSurface_s(e,f,0.,0.)
   source=fi;ploc=loc;pt=tri;poly=BRep_Tool.PolygonOnTriangulation_s(e,pt,ploc) if pt is not None else None
   if poly is None:
    for nf in adj.FindFromKey(e):
     ploc=TopLoc_Location();pt=BRep_Tool.Triangulation_s(TopoDS.Face_s(nf),ploc)
     if pt is None:continue
     poly=BRep_Tool.PolygonOnTriangulation_s(e,pt,ploc)
     if poly is not None:source=fm.FindIndex(nf);break
   pars=[poly.Parameter(i) for i in range(1,poly.NbNodes()+1)]
   uv=np.array([xy(pc.Value(u)) for u in pars]);points=[xyz(pt.Node(poly.Node(i)).Transformed(ploc.Transformation())) for i in range(1,poly.NbNodes()+1)]
   mids=[(a+b)/2 for a,b in zip(pars[:-1],pars[1:])]
   sag=[float(np.linalg.norm(np.array(xy(pc.Value(u)))-(uv[k]+uv[k+1])/2)) for k,u in enumerate(mids)]
   row=dict(length=GCPnts_AbscissaPoint.Length_s(c),type=str(c.GetType()),tolerance=BRep_Tool.Tolerance_s(e),
            adjacent=[dict(face=fm.FindIndex(nf),type=str(BRepAdaptor_Surface(TopoDS.Face_s(nf)).GetType())) for nf in adj.FindFromKey(e)],
            source_face=source,params=pars,uv=uv.tolist(),points=points,midpoint_chord_error_mm=sag)
   edges.append(row);curves.append(Geom2d_TrimmedCurve(pc,c.FirstParameter(),c.LastParameter()))
  pairs=[]
  for i in range(len(edges)):
   for j in range(i+1,len(edges)):
    hits=crosses(np.array(edges[i]['uv']),np.array(edges[j]['uv']))
    actual=Geom2dAPI_InterCurveCurve(curves[i],curves[j],1e-10)
    ints=[]
    ends=[edges[k]['uv'][n] for k in [i,j] for n in [0,-1]]
    for n in range(1,actual.NbPoints()+1):
     p=xy(actual.Point(n));ints.append(dict(uv=p,distance_to_nearest_endpoint_mm=min(float(np.linalg.norm(np.array(p)-q)) for q in ends)))
    if hits or ints:pairs.append(dict(edges=[i,j],polyline_crossings=hits,curve_intersections=ints,curve_overlap_segments=actual.NbSegments()))
  row=dict(face=fi,triangles=0 if tri is None else tri.NbTriangles(),area=kernel._face_props(f)[0],edges=edges,pairs=pairs)
  rows.append(row);print(json.dumps(dict(face=fi,triangles=row['triangles'],edges=len(edges),crossings=sum(len(p['polyline_crossings']) for p in pairs),curve_intersections=[p['curve_intersections'] for p in pairs])),flush=True)
  (OUT/'diagnosis.json').write_text(json.dumps(rows,indent=2))
if __name__=='__main__':main()
