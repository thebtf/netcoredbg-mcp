# Install an EnC-capable netcoredbg with ncdbhook.dll.

[CmdletBinding()]
param(
    [string]$RepositoryUrl = "https://github.com/thebtf/netcoredbg.git",
    [string]$Ref = "3.1.3-1062-enc.2",
    [string]$ReleaseTag = "3.1.3-1062-enc.2",
    [string]$AssetName = "netcoredbg-win64-3.1.3-1062-enc.2.zip",
    [string]$AssetSha256 = "208B94AEC38924ACD6580BD8FFE1E87833F9FBDEB53A95E4ED9139ED84DDE139",
    [string]$WorkDir = (Join-Path $env:TEMP "netcoredbg-mcp-enc-build"),
    [string]$InstallDir = (Join-Path $env:USERPROFILE ".netcoredbg-mcp\netcoredbg"),
    [switch]$BuildFromSource
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

function Fail-IfNotWindows {
    $isWindowsHost = [System.Runtime.InteropServices.RuntimeInformation]::IsOSPlatform(
        [System.Runtime.InteropServices.OSPlatform]::Windows
    )
    if (-not $isWindowsHost) {
        Fail-WithPrerequisites "The prebuilt EnC netcoredbg release currently provides a Windows x64 asset only. Re-run with -BuildFromSource on supported non-Windows hosts."
    }
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

function Assert-InstalledDebugger {
    param(
        [string]$Directory
    )

    $netcoredbgExe = Join-Path $Directory "netcoredbg.exe"
    $ncdbhookDll = Join-Path $Directory "ncdbhook.dll"

    if (-not (Test-Path $netcoredbgExe)) {
        Fail-WithPrerequisites "EnC setup finished but netcoredbg.exe was not installed to $Directory."
    }

    if (-not (Test-Path $ncdbhookDll)) {
        Fail-WithPrerequisites "EnC setup finished but ncdbhook.dll was not installed to $Directory."
    }

    Write-Host "EnC-capable netcoredbg installed:"
    Write-Host "  NETCOREDBG_PATH=$netcoredbgExe"
    Write-Host "  NCDBHOOK_PATH=$ncdbhookDll"
}

function Save-NetcoredbgConfig {
    param(
        [string]$Directory
    )

    $homeDir = Split-Path -Parent $Directory
    $configPath = Join-Path $homeDir "config.json"
    $netcoredbgExe = Join-Path $Directory "netcoredbg.exe"

    if (Test-Path $configPath) {
        try {
            $config = Get-Content $configPath -Raw | ConvertFrom-Json
        }
        catch {
            $config = [pscustomobject]@{}
        }
    }
    else {
        $config = [pscustomobject]@{}
    }

    $netcoredbgConfig = [pscustomobject]@{
        version = $ReleaseTag
        source = "thebtf/netcoredbg"
        path = $netcoredbgExe
    }

    $propertyNames = @($config.PSObject.Properties | ForEach-Object { $_.Name })
    if ($propertyNames -contains "netcoredbg") {
        $config.netcoredbg = $netcoredbgConfig
    }
    else {
        $config | Add-Member -MemberType NoteProperty -Name "netcoredbg" -Value $netcoredbgConfig
    }

    $config | ConvertTo-Json -Depth 8 | Set-Content -Path $configPath -Encoding UTF8
}

New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

if (-not $BuildFromSource) {
    Fail-IfNotWindows

    $downloadUrl = "https://github.com/thebtf/netcoredbg/releases/download/$ReleaseTag/$AssetName"
    $archivePath = Join-Path $WorkDir $AssetName

    Write-Host "Downloading EnC-capable netcoredbg:"
    Write-Host "  $downloadUrl"
    Invoke-WebRequest -Uri $downloadUrl -OutFile $archivePath -UseBasicParsing
    $actualSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash
    if ($actualSha256 -ne $AssetSha256) {
        Fail-WithPrerequisites "Downloaded EnC netcoredbg archive checksum mismatch. Expected $AssetSha256, got $actualSha256."
    }

    Get-ChildItem -Path $InstallDir -Force | Remove-Item -Recurse -Force
    Expand-Archive -Path $archivePath -DestinationPath $InstallDir -Force
    Assert-InstalledDebugger $InstallDir
    Save-NetcoredbgConfig $InstallDir
    exit 0
}

Require-Command "git" "Git"
Require-Command "cmake" "CMake"
Require-Command "dotnet" ".NET SDK"
Require-Command "cl.exe" "Visual Studio C++ compiler"

$repoDir = Join-Path $WorkDir "netcoredbg"
if (-not (Test-Path (Join-Path $repoDir ".git"))) {
    git clone $RepositoryUrl $repoDir
}

git -C $repoDir fetch --tags origin
git -C $repoDir checkout --force $Ref

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
cmake --build $buildDir --target install --config Release

Assert-InstalledDebugger $InstallDir
Save-NetcoredbgConfig $InstallDir
