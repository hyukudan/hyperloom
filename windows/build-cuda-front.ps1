# Build the engine's CUDA apps for a prefill service preset (SPEC section 4, "CUDA build").
# vswhere -> vcvars64 -> CUDA 13.0 check -> CMake + Ninja at low priority -> MANIFEST.json with every binary's sha256.
# The source must be a clean checkout at the pinned commit; the output directory is created and owned by hyperloom.
param(
    [Parameter(Mandatory)][string]$Source,
    [Parameter(Mandatory)][string]$Output,
    [Parameter(Mandatory)][string]$Commit,
    [Parameter(Mandatory)][string]$ConfigureJson,
    [Parameter(Mandatory)][string]$TargetsJson,
    [int]$Jobs = 8
)
$ErrorActionPreference = 'Stop'
(Get-Process -Id $PID).PriorityClass = 'Idle'  # inherited by cmake, ninja, cl and nvcc

function Fail([string]$Message) { [Console]::Error.WriteLine("build-cuda-front: $Message"); exit 1 }

$configure = @(ConvertFrom-Json $ConfigureJson)
$targets = @(ConvertFrom-Json $TargetsJson)
if ($targets.Count -lt 1) { Fail 'no targets' }

$head = (& git -C $Source rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $head -ne $Commit) { Fail "source is at $head, expected $Commit" }
$dirty = & git -C $Source status --porcelain --untracked-files=no
if ($dirty) { Fail 'source checkout has local changes' }

$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path $vswhere)) { Fail 'vswhere.exe not found (install Visual Studio 2022 C++ build tools)' }
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { Fail 'no Visual Studio installation with the x64 C++ tools' }
$vcvars = Join-Path $vs 'VC\Auxiliary\Build\vcvars64.bat'
foreach ($line in (& cmd.exe /d /c "call `"$vcvars`" >nul && set")) {
    if ($line -match '^([^=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2]) }
}

$cuda = $env:CUDA_PATH_V13_0
if (-not $cuda) { Fail 'CUDA 13.0 not installed (CUDA_PATH_V13_0 unset)' }
$nvcc = Join-Path $cuda 'bin\nvcc.exe'
$nvccVersion = (& $nvcc --version | Select-String 'release').ToString().Trim()
if ($nvccVersion -notmatch 'release 13\.0') { Fail "expected CUDA 13.0, found: $nvccVersion" }
$env:PATH = (Join-Path $cuda 'bin') + ';' + $env:PATH

$build = Join-Path $Output 'build'
$bin = Join-Path $Output 'bin'
New-Item -ItemType Directory -Force -Path $build, $bin | Out-Null
$cmakeArgs = @('-S', $Source, '-B', $build) + $configure + @("-DCMAKE_CUDA_COMPILER=$($nvcc -replace '\\', '/')")
& cmake @cmakeArgs
if ($LASTEXITCODE -ne 0) { Fail 'configure failed' }
& cmake --build $build -j $Jobs --target @targets
if ($LASTEXITCODE -ne 0) { Fail 'build failed' }

$binaries = [ordered]@{}
foreach ($target in $targets) {
    $exe = Get-ChildItem -Path $build -Recurse -Filter "$target.exe" | Select-Object -First 1
    if (-not $exe) { Fail "built target $target has no executable" }
    Copy-Item -Force $exe.FullName (Join-Path $bin $exe.Name)
    $binaries[$exe.Name] = (Get-FileHash -Algorithm SHA256 (Join-Path $bin $exe.Name)).Hash.ToLowerInvariant()
}
$manifest = [ordered]@{
    schema = 'hyperloom/cuda-release-v1'
    engine_commit = $Commit
    configure = $configure
    targets = $targets
    toolchain = [ordered]@{
        msvc = $env:VCToolsVersion
        nvcc = $nvccVersion
        cmake = ((& cmake --version) | Select-Object -First 1)
        ninja = ((& ninja --version) | Select-Object -First 1)
    }
    binaries = $binaries
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 (Join-Path $Output 'MANIFEST.json')
Write-Output (Join-Path $Output 'MANIFEST.json')
