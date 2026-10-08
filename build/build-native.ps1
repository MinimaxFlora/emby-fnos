# =============================================================================
#  Emby for fnOS - NATIVE package build script (Windows PowerShell 5.1+)
#
#  Builds a native-install .fpk (no Docker): the Emby server runs directly on
#  the NAS, launched by cmd/main.
#
#  WHY THE PAYLOAD IS ASSEMBLED INSIDE WSL
#  ---------------------------------------
#  Debian names its shared libraries with soname symlinks, for example
#  libstdc++.so.6 pointing at libstdc++.so.6.0.32, and the dynamic linker
#  resolves libraries by those names. Windows cannot create symlinks without
#  admin rights or Developer Mode, and symlinks created by WSL on /mnt/d are
#  unreadable by Windows tools - fnpack fails with
#  "The file cannot be accessed by the system". So the payload is assembled by
#  build/build_in_wsl.sh, which dereferences every symlink into a real file and
#  emits links.tsv; that manifest is restored with ln -s at install time by
#  cmd/common.sh. This script drives WSL and then packs with fnpack.
#
#  WHY THIS FILE IS PURE ASCII
#  ---------------------------
#  Windows PowerShell 5.1 reads a BOM-less UTF-8 script as ANSI, which corrupts
#  non-ASCII literals. Adding a BOM fixes that but makes PowerShell stop
#  recognising the leading [CmdletBinding()] as the parameter block, so every
#  parameter silently becomes empty. Neither way works, therefore this file
#  stays ASCII-only; Chinese documentation lives in README.md.
#
#  Usage:
#     powershell -ExecutionPolicy Bypass -File build\build-native.ps1
#     powershell -ExecutionPolicy Bypass -File build\build-native.ps1 -Arch x86
#     powershell -ExecutionPolicy Bypass -File build\build-native.ps1 -Arch arm
#     powershell -ExecutionPolicy Bypass -File build\build-native.ps1 -SkipFetch
#     powershell -ExecutionPolicy Bypass -File build\build-native.ps1 -SkipPayloadBuild
#
#  Output: dist\emby_VERSION_ARCH_native.fpk
# =============================================================================
[CmdletBinding()]
param(
    [ValidateSet('x86', 'arm', 'all')]
    [string]$Arch = 'all',

    [string]$EmbyVersion = '4.10.1.0',

    [string]$AppName = 'emby',

    [string]$FnpackPath = '',

    [switch]$SkipFetch,

    [switch]$SkipPayloadBuild
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Root     = Split-Path -Parent $PSScriptRoot
$SrcPkg   = Join-Path $Root $AppName
$Assets   = Join-Path $Root 'app-assets'
$BuildDir = Join-Path $Root 'build'
$WorkDir  = Join-Path $Root '_work'
$DistDir  = Join-Path $Root 'dist'

if (-not $FnpackPath) { $FnpackPath = Join-Path $Root '_tools\fnpack.exe' }
if (-not (Test-Path $FnpackPath)) {
    $alt = Join-Path $BuildDir 'fnpack.exe'
    if (Test-Path $alt) { $FnpackPath = $alt }
}

$ArchMap = @{
    'x86' = @{ Platform = 'x86'; EmbyArch = 'amd64' }
    'arm' = @{ Platform = 'arm'; EmbyArch = 'arm64' }
}

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host ('==> ' + $Text) -ForegroundColor Cyan
}
function Write-Ok([string]$Text)   { Write-Host ('    [OK] ' + $Text) -ForegroundColor Green }
function Write-Note([string]$Text) { Write-Host ('    [..] ' + $Text) -ForegroundColor Gray }
function Write-Warn2([string]$Text){ Write-Host ('    [!]  ' + $Text) -ForegroundColor Yellow }

Write-Step 'Checking build environment'

if (-not (Test-Path $FnpackPath)) {
    throw 'fnpack not found. Download fnpack-1.2.3-windows-amd64 and save it as _tools\fnpack.exe'
}
Write-Ok ('fnpack : ' + $FnpackPath)

foreach ($need in @('manifest.template', 'cmd\common.sh', 'cmd\main', 'cmd\service-setup', 'cmd\install_init')) {
    if (-not (Test-Path (Join-Path $SrcPkg $need))) { throw ('missing ' + $need + ' in ' + $SrcPkg) }
}
if (-not (Test-Path (Join-Path $Assets 'bin\emby-server'))) { throw 'missing app-assets\bin\emby-server' }
Write-Ok ('project: ' + $SrcPkg)

$wslExe = Get-Command wsl.exe -ErrorAction SilentlyContinue
if (-not $wslExe) { throw 'wsl.exe not found: the payload must be assembled inside WSL' }
Write-Ok 'wsl    : available'

New-Item -ItemType Directory -Force -Path $DistDir, $WorkDir | Out-Null

# Windows path to WSL path: D:/a/b -> /mnt/d/a/b
# Note: a lone backslash inside single quotes is an unterminated string in
# PowerShell, and a stray one in a comment breaks parsing too. Use [char]92.
$bs = [string][char]92
$drive = $Root.Substring(0, 1).ToLower()
$rest = $Root.Substring(2).Replace($bs, '/')
$RootWsl = '/mnt/' + $drive + $rest

$targets = switch ($Arch) { 'all' { @('x86', 'arm') } default { @($Arch) } }
$results = @()

foreach ($t in $targets) {
    $info     = $ArchMap[$t]
    $embyArch = $info.EmbyArch

    Write-Step ('Building native ' + $t + ' package, emby ' + $EmbyVersion + ', ' + $embyArch)

    # --- 1. payload: assembled inside WSL --------------------------------
    $appOut = Join-Path $SrcPkg 'app'
    $inWsl = $false

    if ($SkipPayloadBuild -and (Test-Path (Join-Path $appOut 'system\EmbyServer'))) {
        Write-Ok 'reusing existing payload from -SkipPayloadBuild'
        $inWsl = $true
    }

    if (-not $inWsl) {
        $wslCmd = "export LANG=C.UTF-8; cd '" + $RootWsl + "' ; bash build/build_payload.sh " + $t
        if ($SkipFetch) { $wslCmd += ' --skip-fetch' }
        Write-Note 'assembling payload inside WSL, this takes several minutes'
        & wsl.exe -e bash -lc $wslCmd 2>&1 | ForEach-Object { Write-Host ('      ' + $_) }
        if ($LASTEXITCODE -eq 0 -and (Test-Path (Join-Path $appOut 'system\EmbyServer'))) {
            $inWsl = $true
            Write-Ok 'payload assembled by WSL'
        }
    }

    if (-not $inWsl) {
        $hint = 'Payload assembly failed or was skipped. Run this inside WSL first, ' +
                'then re-run with -SkipPayloadBuild: bash build/build_payload.sh ' + $t + ' --skip-fetch'
        throw $hint
    }

    # --- 2. overlay our own files ----------------------------------------
    Copy-Item (Join-Path $Assets 'bin') -Destination $appOut -Recurse -Force
    Copy-Item (Join-Path $Assets 'ui')  -Destination $appOut -Recurse -Force
    Write-Ok 'overlaid launcher and desktop entry'

    $linksSrc = Join-Path $Assets ('links-' + $embyArch + '.tsv')
    if (Test-Path $linksSrc) {
        Copy-Item $linksSrc (Join-Path $appOut 'links.tsv') -Force
        $rawLinks = [IO.File]::ReadAllText($linksSrc)
        $dataLines = 0
        foreach ($one in $rawLinks.Split([char]10)) {
            $trimmed = $one.Trim()
            if ($trimmed.Length -eq 0) { continue }
            if ($trimmed.Substring(0, 1) -eq '#') { continue }
            $dataLines = $dataLines + 1
        }
        Write-Ok ('links.tsv in payload, ' + $dataLines + ' symlink entries')
    } else {
        # 真机实测后负载改为「全部实体文件 + 库目录拆分」，soname 别名由
        # split_libs.py 直接生成实体副本，不再需要清单。保留兼容分支。
        Write-Warn2 ('no links manifest for ' + $embyArch + ', relying on split_libs.py aliases')
    }

    # --- 3. render manifest from template --------------------------------
    # Always render from the template: rendering in place would consume the
    # placeholders and break the second build.
    $manifestTpl = Join-Path $SrcPkg 'manifest.template'
    $manifest = [IO.File]::ReadAllText($manifestTpl).Replace([string][char]13, '')
    if ($manifest.IndexOf('@PLATFORM@') -lt 0) { throw 'manifest.template missing @PLATFORM@' }
    if ($manifest.IndexOf('@APPVERSION@') -lt 0) { throw 'manifest.template missing @APPVERSION@' }
    $manifest = $manifest.Replace('@PLATFORM@', $info.Platform)
    $manifest = $manifest.Replace('@APPVERSION@', $EmbyVersion)
    [IO.File]::WriteAllText((Join-Path $SrcPkg 'manifest'), $manifest,
        (New-Object System.Text.UTF8Encoding($false)))
    Write-Ok ('manifest rendered: platform=' + $info.Platform + ' version=' + $EmbyVersion)

    # --- 4. normalize line endings of text files -------------------------
    # CRLF in the launcher breaks its shebang. wizard install/config/upgrade
    # are intentionally absent, so the list must tolerate missing entries.
    $textFiles = @(
        'manifest',
        'config\resource', 'config\privilege', 'config\emby.sc',
        'app\ui\config', 'app\bin\emby-server', 'app\links.tsv',
        'wizard\install', 'wizard\upgrade', 'wizard\uninstall', 'wizard\config',
        'cmd\main', 'cmd\common.sh', 'cmd\service-setup',
        'cmd\install_init', 'cmd\install_callback',
        'cmd\upgrade_init', 'cmd\upgrade_callback',
        'cmd\config_init', 'cmd\config_callback',
        'cmd\uninstall_init', 'cmd\uninstall_callback'
    )
    $normalized = 0
    foreach ($rel in $textFiles) {
        $p = Join-Path $SrcPkg $rel
        if (-not (Test-Path $p)) { continue }
        $c = [IO.File]::ReadAllText($p).Replace([string][char]13, '')
        [IO.File]::WriteAllText($p, $c, (New-Object System.Text.UTF8Encoding($false)))
        $normalized++
    }
    Write-Ok ('line endings normalized to LF, ' + $normalized + ' files')

    # --- 5. hard assertions before packing -------------------------------
    $launcher = Join-Path $SrcPkg 'app\bin\emby-server'
    $lb = [IO.File]::ReadAllBytes($launcher)
    $head = [System.Text.Encoding]::ASCII.GetString($lb[0..20])
    if (-not $head.StartsWith('#!/bin/bash' + [string][char]10)) {
        $shown = $head.Replace([string][char]13, ' CR ').Replace([string][char]10, ' LF ')
        throw ('launcher shebang is not LF terminated: ' + $shown)
    }
    Write-Ok 'launcher shebang verified as LF'

    $core = @(
        'app\system\EmbyServer',
        'app\system\Emby.Web.dll',
        'app\system\libcoreclr.so',
        'app\system\EmbyServer.runtimeconfig.json',
        'app\system\dashboard-ui\index.html',
        'app\system\dashboard-ui\ext.js',
        'app\system\libsqlite3.so',
        'app\system\libSkiaSharp.so',
        'app\bin\ffmpeg',
        'app\lib\libavcodec.so.59.37.100',
        'app\ui\config'
    )
    foreach ($rel in $core) {
        if (-not (Test-Path (Join-Path $SrcPkg $rel))) { throw ('payload missing required file: ' + $rel) }
    }
    Write-Ok ('payload core files verified, ' + $core.Count + ' items')

    # The payload must not contain symlinks: fnpack cannot read them on Windows.
    # Assert here so the failure is understandable instead of a cryptic IO error.
    $strayLinks = 0
    foreach ($f in (Get-ChildItem (Join-Path $SrcPkg 'app') -Recurse -Force -ErrorAction SilentlyContinue)) {
        if ($f.LinkType) { $strayLinks++ }
    }
    if ($strayLinks -gt 0) {
        throw ('payload still contains ' + $strayLinks + ' symlinks which fnpack cannot read; ' +
               'build_native.py must dereference them')
    }
    Write-Ok 'payload contains no symlinks'

    foreach ($rel in @('app\etc\s6-overlay', 'app\etc\passwd', 'app\etc\shadow', 'app\config')) {
        if (Test-Path (Join-Path $SrcPkg $rel)) { throw ('container-only path leaked into payload: ' + $rel) }
    }
    Write-Ok 'no container-only files in payload'

    # --- 6. pack ----------------------------------------------------------
    Write-Note 'running fnpack build, payload is about 1.6 GB, this takes a while'
    # fnpack writes the fpk into the CURRENT WORKING DIRECTORY, not into the
    # directory given by --directory. Run it from a scratch directory so the
    # artefact lands somewhere predictable.
    $packDir = Join-Path $WorkDir 'fpk-out'
    if (Test-Path $packDir) { Remove-Item $packDir -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $packDir | Out-Null
    Push-Location $packDir
    try {
        & $FnpackPath build --directory $SrcPkg 2>&1 | ForEach-Object { Write-Host ('      ' + $_) }
        if ($LASTEXITCODE -ne 0) { throw ('fnpack build failed for ' + $t) }
    } finally {
        Pop-Location
    }

    $fpk = Get-ChildItem $packDir -Filter '*.fpk' -File | Select-Object -First 1
    if (-not $fpk) {
        $fpk = Get-ChildItem $SrcPkg -Filter '*.fpk' -File | Select-Object -First 1
    }
    if (-not $fpk) { throw ('fnpack produced no fpk; checked ' + $packDir + ' and ' + $SrcPkg) }

    $outName = 'emby_' + $EmbyVersion + '_' + $t + '_native.fpk'
    $outPath = Join-Path $DistDir $outName
    Move-Item $fpk.FullName $outPath -Force
    $sizeMB = [math]::Round((Get-Item $outPath).Length / 1MB, 1)
    Write-Ok ('produced ' + $outName + ' (' + $sizeMB + ' MB)')

    $results += [pscustomobject]@{
        Arch     = $t
        Platform = $info.Platform
        EmbyArch = $embyArch
        Fpk      = $outName
        SizeMB   = $sizeMB
        Path     = $outPath
    }
}

Write-Step 'Build finished'
$results | Format-Table -AutoSize | Out-String | Write-Host
Write-Host ('Output directory: ' + $DistDir) -ForegroundColor Green
