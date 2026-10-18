# 安定版の導入(やり直しても壊れない)。docs/STABLE.md
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\stable\install.ps1 [-ImportModelsFrom <dir>] [-Commit <sha>]
#
# 1. 安定版のcloneを作る(既にあれば触らない。更新は画面のボタンか update.ps1)。
#    -Commit を渡すと、mainではなくこの開発用チェックアウトにあるそのコミットから始める
#    (pushもPRもせずに導入するとき。**完全なハッシュ**を渡す —— ローカルのリポジトリからの fetch は
#    短縮ハッシュを解決しない)。ローカルの main をそのコミットに置き、追跡先は origin/main のまま。
#    mainがそのコミットを取り込むまで更新ボタンは出ない(docs/STABLE.md)
# 2. uv sync --frozen(GPU の部品を入れる clone なら --extra gpu も) / npm ci / npm run build
# 3. 制作データの置き場(models/ export/ logs/)を作る。-ImportModelsFrom の *.json を写す(上書きしない)
# 4. スタートメニューに「duocad」を登録する(Win -> duocad -> Enter で起動)
# 5. 安定版のフォルダで開いた Claude Code だけが安定版のMCP(8776)へ繋ぐよう、local スコープで登録する
# 6. 安定版のフォルダに CLAUDE.local.md を置く(そこで開いたセッションにコードを触らせない)
#
# **このファイルはUTF-8(BOM付き)で保つ**(update.ps1 と同じ理由)。

param(
    [string]$StableRoot = (Join-Path $env:USERPROFILE 'mf\duocad-stable'),
    [string]$DataDir = (Join-Path $env:USERPROFILE 'mf\duocad-data'),
    [string]$Remote = '',
    [string]$Branch = 'main',
    [string]$ImportModelsFrom = '',
    [string]$Commit = '',
    [int]$Port = 8775,
    [int]$HttpPort = 8776
)

$ErrorActionPreference = 'Stop'
$devRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $Remote) { $Remote = (& git -C $devRoot remote get-url origin).Trim() }

# 外部コマンドの stderr を例外にしない(update.ps1 の Run と同じ理由)。終了コードで判定する。
function Run([string]$exe, [string[]]$arguments, [string]$cwd, [switch]$AllowFailure) {
    Write-Host "> $exe $($arguments -join ' ')" -ForegroundColor DarkGray
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    Push-Location $cwd
    try {
        $global:LASTEXITCODE = 0
        & $exe @arguments
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0 -and -not $AllowFailure) { throw "$exe failed (exit $code)" }
}

# ---- 1. clone ----
if (Test-Path (Join-Path $StableRoot '.git')) {
    Write-Host "安定版のcloneは既にある: $StableRoot"
    if ($Commit) { Write-Warning "-Commit は無視した(cloneが既にある)。入れ直すときは $StableRoot を消してからやり直す" }
} else {
    Run 'git' @('clone', '--branch', $Branch, $Remote, $StableRoot) $env:USERPROFILE
    if ($Commit) {
        Run 'git' @('fetch', $devRoot, $Commit) $StableRoot
        Run 'git' @('checkout', '-B', $Branch, 'FETCH_HEAD') $StableRoot
        Run 'git' @('branch', "--set-upstream-to=origin/$Branch", $Branch) $StableRoot
    }
}

# ---- 2. 依存とビルド ----
# GPU の部品(extra gpu = CuPy)を入れるか。**その版が extra gpu を持ち**(持たない旧版へ巻き戻すとき、付けると
# uv sync が失敗する)、かつ画面の「設定」で GPU を選んでいるか、すでに入っているとき。**extra を付けない
# `uv sync` は入っている CuPy を消す**ので、GPU を切っても入れたままにする(切り直すたびに約 2GB を入れ直さない)。
# 設定ファイルの鍵は duocad/local_settings.py と揃える(docs/TOPOPT_GPU.md)。
function Want-GpuExtra([string]$root) {
    if (-not (Select-String -Path (Join-Path $root 'pyproject.toml') -Pattern '^\s*gpu\s*=' -Quiet)) { return $false }
    if (Test-Path (Join-Path $root '.venv\Lib\site-packages\cupy')) { return $true }
    try {
        $settings = Get-Content -Raw -Encoding UTF8 -Path (Join-Path $root 'duocad_settings.json') | ConvertFrom-Json
        return ($settings.fem_device -eq 'gpu')
    } catch {
        return $false
    }
}
$syncArgs = @('sync', '--frozen')
if (Want-GpuExtra $StableRoot) { $syncArgs += @('--extra', 'gpu') }
Run 'uv' $syncArgs $StableRoot
$web = Join-Path $StableRoot 'web'
Run 'npm.cmd' @('ci') $web
Run 'npm.cmd' @('run', 'build') $web

# ---- 3. 制作データ ----
foreach ($sub in 'models', 'export', 'logs') {
    New-Item -ItemType Directory -Force -Path (Join-Path $DataDir $sub) | Out-Null
}
if ($ImportModelsFrom) {
    # -Path はワイルドカード解釈なので、名前に [ ] を含む文書(part[1].json)は写されず Test-Path も偽になる。
    # Copy-Item は一致なしでもエラーにしないので、写した後に宛先を確かめる。
    Get-ChildItem -LiteralPath $ImportModelsFrom -Filter '*.json' -File | ForEach-Object {
        $dest = Join-Path (Join-Path $DataDir 'models') $_.Name
        if (Test-Path -LiteralPath $dest) { Write-Host "skip (既にある): $($_.Name)" }
        else {
            Copy-Item -LiteralPath $_.FullName -Destination $dest
            if (-not (Test-Path -LiteralPath $dest)) { throw "写せなかった: $($_.Name)" }
            Write-Host "copied: $($_.Name)"
        }
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
$mcpUrl = "http://127.0.0.1:$HttpPort/mcp"
$claude = Get-Command claude -ErrorAction SilentlyContinue
if ($claude) {
    # 初回は登録が無いので remove は失敗する。それは無視する。
    Run 'claude' @('mcp', 'remove', '--scope', 'local', 'duocad') $StableRoot -AllowFailure
    Run 'claude' @('mcp', 'add', '--scope', 'local', '--transport', 'http', 'duocad', $mcpUrl) $StableRoot
    # **登録が効いていないと、このフォルダのセッションは .mcp.json の 8766(開発版)へ繋ぐ。** 確かめて止める。
    Push-Location $StableRoot
    try { $got = (& claude mcp get duocad 2>&1 | Out-String) } finally { Pop-Location }
    if ($got -notmatch [regex]::Escape($mcpUrl)) { throw "MCPの登録を確かめられなかった(claude mcp get duocad):`n$got" }
} else {
    throw "claude が見つからない。安定版のフォルダで次を実行してから install をやり直すこと: claude mcp add --scope local --transport http duocad $mcpUrl"
}

# ---- 6. 安定版のフォルダで開いたセッション向けの指示 ----
# 未追跡のファイルなので、更新の「未コミットの変更」検査(--untracked-files=no)には掛からない。
$localGuide = @"
# このフォルダは制作用の duocad(安定版)

本人が作品を作るためのフォルダです。docs/MANUAL.md

- **コードを編集しない・コミットしない・ブランチを操作しない・ビルドしない。** 書き換えると画面の更新ボタンが止まる。
- duocad の操作は MCP の ``duocad``($mcpUrl。local スコープで登録済み)で行う。
- リポジトリの CLAUDE.md にある開発の規律(develop で作業・即コミット・「安定版に触らない」)は開発用チェックアウト向けで、このフォルダには適用しない。
- 不具合を見つけたら直さずに本人へ伝える。直すのは開発用のチェックアウト(mf\duocad)。
"@
Set-Content -Path (Join-Path $StableRoot 'CLAUDE.local.md') -Value $localGuide -Encoding UTF8

Write-Host ''
Write-Host "完了。Win キー -> duocad -> Enter で起動する(初回はサーバの起動に十数秒かかる)。" -ForegroundColor Green
