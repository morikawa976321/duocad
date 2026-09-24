"""`voxelize` の内外判定と表面検査が、辺に乗るレイ・複数ソリッドのまとめた形・列の間に収まる形で壊れないこと。"""
import numpy as np
import pytest
from build123d import Box, Pos

from duocad import kernel
from duocad.topopt import voxelize as vx


def _prism(x0, x1, y0, y1, ym, z0, z1, *, inward=False):
    """xy が長方形の角柱。上下の面を y=ym の直線で2つに割る(割った辺は -x と +x の側面の頂点で止まる)。"""
    polygon = [(x0, y0), (x1, y0), (x1, ym), (x1, y1), (x0, y1), (x0, ym)]
    n = len(polygon)
    positions = [(x, y, z0) for x, y in polygon] + [(x, y, z1) for x, y in polygon]
    a, b, e, c, d, f = range(n)
    triangles = []
    for p, q, r in ((a, b, e), (a, e, f), (f, e, c), (f, c, d)):
        triangles += [(p + n, q + n, r + n), (p, r, q)]
    for i in range(n):
        j = (i + 1) % n
        triangles += [(i, j, j + n), (i, j + n, i + n)]
    triangles = np.array(triangles)
    return np.array(positions, float), triangles[:, ::-1] if inward else triangles


def _soup(*parts):
    positions, triangles, faces, base = [], [], [], 0
    for k, (points, tris) in enumerate(parts):
        positions.append(points)
        triangles.append(tris + base)
        faces.append(np.full(len(tris), k + 1))
        base += len(points)
    return vx.TriangleSoup(positions=np.concatenate(positions), triangles=np.concatenate(triangles),
                           face_index=np.concatenate(faces))


def test_a_ray_along_a_shared_edge_does_not_fill_the_cavity():
    """落ちたら、レイが同じ向きの2枚の共有辺をちょうど通ると両方に数え、列の巻き数が 2,1,2,0 と釣り合ったまま
    空洞が材料として埋まる(issue #40)。"""
    grid = vx.Grid((0.0, 0.0, 0.0), 1.0, (5, 5, 5))
    ray_y = 2.5 + vx.RAY_JITTER_SEQUENCE[0][1]
    soup = _soup(_prism(0.2, 4.8, 0.2, 4.8, ray_y, 0.1, 4.9),
                 _prism(1.9, 3.1, 1.9, 3.1, 2.5, 0.9, 4.1, inward=True))
    vx.inspect_soup(soup)
    with pytest.raises(ValueError, match="辺か頂点"):
        vx.occupancy(soup, grid, jitter=vx.RAY_JITTER_SEQUENCE[0])
    mask, stats = vx.occupancy(soup, grid)
    assert stats['jitter_attempts'] > 1
    assert mask[2, 2].tolist() == [True, False, False, False, True]
    assert int(mask.sum()) == 125 - 3


@pytest.mark.parametrize('second', [
    Pos(10, 0, 0) * Box(10, 10, 10),   # 面で接する
    Pos(0, 0, 3) * Box(10, 10, 10),    # 重なる
    Box(4, 4, 4),                      # 入れ子
], ids=['touching', 'overlapping', 'nested'])
def test_a_compound_of_solids_is_inspected_solid_by_solid(second):
    """落ちたら、まとめた形(`make_compound`)が1つの閉じた表面として検査され、面で接する・重なる・入れ子の
    ボディ(組み立てたモデルでは普通の入力)で「重複辺」「殻の向き」の例外になる(issue #28)。"""
    first = Box(10, 10, 10)
    compound = kernel.make_compound([first.wrapped, second.wrapped])
    soup = vx.triangles_from_shape(compound, deviation=0.1)
    report = vx.inspect_soup(soup)
    assert report['solids'] == 2
    grid = vx.grid_for_bbox(*soup.bbox, 10)
    mask, _ = vx.occupancy(soup, grid)
    alone, _ = vx.occupancy(vx.triangles_from_shape(first.wrapped, deviation=0.1), grid)
    assert np.all(mask[alone])


def test_a_broken_solid_inside_a_compound_is_still_rejected():
    """落ちたら、ソリッドごとに分けて見るようにしたせいで、壊れた殻(空洞の殻だけ裏返した)を持つ入力が通る。"""
    ray_y = 2.5 + vx.RAY_JITTER_SEQUENCE[0][1]
    broken = _soup(_prism(0.2, 4.8, 0.2, 4.8, ray_y, 0.1, 4.9), _prism(1.9, 3.1, 1.9, 3.1, 2.5, 0.9, 4.1))
    solid = vx.TriangleSoup(positions=broken.positions, triangles=broken.triangles, face_index=broken.face_index,
                            solid_index=np.zeros(broken.triangle_count, dtype=np.int64))
    with pytest.raises(ValueError, match="殻の向き"):
        vx.inspect_soup(solid)


def test_a_shape_between_the_column_centres_is_zero_cells_only_when_allowed():
    """落ちたら、列の中心を1本も通らない薄い形が「ずらしが全部縮退した」として落ちる(issue #29・#34)、
    または 0 セルを許していない呼び出しで黙って空のマスクが返る。"""
    grid = vx.Grid((0.0, 0.0, 0.0), 2.0, (6, 6, 6))
    wall = vx.triangles_from_shape((Pos(6.0, 6.0, 6.0) * Box(0.5, 10.0, 10.0)).wrapped, deviation=0.05)
    mask, stats = vx.occupancy(wall, grid, allow_empty=True)
    assert not mask.any() and stats['empty']
    with pytest.raises(vx.NoRayCrossings):
        vx.occupancy(wall, grid)
