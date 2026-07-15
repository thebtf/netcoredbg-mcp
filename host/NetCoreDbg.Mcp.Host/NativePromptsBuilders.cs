namespace NetCoreDbg.Mcp.Host;

/// <summary>
/// Ports of <c>_build_investigation_plan</c> and <c>_build_scenario_plan</c> from
/// <c>src/netcoredbg_mcp/prompts.py</c>, used to render the <c>investigate</c> and
/// <c>debug-scenario</c> prompts. Both builders reproduce their Python counterparts'
/// exact string-assembly mechanics (not just their final text) because the mechanics
/// are what determines blank-line placement around the conditional segments:
/// <list type="bullet">
/// <item>
/// <see cref="BuildGenericInvestigationPlan"/> mirrors an f-string with two conditional
/// placeholders sitting on their own template line; a placeholder that evaluates to an
/// empty string still occupies that line, producing an extra blank line - it is NOT
/// omitted as a whole line the way an <c>if</c> around a <c>steps.append(...)</c> call
/// would omit one.
/// </item>
/// <item>
/// <see cref="BuildScenarioPlan"/> mirrors Python's <c>steps: list[str]</c> +
/// <c>"\n".join(steps)</c> construction with a real <see cref="List{T}"/> and
/// <see cref="string.Join(string, IEnumerable{string})"/>: several entries already carry
/// their own trailing <c>\n</c>, so the single join separator plus that trailing
/// newline is what produces each section's blank line.
/// </item>
/// </list>
/// </summary>
internal static partial class NativePrompts
{
    /// <summary>Ported from Python's <c>_build_investigation_plan</c>.</summary>
    private static string BuildInvestigationPlan(string symptom, string appType)
    {
        var symptomLower = symptom.ToLowerInvariant().Trim();

        string? playbookKey = null;
        foreach (var (keyword, key) in SymptomMapping)
        {
            if (symptomLower.Contains(keyword, StringComparison.Ordinal))
            {
                playbookKey = key;
                break;
            }
        }

        if (playbookKey is not null && ExceptionPlaybooks.TryGetValue(playbookKey, out var playbook))
        {
            var header = $"# Investigation Plan: {symptom}\n\nApp type: {appType}\n\n";
            if (appType == "gui")
            {
                header +=
                    "**GUI app reminder:** App UI is frozen while stopped. " +
                    "Resume after inspecting. Set breakpoints only after window is visible.\n\n";
            }

            return header + playbook;
        }

        return BuildGenericInvestigationPlan(symptom, appType);
    }

    /// <summary>Ported from the generic fallback tail of Python's <c>_build_investigation_plan</c>.</summary>
    private static string BuildGenericInvestigationPlan(string symptom, string appType)
    {
        var waitForWindowLine = appType == "gui" ? "Wait for window: ui_get_window_tree()" : "";
        var triggerLine = appType == "gui"
            ? "Interact with UI to reproduce: ui_click, ui_send_keys, etc."
            : "Let the app run and reproduce the issue.";

        return
            $"# Investigation Plan: {symptom}\n" +
            "\n" +
            $"App type: {appType}\n" +
            "\n" +
            "No specific playbook for this symptom. Follow the general approach:\n" +
            "\n" +
            "## Step 1: Reproduce\n" +
            "```\n" +
            "start_debug(program=\"...\", build_project=\"...\")\n" +
            "configure_exceptions(filters=[\"all\"])\n" +
            "```\n" +
            waitForWindowLine + "\n" +
            "\n" +
            "## Step 2: Trigger the issue\n" +
            triggerLine + "\n" +
            "\n" +
            "## Step 3: When it stops (breakpoint or exception)\n" +
            "```\n" +
            "get_exception_info()      # if exception\n" +
            "get_call_stack()          # where\n" +
            "get_variables(ref=...)    # state\n" +
            "get_output_tail(lines=30) # recent output\n" +
            "```\n" +
            "\n" +
            "## Step 4: Narrow down\n" +
            "- If you know the file: add_breakpoint(file=\"...\", line=...)\n" +
            "- If you know the method: add_function_breakpoint(function_name=\"...\")\n" +
            "- If you need to see UI: ui_take_annotated_screenshot()\n" +
            "- If build warnings matter: get_build_diagnostics()\n" +
            "\n" +
            "## Step 5: Step through\n" +
            "```\n" +
            "step_over()   # follow the flow\n" +
            "step_into()   # enter suspicious functions\n" +
            "step_out()    # exit when you've seen enough\n" +
            "```\n";
    }

    /// <summary>Ported from Python's <c>_build_scenario_plan</c>.</summary>
    private static string BuildScenarioPlan(string problem, string appType, string fileHint)
    {
        var steps = new List<string> { $"# Debug Plan: {problem}\n" };

        steps.Add(appType == "gui"
            ? "**App type: GUI (WPF/Avalonia)** — breakpoints AFTER window loads.\n"
            : "**App type: Console** — breakpoints before or after launch.\n");

        steps.Add("## Step 1: Start debug session");
        steps.Add("```");
        steps.Add(
            "start_debug(program=\"bin/Debug/<framework>/App.dll\", build_project=\"App.csproj\", pre_build=True)");
        steps.Add("```\n");

        if (appType == "gui")
        {
            steps.Add("## Step 2: Wait for window");
            steps.Add("```");
            steps.Add("ui_get_window_tree()            # confirm loaded");
            steps.Add("ui_take_annotated_screenshot()   # see the UI");
            steps.Add("```\n");
        }

        steps.Add($"## Step {(appType == "gui" ? "3" : "2")}: Set breakpoints");
        if (!string.IsNullOrEmpty(fileHint))
        {
            steps.Add($"```\nadd_breakpoint(file=\"{fileHint}\", line=<suspected_line>)\n```\n");
        }
        else
        {
            steps.Add("```");
            steps.Add("# If you know the method name:");
            steps.Add("add_function_breakpoint(function_name=\"<MethodName>\")");
            steps.Add("# If you know the file and line:");
            steps.Add("add_breakpoint(file=\"<file.cs>\", line=<N>)");
            steps.Add("```\n");
        }

        int nextStep;
        if (appType == "gui")
        {
            steps.Add("## Step 4: Trigger the code path");
            steps.Add("```");
            steps.Add("ui_click(automation_id=\"<trigger_element>\")");
            steps.Add("# Or: ui_send_keys, ui_double_click, etc.");
            steps.Add("```\n");
            nextStep = 5;
        }
        else
        {
            steps.Add("## Step 3: Run to breakpoint");
            steps.Add("```\ncontinue_execution()\n```\n");
            nextStep = 4;
        }

        steps.Add($"## Step {nextStep}: Inspect state");
        steps.Add("```");
        steps.Add("get_call_stack()           # where are we?");
        steps.Add("get_scopes(frame_id=...)   # scope references");
        steps.Add("get_variables(ref=...)     # actual values");
        steps.Add("get_output_tail(lines=20)  # recent log output");
        steps.Add("```\n");

        steps.Add($"## Step {nextStep + 1}: Iterate");
        steps.Add("```");
        steps.Add("step_over()          # follow the flow");
        steps.Add("step_into()          # enter suspicious function");
        steps.Add("continue_execution() # jump to next breakpoint");
        steps.Add("```\n");

        steps.Add($"## Step {nextStep + 2}: Clean up");
        steps.Add("```");
        steps.Add("stop_debug()");
        steps.Add("```");

        return string.Join("\n", steps);
    }
}
