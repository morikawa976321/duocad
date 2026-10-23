"""実行中のデバッグ表示と記録済みステップに共通の、ボクセル表示用データ。"""
from __future__ import annotations

#: デバッグ表示(`?debug=space` / `?voxel=1`)で1回に配るボクセルの上限。表示は `InstancedMesh`
#: (立方体1個 = 12三角形)なので、5〜12万要素の塊をそのまま出すと 60〜144万三角形になる。
#: 超えたら軸方向に等間隔で間引き(`debug_voxel_payload` の `stride`)、payload の `stride`/`total`/`shown`
#: に出す。間引きは表示だけで、計算には触れない。
DEBUG_VOXEL_LIMIT = 150_000


def _debug_stride_for(mask, limit: int) -> int:
    """`mask[::s,::s,::s]` の立っている数が `limit` 以下になる最小の `s`。"""
    import numpy as np

    if int(np.count_nonzero(mask)) <= limit:
        return 1
    longest = max(int(n) for n in mask.shape)
    for stride in range(2, longest + 1):
        if int(np.count_nonzero(mask[::stride, ::stride, ::stride])) <= limit:
            return stride
    return longest


def debug_voxel_payload(kind: str, stage: str, grid, mask, *, limit: int) -> dict:
    """立っている要素の通し添字(C順 `i*(ny*nz) + j*nz + k`)を配る。
    座標でなく添字なのは配る量を1/3にするため。画面は `grid.shape` から `(i,j,k)` へ戻す。
    """
    import numpy as np

    mask = np.asarray(mask, dtype=bool)
    total = int(np.count_nonzero(mask))
    stride = _debug_stride_for(mask, int(limit))
    idx = np.argwhere(mask)
    if stride > 1 and idx.size:
        idx = idx[(idx % stride == 0).all(axis=1)]
    _, ny, nz = (int(n) for n in mask.shape)
    flat = idx[:, 0] * (ny * nz) + idx[:, 1] * nz + idx[:, 2]
    return {
        "kind": kind,
        "stage": stage,
        "grid": grid.as_dict(),
        "elements": [int(v) for v in flat.tolist()],
        "total": total,
        "shown": int(flat.shape[0]),
        "stride": int(stride),
    }
