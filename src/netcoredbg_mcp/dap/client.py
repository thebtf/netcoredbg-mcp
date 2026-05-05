"""DAP Client - communicates with netcoredbg process."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable, Mapping
from typing import Any

from .protocol import (
    Commands,
    DAPEvent,
    DAPRequest,
    DAPResponse,
    parse_message,
)

logger = logging.getLogger(__name__)

# Limits for security
MAX_CONTENT_LENGTH = 10_000_000  # 10MB max DAP message size
REDACTED_ENV_VALUE = "<redacted>"


def format_request_arguments_for_log(
    command: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Return request arguments safe for debug logging."""

    if command != Commands.LAUNCH or "env" not in arguments:
        return dict(arguments)

    redacted = dict(arguments)
    env = arguments["env"]
    if isinstance(env, dict):
        redacted["env"] = f"<{len(env)} environment variables>"
    else:
        redacted["env"] = REDACTED_ENV_VALUE
    return redacted


def build_launch_environment(
    overrides: dict[str, str | None] | None = None,
) -> dict[str, str | None]:
    """Build the DAP launch environment from the server process environment."""

    if os.name == "nt":
        return build_windows_launch_environment(os.environ, overrides)

    launch_env: dict[str, str | None] = dict(os.environ)
    if overrides:
        launch_env.update(overrides)
    return launch_env


def build_windows_launch_environment(
    process_env: Mapping[str, str],
    overrides: Mapping[str, str | None] | None = None,
) -> dict[str, str | None]:
    """Build a Windows launch environment with case-insensitive override semantics."""

    launch_env: dict[str, str | None] = {
        name.upper(): value for name, value in process_env.items()
    }
    explicit_keys: set[str] = set()
    if overrides:
        normalized_overrides = {name.upper(): value for name, value in overrides.items()}
        explicit_keys = set(normalized_overrides)
        launch_env.update(normalized_overrides)

    sync_windows_environment_aliases(launch_env, explicit_keys)
    return launch_env


def sync_windows_environment_aliases(
    env: dict[str, str | None],
    explicit_keys: set[str],
) -> None:
    """Populate Windows aliases without overriding explicit caller values."""

    sync_windows_environment_alias_group(env, ("WINDIR", "SYSTEMROOT"), explicit_keys)


def sync_windows_environment_alias_group(
    env: dict[str, str | None],
    names: tuple[str, ...],
    explicit_keys: set[str],
) -> None:
    explicit_value = first_env_value(
        env,
        *(name for name in names if name in explicit_keys),
    )
    value = explicit_value or first_env_value(env, *names)
    if not value:
        return

    for name in names:
        if name not in explicit_keys:
            env[name] = value


def first_env_value(env: Mapping[str, str | None], *names: str) -> str | None:
    for name in names:
        value = env.get(name)
        if value:
            return value
    return None


class DAPClient:
    """Async DAP client for netcoredbg communication."""

    def __init__(self, netcoredbg_path: str | None = None):
        self.netcoredbg_path = netcoredbg_path or self._find_netcoredbg()
        self._seq = 0
        self._request_lock = asyncio.Lock()  # Protect sequence number
        self._pending: dict[int, asyncio.Future[DAPResponse]] = {}
        self._event_handlers: dict[str, list[Callable[[DAPEvent], None]]] = {}
        self._process: asyncio.subprocess.Process | None = None
        self._read_task: asyncio.Task | None = None
        self._buffer = b""
        self._capabilities: dict[str, Any] = {}

    @property
    def capabilities(self) -> dict[str, Any]:
        """Get the adapter capabilities from initialize response."""
        return dict(self._capabilities)

    def update_capabilities(
        self,
        capabilities: dict[str, Any],
    ) -> tuple[list[str], list[str], int, int]:
        """Shallow-merge a capabilities event delta into adapter capabilities."""
        current = dict(self._capabilities)
        before_keys = set(current)
        changed_keys = [
            key for key, value in capabilities.items() if current.get(key) != value
        ]
        merged = {**current, **capabilities}
        self._capabilities = merged
        after_keys = set(merged)
        return (
            sorted(after_keys - before_keys),
            sorted(changed_keys),
            len(before_keys),
            len(after_keys),
        )

    def _find_netcoredbg(self) -> str:
        """Find netcoredbg executable.

        Delegates to setup.netcoredbg.find_netcoredbg() which handles:
        NETCOREDBG_PATH → ~/.netcoredbg-mcp/netcoredbg/ → PATH → auto-download
        """
        from ..setup.netcoredbg import find_netcoredbg
        return find_netcoredbg()

    @property
    def is_running(self) -> bool:
        """Check if DAP client is connected."""
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        """Start netcoredbg process."""
        if self.is_running:
            return

        logger.info(f"Starting netcoredbg: {self.netcoredbg_path}")
        self._process = await asyncio.create_subprocess_exec(
            self.netcoredbg_path,
            "--interpreter=vscode",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._read_task = asyncio.create_task(self._read_loop())
        logger.info(f"netcoredbg started with PID {self._process.pid}")

    async def stop(self) -> None:
        """Stop netcoredbg process."""
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
            self._read_task = None

        if self._process:
            pid = self._process.pid
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(f"Process {pid} did not terminate, killing...")
                self._process.kill()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    logger.exception(f"Failed to kill process {pid}")
                    # Cancel pending requests even on timeout
                    for future in self._pending.values():
                        if not future.done():
                            future.cancel()
                    self._pending.clear()
                    return
            self._process = None

        # Cancel pending requests
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

        logger.info("netcoredbg stopped")

    def on_event(self, event_name: str, handler: Callable[[DAPEvent], None]) -> None:
        """Register event handler."""
        if event_name not in self._event_handlers:
            self._event_handlers[event_name] = []
        self._event_handlers[event_name].append(handler)

    def off_event(self, event_name: str, handler: Callable[[DAPEvent], None]) -> None:
        """Unregister event handler."""
        if event_name in self._event_handlers:
            try:
                self._event_handlers[event_name].remove(handler)
            except ValueError:
                pass  # Handler not registered

    async def send_request(
        self, command: str, arguments: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> DAPResponse:
        """Send DAP request and wait for response."""
        if not self.is_running:
            raise RuntimeError("DAP client not running")

        # Atomically increment seq and register future
        async with self._request_lock:
            self._seq += 1
            seq = self._seq
            future: asyncio.Future[DAPResponse] = asyncio.Future()
            self._pending[seq] = future

        request = DAPRequest(seq=seq, command=command, arguments=arguments or {})
        await self._send(request)

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(seq, None)
            raise TimeoutError(f"Request {command} timed out after {timeout}s") from None

    async def _send(self, request: DAPRequest) -> None:
        """Send request to netcoredbg."""
        if not self._process or not self._process.stdin:
            raise RuntimeError("Process not running")

        data = request.to_bytes()
        logger.debug(
            ">>> %s: %s",
            request.command,
            format_request_arguments_for_log(request.command, request.arguments),
        )
        self._process.stdin.write(data)
        await self._process.stdin.drain()

    async def _read_loop(self) -> None:
        """Read messages from netcoredbg."""
        assert self._process and self._process.stdout

        try:
            while True:
                try:
                    # Read header
                    header_line = await self._process.stdout.readline()
                    if not header_line:
                        logger.warning("netcoredbg stdout closed")
                        break

                    header = header_line.decode("utf-8").strip()
                    if not header.startswith("Content-Length:"):
                        continue

                    content_length = int(header.split(":")[1].strip())

                    # Validate Content-Length (security: prevent DoS)
                    if content_length < 0 or content_length > MAX_CONTENT_LENGTH:
                        logger.error(f"Invalid Content-Length: {content_length}")
                        raise ValueError(f"Invalid Content-Length: {content_length}")

                    # Read empty line
                    await self._process.stdout.readline()

                    # Read content
                    content = await self._process.stdout.readexactly(content_length)
                    data = json.loads(content.decode("utf-8"))

                    self._handle_message(data)

                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.exception("Error reading DAP message")
                    break
        finally:
            # Cancel all pending request futures immediately so callers don't
            # hang for 30s waiting for a response from a dead process.
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(
                        RuntimeError("netcoredbg process died — pending request cancelled")
                    )
            self._pending.clear()

            # Cleanup on read loop exit (handles zombie process)
            if self._process and self._process.returncode is None:
                logger.warning("Read loop exited, cleaning up process...")
                try:
                    self._process.terminate()
                except Exception:
                    logger.debug("Failed to terminate process during cleanup")

    def _handle_message(self, data: dict[str, Any]) -> None:
        """Handle incoming DAP message."""
        try:
            message = parse_message(data)

            if isinstance(message, DAPResponse):
                logger.debug(f"<<< Response {message.command}: success={message.success}")
                future = self._pending.pop(message.request_seq, None)
                if future and not future.done():
                    future.set_result(message)

            elif isinstance(message, DAPEvent):
                logger.debug(f"<<< Event {message.event}: {message.body}")
                handlers = self._event_handlers.get(message.event, [])
                if not handlers:
                    body_json = json.dumps(message.body, default=str)
                    logger.warning(
                        "Unhandled DAP event '%s' dropped: body_size=%d body_preview=%s",
                        message.event,
                        len(body_json.encode("utf-8")),
                        body_json[:200],
                    )
                for handler in handlers:
                    try:
                        handler(message)
                    except Exception:
                        logger.exception("Event handler error")

        except Exception:
            logger.exception(f"Error handling message, data: {data}")

    # High-level DAP commands

    async def initialize(self) -> dict[str, Any]:
        """Initialize DAP session."""
        response = await self.send_request(
            Commands.INITIALIZE,
            {
                "clientID": "netcoredbg-mcp",
                "clientName": "NetCoreDbg MCP Server",
                "adapterID": "coreclr",
                "pathFormat": "path",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "supportsVariableType": True,
                "supportsVariablePaging": False,
                "supportsRunInTerminalRequest": False,
                "supportsProgressReporting": True,
                "supportsMemoryReferences": True,
            },
        )
        if response.success:
            self._capabilities = response.body
        return self._capabilities

    async def launch(
        self,
        program: str,
        cwd: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str | None] | None = None,
        stop_at_entry: bool = False,
        just_my_code: bool = False,
    ) -> DAPResponse:
        """Launch program for debugging."""
        arguments = {
            "program": program,
            "cwd": cwd or os.path.dirname(program),
            "args": args or [],
            "env": build_launch_environment(env),
            "stopAtEntry": stop_at_entry,
            "justMyCode": just_my_code,
        }
        return await self.send_request(Commands.LAUNCH, arguments)

    async def attach(self, process_id: int, just_my_code: bool = False) -> DAPResponse:
        """Attach to running process."""
        return await self.send_request(
            Commands.ATTACH, {"processId": process_id, "justMyCode": just_my_code}
        )

    async def configuration_done(self) -> DAPResponse:
        """Signal that configuration is complete."""
        return await self.send_request(Commands.CONFIGURATION_DONE)

    async def disconnect(self, terminate: bool = True) -> DAPResponse:
        """Disconnect from debuggee."""
        return await self.send_request(
            Commands.DISCONNECT, {"terminateDebuggee": terminate}
        )

    async def set_breakpoints(
        self, source_path: str, breakpoints: list[dict[str, Any]]
    ) -> DAPResponse:
        """Set breakpoints in a source file."""
        return await self.send_request(
            Commands.SET_BREAKPOINTS,
            {
                "source": {"path": source_path},
                "breakpoints": breakpoints,
            },
        )

    async def set_function_breakpoints(
        self, breakpoints: list[dict[str, Any]]
    ) -> DAPResponse:
        """Set function breakpoints."""
        return await self.send_request(
            Commands.SET_FUNCTION_BREAKPOINTS,
            {"breakpoints": breakpoints},
        )

    async def set_variable(
        self, variables_reference: int, name: str, value: str
    ) -> DAPResponse:
        """Set a variable's value."""
        return await self.send_request(
            Commands.SET_VARIABLE,
            {
                "variablesReference": variables_reference,
                "name": name,
                "value": value,
            },
        )

    async def set_exception_breakpoints(
        self, filters: list[str] | None = None
    ) -> DAPResponse:
        """Set exception breakpoints."""
        return await self.send_request(
            Commands.SET_EXCEPTION_BREAKPOINTS,
            {"filters": filters or []},
        )

    async def continue_execution(self, thread_id: int) -> DAPResponse:
        """Continue execution."""
        return await self.send_request(Commands.CONTINUE, {"threadId": thread_id})

    async def step_over(self, thread_id: int) -> DAPResponse:
        """Step over (next line)."""
        return await self.send_request(Commands.NEXT, {"threadId": thread_id})

    async def step_in(self, thread_id: int, target_id: int | None = None) -> DAPResponse:
        """Step into function."""
        args: dict[str, Any] = {"threadId": thread_id}
        if target_id is not None:
            args["targetId"] = target_id
        return await self.send_request(Commands.STEP_IN, args)

    async def step_in_targets(self, frame_id: int) -> DAPResponse:
        """Get possible step-in targets for a frame."""
        return await self.send_request(
            Commands.STEP_IN_TARGETS, {"frameId": frame_id}
        )

    async def step_out(self, thread_id: int) -> DAPResponse:
        """Step out of function."""
        return await self.send_request(Commands.STEP_OUT, {"threadId": thread_id})

    async def pause(self, thread_id: int) -> DAPResponse:
        """Pause execution."""
        return await self.send_request(Commands.PAUSE, {"threadId": thread_id})

    async def terminate(self) -> DAPResponse:
        """Send terminate request for graceful shutdown."""
        return await self.send_request(Commands.TERMINATE)

    async def threads(self) -> DAPResponse:
        """Get all threads."""
        return await self.send_request(Commands.THREADS)

    async def stack_trace(
        self, thread_id: int, start_frame: int = 0, levels: int = 20
    ) -> DAPResponse:
        """Get stack trace for thread."""
        return await self.send_request(
            Commands.STACK_TRACE,
            {"threadId": thread_id, "startFrame": start_frame, "levels": levels},
        )

    async def scopes(self, frame_id: int) -> DAPResponse:
        """Get scopes for stack frame."""
        return await self.send_request(Commands.SCOPES, {"frameId": frame_id})

    async def variables(
        self,
        variables_reference: int,
        filter: str | None = None,
        start: int | None = None,
        count: int | None = None,
    ) -> DAPResponse:
        """Get variables for scope/variable."""
        args: dict[str, Any] = {"variablesReference": variables_reference}
        if filter is not None:
            args["filter"] = filter  # "indexed" or "named"
        if start is not None:
            args["start"] = start
        if count is not None:
            args["count"] = count
        return await self.send_request(Commands.VARIABLES, args)

    async def read_memory(
        self,
        memory_reference: str,
        offset: int = 0,
        count: int = 0,
    ) -> DAPResponse:
        """Read bytes from a memory reference."""
        return await self.send_request(
            Commands.READ_MEMORY,
            {
                "memoryReference": memory_reference,
                "offset": offset,
                "count": count,
            },
        )

    async def write_memory(
        self,
        memory_reference: str,
        data: str,
        offset: int = 0,
        allow_partial: bool = False,
    ) -> DAPResponse:
        """Write base64-encoded bytes to a memory reference."""
        return await self.send_request(
            Commands.WRITE_MEMORY,
            {
                "memoryReference": memory_reference,
                "offset": offset,
                "data": data,
                "allowPartial": allow_partial,
            },
        )

    async def loaded_sources(self) -> DAPResponse:
        """Get all sources currently loaded by the debugged process."""
        return await self.send_request(Commands.LOADED_SOURCES)

    async def disassemble(
        self,
        memory_reference: str,
        offset: int = 0,
        instruction_offset: int = 0,
        instruction_count: int = 64,
        resolve_symbols: bool = True,
    ) -> DAPResponse:
        """Disassemble instructions from a memory reference."""
        return await self.send_request(
            Commands.DISASSEMBLE,
            {
                "memoryReference": memory_reference,
                "offset": offset,
                "instructionOffset": instruction_offset,
                "instructionCount": instruction_count,
                "resolveSymbols": resolve_symbols,
            },
        )

    async def locations(self, location_reference: int) -> DAPResponse:
        """Resolve a DAP locationReference into source coordinates."""
        return await self.send_request(
            Commands.LOCATIONS,
            {"locationReference": location_reference},
        )

    async def evaluate(
        self, expression: str, frame_id: int | None = None, context: str = "watch"
    ) -> DAPResponse:
        """Evaluate expression."""
        args: dict[str, Any] = {"expression": expression, "context": context}
        if frame_id is not None:
            args["frameId"] = frame_id
        return await self.send_request(Commands.EVALUATE, args)

    async def exception_info(self, thread_id: int) -> DAPResponse:
        """Get exception info for thread."""
        return await self.send_request(Commands.EXCEPTION_INFO, {"threadId": thread_id})
