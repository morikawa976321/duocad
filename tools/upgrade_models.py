"""保存ファイルを今の版(`OpLog.FORMAT_VERSION`)へ書き換える。

    .venv/Scripts/python.exe tools/upgrade_models.py <models フォルダ> [--write]

`--write` が無ければ何が変わるかを表示するだけ。書くときは元のファイルを `<名前>.json.v<元の版>.bak` に残す。
読むときの書き換え(`duocad/oplog_upgrade.py`)と同じ処理を通すだけなので、書き換えなくても開けば読める。
**版を上げたファイルは古い duocad では開けなくなる**(安定版を更新してから書くこと)。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from duocad.oplog import OpLog  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("models_dir", type=Path)
    parser.add_argument("--write", action="store_true")
    options = parser.parse_args()

    for path in sorted(options.models_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        version = payload.get("version") if isinstance(payload, dict) else None
        if version == OpLog.FORMAT_VERSION:
            print(f"{path.name}: 今の版(version {version})")
            continue
        try:
            log = OpLog.from_payload(payload)
        except ValueError as error:
            print(f"{path.name}: 読めない —— {error}")
            continue
        circles = sum(1 for operation in log.entries if '"circle"' in json.dumps(operation.args))
        print(f"{path.name}: version {version} → {OpLog.FORMAT_VERSION}(円を含む操作 {circles} 件)")
        if options.write:
            shutil.copy2(path, path.with_name(f"{path.name}.v{version}.bak"))
            path.write_text(log.to_json(created=OpLog.created_of(payload)), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
