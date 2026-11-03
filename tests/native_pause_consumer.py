"""Real source-native pause proof; builds nothing and never replays an uncertain pause.

From this candidate's repository root, build both source artifacts:
    dotnet build host/NetCoreDbg.Mcp.Stateless/NetCoreDbg.Mcp.Stateless.csproj -c Debug
    dotnet build tests/fixtures/NativePauseApp/NativePauseApp.csproj -c Debug
Then, with the project's MCP/psutil dependencies and real netcoredbg available
through --netcoredbg, NETCOREDBG_PATH or PATH, run:
    python tests/native_pause_consumer.py
This proves source-built behavior only, not installed delivery.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import time
import traceback
from datetime import timedelta
from pathlib import Path
from typing import Any

import psutil
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
METADATA = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "native-pause-consumer", "version": "1"},
    "io.modelcontextprotocol/clientCapabilities": {},
}
REQUIRED_TOOLS = {
    "start_debug",
    "stop_debug",
    "get_debug_state",
    "get_call_stack",
    "get_threads",
    "get_output",
    "remove_breakpoint",
    "continue_execution",
    "pause_execution",
}
STATE_TIMEOUT = 15.0
RPC_TIMEOUT = 35.0
SCENARIO_TIMEOUT = 150.0


def emit(event: str, **data: Any) -> None:
    print(json.dumps({"event": event, "monotonic": time.monotonic(), **data}), flush=True)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(
        str(Path(right).resolve())
    )


def prerequisites(
    configuration: str, debugger_argument: str | None
) -> tuple[Path, Path, Path, Path, int]:
    host_name = "NetCoreDbg.Mcp.Stateless" + (".exe" if os.name == "nt" else "")
    host = ROOT / "host/NetCoreDbg.Mcp.Stateless/bin" / configuration / "net8.0" / host_name
    fixture = ROOT / "tests/fixtures/NativePauseApp"
    program = fixture / "bin" / configuration / "net8.0/NativePauseApp.dll"
    source = fixture / "Program.cs"
    host_dll = host.parent / "NetCoreDbg.Mcp.Stateless.dll"
    for path in (
        host,
        host_dll,
        host_dll.with_suffix(".runtimeconfig.json"),
        program,
        program.with_suffix(".pdb"),
        program.with_suffix(".runtimeconfig.json"),
        source,
    ):
        require(path.is_file(), f"Missing prebuilt prerequisite: {path}")
    configured = debugger_argument or os.environ.get("NETCOREDBG_PATH") or "netcoredbg"
    local = (ROOT / configured).resolve()
    found = str(local) if local.is_file() else shutil.which(configured)
    if found is None:
        raise AssertionError(
            "Real netcoredbg must resolve via --netcoredbg, NETCOREDBG_PATH or PATH"
        )
    debugger = Path(found).resolve()
    require(debugger.is_file(), f"Debugger is not a file: {debugger}")
    lines = source.read_text(encoding="utf-8-sig").splitlines()
    matches = [index for index, line in enumerate(lines, 1) if "// NATIVE_PAUSE_BREAKPOINT" in line]
    require(len(matches) == 1, "Fixture must contain exactly one breakpoint marker")
    require(
        'Console.WriteLine("NATIVE_PAUSE_READY");' in lines[matches[0] - 1],
        "Breakpoint marker must identify the ready statement",
    )
    return host.resolve(), program.resolve(), source.resolve(), debugger, matches[0]


class OwnedProcesses:
    """Observe immediate parent links, then pin exact PID/birth identities.

    Fresh repeated psutil reads are not an atomic OS ownership primitive. They
    reject observed reuse/reparenting; unobserved changes between reads remain
    possible. Never infer ownership from recursive enumeration or a name.
    """

    MAX_PROCESSES = 64

    def __init__(self, host: Path, debugger: Path, program: Path) -> None:
        self.host_path = host
        self.debugger_path = debugger
        self.program_path = program
        self.processes: dict[tuple[int, float], dict[str, Any]] = {}
        client = psutil.Process()
        self.client_identity = (client.pid, client.create_time())
        self.before = {(p.pid, p.create_time()) for p in client.children(recursive=False)}
        self.spawn_started = time.time()
        self.finished = asyncio.Event()
        self.watcher: asyncio.Task[None] | None = None
        self.ownership_errors: list[dict[str, str]] = []

    @staticmethod
    def live(identity: tuple[int, float]) -> psutil.Process | None:
        try:
            process = psutil.Process(identity[0])
            if process.create_time() != identity[1] or process.status() == psutil.STATUS_ZOMBIE:
                return None
            if not process.is_running():
                return None
            return process
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return None

    def reject(self, pid: int, parent: tuple[int, float], error: Exception) -> None:
        record = {"phase": f"ownership PID {pid} parent {parent}", "exception": repr(error)}
        if record not in self.ownership_errors:
            self.ownership_errors.append(record)
            emit(
                "ownership_rejected",
                pid=pid,
                expected_parent=parent,
                exception_type=type(error).__name__,
                exception=repr(error),
            )

    def linked(self, parent_id: tuple[int, float], child_id: tuple[int, float]) -> psutil.Process:
        """Re-read both identities and the child's immediate PPID, not a cached tree."""
        if child_id == parent_id or child_id[1] < parent_id[1]:
            raise ValueError("Candidate cannot be a child of this parent identity")
        parent = self.live(parent_id)
        child = self.live(child_id)
        if parent is None or child is None:
            raise ValueError("Parent or child identity changed during ownership proof")
        ppid = child.ppid()
        if ppid != parent_id[0]:
            raise ValueError("Candidate's immediate PPID does not match the pinned parent")
        actual_parent = psutil.Process(ppid)
        if actual_parent.create_time() != parent_id[1] or not actual_parent.is_running():
            raise ValueError("Immediate parent PID now identifies another process")
        # Reconstruct Process objects: create_time on an existing object is
        # cached by psutil and cannot prove that its PID has not been reused.
        parent = self.live(parent_id)
        child = self.live(child_id)
        if parent is None or child is None or child.ppid() != parent_id[0]:
            raise ValueError("Parent-child link changed during ownership proof")
        return child

    def ancestry(self, identity: tuple[int, float]) -> None:
        seen: set[tuple[int, float]] = set()
        while identity != self.client_identity:
            if identity in seen or len(seen) >= self.MAX_PROCESSES:
                raise ValueError("Ownership ancestry is cyclic or exceeds its bound")
            seen.add(identity)
            row = self.processes.get(identity)
            if row is None:
                raise ValueError("Immediate ancestor was never admitted")
            parent_id = row["observed_parent"]
            self.linked(parent_id, identity)
            identity = parent_id
        if self.live(self.client_identity) is None:
            raise ValueError("The original consumer process identity is no longer live")

    def prove(self, parent_id: tuple[int, float], child_id: tuple[int, float]) -> psutil.Process:
        self.ancestry(parent_id)
        self.linked(parent_id, child_id)
        self.ancestry(parent_id)
        return self.linked(parent_id, child_id)

    def remember(self, process: psutil.Process, parent: tuple[int, float] | None) -> bool:
        parent_id = self.client_identity if parent is None else parent
        try:
            identity = (process.pid, process.create_time())
            if identity in self.processes:
                return True
            if len(self.processes) >= self.MAX_PROCESSES:
                raise ValueError("Owned process inventory exceeds its bound")
            current = self.prove(parent_id, identity)
            executable = current.exe()
            arguments = current.cmdline()
            if parent is None:
                if (
                    identity in self.before
                    or identity[1] < self.spawn_started - 2
                    or not same_path(executable, self.host_path)
                ):
                    raise ValueError("Host does not match this launch's fresh exact-path child")
                role = "host"
            elif same_path(executable, self.debugger_path):
                role = "debugger"
            elif same_path(
                executable, self.program_path.with_suffix(".exe" if os.name == "nt" else "")
            ) or any(same_path(argument, self.program_path) for argument in arguments[1:]):
                role = "debuggee"
            else:
                role = "owned_descendant"
            # Metadata reads also create a race window. Re-prove the same exact
            # child, immediate parent and pinned ancestry before admitting it.
            self.prove(parent_id, identity)
        except (
            psutil.NoSuchProcess,
            psutil.ZombieProcess,
            psutil.AccessDenied,
            OSError,
            ValueError,
        ) as error:
            self.reject(process.pid, parent_id, error)
            return False
        row = {
            "pid": identity[0],
            "create_time": identity[1],
            "role": role,
            "executable": executable,
            "arguments": arguments,
            "observed_parent": parent_id,
        }
        self.processes[identity] = row
        emit("owned_process", **row)
        return True

    def bind_host(self) -> None:
        client = self.live(self.client_identity)
        if client is None:
            raise AssertionError("The original consumer process is unavailable")
        candidates = []
        for process in client.children(recursive=False):
            try:
                identity = (process.pid, process.create_time())
                if (
                    identity not in self.before
                    and identity[1] >= self.spawn_started - 2
                    and same_path(process.exe(), self.host_path)
                ):
                    self.prove(self.client_identity, identity)
                    candidates.append(process)
            except (
                psutil.NoSuchProcess,
                psutil.ZombieProcess,
                psutil.AccessDenied,
                OSError,
                ValueError,
            ) as error:
                self.reject(process.pid, self.client_identity, error)
        require(
            len(candidates) == 1,
            f"Official stdio client must own one fresh exact-path host, found {len(candidates)}",
        )
        require(self.remember(candidates[0], None), "Host ancestry could not be revalidated")
        self.capture()
        self.watcher = asyncio.create_task(self.watch(), name="native-pause-owned-processes")

    def capture(self) -> None:
        queue = list(self.processes)
        visited: set[tuple[int, float]] = set()
        cursor = 0
        while cursor < len(queue):
            identity = queue[cursor]
            cursor += 1
            if identity in visited:
                continue
            visited.add(identity)
            parent = self.live(identity)
            if parent is None:
                continue
            try:
                self.ancestry(identity)
                children = parent.children(recursive=False)
                if len(children) > self.MAX_PROCESSES:
                    raise ValueError("Immediate-child candidate inventory exceeds its bound")
            except (
                psutil.NoSuchProcess,
                psutil.ZombieProcess,
                psutil.AccessDenied,
                OSError,
                ValueError,
            ) as error:
                self.reject(identity[0], identity, error)
                continue
            for process in children:
                if self.remember(process, identity):
                    child_id = (process.pid, process.create_time())
                    if child_id not in visited:
                        queue.append(child_id)

    async def watch(self) -> None:
        while not self.finished.is_set():
            self.capture()
            try:
                await asyncio.wait_for(self.finished.wait(), timeout=0.05)
            except asyncio.TimeoutError:
                pass

    def require_fixture_tree(self) -> None:
        self.capture()
        if self.watcher is not None and self.watcher.done():
            self.watcher.result()
        roles = {row["role"] for key, row in self.processes.items() if self.live(key) is not None}
        require(
            {"host", "debugger", "debuggee"} <= roles,
            f"Live exact-owned host/debugger/debuggee identities are not all observed: {roles}",
        )

    async def cleanup(self) -> dict[str, Any]:
        """Observe owner teardown without signalling any process by a sampled PID."""
        errors: list[dict[str, str]] = []
        try:
            self.capture()
        except Exception as error:
            errors.append({"phase": "capture", "exception": repr(error)})
        self.finished.set()
        if self.watcher is not None:
            _, pending = await asyncio.wait({self.watcher}, timeout=2)
            if pending:
                self.watcher.cancel()
                errors.append(
                    {"phase": "watcher", "exception": "Watcher did not settle within two seconds"}
                )
            else:
                try:
                    self.watcher.result()
                except Exception as error:
                    errors.append({"phase": "watcher", "exception": repr(error)})

        def survivors() -> list[tuple[tuple[int, float], psutil.Process]]:
            result = []
            for identity in self.processes:
                try:
                    process = self.live(identity)
                    if process is not None:
                        result.append((identity, process))
                except Exception as error:
                    errors.append({"phase": f"identity {identity}", "exception": repr(error)})
            return result

        alive = survivors()
        if alive:
            try:
                await asyncio.to_thread(psutil.wait_procs, [p for _, p in alive], timeout=3)
            except Exception as error:
                errors.append({"phase": "normal_exit_wait", "exception": repr(error)})
        remaining = [self.processes[key] for key, _ in survivors()]
        for row in remaining:
            errors.append(
                {
                    "phase": f"surviving owned identity {(row['pid'], row['create_time'])}",
                    "exception": (
                        "Process remains alive after owner teardown; "
                        "PID-based termination is forbidden"
                    ),
                }
            )
            emit("cleanup_survivor", **row)
        errors.extend(self.ownership_errors)
        result = {
            "captured": list(self.processes.values()),
            "remaining": remaining,
            "errors": errors,
        }
        emit("cleanup", **result)
        return result


class Consumer:
    def __init__(self, client: ClientSession) -> None:
        self.client = client
        self.session_id: str | None = None
        self.continuations: list[asyncio.Task[dict[str, Any]]] = []
        self.calls = 0

    async def call(
        self, name: str, arguments: dict[str, Any], timeout: float = RPC_TIMEOUT
    ) -> dict[str, Any]:
        self.calls += 1
        call_id = self.calls
        emit("request", call_id=call_id, tool=name, arguments=arguments)
        request = types.CallToolRequest(
            params=types.CallToolRequestParams(
                name=name,
                arguments=arguments,
                _meta=types.RequestParams.Meta(**METADATA),
            )
        )
        try:
            response = await asyncio.wait_for(
                self.client.send_request(types.ClientRequest(request), types.CallToolResult),
                timeout,
            )
        except BaseException as error:
            emit(
                "request_exception",
                call_id=call_id,
                tool=name,
                exception_type=type(error).__name__,
                exception=repr(error),
            )
            raise
        raw = response.model_dump(mode="json", by_alias=True)
        emit("response", call_id=call_id, tool=name, result=raw)
        require(raw.get("resultType") == "complete", f"{name}: result is not complete: {raw}")
        require(raw.get("isError", False) is False, f"{name}: exact error result: {raw}")
        payload = raw.get("structuredContent")
        if not isinstance(payload, dict):
            raise AssertionError(f"{name}: structured content missing: {raw}")
        content = raw.get("content", [])
        require(
            len(content) == 1 and content[0].get("type") == "text",
            f"{name}: expected one JSON text mirror: {raw}",
        )
        require(
            json.loads(content[0]["text"]) == payload,
            f"{name}: text/structured mirror differs: {raw}",
        )
        return payload

    def arguments(self, **extra: Any) -> dict[str, Any]:
        return {"debugSessionId": self.session_id, **extra}

    async def state(
        self, event: str, continuation: asyncio.Task[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + STATE_TIMEOUT
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            require(remaining > 0, f"Deadline waiting for independently observed {event}")
            data = await self.call("get_debug_state", self.arguments(), timeout=min(5, remaining))
            state = data.get("state")
            if data.get("kind") != "debug_state_success" or not isinstance(state, dict):
                raise AssertionError(f"Unexpected debug state response: {data}")
            require(
                state.get("event") not in ("exited", "terminated"),
                f"Unexpected terminal event: {state}",
            )
            if event == "continued" and continuation is not None and continuation.done():
                outcome = continuation.result()
                raise AssertionError(f"Continue settled before the running barrier: {outcome}")
            if state.get("event") == event:
                emit("state_barrier", expected=event, state=state)
                return state
            await asyncio.sleep(min(0.05, max(0, deadline - asyncio.get_running_loop().time())))

    async def stack(
        self, state: dict[str, Any], source: Path, breakpoint_line: int | None = None
    ) -> None:
        thread_id = state.get("currentThreadId")
        require(
            type(thread_id) is int, f"Stopped reader state omitted its actual thread ID: {state}"
        )
        result = await self.call("get_call_stack", self.arguments(threadId=thread_id, levels=32))
        frames = result.get("frames", [])
        require(
            result.get("kind") == "call_stack_success" and bool(frames),
            f"Managed stack missing: {result}",
        )
        if breakpoint_line is not None:
            first = frames[0]
            require(
                "NativePauseApp.Program.Main" in first.get("name", "")
                and first.get("line") == breakpoint_line
                and same_path(first.get("source") or "", source),
                f"Breakpoint did not hit the exact source statement: {result}",
            )
        else:
            require(
                any(
                    "NativePauseApp.Program.RunLoop" in frame.get("name", "")
                    and type(frame.get("line")) is int
                    and frame["line"] > 0
                    and same_path(frame.get("source") or "", source)
                    for frame in frames
                ),
                f"Pause did not expose the real fixture's managed loop: {result}",
            )
        emit("managed_stack_barrier", thread_id=thread_id, breakpoint_line=breakpoint_line)

    async def heartbeat(self) -> None:
        deadline = asyncio.get_running_loop().time() + 10
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            require(remaining > 0, "Running loop did not produce its real heartbeat")
            output = await self.call("get_output", self.arguments(), timeout=min(5, remaining))
            require(output.get("kind") == "output_success", f"Unexpected output: {output}")
            if "NATIVE_PAUSE_HEARTBEAT iteration=" in output.get("output", ""):
                emit("heartbeat_barrier")
                return
            await asyncio.sleep(min(0.05, max(0, deadline - asyncio.get_running_loop().time())))

    async def pause_cycle(
        self, state: dict[str, Any], source: Path, explicit: bool
    ) -> dict[str, Any]:
        thread_id = state["currentThreadId"]
        continuation = asyncio.create_task(
            self.call(
                "continue_execution",
                self.arguments(threadId=thread_id),
                timeout=40,
            ),
            name=f"continue-before-{'explicit' if explicit else 'omitted'}-pause",
        )
        self.continuations.append(continuation)
        await self.state("continued", continuation)
        if not explicit:
            await self.heartbeat()
        arguments = self.arguments()
        if explicit:
            observed = await self.call("get_threads", self.arguments())
            threads = observed.get("threads", [])
            require(
                observed.get("kind") == "threads_success"
                and any(
                    type(item.get("id")) is int and item["id"] == thread_id for item in threads
                ),
                f"The previously stopped thread was not independently observed live: {observed}",
            )
            arguments["threadId"] = thread_id
            emit("explicit_thread_barrier", thread_id=thread_id, observation=observed)
        require(not continuation.done(), "Pause must overlap an unresolved continue request")
        emit("continue_pending_barrier", explicit_thread=explicit)
        # Exactly one call per cycle. Every error/timeout/cancellation exits to cleanup.
        pause = await self.call("pause_execution", arguments)
        require(
            pause.get("kind") == "pause_acknowledged" and type(pause.get("threadId")) is int,
            f"Pause did not acknowledge one selected thread: {pause}",
        )
        if explicit:
            require(
                pause["threadId"] == thread_id,
                f"Pause selected a different explicit thread: {pause}",
            )
        # ACK is deliberately not an input to the stopped-state or frame proof.
        stopped = await self.state("stopped")
        require(stopped.get("stopReason") == "pause", f"Expected a real pause stop, got: {stopped}")
        await self.stack(stopped, source)
        continued = await asyncio.wait_for(asyncio.shield(continuation), timeout=5)
        require(
            continued.get("kind") == "continue_stopped"
            and continued.get("reason") == stopped["stopReason"],
            f"Continue did not independently observe this pause stop: {continued}",
        )
        if continued.get("threadId") is not None:
            require(
                continued["threadId"] == stopped["currentThreadId"],
                "Continue and reader state disagree on observed stop thread: "
                f"{continued}, {stopped}",
            )
        emit(
            "pause_cycle_complete",
            explicit_thread=explicit,
            stopped=stopped,
            continuation=continued,
        )
        return stopped

    async def stop(self) -> None:
        try:
            if self.session_id is not None:
                result = await self.call("stop_debug", self.arguments(), timeout=10)
                require(
                    result.get("kind") == "stop_debug_success", f"Explicit stop failed: {result}"
                )
        finally:
            if self.continuations:
                _, pending = await asyncio.wait(self.continuations, timeout=5)
                for task in pending:
                    task.cancel()
                if pending:
                    _, pending = await asyncio.wait(pending, timeout=2)
                for index, task in enumerate(self.continuations):
                    if not task.done():
                        emit(
                            "continuation_teardown",
                            index=index,
                            outcome="unsettled_after_cancel_deadline",
                        )
                        continue
                    try:
                        result = task.result()
                    except BaseException as error:
                        emit("continuation_teardown", index=index, exception=repr(error))
                    else:
                        emit("continuation_teardown", index=index, outcome=result)
                require(not pending, "Owned continuation did not settle within cleanup deadlines")


async def scenario(
    host: Path,
    program: Path,
    source: Path,
    debugger: Path,
    breakpoint_line: int,
    owned: OwnedProcesses,
) -> None:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("NETCOREDBG_TEST_")
    }
    environment.update(
        {
            "NETCOREDBG_PATH": str(debugger),
            "DOTNET_ADD_GLOBAL_TOOLS_TO_PATH": "false",
            "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        }
    )
    parameters = StdioServerParameters(command=str(host), cwd=str(ROOT), env=environment)
    async with stdio_client(parameters) as (read_stream, write_stream):
        owned.bind_host()
        async with ClientSession(
            read_stream, write_stream, read_timeout_seconds=timedelta(seconds=45)
        ) as client:
            # This stateless source route uses request-local metadata, not legacy initialize.
            consumer = Consumer(client)
            try:
                request = types.ListToolsRequest(
                    params=types.PaginatedRequestParams(
                        _meta=types.RequestParams.Meta(**METADATA),
                    )
                )
                catalog = await asyncio.wait_for(
                    client.send_request(
                        types.ClientRequest(request),
                        types.ListToolsResult,
                    ),
                    timeout=10,
                )
                emit("catalog", result=catalog.model_dump(mode="json", by_alias=True))
                require(not catalog.nextCursor, "Native catalog unexpectedly requires pagination")
                names = [tool.name for tool in catalog.tools]
                require(
                    all(names.count(name) == 1 for name in REQUIRED_TOOLS),
                    "Native catalog missing/duplicating required tools: "
                    f"{sorted(REQUIRED_TOOLS - set(names))}",
                )
                started = await consumer.call(
                    "start_debug",
                    {
                        "program": str(program),
                        "breakpoints": [{"file": str(source), "line": breakpoint_line}],
                    },
                )
                token = started.get("debugSessionId")
                require(
                    started.get("kind") == "start_debug_success"
                    and isinstance(token, str)
                    and bool(token.strip()),
                    f"Start failed: {started}",
                )
                consumer.session_id = token
                stopped = await consumer.state("stopped")
                require(
                    stopped.get("stopReason") == "breakpoint",
                    f"Initial stop is not a breakpoint: {stopped}",
                )
                await consumer.stack(stopped, source, breakpoint_line)
                owned.require_fixture_tree()
                removed = await consumer.call(
                    "remove_breakpoint",
                    consumer.arguments(
                        file=str(source),
                        line=breakpoint_line,
                    ),
                )
                require(
                    removed.get("kind") == "breakpoint_removed" and removed.get("removed") is True,
                    f"Breakpoint removal was not acknowledged: {removed}",
                )
                stopped = await consumer.pause_cycle(stopped, source, explicit=False)
                owned.require_fixture_tree()
                await consumer.pause_cycle(stopped, source, explicit=True)
                owned.require_fixture_tree()
                emit("behavior_complete", pause_calls=2)
            finally:
                await consumer.stop()


async def main(arguments: argparse.Namespace) -> int:
    owned: OwnedProcesses | None = None
    status = "FAIL"
    try:
        host, program, source, debugger, line = prerequisites(
            arguments.configuration, arguments.netcoredbg
        )
        emit(
            "prerequisites",
            host=str(host),
            program=str(program),
            source=str(source),
            debugger=str(debugger),
            breakpoint_line=line,
            delivery_scope="source-built; not installed delivery",
            sha256={
                str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (
                    host,
                    host.parent / "NetCoreDbg.Mcp.Stateless.dll",
                    program,
                    program.with_suffix(".pdb"),
                    source,
                )
            },
        )
        owned = OwnedProcesses(host, debugger, program)
        await asyncio.wait_for(
            scenario(host, program, source, debugger, line, owned), SCENARIO_TIMEOUT
        )
        status = "PASS"
    except Exception as error:
        emit(
            "failure",
            exception_type=type(error).__name__,
            exception=repr(error),
            traceback=traceback.format_exc(),
        )
    finally:
        if owned is not None:
            try:
                cleanup = await owned.cleanup()
                if cleanup["remaining"] or cleanup["errors"]:
                    status = "FAIL"
            except Exception as error:
                status = "FAIL"
                emit("cleanup_failure", exception=repr(error), traceback=traceback.format_exc())
        emit("summary", status=status)
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configuration", choices=("Debug", "Release"), default="Debug")
    parser.add_argument(
        "--netcoredbg", help="Real netcoredbg executable; otherwise use NETCOREDBG_PATH or PATH"
    )
    sys.exit(asyncio.run(main(parser.parse_args())))
