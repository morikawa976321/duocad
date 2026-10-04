# 安定版の導入(やり直しても壊れない)。docs/STABLE.md
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\stable\install.ps1 [-ImportModelsFrom <dir>]
#
# 1. 安定版のcloneを作る(既にあれば触らない。更新は画面のボタンか update.ps1)
# 2. uv sync --frozen / npm ci / npm run build
# 3. 制作データの置き場(models/ export/ logs/)を作る。-ImportModelsFrom の *.json を写す(上書きしない)
# 4. スタートメニューに「duocad」を登録する(Win -> duocad -> Enter で起動)
# 5. 安定版のフォルダで開いた Claude Code だけが安定版のMCP(8776)へ繋ぐよう、local スコープで登録する
#
# **このファイルはUTF-8(BOM付き)で保つ**(update.ps1 と同じ理由)。

param(
    [string]$StableRoot = (Join-Path $env:USERPROFILE 'duocad-stable'),
    [string]$DataDir = (Join-Path $env:USERPROFILE 'duocad-data'),
    [string]$Remote = '',
    [string]$Branch = 'main',
    [string]$ImportModelsFrom = '',
    [int]$Port = 8775,
    [int]$HttpPort = 8776
)

$ErrorActionPreference = 'Stop'
$devRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $Remote) { $Remote = (& git -C $devRoot remote get-url origin).Trim() }

function Run([string]$exe, [string[]]$arguments, [string]$cwd) {
    Write-Host "> $exe $($arguments -join ' ')" -ForegroundColor DarkGray
    Push-Location $cwd
    try {
        & $exe @arguments
        if ($LASTEXITCODE -ne 0) { throw "$exe failed (exit $LASTEXITCODE)" }
    } finally { Pop-Location }
}

# ---- 1. clone ----
if (Test-Path (Join-Path $StableRoot '.git')) {
    Write-Host "安定版のcloneは既にある: $StableRoot"
} else {
    Run 'git' @('clone', '--branch', $Branch, $Remote, $StableRoot) $env:USERPROFILE
}

# ---- 2. 依存とビルド ----
Run 'uv' @('sync', '--frozen') $StableRoot
$web = Join-Path $StableRoot 'web'
Run 'npm.cmd' @('ci') $web
Run 'npm.cmd' @('run', 'build') $web

# ---- 3. 制作データ ----
foreach ($sub in 'models', 'export', 'logs') {
    New-Item -ItemType Directory -Force -Path (Join-Path $DataDir $sub) | Out-Null
}
if ($ImportModelsFrom) {
    Get-ChildItem -Path $ImportModelsFrom -Filter '*.json' -File | ForEach-Object {
        $dest = Join-Path (Join-Path $DataDir 'models') $_.Name
        if (Test-Path $dest) { Write-Host "skip (既にある): $($_.Name)" }
        else { Copy-Item $_.FullName $dest; Write-Host "copied: $($_.Name)" }
    }
}

# ---- 4. スタートメニュー ----
$python = Join-Path $StableRoot '.venv\Scripts\python.exe'
$pythonw = Join-Path $StableRoot '.venv\Scripts\pythonw.exe'
$iconDir = Join-Path $env:LOCALAPPDATA 'duocad'
$icon = Join-Path $iconDir 'duocad.ico'
Run $python @((Join-Path $StableRoot 'tools\stable\make_icon.py'), $icon) $StableRoot

$lnk = Join-Path ([Environment]::GetFolderPath('Programs')) 'duocad.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($lnk)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = "-m duocad.launcher --port $Port --http-port $HttpPort --data-dir `"$DataDir`""
$shortcut.WorkingDirectory = $StableRoot
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = 'duocad (安定版)'
$shortcut.Save()
Write-Host "スタートメニューに登録: $lnk"

# ---- 5. MCP(local スコープ = このフォルダで開いたセッションだけ) ----
$claude = Get-Command claude -ErrorAction SilentlyContinue
if ($claude) {
    Push-Location $StableRoot
    try {
        & claude mcp remove --scope local duocad 2>$null | Out-Null
        & claude mcp add --scope local --transport http duocad "http://127.0.0.1:$HttpPort/mcp"
    } finally { Pop-Location }
} else {
    Write-Warning "claude が見つからない。安定版のフォルダで次を実行すること: claude mcp add --scope local --transport http duocad http://127.0.0.1:$HttpPort/mcp"
}

Write-Host ''
Write-Host "完了。Win キー -> duocad -> Enter で起動する(初回はサーバの起動に十数秒かかる)。" -ForegroundColor Green
