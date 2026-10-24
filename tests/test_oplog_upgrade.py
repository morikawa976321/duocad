"""版2の保存ファイル(円を半径で持つ)を版3(直径)へ写す `duocad/oplog_upgrade.py` の契約。

落ちたら、版2で保存したモデル(制作データを含む)を開くと円が半分の大きさになるか、開けなくなる。
"""
from __future__ import annotations

import json
import math

import pytest

from duocad.document import Document
from duocad.oplog import OpLog
from duocad.oplog_upgrade import upgrade_operations
from duocad.region import region_ref, regions
from duocad.sketch import add_circle, entity_params, sketch_create
from duocad.verbs import extrude

from duocad import (
    brep_geometry as _brep_geometry,
)


def _as_version_2(document: Document) -> str:
    """今の版で書いた文書を、版2の書き方(円は `radius`)に戻した JSON。"""
    payload = json.loads(document.log.to_json())
    payload["version"] = 2
    for operation in payload["operations"]:
        if operation["verb"] == "add_circle":
            params = operation["args"]["params"]
            operation["args"]["params"] = {
                ("radius" if key == "diameter" else key): (value / 2.0 if key == "diameter" else value)
                for key, value in params.items()
            }
    return json.dumps(payload)


def test_a_version_2_file_opens_with_the_same_circle_and_saves_as_the_current_version():
    document = Document()
    sketch_id = sketch_create(document, "XY")
    entity_id = add_circle(document, sketch_id, (1.0, 2.0), 10.0)
    extrude(document, [region_ref(sketch_id, regions(document, sketch_id)[0].index)], 3.0, mode="new")

    restored = Document()
    restored.log = OpLog.from_json(_as_version_2(document))
    restored.log.replay(restored)

    assert entity_params(restored, sketch_id, entity_id) == {"cx": 1.0, "cy": 2.0, "diameter": 10.0}
    assert _brep_geometry.volume(restored.bodies[0].shape) == pytest.approx(math.pi * 25.0 * 3.0, rel=1e-6)
    assert json.loads(restored.log.to_json())["version"] == OpLog.FORMAT_VERSION
    # 名前つきの値の振り順(`d<n>`)は変わらない: 中心2つのあとに直径。
    assert restored.sketches[0].entities[0].params == {"cx": "d1", "cy": "d2", "diameter": "d3"}


def test_every_place_a_circle_is_recorded_is_converted():
    """図形を記録する操作(追加・取り込み・包む・削除の退避)のどれに入った円も直径になる。"""
    circle = {"kind": "circle", "params": {"cx": 0.0, "cy": 1.0, "radius": 2.5}}
    operations = [
        {"seq": 1, "verb": "add_circle", "args": {"sketch_id": "s", "entity_id": "e", **circle}},
        {"seq": 2, "verb": "import_svg", "args": {"entities": [{"entity_id": "f", **circle}]}},
        {"seq": 3, "verb": "envelope_regions", "args": {"radius": 1.0, "entities": [circle]}},
        {
            "seq": 4,
            "verb": "delete",
            "args": {"entities": [{"entity_id": "e", "index": 0, **circle, "named": ["cx", "cy", "radius"]}]},
        },
        {
            "seq": 5,
            "verb": "delete_items",
            "args": {
                "sketches": [
                    {
                        "sketch_id": "s",
                        "entities": [
                            {"entity_id": "g", "kind": "circle", "refs": {"cx": "d1", "cy": 0.0, "radius": "d3"}},
                            {"entity_id": "h", "kind": "arc", "refs": {"radius": "d4"}},
                        ],
                        "values": {"d1": 7.0, "d3": 4.0, "d4": 9.0},
                    }
                ]
            },
        },
    ]
    upgraded = upgrade_operations(2, operations)

    diameter = {"cx": 0.0, "cy": 1.0, "diameter": 5.0}
    assert upgraded[0]["args"]["params"] == diameter
    assert upgraded[1]["args"]["entities"][0]["params"] == diameter
    assert upgraded[2]["args"]["entities"][0]["params"] == diameter
    assert upgraded[2]["args"]["radius"] == 1.0, "包む verb 自身の R は円ではない"
    assert upgraded[3]["args"]["entities"][0]["params"] == diameter
    assert upgraded[3]["args"]["entities"][0]["named"] == ["cx", "cy", "diameter"]
    spec = upgraded[4]["args"]["sketches"][0]
    assert spec["entities"][0]["refs"] == {"cx": "d1", "cy": 0.0, "diameter": "d3"}
    assert spec["entities"][1]["refs"] == {"radius": "d4"}, "円弧は半径のまま"
    assert spec["values"] == {"d1": 7.0, "d3": 8.0, "d4": 9.0}
    assert operations[0]["args"]["params"]["radius"] == 2.5, "元の並びを書き換えない"


def test_a_version_2_file_with_set_parameter_is_refused():
    """名前つきの値が円の半径かは再生しないと分からないので、黙って読まずに断る。"""
    with pytest.raises(ValueError, match="set_parameter"):
        upgrade_operations(2, [{"seq": 1, "verb": "set_parameter", "args": {"name": "d3", "value": 4.0}}])
