using System.Text.Json;
using NetcoredbgMcp.EncCompiler;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal sealed record LegacyEncEdit(int StartLine, int EndLine, string NewText);


internal sealed record LegacyEncResult(
    bool Success,
    string? Error = null,
    string? File = null,
    string? Module = null,
    string? MetadataPath = null,
    string? IlPath = null,
    string? PdbPath = null,
    JsonElement? Apply = null,
    string? BackupPath = null,
    bool OutcomeUnknown = false,
    bool RequiresDrain = false);

internal sealed partial class NetCoreDbgSession
{
    private readonly HashSet<string> _legacyEncAttempts = new(StringComparer.OrdinalIgnoreCase);
    private readonly Dictionary<string, LegacyEncGeneration> _legacyEncGenerations = new(StringComparer.OrdinalIgnoreCase);
    private TaskCompletionSource<bool>? _legacyEncSettled;
    private sealed record LegacyEncGeneration((string Name, string Path) Module, string ProjectRoot,
        (long NewCount, long ChangedCount, long RemovedCount) Revision, DeltaEmitter.DeltaGeneration Compiler);
    private (long NewCount, long ChangedCount, long RemovedCount)? _legacyEncModuleRevision;

    internal async Task<LegacyEncResult> ApplyLegacyCodeChangeAsync(
        string projectRoot, string file, IReadOnlyList<LegacyEncEdit> edits, CancellationToken cancellationToken)
    {
        if (!_encEnabled)
            return new(false, "Edit-and-Continue is unavailable; use a debugger with ncdbhook.dll and restart debugging.");
        lock (_stateGate)
        {
            if (!TryAdmitExecution())
                return new(false, "Another execution operation is in progress or the debug session is closing.");
            _legacyEncSettled = new(TaskCreationOptions.RunContinuationsAsynchronously);
        }

        LegacyEncSource? source = null;
        PendingRequest? request = null;
        DeltaEmitter.DeltaGeneration? generation = null;
        var ownsGeneration = false;
        (long NewCount, long ChangedCount, long RemovedCount) moduleRevision = default;
        string? moduleName = null;
        string? deltaDirectory = null;
        var sourceWriteAttempted = false;
        var attempted = false;
        var preserveDeltas = false;
        try
        {
            var stopped = CaptureStoppedContext();
            if (stopped is null || stopped.Value.State.CurrentThreadId is not { } threadId)
                return new(false, "A current stopped debug session is required.");
            source = await LegacyEncSource.OpenAsync(projectRoot, file, cancellationToken).ConfigureAwait(false);
            var validation = ValidateLegacyEncEdits(source.Snapshot, edits);
            if (validation is not null) return new(false, validation);
            (string Name, string Path)? module;
            await _callStackAdmissionGate.WaitAsync(cancellationToken).ConfigureAwait(false);
            try
            {
                lock (_stateGate)
                {
                    moduleRevision = (_activityModuleNew, _activityModuleChanged, _activityModuleRemoved);
                    _legacyEncModuleRevision = moduleRevision;
                }
                module = ResolveLegacyEncModule(projectRoot, source.TargetPath);
            }
            finally { _callStackAdmissionGate.Release(); }
            if (module is null) return new(false, "The loaded project module is unavailable or ambiguous.");
            moduleName = module.Value.Name;
            LegacyEncGeneration? retained;
            var normalizedRoot = Path.TrimEndingDirectorySeparator(Path.GetFullPath(projectRoot));
            lock (_stateGate)
            {
                _legacyEncGenerations.TryGetValue(moduleName, out retained);
                if (retained is null && _legacyEncAttempts.Contains(moduleName))
                    return new(false, "An Edit-and-Continue delta outcome is unavailable for this module; restart debugging.");
                if (retained is not null && (retained.Module != module.Value || retained.Revision != moduleRevision
                    || !StringComparer.OrdinalIgnoreCase.Equals(retained.ProjectRoot, normalizedRoot)))
                    return new(false, "The loaded module changed since the previous code change; restart debugging.");
            }

            deltaDirectory = Directory.CreateTempSubdirectory("netcoredbg-mcp-enc-").FullName;
            var compilerEdits = edits.Select(static edit => new SourceEdit(edit.StartLine, edit.EndLine, edit.NewText)).ToArray();
            generation = retained?.Compiler;
            if (generation is null)
            {
                generation = new DeltaEmitter().StartGeneration(projectRoot, source.TargetPath, source.Snapshot,
                    module.Value.Path, cancellationToken);
                ownsGeneration = true;
            }
            var proposal = await generation.ProposeAsync(source.TargetPath, source.Snapshot, compilerEdits,
                deltaDirectory, cancellationToken).ConfigureAwait(false);
            var delta = proposal.Result;
            if (!delta.Success)
            {
                var details = delta.RudeEdits.Count != 0 ? delta.RudeEdits : delta.Diagnostics;
                return new(false, details.Count == 0 ? "Delta compilation failed." : string.Join("; ", details));
            }
            if (delta.MetadataDeltaPath is null || delta.IlDeltaPath is null || delta.PdbDeltaPath is null)
                return new(false, "The compiler returned incomplete delta paths.");
            var editedSource = DeltaEmitter.ApplyEdits(source.Snapshot, compilerEdits);
            var lineUpdates = Path.Combine(deltaDirectory, "lineupdates.bin");
            await File.WriteAllBytesAsync(lineUpdates, new byte[4], cancellationToken).ConfigureAwait(false);

            await _callStackAdmissionGate.WaitAsync(cancellationToken).ConfigureAwait(false);
            try
            {
                lock (_stateGate)
                {
                    if (_inspectionClosed || IsRetirementRequested || _executionAdmission != ExecutionAdmission.Active
                        || _state.Event != "stopped" || _state.CurrentThreadId != threadId
                        || !StringComparer.Ordinal.Equals(_inspectionContextId, stopped.Value.Context)
                        || _legacyEncModuleRevision != (_activityModuleNew, _activityModuleChanged, _activityModuleRemoved))
                        return new(false, "The stopped debug context changed while compiling the delta.");
                }
                var currentModule = ResolveLegacyEncModule(projectRoot, source.TargetPath);
                if (currentModule is null || currentModule.Value != module.Value)
                    return new(false, "The loaded project module changed while compiling the delta.");
                sourceWriteAttempted = true;
                await source.WriteAsync(editedSource, cancellationToken).ConfigureAwait(false);
                lock (_stateGate)
                {
                    if (_inspectionClosed || IsRetirementRequested || _executionAdmission != ExecutionAdmission.Active
                        || _state.Event != "stopped" || _state.CurrentThreadId != threadId
                        || !StringComparer.Ordinal.Equals(_inspectionContextId, stopped.Value.Context)
                        || _legacyEncModuleRevision != (_activityModuleNew, _activityModuleChanged, _activityModuleRemoved))
                        throw new InvalidOperationException("The stopped debug context changed before delta dispatch.");
                    attempted = _legacyEncAttempts.Add(moduleName);
                }
                request = await BeginRequestAsync("applyDeltas", new
                {
                    dllFileName = moduleName,
                    metadataPath = delta.MetadataDeltaPath,
                    ilPath = delta.IlDeltaPath,
                    pdbPath = delta.PdbDeltaPath,
                    lineUpdatesPath = lineUpdates,
                }, _requestTimeout, cancellationToken, inspectionContextId: stopped.Value.Context,
                    requestCreated: created => request = created).ConfigureAwait(false);
            }
            finally { _callStackAdmissionGate.Release(); }

            var response = await WaitForResponseAsync(request, _requestTimeout, cancellationToken).ConfigureAwait(false);
            await _callStackAdmissionGate.WaitAsync(cancellationToken).ConfigureAwait(false);
            try
            {
                bool current;
                lock (_stateGate)
                    current = !_inspectionClosed && !IsRetirementRequested && _executionAdmission == ExecutionAdmission.Active
                        && _state.Event == "stopped" && _state.CurrentThreadId == threadId
                        && response.InspectionContextId is not null
                        && StringComparer.Ordinal.Equals(_inspectionContextId, response.InspectionContextId)
                        && _legacyEncModuleRevision == (_activityModuleNew, _activityModuleChanged, _activityModuleRemoved);
                if (!response.Success || !response.TargetCurrent || !current
                    || ResolveLegacyEncModule(projectRoot, source.TargetPath) != module)
                {
                    preserveDeltas = true;
                    Interlocked.Exchange(ref _inspectionWriteDrainRequired, 1);
                    lock (_stateGate)
                    {
                        _inspectionContextId = null;
                        _inspectionClosed = true;
                    }
                    return new(false, "The delta outcome is unknown; source edits were retained. Restart debugging.",
                        BackupPath: source.BackupPath, OutcomeUnknown: true, RequiresDrain: true);
                }
                generation.Commit(proposal);
                if (ownsGeneration)
                {
                    lock (_stateGate)
                        _legacyEncGenerations.Add(moduleName, new(module.Value, normalizedRoot, moduleRevision, generation));
                    ownsGeneration = false;
                }
            }
            finally { _callStackAdmissionGate.Release(); }
            preserveDeltas = true;
            string? retainedBackup = null;
            try { await source.CompleteAsync().ConfigureAwait(false); }
            catch (Exception) { retainedBackup = source.BackupPath; }
            return new(true, File: source.TargetPath, Module: moduleName,
                MetadataPath: delta.MetadataDeltaPath, IlPath: delta.IlDeltaPath, PdbPath: delta.PdbDeltaPath,
                Apply: response.Body.ValueKind == JsonValueKind.Undefined ? null : response.Body.Clone(),
                BackupPath: retainedBackup);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            if (request?.WriteStarted == true) MarkLegacyEncUnknown();
            else if (source is not null && sourceWriteAttempted)
            {
                try
                {
                    await source.RestoreBeforeDispatchAsync().ConfigureAwait(false);
                    await source.CompleteAsync().ConfigureAwait(false);
                    if (attempted && moduleName is not null) lock (_stateGate) _legacyEncAttempts.Remove(moduleName);
                }
                catch (Exception) { preserveDeltas = true; }
            }
            throw;
        }
        catch (Exception exception)
        {
            if (request?.WriteStarted == true)
            {
                preserveDeltas = true;
                MarkLegacyEncUnknown();
                return new(false, "The delta outcome is unknown; source edits were retained. Restart debugging.",
                    BackupPath: source?.BackupPath, OutcomeUnknown: true, RequiresDrain: true);
            }
            if (source is not null && sourceWriteAttempted)
            {
                try
                {
                    await source.RestoreBeforeDispatchAsync().ConfigureAwait(false);
                    await source.CompleteAsync().ConfigureAwait(false);
                    if (attempted && moduleName is not null) lock (_stateGate) _legacyEncAttempts.Remove(moduleName);
                }
                catch (Exception)
                {
                    preserveDeltas = true;
                    return new(false, "Source restoration failed; the original backup was retained.",
                        BackupPath: source.BackupPath);
                }
            }
            return new(false, exception.Message);
        }
        finally
        {
            try
            {
                if (request is not null) RemovePending(request);
                if (ownsGeneration) generation?.Dispose();
                if (source is not null) await source.DisposeAsync().ConfigureAwait(false);
                if (deltaDirectory is not null && !preserveDeltas && request?.WriteStarted != true)
                {
                    try { Directory.Delete(deltaDirectory, recursive: true); } catch (IOException) { }
                }
            }
            finally
            {
                lock (_stateGate)
                {
                    _legacyEncModuleRevision = null;
                    _legacyEncSettled?.TrySetResult(true);
                    _legacyEncSettled = null;
                    if (_executionAdmission == ExecutionAdmission.Active) _executionAdmission = ExecutionAdmission.Available;
                    _executionSettled?.TrySetResult(true);
                    _executionSettled = null;
                }
            }
        }
    }

    private async Task DisposeLegacyEncGenerationsAsync()
    {
        Task? settled;
        lock (_stateGate) settled = _legacyEncSettled?.Task;
        if (settled is not null) await settled.ConfigureAwait(false);
        LegacyEncGeneration[] generations;
        lock (_stateGate)
        {
            generations = _legacyEncGenerations.Values.ToArray();
            _legacyEncGenerations.Clear();
        }
        foreach (var generation in generations) generation.Compiler.Dispose();
    }

    private void MarkLegacyEncUnknown()
    {
        Interlocked.Exchange(ref _inspectionWriteDrainRequired, 1);
        InvalidateInspectionContext(close: true);
    }

    private static string? ValidateLegacyEncEdits(string source, IReadOnlyList<LegacyEncEdit> edits)
    {
        if (edits.Count == 0) return "At least one source edit is required.";
        var lines = source.Replace("\r\n", "\n", StringComparison.Ordinal).Replace('\r', '\n').Split('\n');
        var lineCount = source.Length == 0 ? 0 : lines.Length - (source.EndsWith('\n') || source.EndsWith('\r') ? 1 : 0);
        foreach (var edit in edits)
        {
            if (edit.StartLine < 1 || edit.EndLine < edit.StartLine || edit.EndLine > lineCount)
                return "The source edit range is invalid.";
            var replacement = edit.NewText.TrimEnd('\r', '\n').Replace("\r\n", "\n", StringComparison.Ordinal).Replace('\r', '\n');
            var replacementCount = replacement.Length == 0 ? 1 : replacement.Split('\n').Length;
            if (replacementCount != edit.EndLine - edit.StartLine + 1)
                return "Line-changing edits are not supported until line updates are available.";
        }
        return null;
    }

    private (string Name, string Path)? ResolveLegacyEncModule(string projectRoot, string targetFile)
    {
        var inventory = GetRuntimeInventory(modules: true, maximumSerializedBytes: 256 * 1024);
        if (inventory.Status != DapRuntimeInventoryStatus.Success || inventory.SerializedContent is null) return null;
        using var document = JsonDocument.Parse(inventory.SerializedContent);
        var modules = new List<(string Name, string Path)>();
        foreach (var item in document.RootElement.GetProperty("modules").EnumerateArray())
        {
            var name = item.GetProperty("name").GetString();
            var path = item.GetProperty("path").ValueKind == JsonValueKind.String ? item.GetProperty("path").GetString() : null;
            if (!string.IsNullOrEmpty(name) && !string.IsNullOrEmpty(path)
                && name.EndsWith(".dll", StringComparison.OrdinalIgnoreCase))
                modules.Add((name, path));
        }
        var root = Path.GetFullPath(projectRoot);
        if (File.Exists(root) && Path.GetExtension(root).Equals(".csproj", StringComparison.OrdinalIgnoreCase))
            root = Path.GetDirectoryName(root)!;
        var current = Path.GetDirectoryName(targetFile);
        string? projectName = null;
        while (current is not null)
        {
            var project = Directory.EnumerateFiles(current, "*.csproj", SearchOption.TopDirectoryOnly)
                .Order(StringComparer.OrdinalIgnoreCase).FirstOrDefault();
            if (project is not null) { projectName = Path.GetFileNameWithoutExtension(project); break; }
            if (string.Equals(Path.TrimEndingDirectorySeparator(current), Path.TrimEndingDirectorySeparator(root), StringComparison.OrdinalIgnoreCase)) break;
            current = Path.GetDirectoryName(current);
        }
        if (projectName is not null)
        {
            var matching = modules.Where(module => string.Equals(module.Name, projectName + ".dll", StringComparison.OrdinalIgnoreCase)).ToArray();
            if (matching.Length == 1) return matching[0];
            if (matching.Length > 1) return null;
        }
        return modules.Count == 1 ? modules[0] : null;
    }
}
