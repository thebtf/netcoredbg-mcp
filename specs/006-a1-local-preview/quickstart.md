# Quickstart: A1 Local Stateless Preview

This is a future local source-run validation guide. It does not build a release
asset, invoke a workflow, publish a package, create a tag, or contact a release
service.

## Prerequisites

1. Build the shared core, compatibility host, preview process, and their focused
   tests as specified by the eventual tasks.
2. Create the deterministic `PreviewSearchApp` fixture root plus contained and
   escaping-link denial fixtures.
3. Keep the installed Python consumer command available for rollback proof.

## Local positive journey

1. Start the preview with exactly `--project <fixture-root>`.
2. Connect a local stdio modern client with all three required metadata fields.
3. Call `server/discover`, `tools/list`, and `find_code_symbol` for a contained
   fixture symbol.
4. Assert exactly one tool, root-relative ordered result, text/structured parity,
   cache metadata, and protocol-only stdout.
5. Close stdin and assert bounded process completion.

## Local denial journey

Exercise invalid root startup, alternate authority inputs, reparse/outside-root
entry, malformed arguments, unreadable/injected I/O, each budget boundary,
unsupported version, excluded tool/method, cancellation, and EOF. Assert only
the contract’s named launch/error outcome, no partial result, and no secret/root
content.

## Compatibility and rollback

Run each existing legacy/Python parity owner separately. Each command MUST exit
zero with the stated result; do not replace these selectors with a broad suite.

| Exact selector | Command | Expected result |
|---|---|---|
| `tests/test_host_proxy.py::test_host_native_code_search_has_exact_python_catalog_and_call_parity` | `uv run --no-sync pytest -q tests/test_host_proxy.py::test_host_native_code_search_has_exact_python_catalog_and_call_parity` | `1 passed`; host catalog and native call serialization equal direct Python. |
| `tests/test_host_proxy.py::test_host_publicly_owns_only_native_code_search_calls_and_retains_python_rollback` | `uv run --no-sync pytest -q tests/test_host_proxy.py::test_host_publicly_owns_only_native_code_search_calls_and_retains_python_rollback` | `1 passed`; native calls stay host-owned while `search_source` and `relay_probe` remain Python-owned. |
| `tests/test_host_proxy.py::test_host_native_code_search_resolves_operator_client_and_cwd_roots_like_python` | `uv run --no-sync pytest -q tests/test_host_proxy.py::test_host_native_code_search_resolves_operator_client_and_cwd_roots_like_python` | `1 passed`; operator, client, explicit, CWD, and fallback root precedence equals Python. |
| `tests/test_host_proxy.py::test_host_native_code_search_preserves_python_order_ignore_and_symlink_boundary` | `uv run --no-sync pytest -q tests/test_host_proxy.py::test_host_native_code_search_preserves_python_order_ignore_and_symlink_boundary` | `1 passed`; ordering, ignore, and linked-file boundary equal Python. A legal platform skip is permitted only when it cannot create the file symlink. |
| `tests/test_host_proxy.py::test_host_forwarded_search_timeout_is_structured_and_session_stays_usable` | `uv run --no-sync pytest -q tests/test_host_proxy.py::test_host_forwarded_search_timeout_is_structured_and_session_stays_usable` | `1 passed`; Python-owned `search_source` returns a structured timeout, then the same session's `find_code_symbol` follow-up succeeds with exact host/Python serialization parity. |

Then run the strict-core owners:

```powershell
dotnet test host/NetCoreDbg.Mcp.CodeSearch.Core.Tests/NetCoreDbg.Mcp.CodeSearch.Core.Tests.csproj -c Release --filter "FullyQualifiedName~PreviewSearchPolicyBoundaryTests|FullyQualifiedName~SymbolSearchEngineTests"
```

Expected result: all selected tests pass with a nonzero denominator.

### Reproducible retained-Python rollback — `PRODUCT_WORKS`

Preview selection is source-run only: remove or stop only the preview process or
local preview configuration. Do not change the installed Python package,
console-script selection, or consumer configuration. From the repository root,
run the complete retained-Python setup block in section 2 of
[`specs/001-mcp-stateless-strangler/quickstart.md`](../001-mcp-stateless-strangler/quickstart.md):
it builds `SmokeTestApp`, builds one wheel, installs that exact wheel into the
disposable `.agent/tmp/t001-retained-python` environment, and writes the
consumer driver. With the same environment and `NETCOREDBG_PATH` naming an
existing debugger, replay exactly:

```powershell
& $consumerPython .agent/tmp/t001-retained-python-consumer.py
```

Expected result: the installed public `netcoredbg-mcp --project-from-cwd`
consumer journey emits `product_works: true`, `denominator: "5/5"`,
`tool_count: 135`, and `stopped_at_entry: true`. This replay is the rollback
receipt; it proves the retained Python consumer route without package,
selector, or configuration reversal. After recording the result, run the
canonical cleanup in that same setup block.

No preview release or consumer distribution is part of this quickstart.
