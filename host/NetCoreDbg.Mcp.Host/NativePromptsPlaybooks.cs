namespace NetCoreDbg.Mcp.Host;

/// <summary>
/// The exception-specific investigation knowledge base and symptom-keyword mapping used by
/// <c>investigate</c>, ported verbatim from <c>src/netcoredbg_mcp/prompts.py</c>'s
/// <c>_EXCEPTION_PLAYBOOKS</c> and <c>_SYMPTOM_MAPPING</c>. See the remarks on
/// <see cref="NativePrompts"/> in <c>NativePromptsContent.cs</c> for why the playbook text
/// blocks below are flush-left.
/// </summary>
internal static partial class NativePrompts
{
    private static readonly string NullReferenceExceptionPlaybook = NormalizeSourceOwnedText("""
## NullReferenceException Investigation

This is the #1 .NET exception. Something is null that shouldn't be.

### Step 1: Quick context (one call)
```
get_exception_context()
```
This returns exception info + call stack + variables + recent output.

### Step 2: Deeper investigation (if needed)

Find the null:
```
get_scopes(frame_id=...)
get_variables(ref=...)    # scan locals for null values
```

### Step 3: Trace the null backwards
Look at the null variable. How was it assigned?
```
# Set breakpoint at the assignment point
add_breakpoint(file="...", line=<assignment_line>)
restart_debug(rebuild=False)
# When hit, inspect what the source returns
```

### Step 4: Common causes
- Database query returned no results (FirstOrDefault → null)
- Dependency injection not registered (service is null)
- JSON deserialization missing property (model field is null)
- UI element not found (FindName returns null)
- Race condition: value set after null check but before use

### Step 5: Verify fix
```
set_variable(ref=..., name="suspect", value="new object()")
continue_execution()
# If it works → confirms the null was the issue
```

""");

    private static readonly string InvalidOperationExceptionPlaybook = NormalizeSourceOwnedText("""
## InvalidOperationException Investigation

Something was called at the wrong time or in the wrong state.

### Step 1: Quick context (one call)
```
get_exception_context()
```
This returns exception info + call stack + variables + recent output.

### Step 2: Deeper investigation (if needed)

Read the message carefully — it usually tells you EXACTLY what's wrong:
- "Collection was modified; enumeration operation may not proceed"
- "Sequence contains no elements"
- "Cannot access a disposed object"

Inspect state:
```
get_variables(ref=...)
# Look for: disposed objects, empty collections, wrong phase of lifecycle
```

### Step 3: Common causes by message
- "Collection was modified" → iterating while adding/removing. Use .ToList() first.
- "Sequence contains no elements" → .First() on empty. Use .FirstOrDefault().
- "disposed object" → using a resource after its scope ended. Check using/IDisposable.
- "not on UI thread" → cross-thread UI access. Use Dispatcher.Invoke.

""");

    private static readonly string TaskCanceledExceptionPlaybook = NormalizeSourceOwnedText("""
## TaskCanceledException / OperationCanceledException Investigation

An async operation was cancelled — usually a timeout or explicit cancellation.

### Step 1: Quick context (one call)
```
get_exception_context()
```
This returns exception info + call stack + variables + recent output.

### Step 2: Deeper investigation (if needed)

Check cancellation source:
```
get_variables(ref=...)
# Look for: CancellationToken.IsCancellationRequested
# Look for: HttpClient.Timeout value
# Look for: Task.Delay with cancellation
# Look for: HttpClient calls, database queries, CancellationToken usage
```

### Step 3: Common causes
- HttpClient timeout (default 100s) — endpoint too slow or unreachable
- CancellationToken from request pipeline — user navigated away
- Task.WhenAny with timeout task winning — operation took too long
- Disposed HttpClient or DbContext cancelling pending operations

""");

    private static readonly string ObjectDisposedExceptionPlaybook = NormalizeSourceOwnedText("""
## ObjectDisposedException Investigation

Using a resource after it was disposed.

### Step 1: Quick context (one call)
```
get_exception_context()
```
This returns exception info + call stack + variables + recent output.

### Step 2: Deeper investigation (if needed)

Find where it was disposed:
```
add_function_breakpoint(function_name="Dispose")
restart_debug(rebuild=False)
# When Dispose hits, check call stack — who disposed it and when
```

### Step 3: Common causes
- DbContext in async/closure: `using var db = ...; Task.Run(() => db.Query())`
- HttpClient disposed by DI container while request in flight
- Timer callback accessing disposed resources
- WPF binding accessing disposed ViewModel

""");

    private static readonly string DeadlockPlaybook = NormalizeSourceOwnedText("""
## Deadlock Investigation

App stops responding. No exception. UI frozen. No crash.

### Step 1: Pause and inspect all threads
```
pause_execution()
get_threads()
# For each thread:
get_call_stack(thread_id=<each>)
```

### Step 2: Look for the pattern
- Thread A waiting on Thread B (lock, Monitor.Enter, SemaphoreSlim)
- Thread B waiting on Thread A
- OR: Task waiting for UI thread (.Result or .Wait() in WPF)

### Step 3: Classic WPF/Avalonia deadlock
```csharp
// DEADLOCK: .Result blocks UI thread, task needs UI thread to complete
public void OnClick() {
    var result = GetDataAsync().Result;  // BLOCKS UI THREAD
}
```
Fix: `async void OnClick() { var result = await GetDataAsync(); }`

### Step 4: Verify
```
restart_debug()
# Reproduce the scenario
# If app responds now → deadlock was the issue
```

""");

    private static readonly string CrashPlaybook = NormalizeSourceOwnedText("""
## App Crash Investigation

App terminates unexpectedly.

### Step 1: Catch everything
```
configure_exceptions(filters=["all"])
start_debug(program="...", build_project="...")
```

### Step 2: Reproduce the crash
If GUI app: interact with UI to trigger crash path.
If console: let it run — exception will be caught.

### Step 3: When exception hits
```
get_exception_context()
```
This returns exception info + call stack + variables + recent output in ONE call.

### Step 4: Deeper investigation (if needed)
```
get_variables(ref=...)    # specific scope inspection
get_output_tail(lines=50) # last log messages before crash
```

### Step 5: If no exception caught
App may crash in native code (access violation, stack overflow).
```
get_output()  # check for native crash messages
# Look for: "Process terminated with exit code -1073741819" (access violation)
# Look for: "Stack overflow" in output
```

""");

    private static readonly string PerformancePlaybook = NormalizeSourceOwnedText("""
## Performance Issue Investigation

App is slow, laggy, or uses too much CPU/memory.

### Step 1: Identify the slow operation
```
start_debug(program="...", build_project="...")
# Set breakpoint BEFORE the slow operation
add_breakpoint(file="...", line=<before_slow_code>)
```

### Step 2: Step through and time
```
# When breakpoint hits:
step_over()  # one line at a time
# Watch: which step_over takes noticeably longer to return?
# That's your bottleneck.
```

### Step 3: Inspect the bottleneck
```
get_variables(ref=...)
evaluate_expression("collection.Count")  # large collection?
evaluate_expression("query.ToQueryString()")  # N+1 query?
```

### Step 4: Common causes
- N+1 database queries (loop calling DB per item)
- Synchronous I/O on UI thread
- Large collection iteration without pagination
- Unnecessary re-rendering in MVVM (property changed spam)

""");

    /// <summary>
    /// Playbook key -> playbook text. Lookup only (never enumerated for ordering), so a plain
    /// dictionary is fine here even though .NET does not contractually guarantee
    /// <see cref="Dictionary{TKey,TValue}"/> enumeration order.
    /// </summary>
    private static readonly Dictionary<string, string> ExceptionPlaybooks = new()
    {
        ["nullreferenceexception"] = NullReferenceExceptionPlaybook,
        ["invalidoperationexception"] = InvalidOperationExceptionPlaybook,
        ["taskcanceledexception"] = TaskCanceledExceptionPlaybook,
        ["objectdisposedexception"] = ObjectDisposedExceptionPlaybook,
        ["deadlock"] = DeadlockPlaybook,
        ["crash"] = CrashPlaybook,
        ["performance"] = PerformancePlaybook,
    };

    /// <summary>
    /// Symptom keyword -&gt; playbook key, in the EXACT order Python's <c>_SYMPTOM_MAPPING</c>
    /// dict declares them. Order is load-bearing: <see cref="BuildInvestigationPlan"/> matches
    /// the FIRST keyword that is a substring of the (lowercased, trimmed) symptom text, exactly
    /// like Python's <c>for keyword, key in _SYMPTOM_MAPPING.items(): if keyword in symptom_lower</c>
    /// loop. An ordinary <see cref="Dictionary{TKey,TValue}"/> does not contractually guarantee
    /// enumeration order, so this uses an explicit ordered array instead.
    /// </summary>
    private static readonly (string Keyword, string PlaybookKey)[] SymptomMapping =
    {
        ("null", "nullreferenceexception"),
        ("nullreference", "nullreferenceexception"),
        ("nullreferenceexception", "nullreferenceexception"),
        ("object reference not set", "nullreferenceexception"),
        ("invalidoperation", "invalidoperationexception"),
        ("invalidoperationexception", "invalidoperationexception"),
        ("collection was modified", "invalidoperationexception"),
        ("sequence contains no elements", "invalidoperationexception"),
        ("disposed", "objectdisposedexception"),
        ("objectdisposed", "objectdisposedexception"),
        ("objectdisposedexception", "objectdisposedexception"),
        ("cancel", "taskcanceledexception"),
        ("timeout", "taskcanceledexception"),
        ("taskcanceled", "taskcanceledexception"),
        ("operationcanceled", "taskcanceledexception"),
        ("deadlock", "deadlock"),
        ("freeze", "deadlock"),
        ("hang", "deadlock"),
        ("not responding", "deadlock"),
        ("crash", "crash"),
        ("terminated", "crash"),
        ("exit code", "crash"),
        ("access violation", "crash"),
        ("slow", "performance"),
        ("performance", "performance"),
        ("lag", "performance"),
        ("high cpu", "performance"),
        ("memory", "performance"),
        ("argumentnull", "nullreferenceexception"),
        ("argumentnullexception", "nullreferenceexception"),
        ("filenotfound", "crash"),
        ("directorynotfound", "crash"),
        ("ioexception", "crash"),
        ("stackoverflow", "crash"),
        ("stackoverflowexception", "crash"),
        ("httprequest", "taskcanceledexception"),
        ("httprequestexception", "taskcanceledexception"),
        ("network", "taskcanceledexception"),
        ("connection refused", "taskcanceledexception"),
        ("json", "invalidoperationexception"),
        ("jsonexception", "invalidoperationexception"),
        ("deserialization", "invalidoperationexception"),
        ("format", "invalidoperationexception"),
        ("formatexception", "invalidoperationexception"),
        ("parse error", "invalidoperationexception"),
        ("sqlexception", "invalidoperationexception"),
        ("database", "invalidoperationexception"),
        ("dbupdate", "invalidoperationexception"),
    };
}
