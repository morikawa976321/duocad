"""`tools/stable/install.ps1` の「models/*.json の写し」の段(`-ImportModelsFrom`)を PowerShell で評価する。

install.ps1 そのものは走らせない(clone・ビルド・スタートメニュー・MCP の登録をする)。写しの段の文だけを取り出して回す。
落ちたら、名前に `[` `]` を含む文書が制作データへ写されないのに「copied」と出る。
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from duocad.paths import REPO_ROOT

INSTALL = REPO_ROOT / "tools" / "stable" / "install.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def import_block() -> str:
    text = INSTALL.read_text(encoding="utf-8-sig")
    start = text.index("if ($ImportModelsFrom) {")
    end = text.index("# ---- 4. ")
    return text[start:end]


def run_import(tmp_path, source, data) -> str:
    script = tmp_path / "import.ps1"
    script.write_text(
        f"$ErrorActionPreference = 'Stop'\n$ImportModelsFrom = '{source}'\n$DataDir = '{data}'\n"
        "New-Item -ItemType Directory -Force -Path (Join-Path $DataDir 'models') | Out-Null\n" + import_block(),
        encoding="utf-8-sig",
    )
    done = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True, timeout=120,
    )
    output = (done.stdout + done.stderr).decode("utf-8", "replace")
    assert done.returncode == 0, output
    return output


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell が無い")
def test_documents_with_brackets_in_the_name_are_copied_and_skipped_on_the_second_run(tmp_path) -> None:
    source, data = tmp_path / "dev-models", tmp_path / "data"
    source.mkdir()
    for name in ("part[1].json", "plain.json"):
        (source / name).write_text("{}", encoding="utf-8")

    first = run_import(tmp_path, source, data)

    assert sorted(path.name for path in (data / "models").iterdir()) == ["part[1].json", "plain.json"]
    assert "copied: part[1].json" in first

    second = run_import(tmp_path, source, data)

    # 日本語の部分は端末の文字コードで化けるので、ASCII の部分だけ見る。
    assert [line.split(":")[-1].strip() for line in second.splitlines() if line.startswith("skip")] == [
        "part[1].json", "plain.json",
    ]
    assert "copied" not in second
