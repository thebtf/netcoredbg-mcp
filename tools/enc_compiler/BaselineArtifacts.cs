using System.Collections.Immutable;
using System.Reflection.Metadata;
using System.Reflection.PortableExecutable;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.Emit;

namespace NetcoredbgMcp.EncCompiler;

internal sealed class BaselineArtifacts : IDisposable
{
    private static readonly Guid EncLocalSlotMapKind = new("755F52A8-91C5-45BE-B4B8-209571E552BD");
    private static readonly Guid EncLambdaAndClosureMapKind = new("A643004C-0240-496F-A783-30D64F4979DE");
    private static readonly Guid EncStateMachineStateMapKind = new("8B78CD68-2EDE-420B-980B-E15884B8AAA3");

    private readonly MetadataReaderProvider? _pdbProvider;
    private readonly MetadataReader? _pdbReader;

    private BaselineArtifacts(
        ModuleMetadata module,
        MetadataReaderProvider? pdbProvider,
        MetadataReader? pdbReader)
    {
        Module = module;
        _pdbProvider = pdbProvider;
        _pdbReader = pdbReader;
    }

    public ModuleMetadata Module { get; }

    public bool HasPortableDebugInformation => _pdbReader is not null;

    public static BaselineArtifacts FromModuleFile(string modulePath)
    {
        var module = ModuleMetadata.CreateFromFile(modulePath);
        try
        {
            var pdbProvider = OpenAssociatedPortablePdb(modulePath);
            return new BaselineArtifacts(module, pdbProvider, pdbProvider.GetMetadataReader());
        }
        catch
        {
            module.Dispose();
            throw;
        }
    }

    public static BaselineArtifacts FromImages(byte[] assemblyBytes, byte[] pdbBytes)
    {
        var module = ModuleMetadata.CreateFromImage(assemblyBytes);
        try
        {
            var pdbProvider = MetadataReaderProvider.FromPortablePdbImage(ImmutableArray.Create(pdbBytes));
            return new BaselineArtifacts(module, pdbProvider, pdbProvider.GetMetadataReader());
        }
        catch
        {
            module.Dispose();
            throw;
        }
    }

    public EditAndContinueMethodDebugInformation GetDebugInformation(MethodDefinitionHandle methodHandle)
    {
        if (_pdbReader is null)
        {
            return EditAndContinueMethodDebugInformation.Create(default, default, default);
        }

        return EditAndContinueMethodDebugInformation.Create(
            GetCustomDebugInformationBytes(methodHandle, EncLocalSlotMapKind),
            GetCustomDebugInformationBytes(methodHandle, EncLambdaAndClosureMapKind),
            GetCustomDebugInformationBytes(methodHandle, EncStateMachineStateMapKind));
    }

    public StandaloneSignatureHandle GetLocalSignature(MethodDefinitionHandle methodHandle)
    {
        return _pdbReader is null
            ? default
            : _pdbReader.GetMethodDebugInformation(methodHandle).LocalSignature;
    }

    public void Dispose()
    {
        _pdbProvider?.Dispose();
        Module.Dispose();
    }

    private static MetadataReaderProvider OpenAssociatedPortablePdb(string modulePath)
    {
        using var moduleStream = File.OpenRead(modulePath);
        using var peReader = new PEReader(moduleStream, PEStreamOptions.PrefetchEntireImage);
        if (peReader.TryOpenAssociatedPortablePdb(
            modulePath,
            OpenPdbStream,
            out var pdbProvider,
            out _))
        {
            return pdbProvider!;
        }

        throw new FileNotFoundException($"Associated portable PDB not found for baseline module: {modulePath}");
    }

    private static Stream OpenPdbStream(string path)
    {
        return File.Exists(path) ? File.OpenRead(path) : null!;
    }

    private ImmutableArray<byte> GetCustomDebugInformationBytes(MethodDefinitionHandle methodHandle, Guid kind)
    {
        foreach (var handle in _pdbReader!.GetCustomDebugInformation(methodHandle))
        {
            var customDebugInformation = _pdbReader.GetCustomDebugInformation(handle);
            if (_pdbReader.GetGuid(customDebugInformation.Kind) == kind)
            {
                return _pdbReader.GetBlobContent(customDebugInformation.Value);
            }
        }

        return default;
    }
}
