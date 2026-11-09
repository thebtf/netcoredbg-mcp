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

function Assert-NoReparseAncestors {
    param([string]$Path)
    $itemPath = [IO.Path]::GetFullPath($Path)
    while ($itemPath) {
        $item = Get-Item -LiteralPath $itemPath -Force -ErrorAction SilentlyContinue
        if ($null -ne $item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Refusing reparse point: $itemPath"
        }
        $parent = [IO.Path]::GetDirectoryName($itemPath)
        if (-not $parent -or $parent -eq $itemPath) { break }
        $itemPath = $parent
    }
}

function Assert-PlainTree {
    param([string]$Path)
    Assert-NoReparseAncestors $Path
    if ([IO.Directory]::Exists($Path)) {
        foreach ($entry in [IO.Directory]::EnumerateFileSystemEntries($Path)) {
            Assert-PlainTree $entry
        }
    }
}

function Assert-Debugger {
    param([string]$Directory)
    foreach ($name in @('netcoredbg.exe', 'ncdbhook.dll')) {
        $path = Join-Path $Directory $name
        Assert-NoReparseAncestors $path
        if (-not [IO.File]::Exists($path) -or (Get-Item -LiteralPath $path).Length -eq 0) {
            throw "Required non-empty debugger file is missing: $path"
        }
    }
}

function Get-ConfigSnapshot {
    param([string]$Path)
    Assert-NoReparseAncestors $Path
    if ([IO.Directory]::Exists($Path)) { throw "Configuration path is a directory: $Path" }
    if (-not [IO.File]::Exists($Path)) { return $null }
    return ,([IO.File]::ReadAllBytes($Path))
}

function Assert-ConfigUnchanged {
    param([string]$Path, [AllowNull()][byte[]]$Original)
    $current = Get-ConfigSnapshot $Path
    if ($null -eq $Original) {
        if ($null -ne $current) { throw "Configuration appeared during setup: $Path" }
    } elseif ($null -eq $current -or [Convert]::ToBase64String($current) -cne [Convert]::ToBase64String($Original)) {
        throw "Configuration changed during setup: $Path"
    }
}

# Edit only the three owned JSON values. Never deserialize unrelated numbers or
# date-like strings through PowerShell's version-dependent JSON conversions.
if (-not ('EncConfigEditor' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Text;

public sealed class EncConfigEditor {
    private readonly string text;
    private readonly string[] values;
    private readonly bool[] found = new bool[3];
    private readonly List<Edit> edits = new List<Edit>();
    private int pos;
    private bool foundDebugger;
    private static readonly string[] names = { "version", "source", "path" };
    private sealed class Edit {
        internal int Start, Length;
        internal string Value;
        internal Edit(int start, int length, string value) { Start = start; Length = length; Value = value; }
    }
    private EncConfigEditor(string input, string version, string path) {
        text = input;
        values = new[] { Quote(version), Quote("thebtf/netcoredbg"), Quote(path) };
    }
    private static string Quote(string s) {
        var b = new StringBuilder("\"");
        foreach (char c in s) {
            if (c == '"' || c == '\\') b.Append('\\').Append(c);
            else if (c < 0x20 || char.IsSurrogate(c)) b.Append("\\u").Append(((int)c).ToString("x4"));
            else b.Append(c);
        }
        return b.Append('"').ToString();
    }
    private void Error() { throw new FormatException("Invalid, ambiguous, or excessively deep configuration JSON at character " + pos); }
    private void Space() { while (pos < text.Length && (text[pos] == ' ' || text[pos] == '\t' || text[pos] == '\r' || text[pos] == '\n')) pos++; }
    private bool Take(char c) { Space(); if (pos < text.Length && text[pos] == c) { pos++; return true; } return false; }
    private void Expect(char c) { if (!Take(c)) Error(); }
    private string StringValue() {
        Expect('"');
        var b = new StringBuilder();
        while (pos < text.Length) {
            char c = text[pos++];
            if (c == '"') return b.ToString();
            if (c < 0x20) Error();
            if (c != '\\') { b.Append(c); continue; }
            if (pos >= text.Length) Error();
            c = text[pos++];
            switch (c) {
                case '"': case '\\': case '/': b.Append(c); break;
                case 'b': b.Append('\b'); break;
                case 'f': b.Append('\f'); break;
                case 'n': b.Append('\n'); break;
                case 'r': b.Append('\r'); break;
                case 't': b.Append('\t'); break;
                case 'u':
                    if (pos + 4 > text.Length) Error();
                    int n = 0;
                    for (int i = 0; i < 4; i++) {
                        char h = text[pos++];
                        int d = h >= '0' && h <= '9' ? h - '0' : h >= 'a' && h <= 'f' ? h - 'a' + 10 : h >= 'A' && h <= 'F' ? h - 'A' + 10 : -1;
                        if (d < 0) Error();
                        n = n * 16 + d;
                    }
                    b.Append((char)n); break;
                default: Error(); break;
            }
        }
        Error(); return null;
    }
    private bool Digit() { return pos < text.Length && text[pos] >= '0' && text[pos] <= '9'; }
    private void Number() {
        if (pos < text.Length && text[pos] == '-') pos++;
        if (pos < text.Length && text[pos] == '0') pos++;
        else { if (!Digit() || text[pos] == '0') Error(); while (Digit()) pos++; }
        if (pos < text.Length && text[pos] == '.') { pos++; if (!Digit()) Error(); while (Digit()) pos++; }
        if (pos < text.Length && (text[pos] == 'e' || text[pos] == 'E')) {
            pos++; if (pos < text.Length && (text[pos] == '+' || text[pos] == '-')) pos++;
            if (!Digit()) Error(); while (Digit()) pos++;
        }
    }
    private void Value(int depth) {
        if (depth > 64) Error();
        Space(); if (pos >= text.Length) Error();
        char c = text[pos];
        if (c == '{') { Object(depth, false, false); return; }
        if (c == '[') {
            pos++; if (Take(']')) return;
            do { Value(depth + 1); } while (Take(','));
            Expect(']'); return;
        }
        if (c == '"') { StringValue(); return; }
        foreach (string literal in new[] { "true", "false", "null" }) {
            if (text.Length - pos >= literal.Length && string.CompareOrdinal(text, pos, literal, 0, literal.Length) == 0) { pos += literal.Length; return; }
        }
        if (c == '-' || Digit()) { Number(); return; }
        Error();
    }
    private string Fields() {
        var result = new StringBuilder();
        for (int i = 0; i < 3; i++) if (!found[i]) {
            if (result.Length > 0) result.Append(',');
            result.Append(Quote(names[i])).Append(':').Append(values[i]);
        }
        return result.ToString();
    }
    private void Object(int depth, bool root, bool debugger) {
        if (depth > 64) Error();
        Expect('{'); int count = 0;
        Space();
        if (pos >= text.Length) Error();
        while (text[pos] != '}') {
            if (count > 0) Expect(',');
            string key = StringValue(); Expect(':'); Space();
            int start = pos;
            if (root && string.Equals(key, "netcoredbg", StringComparison.Ordinal)) {
                if (foundDebugger) Error();
                foundDebugger = true;
                if (pos >= text.Length || text[pos] != '{') Error();
                Object(depth + 1, false, true);
            } else {
                Value(depth + 1);
                if (debugger) for (int i = 0; i < 3; i++) {
                    if (string.Equals(key, names[i], StringComparison.Ordinal)) {
                        if (found[i]) Error();
                        found[i] = true;
                        edits.Add(new Edit(start, pos - start, values[i]));
                    }
                }
            }
            count++; Space(); if (pos >= text.Length) Error();
        }
        if (debugger) {
            string fields = Fields();
            if (fields.Length > 0) edits.Add(new Edit(pos, 0, (count > 0 ? "," : "") + fields));
        }
        if (root && !foundDebugger) {
            edits.Add(new Edit(pos, 0, (count > 0 ? "," : "") + "\"netcoredbg\":{" + Fields() + "}"));
        }
        pos++;
    }
    public static string Update(string input, string version, string path) {
        var parser = new EncConfigEditor(input, version, path);
        parser.Space(); parser.Object(0, true, false); parser.Space();
        if (parser.pos != input.Length) parser.Error();
        parser.edits.Sort((a, b) => b.Start.CompareTo(a.Start));
        var result = new StringBuilder(input);
        foreach (var edit in parser.edits) result.Remove(edit.Start, edit.Length).Insert(edit.Start, edit.Value);
        return result.ToString();
    }
    public static string Decode(byte[] bytes) {
        Encoding encoding = new UTF8Encoding(false, true); int offset = 0;
        if (bytes.Length >= 4 && bytes[0] == 0xff && bytes[1] == 0xfe && bytes[2] == 0 && bytes[3] == 0) { encoding = new UTF32Encoding(false, true, true); offset = 4; }
        else if (bytes.Length >= 4 && bytes[0] == 0 && bytes[1] == 0 && bytes[2] == 0xfe && bytes[3] == 0xff) { encoding = new UTF32Encoding(true, true, true); offset = 4; }
        else if (bytes.Length >= 3 && bytes[0] == 0xef && bytes[1] == 0xbb && bytes[2] == 0xbf) offset = 3;
        else if (bytes.Length >= 2 && bytes[0] == 0xff && bytes[1] == 0xfe) { encoding = new UnicodeEncoding(false, true, true); offset = 2; }
        else if (bytes.Length >= 2 && bytes[0] == 0xfe && bytes[1] == 0xff) { encoding = new UnicodeEncoding(true, true, true); offset = 2; }
        return encoding.GetString(bytes, offset, bytes.Length - offset);
    }
}
'@
}

function Assert-ArchiveEntries {
    param([string]$Archive)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName.Replace('\', '/')
            if ($name.StartsWith('/') -or $name.Contains(':') -or @($name.Split('/') | Where-Object { $_ -eq '..' }).Count -gt 0) {
                throw "Unsafe archive entry: $name"
            }
            $mode = ($entry.ExternalAttributes -shr 16) -band 0xF000
            if ($mode -eq 0xA000 -or ($entry.ExternalAttributes -band 0x400) -ne 0) {
                throw "Archive contains a link or reparse point: $name"
            }
        }
    } finally { $zip.Dispose() }
}

if (-not $BuildFromSource -and -not [System.Runtime.InteropServices.RuntimeInformation]::IsOSPlatform([System.Runtime.InteropServices.OSPlatform]::Windows)) {
    throw 'The prebuilt EnC release currently requires Windows. Use -BuildFromSource on supported hosts.'
}

if ($BuildFromSource) {
    foreach ($command in @('git', 'cmake', 'dotnet', 'cl.exe')) {
        if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
            throw "BuildFromSource requires '$command' on PATH. See https://github.com/Samsung/netcoredbg#building"
        }
    }
}

$InstallDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
$WorkDir = [IO.Path]::GetFullPath($WorkDir)
$parent = [IO.Path]::GetDirectoryName($InstallDir)
if (($WorkDir.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar).StartsWith($InstallDir + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'WorkDir must not be inside InstallDir, because the installation is moved during replacement.'
}
if (-not $parent -or $InstallDir -eq [IO.Path]::GetPathRoot($InstallDir)) { throw 'InstallDir must have a parent directory.' }
$configPath = Join-Path $parent 'config.json'
if ($InstallDir -ieq $configPath) { throw 'InstallDir conflicts with config.json.' }
Assert-NoReparseAncestors $parent
Assert-NoReparseAncestors $WorkDir
[IO.Directory]::CreateDirectory($parent) | Out-Null
[IO.Directory]::CreateDirectory($WorkDir) | Out-Null
Assert-NoReparseAncestors $parent
Assert-NoReparseAncestors $WorkDir

$id = [Guid]::NewGuid().ToString('N')
$runDir = Join-Path $WorkDir ("enc-setup-" + $id)
$stage = Join-Path $parent (([IO.Path]::GetFileName($InstallDir)) + ".staging-" + $id)
$backup = Join-Path $parent (([IO.Path]::GetFileName($InstallDir)) + ".backup-" + $id)
$configStage = Join-Path $parent ("config.json.staging-" + $id)
$configBackup = Join-Path $parent ("config.json.backup-" + $id)
$lockPath = Join-Path $parent 'config.json.enc-setup.lock'
$lock = $null
try {
    $lock = [IO.FileStream]::new($lockPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None, 4096, [IO.FileOptions]::DeleteOnClose)
    foreach ($path in @($runDir, $stage, $backup, $configStage, $configBackup)) {
        if ([IO.File]::Exists($path) -or [IO.Directory]::Exists($path)) { throw "Workspace path already exists: $path" }
    }
    if ([IO.File]::Exists($InstallDir)) { throw "InstallDir is not a directory: $InstallDir" }
    Assert-PlainTree $InstallDir
    $originalConfig = Get-ConfigSnapshot $configPath
    try {
        $text = if ($null -eq $originalConfig) { '{}' } else { [EncConfigEditor]::Decode($originalConfig) }
        $json = [EncConfigEditor]::Update($text, $ReleaseTag, (Join-Path $InstallDir 'netcoredbg.exe'))
    } catch { throw "Cannot safely read configuration $configPath : $_" }
    $configStream = [IO.File]::Open($configStage, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($json)
        $configStream.Write($bytes, 0, $bytes.Length)
    } finally { $configStream.Dispose() }

    [IO.Directory]::CreateDirectory($runDir) | Out-Null
    [IO.Directory]::CreateDirectory($stage) | Out-Null
    if ($BuildFromSource) {
        $repoDir = Join-Path $runDir 'netcoredbg'
        $buildDir = Join-Path $runDir 'build-enc'
        git clone $RepositoryUrl $repoDir
        if ($LASTEXITCODE -ne 0) { throw "git clone failed with exit code $LASTEXITCODE" }
        git -C $repoDir fetch --tags origin
        if ($LASTEXITCODE -ne 0) { throw "git fetch failed with exit code $LASTEXITCODE" }
        git -C $repoDir checkout $Ref
        if ($LASTEXITCODE -ne 0) { throw "git checkout failed with exit code $LASTEXITCODE" }
        $hookPath = (Join-Path $InstallDir 'ncdbhook.dll').Replace('\', '/')
        $cmakeConfigureArgs = @('-S', $repoDir, '-B', $buildDir,
            "-DNCDB_DOTNET_STARTUP_HOOK=$hookPath", '-DBUILD_MANAGED=1',
            "-DCMAKE_INSTALL_PREFIX=$stage")
        cmake @cmakeConfigureArgs
        if ($LASTEXITCODE -ne 0) { throw "cmake configure failed with exit code $LASTEXITCODE" }
        cmake --build $buildDir --target install --config Release
        if ($LASTEXITCODE -ne 0) { throw "cmake build/install failed with exit code $LASTEXITCODE" }
    } else {
        $archivePath = Join-Path $runDir 'release.zip'
        $downloadUrl = "https://github.com/thebtf/netcoredbg/releases/download/$ReleaseTag/$AssetName"
        Write-Host "Downloading EnC-capable netcoredbg: $downloadUrl"
        Invoke-WebRequest -Uri $downloadUrl -OutFile $archivePath -UseBasicParsing
        $actualSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash
        if ($actualSha256 -ne $AssetSha256) { throw "Archive checksum mismatch. Expected $AssetSha256, got $actualSha256." }
        Assert-ArchiveEntries $archivePath
        Expand-Archive -LiteralPath $archivePath -DestinationPath $stage
    }
    Assert-PlainTree $stage
    Assert-Debugger $stage
    Assert-PlainTree $InstallDir
    Assert-ConfigUnchanged $configPath $originalConfig

    if ([IO.Directory]::Exists($InstallDir)) {
        [IO.Directory]::Move($InstallDir, $backup)
        Write-Host "Previous installation retained: $backup"
    }
    [IO.Directory]::Move($stage, $InstallDir)
    Assert-ConfigUnchanged $configPath $originalConfig
    if ($null -eq $originalConfig) {
        [IO.File]::Move($configStage, $configPath)
    } else {
        [IO.File]::Replace($configStage, $configPath, $configBackup)
        Write-Host "Previous configuration retained: $configBackup"
    }
    Write-Host "EnC-capable netcoredbg installed:"
    Write-Host "  NETCOREDBG_PATH=$(Join-Path $InstallDir 'netcoredbg.exe')"
    Write-Host "  NCDBHOOK_PATH=$(Join-Path $InstallDir 'ncdbhook.dll')"
    try {
        Assert-PlainTree $runDir
        Remove-Item -LiteralPath $runDir -Recurse -Force
    } catch {
        Write-Warning "Installation succeeded but workspace cleanup was refused or failed; inspect $runDir : $_"
    }
} catch {
    Write-Host "Setup failed; inspect these recovery paths before retrying:"
    Write-Host "  Installation: $InstallDir"
    Write-Host "  Previous installation: $backup"
    Write-Host "  Staged installation: $stage"
    Write-Host "  Configuration: $configPath"
    Write-Host "  Previous configuration: $configBackup"
    Write-Host "  Staged configuration: $configStage"
    Write-Host "  Download workspace: $runDir"
    throw
} finally {
    if ($null -ne $lock) {
        $lock.Dispose()
    }
}
