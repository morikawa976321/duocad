"""topopt不要材料検証(先行工程0)の小型題材2つを、製品と同じSession verbで決定的に生成する。

    .\\.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\build_compact_models.py
    .\\.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\build_compact_models.py --check   # 保存せず要素数だけ見る

`models/topopt_idle_dilution_small.json`(原因1=母数希釈)と `models/topopt_idle_path_small.json`
(原因2=経路喪失)を書く。どちらも `Session` の verb(スケッチ→押し出し→`set_preserve_bodies`/
`set_fixtures`/`set_loads`)だけで組み、`save_document`/`open_document` の操作ログ往復を経る
(手動Body挿入だけの模型は操作ログ再生で消えるので使わない —— タスクの完了条件)。

パラメータは本ファイル冒頭の定数(`DILUTION_*`/`PATH_*`)。値の根拠(荷重・要素サイズの選定過程、
不採用候補)は `docs/topopt_idle_validation/00_COMPACT_MODELS.md` の「パラメータ探索」節。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

from duocad import optimize_run, query  # noqa: E402
from duocad.server import Session  # noqa: E402
from duocad.topopt import design_space as ds  # noqa: E402

MODELS_DIR = REPO / "models"

# ---------------------------------------------------------------------------
# 原因1(母数希釈): 保持リング(穴あき台座)+離れた荷重パッド。
# 穴の内周円筒面(kind="cylinder_full")を拘束する —— 平面拘束と違い、ボクセル境界の
# 階段状の丸まりが荷重の全周で起きるため、実際の保持リング(hanger)に近い応力集中を作る。
# ---------------------------------------------------------------------------
DILUTION_BOSS_W = 24.0
DILUTION_BOSS_D = 24.0
DILUTION_BOSS_H = 12.0
DILUTION_HOLE_R = 4.0
DILUTION_GAP = 36.0  # 台座右端から荷重パッド左端までの距離[mm]
DILUTION_PAD_W = 10.0
DILUTION_PAD_D = 10.0
DILUTION_PAD_H = 12.0
#: 300N・2.0mm・m=2で設計要素19,672・収束時11,046要素(docs 表1参照)。
#: 200Nでは全材料の安全率が探索目標(4.8)にほぼ一致し希釈の余地が無く、500N以上は
#: 全材料でも探索目標を満たせない(既定の代表応力=製品どおりでは`full_material_infeasible`)。
DILUTION_LOAD_N = 300.0

# ---------------------------------------------------------------------------
# 原因2(経路喪失): 離れた2つの支持パッド+偏心荷重パッド。
# 荷重パッドを右支持側へ寄せる(load_x=42, 右支持50..60との隙間8mm)ことで、
# 「腕だけ(右支持だけを厚く使う曲げ)」と「斜材(両支持を使う三角形)」の両方が
# 幾何的に成立する構成にした(docs「原因2の認定」節)。
# ---------------------------------------------------------------------------
PATH_SPAN = 50.0  # 支持パッド間の距離(左パッドのcornerからのオフセット)[mm]
PATH_SUPPORT_W = 10.0
PATH_SUPPORT_D = 20.0
PATH_SUPPORT_H = 10.0
PATH_LOAD_W = 10.0
PATH_LOAD_D = 20.0
PATH_LOAD_H = 10.0
PATH_LOAD_X = 42.0
PATH_LOAD_Z = 30.0
#: 3000Nは全材料の安全率6.6(2.5mm格子)〜13.2(3.0mm)で、探索目標4.8に対して
#: 斜材の有無が体積へ現れる余地を残す(docs 表2参照)。
PATH_LOAD_N = 3000.0

#: 認定形の既定解像度(docs「原因2の認定」節の主表)。
DILUTION_ELEMENT_SIZE_MM = 2.0
DILUTION_MIN_WIDTH_CELLS = 2
PATH_ELEMENT_SIZE_MM = 2.5  # 斜材(三角形)が採用される解像度
PATH_ELEMENT_SIZE_MM_LOCAL_OPT = 2.0  # 腕(曲げ)止まりになる、経路喪失の対照解像度
PATH_MIN_WIDTH_CELLS = 2


def _select(session: Session, refs: list) -> None:
    session.clear_selection()
    session.modify_selection(refs, [])


def _box(session: Session, w: float, d: float, h: float, corner=(0.0, 0.0), body_id=None, mode: str = "new"):
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, list(corner), w, d)
    regions = len(session.state_meta()["sketches"][-1]["regions"])
    _select(session, [{"sketch": sketch_id, "index": regions}])
    session.extrude(h, mode=mode, body_id=body_id)
    session.clear_selection()
    return session.document.bodies[-1]


def _drill_through(session: Session, body, radius: float, center, depth: float):
    """`body` へ円筒の穴を`mode="cut"`で貫通させる(深さは板厚+余裕、面添字は再取得で拾う)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sketch_id, list(center), 2 * radius)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    session.extrude(depth, mode="cut", body_id=body.id)
    session.clear_selection()
    return session.document.get_body(body.id)


def _face_of_kind(body, kind: str) -> int:
    faces = [f for f in query.describe_faces(body.shape) if f["kind"] == kind]
    if len(faces) != 1:
        raise AssertionError(f"{kind} の面がちょうど1枚ではない: {[f['kind'] for f in query.describe_faces(body.shape)]}")
    return int(faces[0]["index"])


def _face_with_normal(body, nx: float, ny: float, nz: float) -> int:
    for face in query.describe_faces(body.shape):
        n = face["normal"]
        if n[0] * nx + n[1] * ny + n[2] * nz > 0.9:
            return int(face["index"])
    raise AssertionError(f"法線 ({nx},{ny},{nz}) の面が見つからない")


def build_dilution_model(
    *,
    boss_w: float = DILUTION_BOSS_W, boss_d: float = DILUTION_BOSS_D, boss_h: float = DILUTION_BOSS_H,
    hole_r: float = DILUTION_HOLE_R, gap: float = DILUTION_GAP,
    pad_w: float = DILUTION_PAD_W, pad_d: float = DILUTION_PAD_D, pad_h: float = DILUTION_PAD_H,
    load_n: float = DILUTION_LOAD_N,
) -> Session:
    """保持リング(拘束)+離れた荷重パッド。`(session)` を返す(呼び出し側で save_document)。"""
    session = Session(None, 0.2)
    boss = _box(session, boss_w, boss_d, boss_h, corner=(0.0, 0.0))
    boss = _drill_through(session, boss, hole_r, (boss_w / 2, boss_d / 2), boss_h + 2.0)
    pad = _box(session, pad_w, pad_d, pad_h, corner=(boss_w + gap, (boss_d - pad_d) / 2))

    ring_face = _face_of_kind(boss, "cylinder_full")
    load_face = _face_with_normal(pad, 0.0, 0.0, 1.0)
    _select(session, [{"body": boss.id, "kind": "body"}, {"body": pad.id, "kind": "body"}])
    session.set_preserve_bodies()
    _select(session, [{"body": boss.id, "kind": "face", "index": ring_face}])
    session.set_fixtures()
    _select(session, [{"body": pad.id, "kind": "face", "index": load_face}])
    session.set_loads(load_n, [0.0, 0.0, -1.0])
    session.clear_selection()
    return session


def build_path_model(
    *,
    span: float = PATH_SPAN, support_w: float = PATH_SUPPORT_W, support_d: float = PATH_SUPPORT_D,
    support_h: float = PATH_SUPPORT_H, load_w: float = PATH_LOAD_W, load_d: float = PATH_LOAD_D,
    load_h: float = PATH_LOAD_H, load_x: float = PATH_LOAD_X, load_z: float = PATH_LOAD_Z,
    load_n: float = PATH_LOAD_N,
) -> Session:
    """離れた2支持パッド(拘束)+偏心荷重パッド。`(session)` を返す。"""
    session = Session(None, 0.2)
    left = _box(session, support_w, support_d, support_h, corner=(0.0, 0.0))
    right = _box(session, support_w, support_d, support_h, corner=(span, 0.0))
    load = _box(session, load_w, load_d, load_h, corner=(load_x, 0.0))
    _select(session, [{"body": load.id, "kind": "body"}])
    session.move_body(0.0, 0.0, load_z)
    session.clear_selection()
    load = session.document.get_body(load.id)

    fixed_l = _face_with_normal(left, -1.0, 0.0, 0.0)
    fixed_r = _face_with_normal(right, 1.0, 0.0, 0.0)
    load_face = _face_with_normal(load, 0.0, 0.0, -1.0)
    _select(session, [{"body": left.id, "kind": "body"}, {"body": right.id, "kind": "body"}, {"body": load.id, "kind": "body"}])
    session.set_preserve_bodies()
    _select(session, [{"body": left.id, "kind": "face", "index": fixed_l}, {"body": right.id, "kind": "face", "index": fixed_r}])
    session.set_fixtures()
    _select(session, [{"body": load.id, "kind": "face", "index": load_face}])
    session.set_loads(load_n, [0.0, 0.0, -1.0])
    session.clear_selection()
    return session


def _check(name: str, session: Session, sizes) -> None:
    bundle = optimize_run.build_conditions(session.document)
    for size in sizes:
        space = ds.derive(bundle.conditions, spacing=size)
        print(f"{name}: size={size}mm elements={space.element_count} shape={tuple(space.grid.shape)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="保存せず要素数の一覧だけ表示する")
    parser.add_argument("--out-dir", type=Path, default=MODELS_DIR)
    args = parser.parse_args()

    dilution = build_dilution_model()
    path = build_path_model()

    if args.check:
        _check("dilution", dilution, (2.5, 2.0, 1.5))
        _check("path", path, (3.0, 2.5, 2.0, 1.5))
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    dilution.models_dir = args.out_dir
    path.models_dir = args.out_dir
    dilution.save_document("topopt_idle_dilution_small.json")
    path.save_document("topopt_idle_path_small.json")

    # 往復検査: 保存した操作ログを新しいSessionで開き直し、条件が消えていないことを確認する。
    for name in ("topopt_idle_dilution_small.json", "topopt_idle_path_small.json"):
        reopened = Session(None, 0.2, models_dir=args.out_dir)
        reopened.open_document(name)
        doc = reopened.document
        assert doc.optimize.fixtures, f"{name}: 拘束面が往復で消えた"
        assert doc.optimize.loads, f"{name}: 荷重面が往復で消えた"
        assert doc.optimize.preserve_bodies, f"{name}: 保持ボディが往復で消えた"
        print(f"{name}: 往復OK(拘束{len(doc.optimize.fixtures)}件・荷重{len(doc.optimize.loads)}件・"
              f"保持{len(doc.optimize.preserve_bodies)}体)")
    print(f"書き出し: {args.out_dir / 'topopt_idle_dilution_small.json'}")
    print(f"書き出し: {args.out_dir / 'topopt_idle_path_small.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
