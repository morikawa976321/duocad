"""診断記録の実行状況・保存範囲・実際の設定を一覧にする。最適化を再実行しない。"""
import argparse
from collections import Counter
import json
from pathlib import Path

import _bootstrap  # noqa: F401
from duocad.topopt.trace import RunArchive


def summarize(directory):
    directory = Path(directory)
    archive = RunArchive(directory)
    manifest = dict(archive.manifest)
    if archive.incomplete_event_line:
        # 実行中や強制終了直後の末尾の不完全行は、次回読み直せるよう記録して返す。
        manifest['incomplete_event_line'] = True
    counts = Counter(e['name'] for e in archive.snapshots())
    return dict(manifest=manifest, requested_options=archive.last('requested-options'), result=archive.last('result'),
                snapshots=dict(sorted(counts.items())),
                bytes=sum(p.stat().st_size for p in directory.rglob('*') if p.is_file()),
                arrays=len(list((directory/'arrays').glob('*.npz'))),
                phases=[dict(id=e['child'],parent=e['phase'],name=e['name']) for e in archive.begins()])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run',type=Path,help='実行フォルダ')
    args=parser.parse_args()
    print(json.dumps(summarize(args.run),ensure_ascii=False,indent=2,allow_nan=False))


if __name__=='__main__': main()
