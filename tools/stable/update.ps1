# 安定版の更新(と巻き戻し)。docs/STABLE.md
#
# 画面の「更新」ボタンから duocad/channel.py の StableUpdater が切り離して起動する
# (一時フォルダへ写したこのファイルを走らせる。取り込みでこのファイル自身が書き換わるため)。
# 手で巻き戻すときは -Rollback を付けて直接呼ぶ(ServerPid を省くと HttpPort から探して止める)。
# -InstallGpu は取り込みもビルドもせず、GPU の部品(extra gpu)だけを入れて立て直す(画面の「設定」の
# 「GPU の部品を入れる」。失敗したら extra なしで同期し直して CPU の構成に戻す)。
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
    [switch]$Rollback,
    [switch]$InstallGpu
)

$ErrorActionPreference = 'Stop'
# サーバ(uv の venv の python)が子へ渡す PYTHONHOME 等を消す。残すと、Python の版を上げる更新で
# 新しい版の python が古い版の標準ライブラリを読んで uv sync が落ちる(duocad/channel.py の _INHERITED_PYTHON_ENV)。
foreach ($name in 'PYTHONHOME', 'UV_INTERNAL__PYTHONHOME', 'PYTHONPATH', '__PYVENV_LAUNCHER__', 'VIRTUAL_ENV') {
    Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
}
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

function Build([string]$fromCommit) {
    $syncArgs = @('sync', '--frozen')
    if (Want-GpuExtra $RepoRoot) { $syncArgs += @('--extra', 'gpu') }
    Run 'uv sync' $uv $syncArgs
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

function Write-Result([bool]$ok, [bool]$restored, [string]$from, [string]$to, [string]$message) {
    $result = [ordered]@{
        ok = $ok; restored = $restored; rollback = [bool]$Rollback; gpu = [bool]$InstallGpu; from = $from; to = $to
        message = $message; log = $log; at = (Get-Date).ToString('o')
    }
    $result | ConvertTo-Json | Set-Content -Path (Join-Path $LogDir 'last-update-result.json') -Encoding UTF8
}

Log "start: repo=$RepoRoot branch=$Branch rollback=$Rollback gpu=$InstallGpu"

# ---- サーバの終了を待つ ----
if ($ServerPid -eq 0) {
    # 手で呼ばれた(-Rollback 等)。サーバに止まるよう頼む口は無いので、すぐ止める。
    # 文書は操作のたびに自動保存されているので、失うのは走っている最適化・AIの途中だけ。
    $listening = Get-NetTCPConnection -LocalPort $HttpPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listening) {
        Log "stopping server pid $($listening.OwningProcess) on port $HttpPort"
        Stop-Process -Id $listening.OwningProcess -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }
} else {
    Log "waiting for server pid $ServerPid"
    Wait-Process -Id $ServerPid -Timeout 60 -ErrorAction SilentlyContinue
    if (Get-Process -Id $ServerPid -ErrorAction SilentlyContinue) {
        Log "server did not exit; stopping it"
        Stop-Process -Id $ServerPid -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }
}

$previousFile = Join-Path $RepoRoot '.git\duocad-previous'
$from = ''

# **ここから先で何が起きても、最後に必ず立て直す**(サーバはもう止まっている)。
try {
    try {
        $uv = Resolve-Tool 'uv' @("$env:USERPROFILE\.local\bin\uv.exe", "$env:USERPROFILE\.cargo\bin\uv.exe")
        $npm = Resolve-Tool 'npm.cmd' @("$env:ProgramFiles\nodejs\npm.cmd")
        $from = (& git -C $RepoRoot rev-parse HEAD).Trim()
        if (-not $from) { throw 'git rev-parse HEAD に失敗' }

        $current = (& git -C $RepoRoot rev-parse --abbrev-ref HEAD).Trim()
        if ($current -ne $Branch) { throw "$Branch 以外の枝($current)にいる。開発用のチェックアウトでは走らせない" }
        # 安定版のcloneを手で書き換えた形跡があれば、潰さずに止める。
        $dirty = & git -C $RepoRoot status --porcelain --untracked-files=no
        if ($dirty) { throw "安定版のフォルダに未コミットの変更がある: $dirty" }

        if ($InstallGpu) {
            if (-not (Select-String -Path (Join-Path $RepoRoot 'pyproject.toml') -Pattern '^\s*gpu\s*=' -Quiet)) {
                throw 'この版は extra gpu を持たない'
            }
            Run 'uv sync (gpu)' $uv @('sync', '--frozen', '--extra', 'gpu')
            Log "ok: gpu extra installed at $from"
            Write-Result $true $true $from $from ''
            return
        }
        if ($Rollback) {
            if (-not (Test-Path $previousFile)) { throw '戻り先(.git\duocad-previous)が無い' }
            $target = (Get-Content $previousFile -Raw).Trim()
            Run 'git reset' 'git' @('reset', '--hard', $target)
        } else {
            Run 'git fetch' 'git' @('fetch', 'origin', $Branch)
            Run 'git merge' 'git' @('merge', '--ff-only', "origin/$Branch")
        }
        $to = (& git -C $RepoRoot rev-parse HEAD).Trim()
        Build $from
        # 戻り先は**成功したときだけ**書き換える(失敗して戻した後も、前回の成功の手前を指し続ける)。
        if ($to -ne $from) { Set-Content -Path $previousFile -Value $from -Encoding ASCII }
        Log "ok: $from -> $to"
        Write-Result $true $true $from $to ''
    } catch {
        $message = $_.Exception.Message
        Log "FAILED: $message"
        $restored = $true
        try {
            if ($InstallGpu) {
                # 入れかけの CuPy を残さない(extra なしの同期は CuPy を消す)。設定は gpu のままなので、
                # 画面は「GPU の部品が入っていない」と出し続ける。
                Run 'uv sync (restore)' $uv @('sync', '--frozen')
            } elseif ($from) {
                $now = (& git -C $RepoRoot rev-parse HEAD).Trim()
                if ($now -ne $from) {
                    Log "restoring $from"
                    Run 'git reset (restore)' 'git' @('reset', '--hard', $from)
                    Build $now
                }
            }
        } catch {
            $restored = $false
            Log "RESTORE FAILED: $($_.Exception.Message)"
            $message = "$message / 元の版への復元にも失敗: $($_.Exception.Message)"
        }
        Write-Result $false $restored $from $from $message
    }
} finally {
    Relaunch
    Log 'done'
}
