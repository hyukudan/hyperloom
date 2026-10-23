[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = 'Stop'

function Find-TelarPython {
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

function Invoke-TelarPython {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python command failed with exit code $LASTEXITCODE" }
}

$interpreter = Find-TelarPython
foreach ($tool in @('git', 'ssh.exe')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "Required command missing: $tool" }
}
if (-not $env:LOCALAPPDATA -or -not $env:APPDATA) { throw 'LOCALAPPDATA and APPDATA must be set.' }
$repository = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$base = Join-Path $env:LOCALAPPDATA 'telar'
$venv = Join-Path $base 'venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'
$component = Join-Path $repository 'third_party\tensormeld'
$wheels = Join-Path $repository 'wheels'
$programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$shortcutPath = Join-Path $programs 'Telar.lnk'
$pipArguments = @('-m', 'pip', 'install')
if (Test-Path -LiteralPath $wheels -PathType Container) {
    $pipArguments += @('--no-index', '--find-links', $wheels)
}
$pipArguments += @($component, $repository)
Write-Output "Python: $interpreter"
Write-Output "Venv: $venv"
Write-Output ("Pip arguments: " + ($pipArguments | ConvertTo-Json -Compress))
Write-Output "Shortcut: $shortcutPath -> $venvPython -m telar ui (Phase 1b)"
Write-Output "Launcher: $(Join-Path $base 'telar.cmd')"
if ($PSCmdlet.ShouldProcess($base, 'Create venv, install TensorMeld and telar, record interpreter, create shortcut and launcher')) {
    New-Item -ItemType Directory -Path $base -Force | Out-Null
    Invoke-TelarPython -Executable $interpreter -Arguments @('-m', 'venv', $venv)
    Set-Content -LiteralPath (Join-Path $base 'python-path.txt') -Value $interpreter -Encoding UTF8
    Invoke-TelarPython -Executable $venvPython -Arguments $pipArguments
    New-Item -ItemType Directory -Path $programs -Force | Out-Null
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $venvPython
    $shortcut.Arguments = '-m telar ui'
    $shortcut.WorkingDirectory = $base
    $shortcut.Save()
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'telar.cmd') -Destination (Join-Path $base 'telar.cmd') -Force
}
