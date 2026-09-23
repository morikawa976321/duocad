"""Extract the two closed solids from reproduce.py's local control geometry."""
import json, sys
from pathlib import Path
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(OUT.parents[1]))
from OCP.BRep import BRep_Builder
from OCP.BRepTools import BRepTools
from OCP.TopoDS import TopoDS_Shape
from OCP.TopAbs import TopAbs_SOLID
from duocad.mesh_geometry import subshapes
from duocad.kernel_brep import inspect_solid

shape = TopoDS_Shape()
BRepTools.Read_s(shape, str(OUT / 'local_shift0.0_radius2.6.brep'), BRep_Builder())
rows = []
for i, solid in enumerate(subshapes(shape, TopAbs_SOLID)):
    BRepTools.Clean_s(solid)
    BRepTools.Write_s(solid, str(OUT / f'minimal_solid_{i}.brep'))
    rows.append(dict(solid=i, inspection=inspect_solid(solid)))
(OUT / 'minimal_validity.json').write_text(json.dumps(rows, indent=2), encoding='utf-8')
print(json.dumps(rows))
