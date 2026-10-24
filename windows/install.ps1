[CmdletBinding(SupportsShouldProcess = $true)]
param(
    # Switch the launcher and shortcut to an installed release (rollback).
    [string]$Activate,
    # List the installed releases and the active one.
    [switch]$List,
    # Delete an installed release that is not active.
    [string]$Remove
)

$ErrorActionPreference = 'Stop'

function Find-HyperloomPython {
    $candidates = @()
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($version in @('-3.13', '-3.12', '-3.11')) {
            $candidates += ,@($launcher.Source, $version)
        }
    }
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) { $candidates += ,@($pythonCommand.Source) }
    foreach ($candidate in $candidates) {
        $commandPath = $candidate[0]
        $prefix = @($candidate | Select-Object -Skip 1)
        try {
            $output = & $commandPath @prefix -c 'import json,sys; print(json.dumps(dict(path=sys.executable,version=list(sys.version_info[:2]))))' 2>$null
            if ($LASTEXITCODE -ne 0) { continue }
            $info = $output | ConvertFrom-Json
            if ($info.version[0] -eq 3 -and $info.version[1] -ge 11 -and
                [System.IO.Path]::IsPathRooted($info.path) -and (Test-Path -LiteralPath $info.path)) {
                return [System.IO.Path]::GetFullPath($info.path)
            }
        } catch { continue }
    }
    throw 'Python 3.11 or newer was not found (py -3.13, -3.12, -3.11, or python).'
}

function Invoke-HyperloomPython {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python command failed with exit code $LASTEXITCODE" }
}

# Every file of a release must match RELEASE.json, and nothing may be added.
function Test-HyperloomRelease {
    param([string]$Root)
    $manifest = Get-Content -LiteralPath (Join-Path $Root 'RELEASE.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema -ne 'hyperloom/release-v1' -or $manifest.name -notmatch '^hyperloom-[0-9A-Za-z.]+-g[0-9a-f]{12}$') {
        throw 'RELEASE.json is not a hyperloom/release-v1 manifest with a valid name.'
    }
    $expected = @{}
    foreach ($entry in $manifest.files.PSObject.Properties) { $expected[$entry.Name] = $entry.Value }
    $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $seen = 0
    $sha = [System.Security.Cryptography.SHA256]::Create()
    foreach ($file in Get-ChildItem -LiteralPath $Root -Recurse -File -Force) {
        $relative = $file.FullName.Substring($rootPath.Length).Replace('\', '/')
        if ($relative -eq 'RELEASE.json') { continue }
        if (-not $expected.ContainsKey($relative)) { throw "File not in RELEASE.json: $relative" }
        # .NET directly: Get-FileHash is missing when PSModulePath points at another PowerShell.
        $stream = [System.IO.File]::OpenRead($file.FullName)
        try { $hash = -join ($sha.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') }) } finally { $stream.Dispose() }
        if ($hash -ne $expected[$relative]) { throw "sha256 mismatch: $relative" }
        $seen++
    }
    # Bundled wheels are verified like every other file: pip installs them from the verified copy.
    $listed = $expected.Count
    if ($seen -ne $listed) { throw "Release is incomplete: $seen of $listed files present." }
    return $manifest
}

function Set-HyperloomActive {
    param([string]$Base, [string]$Name, [string]$Programs, [string]$ShortcutPath)
    $python = Join-Path $Base "venvs\$Name\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) { throw "Release is not installed: $Name" }
    # The launchers read this file first; an atomic replace switches every new invocation.
    $state = Join-Path $Base 'current.txt'
    $temporary = "$state.$PID.tmp"
    Set-Content -LiteralPath $temporary -Value $Name -Encoding ASCII -NoNewline
    Move-Item -LiteralPath $temporary -Destination $state -Force
    New-Item -ItemType Directory -Path $Programs -Force | Out-Null
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = $python
    $shortcut.Arguments = '-P -m hyperloom ui'
    $shortcut.WorkingDirectory = $Base
    $shortcut.Save()
}

if (-not $env:LOCALAPPDATA -or -not $env:APPDATA) { throw 'LOCALAPPDATA and APPDATA must be set.' }
$repository = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$base = Join-Path $env:LOCALAPPDATA 'hyperloom'
$programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$shortcutPath = Join-Path $programs 'Hyperloom.lnk'
$currentFile = Join-Path $base 'current.txt'
$current = if (Test-Path -LiteralPath $currentFile) { (Get-Content -LiteralPath $currentFile -Raw).Trim() } else { $null }

if ($List) {
    $releases = Join-Path $base 'releases'
    if (Test-Path -LiteralPath $releases) {
        foreach ($item in Get-ChildItem -LiteralPath $releases -Directory | Sort-Object Name) {
            $mark = if ($item.Name -eq $current) { '* ' } else { '  ' }
            Write-Output "$mark$($item.Name)"
        }
    }
    if (-not $current) { Write-Output 'No release is active (checkout install or none).' }
    return
}
if ($Activate) {
    if ($PSCmdlet.ShouldProcess($Activate, 'Activate installed release')) {
        Set-HyperloomActive -Base $base -Name $Activate -Programs $programs -ShortcutPath $shortcutPath
        Write-Output "Active release: $Activate"
    }
    return
}
if ($Remove) {
    if ($Remove -eq $current) { throw "Refusing to remove the active release $Remove; activate another first." }
    if ($Remove -notmatch '^hyperloom-[0-9A-Za-z.]+-g[0-9a-f]{12}$') { throw "Not a release name: $Remove" }
    foreach ($directory in @((Join-Path $base "releases\$Remove"), (Join-Path $base "venvs\$Remove"))) {
        if ((Test-Path -LiteralPath $directory) -and $PSCmdlet.ShouldProcess($directory, 'Remove')) {
            Remove-Item -LiteralPath $directory -Recurse -Force
        }
    }
    return
}

$interpreter = Find-HyperloomPython
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Required command missing: git' }
# Windows ships OpenSSH in System32\OpenSSH, which is not always on PATH.
$sshExe = (Get-Command ssh.exe -ErrorAction SilentlyContinue).Source
if (-not $sshExe) {
    $sshExe = Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe'
    if (-not (Test-Path -LiteralPath $sshExe)) { throw 'Required command missing: ssh.exe (enable the Windows OpenSSH client)' }
}
$release = $null
if (Test-Path -LiteralPath (Join-Path $repository 'RELEASE.json')) {
    # Release mode: a verified copy per version, each with its own venv; older ones stay for rollback.
    $release = Test-HyperloomRelease -Root $repository
    $source = Join-Path $base "releases\$($release.name)"
    $venv = Join-Path $base "venvs\$($release.name)"
    Write-Output "Release: $($release.name) (commit $($release.commit)), verified $(@($release.files.PSObject.Properties).Count) files"
} else {
    $source = $repository
    $venv = Join-Path $base 'venv'
}
$venvPython = Join-Path $venv 'Scripts\python.exe'
# Release mode installs only from the verified copy (wheels included), never from the extracted archive.
$wheels = Join-Path $source 'wheels'
$pipArguments = @('-m', 'pip', 'install')
if (Test-Path -LiteralPath (Join-Path $repository 'wheels') -PathType Container) {
    $pipArguments += @('--no-index', '--find-links', $wheels)
}
$pipArguments += @((Join-Path $source 'third_party\tensormeld'), '-e', $source)
Write-Output "Python: $interpreter"
Write-Output "Venv: $venv"
Write-Output ("Pip arguments: " + ($pipArguments | ConvertTo-Json -Compress))
Write-Output "Shortcut: $shortcutPath -> $venvPython -P -m hyperloom ui (Phase 1b)"
Write-Output "Launcher: $(Join-Path $base 'hyperloom.cmd')"
if ($PSCmdlet.ShouldProcess($base, 'Create venv, install TensorMeld and hyperloom, record interpreter, create shortcut and launcher')) {
    New-Item -ItemType Directory -Path $base -Force | Out-Null
    if ($release) {
        if (Test-Path -LiteralPath $source) {
            # An earlier install of the same release must still verify before it is reused.
            Test-HyperloomRelease -Root $source | Out-Null
        } else {
            $staging = "$source.$PID.tmp"
            New-Item -ItemType Directory -Path $staging -Force | Out-Null
            Get-ChildItem -LiteralPath $repository -Force | Copy-Item -Destination $staging -Recurse -Force
            Test-HyperloomRelease -Root $staging | Out-Null
            Move-Item -LiteralPath $staging -Destination $source
        }
    }
    Invoke-HyperloomPython -Executable $interpreter -Arguments @('-m', 'venv', $venv)
    Set-Content -LiteralPath (Join-Path $base 'python-path.txt') -Value $interpreter -Encoding UTF8
    Invoke-HyperloomPython -Executable $venvPython -Arguments $pipArguments
    foreach ($launcher in @('hyperloom.cmd', 'hloom.cmd')) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $launcher) -Destination (Join-Path $base $launcher) -Force
    }
    if ($release) {
        Set-HyperloomActive -Base $base -Name $release.name -Programs $programs -ShortcutPath $shortcutPath
        Write-Output "Active release: $($release.name) (previous: $(if ($current) { $current } else { 'none' }))"
    } else {
        # A checkout install is the unversioned venv; it takes over from any active release.
        Remove-Item -LiteralPath $currentFile -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Path $programs -Force | Out-Null
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $venvPython
        $shortcut.Arguments = '-P -m hyperloom ui'
        $shortcut.WorkingDirectory = $base
        $shortcut.Save()
    }
}
