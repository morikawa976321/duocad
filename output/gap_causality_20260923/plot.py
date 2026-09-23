import sys,json,os
from pathlib import Path
OUT=Path(__file__).resolve().parent;ROOT=OUT.parents[1];sys.path[:0]=[str(ROOT),str(OUT/'deps')]
os.environ['MPLCONFIGDIR']=str(OUT/'mplconfig')
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from OCP.BRep import BRep_Builder,BRep_Tool
from OCP.BRepTools import BRepTools
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.TopoDS import TopoDS_Shape,TopoDS
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_EDGE

row=json.loads((OUT/'diagnosis.json').read_text())[0]
s=TopoDS_Shape();BRepTools.Read_s(s,str(OUT/'face_74485.brep'),BRep_Builder());face=TopoDS.Face_s(s)
curves=[];exp=TopExp_Explorer(face,TopAbs_EDGE)
while exp.More():
 e=TopoDS.Edge_s(exp.Current());exp.Next();c=BRepAdaptor_Curve(e);pc=BRep_Tool.CurveOnSurface_s(e,face,0.,0.)
 params=np.linspace(c.FirstParameter(),c.LastParameter(),2001)
 curves.append(np.array([[pc.Value(t).X(),pc.Value(t).Y()] for t in params]))
polys=[np.array(e['uv']) for e in row['edges']]
base=polys[2][0];direction=polys[2][-1]-base;direction/=np.linalg.norm(direction)
rotation=np.array([direction,[-direction[1],direction[0]]]).T
reference=(curves[2]-base)@rotation
reference=reference[np.argsort(reference[:,0])]
def transform(p):
 q=(p-base)@rotation
 q[...,1]=(q[...,1]-np.interp(q[...,0],reference[:,0],reference[:,1]))*1000
 return q
fig,axes=plt.subplots(1,3,figsize=(15,4.5),layout='constrained')
colors=['#9a9a9a','#2678bf','#da7830'];labels=['Short closing edge','Fillet / plane curve','Cylinder / plane curve']
for i in range(3):
 q=transform(curves[i]);axes[0].plot(q[:,0],q[:,1],color=colors[i],label=labels[i],lw=1.8)
 dense=np.concatenate([np.linspace(a,b,101) for a,b in zip(polys[i][:-1],polys[i][1:])])
 q=transform(dense);axes[1].plot(q[:,0],q[:,1],color=colors[i],lw=1.4,label=labels[i])
 q=transform(polys[i]);axes[1].scatter(q[:,0],q[:,1],color=colors[i],s=9)
for p in row['pairs']:
 for hit in p['polyline_crossings']:
  q=transform(np.array(hit['point']));axes[1].plot(q[0],q[1],'x',color='crimson',markersize=10,mew=2)
for ax,title in zip(axes[:2],['Original CAD boundary (sampled densely)','BRepMesh boundary at 0.2 mm']):
 ax.set_title(title);ax.set_xlabel('Along chord [mm]');ax.set_ylabel('Offset from exact cylinder curve [micrometers]');ax.grid(alpha=.2)
axes[0].legend(fontsize=8,loc='upper left')
t=np.linspace(0,.15,401);gap=.5-np.sqrt(.5**2-t**2)
axes[2].plot(t,gap*1000,color='#7248a5',lw=2)
axes[2].set_title('Gap near the tangent circle')
axes[2].set_xlabel('Axial distance from tangency [mm]');axes[2].set_ylabel('Radial gap [micrometers]');axes[2].grid(alpha=.2)
axes[2].scatter([.07],[1000*(.5-np.sqrt(.5**2-.07**2))],color='#7248a5')
axes[2].annotate('4.924 micrometers at 0.07 mm',(.07,4.924),xytext=(.013,15),arrowprops=dict(arrowstyle='->'),fontsize=9)
fig.suptitle('Face 74485: boundary crossings after tessellation (vertical offsets magnified)',fontsize=14)
fig.savefig(OUT/'mechanism.png',dpi=180)
fig.savefig(OUT/'mechanism.svg')
