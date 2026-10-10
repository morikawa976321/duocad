"""この clone だけの設定(リポジトリ直下の未追跡ファイル `duocad_settings.json`)。画面の「設定」が書く。

clone ごとに持つので、安定版(`duocad-stable`)と開発版で別の値になる。`tools/stable/update.ps1` も
同じファイルを読む(`fem_device` が `gpu` なら `uv sync` に `--extra gpu` を付ける)—— **鍵の名前と値は
update.ps1 と揃える**(層をまたぐ契約)。

今ある鍵: `fem_device`(`cpu` / `gpu`。形状最適化の CG を解く装置。docs/TOPOPT_GPU.md)。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

FILENAME = "duocad_settings.json"
PATH = Path(__file__).resolve().parents[1] / FILENAME
FEM_DEVICES = ("cpu", "gpu")


def read() -> dict:
    """中身。無い・読めない・形が違うときは空の辞書(例外にしない。設定が壊れていても起動はする)。"""
    try:
        data = json.loads(PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def update(**values) -> None:
    """鍵を書き足す(他の鍵は残す)。一時ファイルに書いてから置き換える(書きかけを読ませない)。"""
    staging = PATH.with_name(PATH.name + ".tmp")
    try:
        staging.write_text(json.dumps({**read(), **values}, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(staging, PATH)
    finally:
        staging.unlink(missing_ok=True)


def fem_device() -> str:
    """`fem_device` の値。無い・知らない値なら `cpu`(既定)。"""
    value = read().get("fem_device")
    return value if value in FEM_DEVICES else "cpu"
