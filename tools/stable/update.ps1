# 安定版の更新(と巻き戻し)。docs/STABLE.md
#
# 画面の「更新」ボタンから duocad/channel.py の StableUpdater が切り離して起動する
# (一時フォルダへ写したこのファイルを走らせる。取り込みでこのファイル自身が書き換わるため)。
# 手で巻き戻すときは -Rollback を付けて直接呼ぶ(ServerPid を省くと HttpPort から探して止める)。
#
# 流れ: サーバの終了を待つ -> origin/<Branch> を早送りで取り込む -> uv sync --frozen
#       -> (package-lock が変わったか node_modules が無ければ) npm ci -> npm run build
#       -> duocad.launcher --no-window で立て直す。
# 失敗したら元のコミットへ戻して同じ手順で立て直す(結果は LogDir の last-update-result.json)。
#
# **このファイルはUTF-8(BOM付き)で保つ。** Windows PowerShell 5.1 はBOM無しのスクリプトを
# ANSI(cp932)で読み、日本語のコメントで構文が壊れる。

param(
    [Parameter(Mandatory = $true)][string]$RepoRoot,
    [string]$Branch = 'main',
    [int]$ServerPid = 0,
    [Parameter(Mandatory = $true)][string]$LogDir,
    [string]$Channel = 'stable',
    [int]$Port = 8775,
    [int]$HttpPort = 8776,
    [Parameter(Mandatory = $true)][string]$DataDir,
    [switch]$Rollback
)

$ErrorActionPreference = 'Stop'
# 外部コマンド(vite等)のUTF-8出力をログで化けさせない(既定はコンソールのcp932で読む)。
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
# 窓が無いので、認証やホスト鍵を聞かれたら答えられない。聞かずに失敗させる。
$env:GIT_TERMINAL_PROMPT = '0'
if (-not $env:GIT_SSH_COMMAND) { $env:GIT_SSH_COMMAND = 'ssh -o BatchMode=yes' }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$log = Join-Path $LogDir "update-$stamp.log"

function Log([string]$text) {
    $line = '{0} {1}' -f (Get-Date -Format 'HH:mm:ss'), $text
    Add-Content -Path $log -Value $line -Encoding UTF8
}

# 外部コマンドを1つ走らせ、出力をログへ流し、終了コードが0でなければ投げる。
# **2>&1 のまま ErrorActionPreference=Stop にしない**: PowerShell 5.1 は外部コマンドの
# stderr の1行目(git の進捗表示など)を例外に変えてしまう。
function Run([string]$name, [string]$exe, [string[]]$arguments, [string]$cwd = $RepoRoot) {
    Log "== $name : $exe $($arguments -join ' ')"
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    Push-Location $cwd
    try {
        $global:LASTEXITCODE = 0
        & $exe @arguments 2>&1 | ForEach-Object { Log ("   " + $_) }
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) { throw "$name failed (exit $code)" }
}

function Resolve-Tool([string]$name, [string[]]$fallbacks) {
    $found = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.Source }
    foreach ($candidate in $fallbacks) { if (Test-Path $candidate) { return $candidate } }
    throw "$name が見つからない"
}

function Build([string]$fromCommit) {
    Run 'uv sync' $uv @('sync', '--frozen')
    $web = Join-Path $RepoRoot 'web'
    $lockChanged = $false
    if ($fromCommit) {
        $changed = & git -C $RepoRoot diff --name-only $fromCommit HEAD -- web/package-lock.json
        $lockChanged = [bool]$changed
    }
    if ($lockChanged -or -not (Test-Path (Join-Path $web 'node_modules'))) {
        Run 'npm ci' $npm @('ci') $web
    }
    Run 'npm run build' $npm @('run', 'build') $web
}

function Relaunch {
    $pythonw = Join-Path $RepoRoot '.venv\Scripts\pythonw.exe'
    Log "== relaunch: $pythonw -m duocad.launcher --no-window"
    Start-Process -FilePath $pythonw -WorkingDirectory $RepoRoot -ArgumentList @(
        '-m', 'duocad.launcher', '--no-window',
        '--channel', $Channel, '--port', "$Port", '--http-port', "$HttpPort", '--data-dir', "`"$DataDir`""
    )
}

function Write-Result([bool]$ok, [string]$from, [string]$to, [string]$message) {
    $result = [ordered]@{
        ok = $ok; rollback = [bool]$Rollback; from = $from; to = $to
        message = $message; log = $log; at = (Get-Date).ToString('o')
    }
    $result | ConvertTo-Json | Set-Content -Path (Join-Path $LogDir 'last-update-result.json') -Encoding UTF8
}

Log "start: repo=$RepoRoot branch=$Branch rollback=$Rollback"

# ---- サーバの終了を待つ ----
if ($ServerPid -eq 0) {
    $listening = Get-NetTCPConnection -LocalPort $HttpPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listening) { $ServerPid = $listening.OwningProcess }
}
if ($ServerPid -ne 0) {
    Log "waiting for server pid $ServerPid"
    Wait-Process -Id $ServerPid -Timeout 60 -ErrorAction SilentlyContinue
    if (Get-Process -Id $ServerPid -ErrorAction SilentlyContinue) {
        Log "server did not exit; stopping it"
        Stop-Process -Id $ServerPid -Force
        Start-Sleep -Seconds 1
    }
}

$uv = Resolve-Tool 'uv' @("$env:USERPROFILE\.local\bin\uv.exe", "$env:USERPROFILE\.cargo\bin\uv.exe")
$npm = Resolve-Tool 'npm.cmd' @("$env:ProgramFiles\nodejs\npm.cmd")

$previousFile = Join-Path $RepoRoot '.git\duocad-previous'
$from = (& git -C $RepoRoot rev-parse HEAD).Trim()
$to = $from

try {
    # 安定版のcloneを手で書き換えた形跡があれば、潰さずに止める。
    $current = (& git -C $RepoRoot rev-parse --abbrev-ref HEAD).Trim()
    if ($current -ne $Branch) { throw "$Branch 以外の枝($current)にいる。開発用のチェックアウトでは走らせない" }
    $dirty = & git -C $RepoRoot status --porcelain --untracked-files=no
    if ($dirty) { throw "安定版のフォルダに未コミットの変更がある: $dirty" }

    if ($Rollback) {
        if (-not (Test-Path $previousFile)) { throw '戻り先(.git\duocad-previous)が無い' }
        $target = (Get-Content $previousFile -Raw).Trim()
        Run 'git reset' 'git' @('reset', '--hard', $target)
    } else {
        Run 'git fetch' 'git' @('fetch', 'origin', $Branch)
        Run 'git merge' 'git' @('merge', '--ff-only', "origin/$Branch")
    }
    $to = (& git -C $RepoRoot rev-parse HEAD).Trim()
    if ($to -ne $from) { Set-Content -Path $previousFile -Value $from -Encoding ASCII }
    Build $from
    Log "ok: $from -> $to"
    Write-Result $true $from $to ''
} catch {
    $message = $_.Exception.Message
    Log "FAILED: $message"
    try {
        $now = (& git -C $RepoRoot rev-parse HEAD).Trim()
        if ($now -ne $from) {
            Log "restoring $from"
            Run 'git reset (restore)' 'git' @('reset', '--hard', $from)
            Build $now
        }
    } catch {
        Log "RESTORE FAILED: $($_.Exception.Message)"
        $message = "$message / 復元にも失敗: $($_.Exception.Message)"
    }
    Write-Result $false $from $from $message
}

Relaunch
Log 'done'
