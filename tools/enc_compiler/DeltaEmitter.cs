using System.Collections.Immutable;
using System.Text;
using System.Text.Json;
using System.Xml.Linq;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.Emit;
using Microsoft.CodeAnalysis.Text;

namespace NetcoredbgMcp.EncCompiler;

public sealed record SourceEdit(int StartLine, int EndLine, string NewText);

public sealed record DeltaEmitResult(
    bool Success,
    string? IlDeltaPath,
    string? MetadataDeltaPath,
    string? PdbDeltaPath,
    IReadOnlyList<string> RudeEdits,
    IReadOnlyList<string> Diagnostics);

public sealed class DeltaEmitter
{
    private static readonly CSharpParseOptions ParseOptions = CSharpParseOptions.Default
        .WithLanguageVersion(LanguageVersion.Preview);

    private static readonly CSharpCompilationOptions CompilationOptions = new(
        OutputKind.DynamicallyLinkedLibrary,
        optimizationLevel: OptimizationLevel.Debug,
        allowUnsafe: true);

    public async Task<DeltaEmitResult> EmitDeltaAsync(
        string projectPath,
        string filePath,
        IReadOnlyList<SourceEdit> edits,
        string? outputDirectory = null,
        CancellationToken cancellationToken = default)
    {
        if (edits.Count == 0)
        {
            return Failure("No edits provided.");
        }

        var projectRoot = ResolveProjectRoot(projectPath);
        var targetFile = ResolveTargetFile(projectRoot, filePath);
        var originalSource = await File.ReadAllTextAsync(targetFile, Encoding.UTF8, cancellationToken);
        var editedSource = ApplyEdits(originalSource, edits);

        var rudeEdits = DetectRudeEdits(originalSource, editedSource);
        if (rudeEdits.Count > 0)
        {
            return new DeltaEmitResult(false, null, null, null, rudeEdits, Array.Empty<string>());
        }

        var originalCompilation = CreateCompilation(projectRoot, targetFile, originalSource);
        var editedCompilation = CreateCompilation(projectRoot, targetFile, editedSource);
        var originalEmit = EmitInitialAssembly(originalCompilation, cancellationToken);
        if (!originalEmit.Success)
        {
            return Failure(originalEmit.Diagnostics);
        }

        var semanticEdits = FindSemanticEdits(originalCompilation, editedCompilation);
        if (semanticEdits.Length == 0)
        {
            return Failure("No supported method body edits found.");
        }

        using var module = ModuleMetadata.CreateFromImage(originalEmit.AssemblyBytes);
        var baseline = EmitBaseline.CreateInitialBaseline(
            originalCompilation,
            module,
            _ => default,
            _ => default,
            hasPortableDebugInformation: true);

        using var metadataDelta = new MemoryStream();
        using var ilDelta = new MemoryStream();
        using var pdbDelta = new MemoryStream();
        var difference = editedCompilation.EmitDifference(
            baseline,
            semanticEdits,
            _ => false,
            metadataDelta,
            ilDelta,
            pdbDelta,
            cancellationToken);

        if (!difference.Success)
        {
            return Failure(FormatDiagnostics(difference.Diagnostics));
        }

        var outputRoot = ResolveOutputDirectory(outputDirectory);
        var prefix = Path.GetFileNameWithoutExtension(targetFile);
        var metadataPath = Path.Combine(outputRoot, $"{prefix}.metadata");
        var ilPath = Path.Combine(outputRoot, $"{prefix}.il");
        var pdbPath = Path.Combine(outputRoot, $"{prefix}.pdb");

        await File.WriteAllBytesAsync(metadataPath, metadataDelta.ToArray(), cancellationToken);
        await File.WriteAllBytesAsync(ilPath, ilDelta.ToArray(), cancellationToken);
        await File.WriteAllBytesAsync(pdbPath, pdbDelta.ToArray(), cancellationToken);

        return new DeltaEmitResult(
            true,
            ilPath,
            metadataPath,
            pdbPath,
            Array.Empty<string>(),
            Array.Empty<string>());
    }

    private static string ApplyEdits(string source, IReadOnlyList<SourceEdit> edits)
    {
        var lines = SplitLines(source);
        foreach (var edit in edits.OrderByDescending(edit => edit.StartLine))
        {
            if (edit.StartLine < 1 || edit.EndLine < edit.StartLine || edit.EndLine > lines.Count)
            {
                throw new ArgumentOutOfRangeException(
                    nameof(edits),
                    $"Invalid edit range {edit.StartLine}..{edit.EndLine} for {lines.Count} lines.");
            }

            var replacementText = edit.NewText.TrimEnd('\r', '\n');
            var replacement = string.IsNullOrEmpty(replacementText)
                ? new List<string>()
                : SplitLines(replacementText);
            lines.RemoveRange(edit.StartLine - 1, edit.EndLine - edit.StartLine + 1);
            if (replacement.Count > 0)
            {
                lines.InsertRange(edit.StartLine - 1, replacement);
            }
        }

        return string.Join(Environment.NewLine, lines) + Environment.NewLine;
    }

    private static List<string> SplitLines(string text)
    {
        return text.Replace("\r\n", "\n", StringComparison.Ordinal)
            .Replace('\r', '\n')
            .Split('\n')
            .ToList();
    }

    private static PathMap ResolveProjectRoot(string projectPath)
    {
        var path = Path.GetFullPath(projectPath);
        if (File.Exists(path) && string.Equals(Path.GetExtension(path), ".csproj", StringComparison.OrdinalIgnoreCase))
        {
            return new PathMap(Path.GetDirectoryName(path)!, path);
        }

        if (Directory.Exists(path))
        {
            var projectFile = Directory.EnumerateFiles(path, "*.csproj", SearchOption.TopDirectoryOnly)
                .Order(StringComparer.OrdinalIgnoreCase)
                .FirstOrDefault();
            return new PathMap(path, projectFile);
        }

        throw new FileNotFoundException($"Project path not found: {path}", path);
    }

    private static string ResolveTargetFile(PathMap projectRoot, string filePath)
    {
        var path = Path.IsPathRooted(filePath)
            ? Path.GetFullPath(filePath)
            : Path.GetFullPath(Path.Combine(projectRoot.RootDirectory, filePath));

        var relativePath = Path.GetRelativePath(projectRoot.RootDirectory, path);
        if (relativePath.StartsWith("..", StringComparison.Ordinal) || Path.IsPathRooted(relativePath))
        {
            throw new InvalidOperationException($"Target file must be inside project root: {path}");
        }

        if (!File.Exists(path))
        {
            throw new FileNotFoundException($"Target file not found: {path}", path);
        }

        return path;
    }

    private static string ResolveOutputDirectory(string? outputDirectory)
    {
        var outputRoot = outputDirectory is null
            ? Path.Combine(Path.GetTempPath(), "netcoredbg-mcp-enc", Guid.NewGuid().ToString("N"))
            : Path.GetFullPath(outputDirectory);
        Directory.CreateDirectory(outputRoot);
        return outputRoot;
    }

    private static CSharpCompilation CreateCompilation(PathMap projectRoot, string targetFile, string targetSource)
    {
        var sourceFiles = Directory.EnumerateFiles(projectRoot.RootDirectory, "*.cs", SearchOption.AllDirectories)
            .Where(IsProjectSourceFile)
            .Order(StringComparer.OrdinalIgnoreCase)
            .ToArray();
        var trees = sourceFiles.Select(path =>
        {
            var source = string.Equals(path, targetFile, StringComparison.OrdinalIgnoreCase)
                ? targetSource
                : File.ReadAllText(path, Encoding.UTF8);
            return CSharpSyntaxTree.ParseText(
                SourceText.From(source, Encoding.UTF8),
                ParseOptions,
                path);
        });

        var assemblyName = projectRoot.ProjectFile is null
            ? Path.GetFileName(projectRoot.RootDirectory)
            : Path.GetFileNameWithoutExtension(projectRoot.ProjectFile);

        return CSharpCompilation.Create(
            assemblyName,
            trees,
            GetMetadataReferences(projectRoot),
            CompilationOptions);
    }

    private static bool IsProjectSourceFile(string path)
    {
        var segments = path.Split(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        return !segments.Any(segment =>
            string.Equals(segment, "bin", StringComparison.OrdinalIgnoreCase)
            || string.Equals(segment, "obj", StringComparison.OrdinalIgnoreCase));
    }

    private static ImmutableArray<MetadataReference> GetMetadataReferences(PathMap projectRoot)
    {
        var trustedAssemblies = ((string?)AppContext.GetData("TRUSTED_PLATFORM_ASSEMBLIES"))
            ?.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries)
            ?? Array.Empty<string>();

        return trustedAssemblies
            .Concat(GetProjectMetadataReferencePaths(projectRoot))
            .Where(File.Exists)
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .Select(path => (MetadataReference)MetadataReference.CreateFromFile(path))
            .ToImmutableArray();
    }

    private static IEnumerable<string> GetProjectMetadataReferencePaths(PathMap projectRoot)
    {
        if (projectRoot.ProjectFile is null)
        {
            yield break;
        }

        foreach (var path in GetReferenceHintPaths(projectRoot.ProjectFile))
        {
            yield return path;
        }

        foreach (var path in GetPackageAssetReferences(projectRoot))
        {
            yield return path;
        }

        foreach (var path in GetProjectReferenceOutputs(projectRoot.ProjectFile))
        {
            yield return path;
        }
    }

    private static IEnumerable<string> GetReferenceHintPaths(string projectFile)
    {
        var projectDirectory = Path.GetDirectoryName(projectFile)!;
        var document = XDocument.Load(projectFile);

        foreach (var reference in document.Descendants().Where(element => element.Name.LocalName == "Reference"))
        {
            var hintPath = reference.Elements()
                .FirstOrDefault(element => element.Name.LocalName == "HintPath")
                ?.Value;
            if (!string.IsNullOrWhiteSpace(hintPath))
            {
                yield return Path.GetFullPath(Path.Combine(projectDirectory, hintPath));
            }
        }
    }

    private static IEnumerable<string> GetPackageAssetReferences(PathMap projectRoot)
    {
        if (projectRoot.ProjectFile is null)
        {
            yield break;
        }

        var assetsPath = Path.Combine(projectRoot.RootDirectory, "obj", "project.assets.json");
        if (!File.Exists(assetsPath))
        {
            yield break;
        }

        using var document = JsonDocument.Parse(File.ReadAllText(assetsPath, Encoding.UTF8));
        var root = document.RootElement;
        if (!root.TryGetProperty("packageFolders", out var packageFolders)
            || !root.TryGetProperty("libraries", out var libraries)
            || !root.TryGetProperty("targets", out var targets))
        {
            yield break;
        }

        var folders = packageFolders.EnumerateObject()
            .Select(folder => folder.Name)
            .ToArray();

        foreach (var target in targets.EnumerateObject())
        {
            foreach (var dependency in target.Value.EnumerateObject())
            {
                if (!dependency.Value.TryGetProperty("compile", out var compileAssets)
                    || !libraries.TryGetProperty(dependency.Name, out var library)
                    || !library.TryGetProperty("path", out var packagePathElement))
                {
                    continue;
                }

                var packagePath = packagePathElement.GetString();
                if (string.IsNullOrWhiteSpace(packagePath))
                {
                    continue;
                }

                foreach (var asset in compileAssets.EnumerateObject())
                {
                    if (asset.Name.EndsWith("_._", StringComparison.Ordinal)
                        || !asset.Name.EndsWith(".dll", StringComparison.OrdinalIgnoreCase))
                    {
                        continue;
                    }

                    foreach (var folder in folders)
                    {
                        var candidate = Path.GetFullPath(Path.Combine(folder, packagePath, asset.Name));
                        if (File.Exists(candidate))
                        {
                            yield return candidate;
                            break;
                        }
                    }
                }
            }
        }
    }

    private static IEnumerable<string> GetProjectReferenceOutputs(string projectFile)
    {
        var projectDirectory = Path.GetDirectoryName(projectFile)!;
        var document = XDocument.Load(projectFile);

        foreach (var reference in document.Descendants()
            .Where(element => element.Name.LocalName == "ProjectReference"))
        {
            var include = reference.Attribute("Include")?.Value;
            if (string.IsNullOrWhiteSpace(include))
            {
                continue;
            }

            var referencedProject = Path.GetFullPath(Path.Combine(projectDirectory, include));
            var targetFramework = GetTargetFramework(referencedProject) ?? GetTargetFramework(projectFile);
            if (targetFramework is null)
            {
                continue;
            }

            var assemblyName = GetAssemblyName(referencedProject) ?? Path.GetFileNameWithoutExtension(referencedProject);
            foreach (var configuration in new[] { "Debug", "Release" })
            {
                var outputPath = Path.Combine(
                    Path.GetDirectoryName(referencedProject)!,
                    "bin",
                    configuration,
                    targetFramework,
                    $"{assemblyName}.dll");
                if (File.Exists(outputPath))
                {
                    yield return outputPath;
                    break;
                }
            }
        }
    }

    private static string? GetTargetFramework(string projectFile)
    {
        if (!File.Exists(projectFile))
        {
            return null;
        }

        var document = XDocument.Load(projectFile);
        var targetFramework = document.Descendants()
            .FirstOrDefault(element => element.Name.LocalName == "TargetFramework")
            ?.Value
            .Trim();
        if (!string.IsNullOrWhiteSpace(targetFramework))
        {
            return targetFramework;
        }

        var targetFrameworks = document.Descendants()
            .FirstOrDefault(element => element.Name.LocalName == "TargetFrameworks")
            ?.Value;
        return targetFrameworks?
            .Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .FirstOrDefault();
    }

    private static string? GetAssemblyName(string projectFile)
    {
        if (!File.Exists(projectFile))
        {
            return null;
        }

        return XDocument.Load(projectFile)
            .Descendants()
            .FirstOrDefault(element => element.Name.LocalName == "AssemblyName")
            ?.Value
            .Trim();
    }

    private static InitialEmitResult EmitInitialAssembly(
        CSharpCompilation compilation,
        CancellationToken cancellationToken)
    {
        using var assemblyStream = new MemoryStream();
        using var pdbStream = new MemoryStream();
        var result = compilation.Emit(
            assemblyStream,
            pdbStream: pdbStream,
            options: new EmitOptions(debugInformationFormat: DebugInformationFormat.PortablePdb),
            cancellationToken: cancellationToken);

        return result.Success
            ? new InitialEmitResult(true, assemblyStream.ToArray(), Array.Empty<string>())
            : new InitialEmitResult(false, Array.Empty<byte>(), FormatDiagnostics(result.Diagnostics));
    }

    private static ImmutableArray<SemanticEdit> FindSemanticEdits(
        CSharpCompilation originalCompilation,
        CSharpCompilation editedCompilation)
    {
        var originalMethods = GetMethodsByKey(originalCompilation);
        var editedMethods = GetMethodsByKey(editedCompilation);
        var edits = ImmutableArray.CreateBuilder<SemanticEdit>();

        foreach (var (key, editedMethod) in editedMethods)
        {
            if (!originalMethods.TryGetValue(key, out var originalMethod))
            {
                continue;
            }

            if (GetBodyText(originalMethod.Node) == GetBodyText(editedMethod.Node))
            {
                continue;
            }

            edits.Add(new SemanticEdit(
                SemanticEditKind.Update,
                originalMethod.Symbol,
                editedMethod.Symbol,
                syntaxMap: null,
                runtimeRudeEdit: null,
                instrumentation: default));
        }

        return edits.ToImmutable();
    }

    private static Dictionary<string, MethodSnapshot> GetMethodsByKey(CSharpCompilation compilation)
    {
        var methods = new Dictionary<string, MethodSnapshot>(StringComparer.Ordinal);
        foreach (var tree in compilation.SyntaxTrees)
        {
            var model = compilation.GetSemanticModel(tree);
            var root = tree.GetCompilationUnitRoot();
            foreach (var method in root.DescendantNodes().OfType<MethodDeclarationSyntax>())
            {
                if (model.GetDeclaredSymbol(method) is not IMethodSymbol symbol)
                {
                    continue;
                }

                methods[GetMethodKey(symbol)] = new MethodSnapshot(method, symbol);
            }
        }

        return methods;
    }

    private static string GetMethodKey(IMethodSymbol symbol)
    {
        return symbol.ToDisplayString(SymbolDisplayFormat.FullyQualifiedFormat);
    }

    private static string? GetBodyText(MethodDeclarationSyntax method)
    {
        return method.Body?.ToFullString() ?? method.ExpressionBody?.ToFullString();
    }

    private static IReadOnlyList<string> DetectRudeEdits(string originalSource, string editedSource)
    {
        var originalTree = CSharpSyntaxTree.ParseText(SourceText.From(originalSource, Encoding.UTF8), ParseOptions);
        var editedTree = CSharpSyntaxTree.ParseText(SourceText.From(editedSource, Encoding.UTF8), ParseOptions);
        var originalFields = GetFieldKeys(originalTree.GetCompilationUnitRoot());
        var editedFields = GetFieldKeys(editedTree.GetCompilationUnitRoot());
        var addedFields = editedFields.Except(originalFields, StringComparer.Ordinal).Order(StringComparer.Ordinal);

        return addedFields
            .Select(field => $"rude edit: cannot add field '{field}' to an existing class; restart debug session with rebuild.")
            .ToArray();
    }

    private static IReadOnlySet<string> GetFieldKeys(CompilationUnitSyntax root)
    {
        return root.DescendantNodes()
            .OfType<FieldDeclarationSyntax>()
            .SelectMany(field =>
            {
                var containingType = field.Ancestors()
                    .OfType<TypeDeclarationSyntax>()
                    .FirstOrDefault()
                    ?.Identifier.ValueText ?? "<global>";
                return field.Declaration.Variables.Select(variable => $"{containingType}.{variable.Identifier.ValueText}");
            })
            .ToHashSet(StringComparer.Ordinal);
    }

    private static string[] FormatDiagnostics(IEnumerable<Diagnostic> diagnostics)
    {
        return diagnostics
            .Where(diagnostic => diagnostic.Severity is DiagnosticSeverity.Error)
            .Select(diagnostic => diagnostic.ToString())
            .ToArray();
    }

    private static DeltaEmitResult Failure(params string[] diagnostics)
    {
        return new DeltaEmitResult(false, null, null, null, Array.Empty<string>(), diagnostics);
    }

    private static DeltaEmitResult Failure(IEnumerable<string> diagnostics)
    {
        return new DeltaEmitResult(false, null, null, null, Array.Empty<string>(), diagnostics.ToArray());
    }

    private sealed record PathMap(string RootDirectory, string? ProjectFile);

    private sealed record InitialEmitResult(bool Success, byte[] AssemblyBytes, IReadOnlyList<string> Diagnostics);

    private sealed record MethodSnapshot(MethodDeclarationSyntax Node, IMethodSymbol Symbol);
}
