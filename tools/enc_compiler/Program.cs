using System.Text.Json;
using System.Text.Json.Serialization;
using NetcoredbgMcp.EncCompiler;

var jsonOptions = new JsonSerializerOptions
{
    PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
    WriteIndented = false,
};

try
{
    var stdin = await Console.In.ReadToEndAsync();
    if (string.IsNullOrWhiteSpace(stdin))
    {
        await WriteResponseAsync(CliResponse.Failure(["JSON stdin is required."]));
        return 1;
    }

    var request = JsonSerializer.Deserialize<CliRequest>(stdin, jsonOptions);
    if (request is null)
    {
        await WriteResponseAsync(CliResponse.Failure(["JSON stdin could not be parsed."]));
        return 1;
    }

    if (request.Edits is null)
    {
        await WriteResponseAsync(CliResponse.Failure(["edits is required."]));
        return 1;
    }

    var edits = request.Edits
        .Select(edit => new SourceEdit(edit.StartLine, edit.EndLine, edit.NewText))
        .ToArray();
    var result = await new DeltaEmitter().EmitDeltaAsync(
        request.ProjectPath,
        request.FilePath,
        edits,
        request.OutputDir);

    await WriteResponseAsync(CliResponse.FromResult(result));
    return result.Success ? 0 : 1;
}
catch (Exception ex) when (ex is JsonException or IOException or ArgumentException or InvalidOperationException)
{
    await WriteResponseAsync(CliResponse.Failure([ex.Message]));
    return 1;
}

async Task WriteResponseAsync(CliResponse response)
{
    await Console.Out.WriteLineAsync(JsonSerializer.Serialize(response, jsonOptions));
}

internal sealed record CliRequest(
    [property: JsonPropertyName("project_path")] string ProjectPath,
    [property: JsonPropertyName("file_path")] string FilePath,
    [property: JsonPropertyName("edits")] IReadOnlyList<CliEdit> Edits,
    [property: JsonPropertyName("output_dir")] string? OutputDir = null);

internal sealed record CliEdit(
    [property: JsonPropertyName("start_line")] int StartLine,
    [property: JsonPropertyName("end_line")] int EndLine,
    [property: JsonPropertyName("new_text")] string NewText);

internal sealed record CliResponse(
    bool Success,
    [property: JsonPropertyName("il_delta_path")] string? IlDeltaPath,
    [property: JsonPropertyName("metadata_delta_path")] string? MetadataDeltaPath,
    [property: JsonPropertyName("pdb_delta_path")] string? PdbDeltaPath,
    [property: JsonPropertyName("rude_edits")] IReadOnlyList<string> RudeEdits,
    IReadOnlyList<string> Diagnostics)
{
    public static CliResponse FromResult(DeltaEmitResult result)
    {
        return new CliResponse(
            result.Success,
            result.IlDeltaPath,
            result.MetadataDeltaPath,
            result.PdbDeltaPath,
            result.RudeEdits,
            result.Diagnostics);
    }

    public static CliResponse Failure(IReadOnlyList<string> diagnostics)
    {
        return new CliResponse(false, null, null, null, Array.Empty<string>(), diagnostics);
    }
}
