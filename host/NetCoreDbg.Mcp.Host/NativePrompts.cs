using System.Text.Json;
using ModelContextProtocol;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;

namespace NetCoreDbg.Mcp.Host;

/// <summary>
/// Native re-implementation of the eight static MCP prompts ("inline skills") owned by the
/// direct Python server's <c>src/netcoredbg_mcp/prompts.py</c>. Prompt names, descriptions,
/// argument schemas, message roles/content, and error behavior are reproduced exactly from
/// that module; <c>src/netcoredbg_mcp/prompts.py</c> remains the source of truth for the
/// public prompt surface, and any future change there must be re-ported here.
///
/// This is intentionally NOT a relay module: unlike <c>ToolsRelay.cs</c> and its
/// <c>RelaySession</c>/<c>RelayRouteCatalog</c> collaborators, this module never talks to the
/// Python backend and owns no relay/session state. It also never appears in
/// <see cref="RelayRouteCatalog"/>, which tracks only downstream-to-upstream Python relay
/// ownership. The one entry point, <see cref="Register"/>, only wires
/// <see cref="McpServerHandlers.ListPromptsHandler"/> and
/// <see cref="McpServerHandlers.GetPromptHandler"/>; the future composition root
/// (<c>RelayComposition.cs</c>) is solely responsible for calling it and for combining this
/// native prompts capability with whatever relay modules it also wires up.
/// </summary>
internal static partial class NativePrompts
{
    public static void Register(McpServerHandlers handlers)
    {
        handlers.ListPromptsHandler = (_, _) => ValueTask.FromResult(ListPromptsResultInstance);
        handlers.GetPromptHandler = (context, _) => ValueTask.FromResult(GetPrompt(context.Params));
    }

    /// <summary>
    /// Normalizes a source-owned raw string literal's embedded line terminators to LF,
    /// matching direct Python's own triple-quoted prompt/playbook content (always LF,
    /// regardless of how the Python source file itself is checked out). C# raw string
    /// literals preserve the PHYSICAL line-ending bytes of the .cs source file verbatim;
    /// a plain Windows checkout with core.autocrlf=true (this repo has no .gitattributes
    /// override) materializes these files as CRLF, which would otherwise leak into every
    /// rendered static prompt and exception playbook as "\r\n" instead of Python's "\n" -
    /// breaking wire-level parity on every checkout/worktree/cherry-pick whose working
    /// tree ends up CRLF, independent of what this worktree's own files happened to be.
    ///
    /// This is applied ONLY to the fixed template constants in
    /// <c>NativePromptsContent.cs</c> and <c>NativePromptsPlaybooks.cs</c> - never to
    /// caller-supplied argument values (symptom/problem/app_type/file_hint) flowing
    /// through <see cref="BuildInvestigationPlan"/>/<see cref="BuildScenarioPlan"/>, which
    /// are opaque user data Python does not touch either.
    /// </summary>
    private static string NormalizeSourceOwnedText(string rawSourceLiteral) =>
        rawSourceLiteral.Replace("\r\n", "\n");

    // ── Catalog ──────────────────────────────────────────────────────────
    //
    // Order matches src/netcoredbg_mcp/prompts.py's register_prompts() declaration order
    // exactly: debug, debug-gui, debug-exception, debug-visual, debug-mistakes, investigate,
    // debug-scenario, dap-escape-hatch. FastMCP prompts/list has no pagination in practice
    // (the low-level SDK's list_prompts() decorator ignores the request's cursor and always
    // returns the full list with no nextCursor - see NativePrompts.Register's ListPromptsHandler),
    // so this catalog is returned in full, unconditionally, on every call.

    private static readonly Prompt DebugPrompt = new()
    {
        Name = "debug",
        Description =
            "Complete guide to debugging .NET apps. " +
            "Start here before your first debug session. " +
            "Covers state machine, tool usage, anti-patterns, workflows.",
        Arguments = new List<PromptArgument>(),
    };

    private static readonly Prompt DebugGuiPrompt = new()
    {
        Name = "debug-gui",
        Description =
            "WPF and Avalonia Desktop UI debugging workflow. " +
            "Use when debugging GUI apps — critical breakpoint timing " +
            "and UI interaction rules that differ from console apps.",
        Arguments = new List<PromptArgument>(),
    };

    private static readonly Prompt DebugExceptionPrompt = new()
    {
        Name = "debug-exception",
        Description =
            "Step-by-step exception investigation. " +
            "Use when the debugger stops on an exception or app crashes.",
        Arguments = new List<PromptArgument>(),
    };

    private static readonly Prompt DebugVisualPrompt = new()
    {
        Name = "debug-visual",
        Description =
            "Visual UI inspection via screenshots and Set-of-Mark annotation. " +
            "Use when you need to SEE the app UI, verify layout, or click " +
            "elements by visual position.",
        Arguments = new List<PromptArgument>(),
    };

    private static readonly Prompt DebugMistakesPrompt = new()
    {
        Name = "debug-mistakes",
        Description =
            "Common debugging anti-patterns with WRONG/CORRECT examples. " +
            "Use as a checklist to avoid known pitfalls.",
        Arguments = new List<PromptArgument>(),
    };

    private static readonly Prompt InvestigatePrompt = new()
    {
        Name = "investigate",
        Description =
            "Targeted investigation for a specific exception type or symptom. " +
            "Pass the exception name or symptom description to get a focused " +
            "debugging plan with exact tools and steps.",
        Arguments = new List<PromptArgument>
        {
            new() { Name = "symptom", Required = true },
            new() { Name = "app_type", Required = false },
        },
    };

    private static readonly Prompt DebugScenarioPrompt = new()
    {
        Name = "debug-scenario",
        Description =
            "Get a step-by-step debugging plan for a specific scenario. " +
            "Pass a description of the problem and get exact tool calls to execute.",
        Arguments = new List<PromptArgument>
        {
            new() { Name = "problem", Required = true },
            new() { Name = "app_type", Required = false },
            new() { Name = "file_hint", Required = false },
        },
    };

    private static readonly Prompt DapEscapeHatchPrompt = new()
    {
        Name = "dap-escape-hatch",
        Description =
            "Unwrapped DAP command reference. " +
            "Use when a specific DAP command has no first-class MCP tool yet.",
        Arguments = new List<PromptArgument>(),
    };

    private static readonly Prompt[] Catalog =
    {
        DebugPrompt,
        DebugGuiPrompt,
        DebugExceptionPrompt,
        DebugVisualPrompt,
        DebugMistakesPrompt,
        InvestigatePrompt,
        DebugScenarioPrompt,
        DapEscapeHatchPrompt,
    };

    private static readonly ListPromptsResult ListPromptsResultInstance = new() { Prompts = Catalog };

    private static readonly Dictionary<string, Prompt> CatalogByName =
        Catalog.ToDictionary(prompt => prompt.Name);

    // ── prompts/get dispatch ─────────────────────────────────────────────

    private static GetPromptResult GetPrompt(GetPromptRequestParams request)
    {
        if (!CatalogByName.TryGetValue(request.Name, out var prompt))
        {
            // Matches direct Python's FastMCP.get_prompt(): "raise ValueError(f"Unknown
            // prompt: {name}")", surfaced by the low-level server as a JSON-RPC error with
            // code 0 (see mcp.server.lowlevel.server.Server._handle_request's generic
            // `except Exception as err: response = types.ErrorData(code=0, ...)` path).
            throw UnknownPromptError(request.Name);
        }

        var arguments = request.Arguments;
        var messages = prompt.Name switch
        {
            "debug" => SingleUserMessage(DebugGuideText),
            "debug-gui" => SingleUserMessage(DebugGuiText),
            "debug-exception" => DebugExceptionMessages(),
            "debug-visual" => SingleUserMessage(DebugVisualText),
            "debug-mistakes" => SingleUserMessage(DebugMistakesText),
            "investigate" => InvestigateMessages(arguments),
            "debug-scenario" => DebugScenarioMessages(arguments),
            "dap-escape-hatch" => SingleUserMessage(DapEscapeHatchText),
            _ => throw UnknownPromptError(request.Name),
        };

        return new GetPromptResult { Description = prompt.Description, Messages = messages };
    }

    private static McpProtocolException UnknownPromptError(string name) =>
        new($"Unknown prompt: {name}", (McpErrorCode)0);

    private static McpProtocolException MissingRequiredArgumentError(string name) =>
        new($"Missing required arguments: {{'{name}'}}", (McpErrorCode)0);

    // ── Message builders for the six argument-less prompts ──────────────

    private static List<PromptMessage> SingleUserMessage(string text) =>
        new() { UserMessage(text) };

    private static List<PromptMessage> DebugExceptionMessages() =>
        new()
        {
            UserMessage("The debugger stopped on an exception."),
            AssistantMessage("I'll investigate. Let me gather details."),
            UserMessage(DebugExceptionText),
        };

    private static PromptMessage UserMessage(string text) =>
        new() { Role = Role.User, Content = new TextContentBlock { Text = text } };

    private static PromptMessage AssistantMessage(string text) =>
        new() { Role = Role.Assistant, Content = new TextContentBlock { Text = text } };

    // ── Parameterized prompts ───────────────────────────────────────────

    private static List<PromptMessage> InvestigateMessages(IDictionary<string, JsonElement>? arguments)
    {
        var symptom = RequireArgument(arguments, "symptom");
        var appType = OptionalArgument(arguments, "app_type", "gui");
        return SingleUserMessage(BuildInvestigationPlan(symptom, appType));
    }

    private static List<PromptMessage> DebugScenarioMessages(IDictionary<string, JsonElement>? arguments)
    {
        var problem = RequireArgument(arguments, "problem");
        var appType = OptionalArgument(arguments, "app_type", "gui");
        var fileHint = OptionalArgument(arguments, "file_hint", "");
        return SingleUserMessage(BuildScenarioPlan(problem, appType, fileHint));
    }

    /// <summary>
    /// Matches direct Python's <c>Prompt.render()</c>: required-argument presence is checked
    /// by KEY membership in the supplied arguments (<c>required - set(arguments or {})</c>),
    /// not by value truthiness - an explicitly supplied empty string still counts as provided.
    /// </summary>
    private static string RequireArgument(IDictionary<string, JsonElement>? arguments, string name)
    {
        if (arguments is null || !arguments.TryGetValue(name, out var element))
        {
            throw MissingRequiredArgumentError(name);
        }

        return element.ValueKind == JsonValueKind.String ? element.GetString()! : element.GetRawText();
    }

    private static string OptionalArgument(IDictionary<string, JsonElement>? arguments, string name, string defaultValue)
    {
        if (arguments is not null
            && arguments.TryGetValue(name, out var element)
            && element.ValueKind == JsonValueKind.String)
        {
            return element.GetString()!;
        }

        return defaultValue;
    }
}
