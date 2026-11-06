# develop -> main の前に回す検査をまとめて走らせ、段ごとの合否と所要時間を表で出す。
#
#   pwsh -NoProfile -ExecutionPolicy Bypass -File tools\gate.ps1                # 全段
#   pwsh -NoProfile -ExecutionPolicy Bypass -File tools\gate.ps1 -List          # 段の一覧
#   pwsh -NoProfile -ExecutionPolicy Bypass -File tools\gate.ps1 -Only check    # 段を選ぶ
#   pwsh -NoProfile -ExecutionPolicy Bypass -File tools\gate.ps1 -Only check,smoke-mirror
#   pwsh -NoProfile -ExecutionPolicy Bypass -File tools\gate.ps1 -Only 'smoke*' -Skip smoke-face-culling
#   pwsh -NoProfile -ExecutionPolicy Bypass -File tools\gate.ps1 -Only pytest -PytestArgs tests/test_resolution.py
#
# 段(この順):
#   pytest         .venv の python で pytest -q(全件。-PytestArgs で絞れる)
#   check          web で npm run check
#   build          smoke が使う画面の組み立て。smoke の段を選ぶと自動で入る。web\dist ではなく別の場所へ出す
#   smoke*         web\scripts\smoke*.mjs を1本1段で(段の名前はファイル名から .mjs を除いたもの)
#
# 1段でも落ちたら終了コード 1(引数の誤りは 2)。落ちた段は、表のあとに出力の末尾を出す。PR 本文にはこの表と末尾を貼る。
# smoke は各スクリプトが使い捨ての --models-dir と OS が選ぶ空きポートで立てる。8765・8766・8775・8776 には触らない。
# 環境変数 GATE_WEB_DIST か -WebDist で組み立て先を変えられる(既定は $env:TEMP\duocad-gate-dist)。
# リポジトリの中(web\dist を含む)・リポジトリを含む場所・前回の組み立てでも空でもない既存の場所は拒む
# (ジャンクション・シンボリックリンク・短い名前は、辿った先の実体のパスで比べる)。

[CmdletBinding()]
param(
    [string[]]$Only = @(),
    [string[]]$Skip = @(),
    [string[]]$PytestArgs = @(),
    [string]$WebDist = $(if ($env:GATE_WEB_DIST) { $env:GATE_WEB_DIST } else { Join-Path $env:TEMP 'duocad-gate-dist' }),
    [int]$TailLines = 30,
    [switch]$List
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$web = Join-Path $repo 'web'
$python = Join-Path $repo '.venv\Scripts\python.exe'
# 相対の -WebDist は呼んだ場所から読む(build と smoke の段は web\ で走るので、相対のまま渡すと別の場所を指す)。
$WebDist = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($WebDist).TrimEnd('\')

# -File 経由だと `-Only a,b` は1本の文字列 'a,b' で届くので、コンマでも分ける。
function Split-Names([string[]]$Values) {
    @($Values | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}
$Only = Split-Names $Only
$Skip = Split-Names $Skip
$PytestArgs = @($PytestArgs | ForEach-Object { $_ -split ',' } | Where-Object { $_ })

$smokeScripts = @(Get-ChildItem (Join-Path $web 'scripts') -Filter 'smoke*.mjs' | Sort-Object Name)
$stages = [System.Collections.Generic.List[object]]::new()
$stages.Add([pscustomobject]@{ Name = 'pytest'; Dir = $repo; Exe = $python; Args = @('-m', 'pytest', '-q') + $PytestArgs })
$stages.Add([pscustomobject]@{ Name = 'check'; Dir = $web; Exe = 'npm'; Args = @('run', 'check') })
$stages.Add([pscustomobject]@{ Name = 'build'; Dir = $web; Exe = 'npm'; Args = @('run', 'build', '--', '--outDir', $WebDist, '--emptyOutDir') })
foreach ($script in $smokeScripts) {
    $stages.Add([pscustomobject]@{
        Name = [IO.Path]::GetFileNameWithoutExtension($script.Name)
        Dir  = $web
        Exe  = 'node'
        Args = @("scripts/$($script.Name)", '--web-dist', $WebDist)
    })
}

if ($List) {
    $stages | ForEach-Object { $_.Name }
    exit 0
}

function Test-Selected([string]$Name, [string[]]$Patterns) {
    foreach ($pattern in $Patterns) { if ($Name -like $pattern) { return $true } }
    return $false
}

# 名前の誤りは黙って0段にせず、はっきり断る。
foreach ($pattern in @($Only) + @($Skip)) {
    if (-not ($stages | Where-Object { $_.Name -like $pattern })) {
        Write-Error "段 '$pattern' はありません。-List で一覧を見てください。" -ErrorAction Continue
        exit 2
    }
}

$selected = @($stages | Where-Object {
    ($Only.Count -eq 0 -or (Test-Selected $_.Name $Only)) -and -not (Test-Selected $_.Name $Skip)
})
$needsBuild = [bool]($selected | Where-Object { $_.Name -like 'smoke*' })
$buildSelected = [bool]($selected | Where-Object { $_.Name -eq 'build' })
if ($needsBuild -and -not $buildSelected -and -not (Test-Selected 'build' $Skip)) {
    $selected = @($stages | Where-Object { $_.Name -eq 'build' }) + $selected
}
if ($selected.Count -eq 0) {
    Write-Error '走らせる段がありません。' -ErrorAction Continue
    exit 2
}

$usesDist = [bool]($selected | Where-Object { $_.Name -eq 'build' -or $_.Name -like 'smoke*' })
if ($usesDist) {
    # build は --emptyOutDir で組み立て先の中身を消す。リポジトリの中(web\dist は本人のサーバが配っている)、
    # リポジトリを含む場所、前回の組み立てでも空でもない既存の場所には出さない。
    # 文字列の上でリポジトリの外でも、ジャンクション・シンボリックリンク・短い名前の先が中のことがあるので、実体のパスで比べる。
    # python の realpath は、まだ無い末尾をそのまま足して返す。
    $realPaths = @(& $python -c 'import os, sys; [print(os.path.realpath(p)) for p in sys.argv[1:]]' $repo $WebDist)
    if ($LASTEXITCODE -ne 0 -or $realPaths.Count -ne 2) {
        Write-Error "組み立て先 '$WebDist' の実体のパスを確かめられません($python が動かない)。確かめられない場所へは組み立てません。" -ErrorAction Continue
        exit 2
    }
    $ordinal = [StringComparison]::OrdinalIgnoreCase
    $repoPrefix = $realPaths[0].TrimEnd('\') + '\'
    $distPrefix = $realPaths[1].TrimEnd('\') + '\'
    if ($distPrefix.StartsWith($repoPrefix, $ordinal) -or $repoPrefix.StartsWith($distPrefix, $ordinal)) {
        Write-Error "組み立て先 '$WebDist'(実体は '$($realPaths[1])')はリポジトリの中か、リポジトリを含む場所です(web\dist は本人のサーバが配っている)。リポジトリの外の -WebDist を渡してください。" -ErrorAction Continue
        exit 2
    }
    $holdsOtherThings = (Test-Path -LiteralPath $WebDist) -and
        -not (Test-Path -LiteralPath (Join-Path $WebDist 'index.html')) -and
        [bool](Get-ChildItem -LiteralPath $WebDist -Force | Select-Object -First 1)
    if ($holdsOtherThings) {
        Write-Error "組み立て先 '$WebDist' には、前回の組み立てではない中身があります(build は中身を消す)。空のフォルダか、新しい場所を渡してください。" -ErrorAction Continue
        exit 2
    }
}

$results = [System.Collections.Generic.List[object]]::new()
$buildFailed = $false

foreach ($stage in $selected) {
    if ($stage.Name -like 'smoke*' -and $buildFailed) {
        $results.Add([pscustomobject]@{ Stage = $stage.Name; Result = 'SKIP'; Seconds = 0.0; Tail = @('build が落ちたため走らせていない') })
        Write-Host ("[gate] {0,-24} SKIP" -f $stage.Name)
        continue
    }

    Write-Host ("[gate] {0,-24} ..." -f $stage.Name)
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    Push-Location $stage.Dir
    try {
        $lines = @(& $stage.Exe @($stage.Args) 2>&1 | ForEach-Object { "$_" })
        $code = $LASTEXITCODE
    } catch {
        $lines = @("$_")
        $code = 1
    } finally {
        Pop-Location
    }
    $watch.Stop()

    $passed = ($code -eq 0)
    if ($stage.Name -eq 'build' -and -not $passed) { $buildFailed = $true }
    $tail = @($lines | Select-Object -Last $TailLines)
    $results.Add([pscustomobject]@{
        Stage   = $stage.Name
        Result  = $(if ($passed) { 'PASS' } else { 'FAIL' })
        Seconds = [math]::Round($watch.Elapsed.TotalSeconds, 1)
        Tail    = $tail
    })
    Write-Host ("[gate] {0,-24} {1} ({2:N1}s)" -f $stage.Name, $(if ($passed) { 'PASS' } else { 'FAIL' }), $watch.Elapsed.TotalSeconds)
}

Write-Host ''
$results | Select-Object Stage, Result, Seconds | Format-Table -AutoSize | Out-String -Width 200 | Write-Host

$failed = @($results | Where-Object { $_.Result -eq 'FAIL' })
foreach ($item in $failed) {
    Write-Host ("---- {0} の出力の末尾 {1} 行 ----" -f $item.Stage, $TailLines)
    $item.Tail | ForEach-Object { Write-Host $_ }
    Write-Host ''
}

$total = [math]::Round(($results | Measure-Object Seconds -Sum).Sum, 1)
if ($failed.Count -gt 0) {
    Write-Host ("NG: {0} 段が落ちた({1})。合計 {2} 秒" -f $failed.Count, (($failed | ForEach-Object { $_.Stage }) -join ', '), $total)
    exit 1
}
Write-Host ("OK: {0} 段すべて通った。合計 {1} 秒" -f $results.Count, $total)
exit 0
