# Build an EnC-capable netcoredbg with ncdbhook.dll.

[CmdletBinding()]
param(
    [string]$RepositoryUrl = "https://github.com/thebtf/netcoredbg.git",
    [string]$Branch = "work/add-apply-deltas-dap",
    [string]$WorkDir = (Join-Path $env:TEMP "netcoredbg-mcp-enc-build"),
    [string]$InstallDir = (Join-Path $env:USERPROFILE ".netcoredbg-mcp\netcoredbg-enc")
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ManualInstructionsUrl = "https://github.com/Samsung/netcoredbg#building"

function Fail-WithPrerequisites {
    param([string]$Message)

    Write-Error @"
$Message

Prerequisites:
- Git
- CMake
- .NET SDK
- Visual Studio C++ Build Tools; run from a Developer PowerShell so cl.exe is on PATH

Manual instructions: $ManualInstructionsUrl
"@
}

function Require-Command {
    param(
        [string]$Command,
        [string]$DisplayName
    )

    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        Fail-WithPrerequisites "$DisplayName is required but '$Command' was not found on PATH."
    }
}

Require-Command "git" "Git"
Require-Command "cmake" "CMake"
Require-Command "dotnet" ".NET SDK"
Require-Command "cl.exe" "Visual Studio C++ compiler"

New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

$repoDir = Join-Path $WorkDir "netcoredbg"
if (-not (Test-Path (Join-Path $repoDir ".git"))) {
    git clone --branch $Branch $RepositoryUrl $repoDir
}
else {
    git -C $repoDir fetch origin $Branch
    git -C $repoDir checkout $Branch
    git -C $repoDir pull --ff-only origin $Branch
}

$buildDir = Join-Path $repoDir "build-enc"
New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
$netcoredbgExe = Join-Path $InstallDir "netcoredbg.exe"
$ncdbhookDll = Join-Path $InstallDir "ncdbhook.dll"
$ncdbhookCMakePath = $ncdbhookDll.Replace("\", "/")

$cmakeConfigureArgs = @(
    "-S", $repoDir,
    "-B", $buildDir,
    "-DNCDB_DOTNET_STARTUP_HOOK=$ncdbhookCMakePath",
    "-DBUILD_MANAGED=1",
    "-DCMAKE_INSTALL_PREFIX=$InstallDir"
)

cmake @cmakeConfigureArgs
cmake --build $buildDir --target install --config Debug

if (-not (Test-Path $netcoredbgExe)) {
    Fail-WithPrerequisites "Build finished but netcoredbg.exe was not installed to $InstallDir."
}

if (-not (Test-Path $ncdbhookDll)) {
    Fail-WithPrerequisites "Build finished but ncdbhook.dll was not installed to $InstallDir."
}

Write-Host "EnC-capable netcoredbg installed:"
Write-Host "  NETCOREDBG_PATH=$netcoredbgExe"
Write-Host "  NCDBHOOK_PATH=$ncdbhookDll"
