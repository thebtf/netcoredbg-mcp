"""DAP Client - communicates with netcoredbg process."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
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
TERMINAL_STDERR_LIMIT = 16 * 1024
TERMINAL_PREVIEW_LIMIT = 2 * 1024
NATURAL_EXIT_TIMEOUT = 0.25
STREAM_DRAIN_TIMEOUT = 0.25
TERMINATE_TIMEOUT = 5.0
KILL_TIMEOUT = 2.0
TERMINAL_EVENT_NAME_LIMIT = 256

_CREDENTIAL_VALUE_RE = re.compile(
    r"(?i)\b(authorization|access[_-]?token|token|password|secret|api[_-]?key)\b"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_JSON_CREDENTIAL_VALUE_RE = re.compile(
    r'(?i)("(?:authorization|access[_-]?token|token|password|secret|api[_-]?key)"'
    r'\s*:\s*")[^"]*(")'
)
_BEARER_VALUE_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_WINDOWS_PATH_RE = re.compile(r"(?i)(?:[A-Z]:[\\/]|\\\\)[^\s\"'<>|]+")
_POSIX_PATH_RE = re.compile(r"(?<![\w:])/(?:[^/\s\"']+/)+[^\s\"']*")


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

    launch_env: dict[str, str | None] = {name.upper(): value for name, value in process_env.items()}
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
    value = explicit_value
    if value is None:
        value = first_env_value(env, *names)
    if value is None:
        return

    for name in names:
        if name not in explicit_keys:
            env[name] = value


def first_env_value(env: Mapping[str, str | None], *names: str) -> str | None:
    for name in names:
        if name in env and env[name] is not None:
            value = env[name]
            return value
    return None


class DapTerminalTrigger(str, Enum):
    """The first observed fact that made one adapter run terminal."""

    DAP_TERMINATED = "dap_terminated"
    STDOUT_EOF = "stdout_eof"
    READER_FAILURE = "reader_failure"
    PROCESS_EXIT = "process_exit"
    EXPLICIT_STOP = "explicit_stop"


class DapCleanupOutcome(str, Enum):
    """How the elected finalizer completed adapter cleanup."""

    NATURAL_EXIT = "natural_exit"
    TERMINATED = "terminated"
    KILLED = "killed"
    EXIT_UNOBSERVED = "exit_unobserved"


class _RunPhase(str, Enum):
    """Private lifecycle state for one adapter subprocess generation."""

    ACTIVE = "active"
    FINALIZING = "finalizing"
    FINALIZED = "finalized"


@dataclass(frozen=True, slots=True)
class DapTransportTerminal:
    """Immutable, bounded facts from one completed adapter transport run.

    The record separates DAP protocol facts from adapter-process and stream
    observations. Missing return codes or incomplete drains remain explicit
    unknowns; they are never converted into a guessed crash or debuggee exit.
    """

    generation: object
    first_trigger: DapTerminalTrigger
    adapter_pid: int
    process_exited: bool
    returncode: int | None
    protocol_terminated: bool
    debuggee_exit_code: int | None
    stdout_eof: bool
    last_dap_event: tuple[int | None, str] | None
    last_dap_event_body_preview: str | None
    stderr_tail: bytes
    stderr_truncated: bool
    stderr_drained: bool
    reader_error: str | None
    cleanup_outcome: DapCleanupOutcome


TransportTerminalHandler = Callable[[DapTransportTerminal], None]


@dataclass(slots=True)
class _DapRun:
    """Mutable facts and task ownership for exactly one adapter generation.

    All observers capture this object instead of reading client-global process
    state. That fence prevents a late task from an older run from settling
    requests, cleaning up, or publishing terminal facts for a newer adapter.
    """

    generation: object
    process: asyncio.subprocess.Process
    pending: dict[int, asyncio.Future[DAPResponse]]
    phase: _RunPhase = _RunPhase.ACTIVE
    first_trigger: DapTerminalTrigger | None = None
    protocol_terminated: bool = False
    debuggee_exit_code: int | None = None
    stdout_eof: bool = False
    last_dap_event: tuple[int | None, str] | None = None
    last_dap_event_body_preview: str | None = None
    reader_error: str | None = None
    stderr_tail: bytearray = field(default_factory=bytearray)
    stderr_truncated: bool = False
    stderr_drained: bool = False
    process_exited: bool = False
    returncode: int | None = None
    cleanup_outcome: DapCleanupOutcome = DapCleanupOutcome.EXIT_UNOBSERVED
    stdout_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    process_task: asyncio.Task[None] | None = None
    finalizer_task: asyncio.Task[DapTransportTerminal] | None = None
    terminal: DapTransportTerminal | None = None


def sanitize_terminal_text(
    value: object,
    limit: int = TERMINAL_PREVIEW_LIMIT,
) -> str:
    """Return bounded public diagnostic text with secrets and controls removed.

    Adapter output is untrusted. Before terminal facts cross into public MCP
    state, this boundary redacts credential-shaped values and absolute paths,
    and renders control or bidi-format characters as visible escape sequences.
    The final length bound is applied after normalization so replacement text
    cannot expand a small input into an unbounded public record.
    """

    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
    text = _JSON_CREDENTIAL_VALUE_RE.sub(
        lambda match: f"{match.group(1)}<redacted>{match.group(2)}", text
    )
    text = _CREDENTIAL_VALUE_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>", text
    )
    text = _BEARER_VALUE_RE.sub("Bearer <redacted>", text)
    text = _WINDOWS_PATH_RE.sub("<path>", text)
    text = _POSIX_PATH_RE.sub("<path>", text)

    normalized: list[str] = []
    named_controls = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
    for character in text:
        category = unicodedata.category(character)
        if character in named_controls:
            normalized.append(named_controls[character])
        elif ord(character) < 32 or ord(character) == 127 or category in {"Cf", "Cs"}:
            normalized.append(f"\\u{ord(character):04x}")
        else:
            normalized.append(character)

    safe = "".join(normalized)
    if len(safe) <= limit:
        return safe
    return safe[:limit] + "... [truncated]"


def _bounded_text(value: object, limit: int = TERMINAL_PREVIEW_LIMIT) -> str:
    """Normalize one internal diagnostic field for terminal retention."""

    return sanitize_terminal_text(value, limit)


def _append_stderr(run: _DapRun, chunk: bytes) -> None:
    """Append bytes to the fixed-capacity stderr tail for one run."""

    if not chunk:
        return
    combined = bytes(run.stderr_tail) + chunk
    if len(combined) > TERMINAL_STDERR_LIMIT:
        run.stderr_truncated = True
        combined = combined[-TERMINAL_STDERR_LIMIT:]
    run.stderr_tail[:] = combined


class DAPClient:
    """Async DAP client for netcoredbg communication."""

    def __init__(self, netcoredbg_path: str | None = None):
        """Create a client with no active adapter generation.

        Process, observer, request, and terminal facts are rebound for every
        adapter start. The legacy private fields remain the current-run views
        used by focused tests; `_DapRun` is the lifecycle authority.
        """

        self.netcoredbg_path = netcoredbg_path or self._find_netcoredbg()
        self._seq = 0
        self._request_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[DAPResponse]] = {}
        self._event_handlers: dict[str, list[Callable[[DAPEvent], None]]] = {}
        self._process: asyncio.subprocess.Process | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._capabilities: dict[str, Any] = {}
        self._run: _DapRun | None = None
        self._generation_counter = 0
        self._transport_terminal_handler: TransportTerminalHandler | None = None

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
        changed_keys = [key for key, value in capabilities.items() if current.get(key) != value]
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

    @property
    def adapter_pid(self) -> int | None:
        """Return the PID of the current adapter process, when one exists."""

        return self._process.pid if self._process is not None else None

    def set_transport_terminal_handler(self, handler: TransportTerminalHandler | None) -> None:
        """Install the sole synchronous sink for immutable terminal facts.

        `SessionManager` installs this sink before process startup. The client
        never awaits manager policy, and the sink must not perform blocking or
        asynchronous work.
        """

        self._transport_terminal_handler = handler

    async def start(self, *, generation: object | None = None) -> object:
        """Start one netcoredbg generation and its three lifecycle observers.

        Args:
            generation: Manager-issued identity used to reject terminal facts
                from an older adapter run. Direct client callers may omit it;
                the client then creates a process-local monotonic identity.

        Returns:
            The exact identity bound to the new adapter run.
        """

        if self.is_running and self._run is not None:
            return self._run.generation

        if generation is None:
            self._generation_counter += 1
            generation = self._generation_counter

        logger.info("Starting netcoredbg: %s", self.netcoredbg_path)
        process = await asyncio.create_subprocess_exec(
            self.netcoredbg_path,
            "--interpreter=vscode",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        pending: dict[int, asyncio.Future[DAPResponse]] = {}
        run = _DapRun(generation=generation, process=process, pending=pending)
        self._run = run
        self._process = process
        self._pending = pending

        # Task creation has no intervening await. The manager binds the issued
        # generation before calling start, so even eager observers cannot make
        # an older run authoritative for the session.
        run.stderr_task = asyncio.create_task(self._drain_stderr(run))
        run.process_task = asyncio.create_task(self._wait_process(run))
        run.stdout_task = asyncio.create_task(self._read_loop(run))
        self._read_task = run.stdout_task
        logger.info("netcoredbg started with PID %s", process.pid)
        return generation

    async def stop(self) -> None:
        """Join the current generation's guarded finalizer.

        The manager records explicit-stop policy before awaiting this method.
        The client records only transport facts and performs the one bounded
        cleanup sequence shared with EOF, reader failure, and process exit.
        """

        run = self._run
        if run is None:
            self._settle_pending(self._pending, cancel=True)
            logger.info("netcoredbg stopped")
            return

        finalizer, _ = self._request_finalization(run, DapTerminalTrigger.EXPLICIT_STOP)
        await asyncio.shield(finalizer)
        logger.info("netcoredbg stopped")

    def _run_for_direct_reader(self) -> _DapRun:
        """Bind legacy direct-reader tests to a real run-scoped authority."""

        if self._process is None:
            raise RuntimeError("Process not running")
        if self._run is not None and self._run.process is self._process:
            return self._run
        self._generation_counter += 1
        run = _DapRun(
            generation=self._generation_counter,
            process=self._process,
            pending=self._pending,
        )
        self._run = run
        return run

    async def _drain_stderr(self, run: _DapRun) -> None:
        """Drain stderr chunks into a fixed-capacity run-local tail."""

        stream = run.process.stderr
        if stream is None:
            run.stderr_drained = True
            return
        try:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    run.stderr_drained = True
                    return
                _append_stderr(run, chunk)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.debug("Adapter stderr drain failed: %s", error)

    async def _wait_process(self, run: _DapRun) -> None:
        """Observe adapter completion without publishing a second outcome."""

        try:
            run.returncode = await run.process.wait()
            run.process_exited = True
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.debug("Adapter process wait failed: %s", error)
        self._request_finalization(run, DapTerminalTrigger.PROCESS_EXIT)

    def _request_finalization(
        self,
        run: _DapRun,
        trigger: DapTerminalTrigger,
    ) -> tuple[asyncio.Task[DapTransportTerminal], bool]:
        """Elect one finalizer without awaiting or acquiring another lock."""

        if run.finalizer_task is not None:
            return run.finalizer_task, False
        run.phase = _RunPhase.FINALIZING
        run.first_trigger = trigger
        run.finalizer_task = asyncio.create_task(self._finalize_run(run))
        return run.finalizer_task, True

    async def _finalize_run(self, run: _DapRun) -> DapTransportTerminal:
        """Settle work, join bounded observations, clean up, and publish once."""

        self._settle_pending(run.pending)
        process = run.process
        current = asyncio.current_task()

        if not run.process_exited and process.returncode is None:
            process_wait = run.process_task
            if process_wait is not None and process_wait is not current:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(process_wait), timeout=NATURAL_EXIT_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    pass

        if not run.process_exited and process.returncode is None:
            try:
                process.terminate()
                run.returncode = await asyncio.wait_for(process.wait(), timeout=TERMINATE_TIMEOUT)
                run.process_exited = True
                run.cleanup_outcome = DapCleanupOutcome.TERMINATED
            except asyncio.TimeoutError:
                logger.warning("Process %s did not terminate, killing...", process.pid)
                process.kill()
                try:
                    run.returncode = await asyncio.wait_for(process.wait(), timeout=KILL_TIMEOUT)
                    run.process_exited = True
                    run.cleanup_outcome = DapCleanupOutcome.KILLED
                except asyncio.TimeoutError:
                    logger.error("Failed to observe killed process %s", process.pid)
            except ProcessLookupError:
                run.process_exited = True
                run.cleanup_outcome = DapCleanupOutcome.NATURAL_EXIT
        else:
            run.cleanup_outcome = DapCleanupOutcome.NATURAL_EXIT

        if process.returncode is not None:
            run.process_exited = True
            run.returncode = process.returncode

        # A process exit or DAP termination can precede buffered stdout EOF.
        # Let the reader contribute those facts within a fixed bound. Raw EOF
        # and reader-failure finalizers never await their own observer.
        if run.first_trigger in {
            DapTerminalTrigger.PROCESS_EXIT,
            DapTerminalTrigger.DAP_TERMINATED,
        }:
            await self._join_observer(run.stdout_task, STREAM_DRAIN_TIMEOUT)
        await self._join_observer(run.stderr_task, STREAM_DRAIN_TIMEOUT)

        if run.stderr_task is not None and not run.stderr_task.done():
            run.stderr_task.cancel()
        if (
            run.first_trigger
            in {
                DapTerminalTrigger.PROCESS_EXIT,
                DapTerminalTrigger.DAP_TERMINATED,
                DapTerminalTrigger.EXPLICIT_STOP,
            }
            and run.stdout_task is not None
            and not run.stdout_task.done()
        ):
            run.stdout_task.cancel()

        terminal = DapTransportTerminal(
            generation=run.generation,
            first_trigger=run.first_trigger or DapTerminalTrigger.PROCESS_EXIT,
            adapter_pid=process.pid,
            process_exited=run.process_exited,
            returncode=run.returncode,
            protocol_terminated=run.protocol_terminated,
            debuggee_exit_code=run.debuggee_exit_code,
            stdout_eof=run.stdout_eof,
            last_dap_event=run.last_dap_event,
            last_dap_event_body_preview=run.last_dap_event_body_preview,
            stderr_tail=bytes(run.stderr_tail),
            stderr_truncated=run.stderr_truncated,
            stderr_drained=run.stderr_drained,
            reader_error=run.reader_error,
            cleanup_outcome=run.cleanup_outcome,
        )
        run.terminal = terminal
        run.phase = _RunPhase.FINALIZED

        if self._run is run:
            self._process = None
            self._read_task = None

        handler = self._transport_terminal_handler
        if handler is not None:
            try:
                handler(terminal)
            except Exception:
                logger.exception("Transport terminal handler failed")
        return terminal

    @staticmethod
    async def _join_observer(
        task: asyncio.Task[None] | None,
        timeout: float,
    ) -> None:
        """Join a different observer within a fixed lifecycle bound."""

        if task is None or task.done() or task is asyncio.current_task():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            return
        except asyncio.CancelledError:
            return

    @staticmethod
    def _settle_pending(
        pending: dict[int, asyncio.Future[DAPResponse]],
        *,
        cancel: bool = False,
    ) -> None:
        """Complete every pending request exactly once, then clear the map."""

        for future in pending.values():
            if future.done():
                continue
            if cancel:
                future.cancel()
            else:
                future.set_exception(
                    RuntimeError("netcoredbg process died — pending request cancelled")
                )
        pending.clear()

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
        """Send one request only while the current adapter run accepts work."""
        run = self._run
        if not self.is_running or run is None or run.phase is not _RunPhase.ACTIVE:
            raise RuntimeError("DAP client not running")

        # Admission and registration share the request lock. If finalization
        # wins first, no future can be registered after the one settlement pass.
        async with self._request_lock:
            if self._run is not run or run.phase is not _RunPhase.ACTIVE:
                raise RuntimeError("DAP client not running")
            self._seq += 1
            seq = self._seq
            future: asyncio.Future[DAPResponse] = asyncio.get_running_loop().create_future()
            run.pending[seq] = future
            self._pending = run.pending
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

    async def _read_loop(self, run: _DapRun | None = None) -> None:
        """Parse DAP stdout and request one bounded terminal finalization."""

        run = run or self._run_for_direct_reader()
        stream = run.process.stdout
        assert stream is not None

        while True:
            try:
                header_line = await stream.readline()
                if not header_line:
                    logger.warning("netcoredbg stdout closed")
                    run.stdout_eof = True
                    finalizer, created = self._request_finalization(
                        run, DapTerminalTrigger.STDOUT_EOF
                    )
                    if created:
                        await asyncio.shield(finalizer)
                    return

                header = header_line.decode("utf-8").strip()
                if not header.startswith("Content-Length:"):
                    continue

                content_length = int(header.split(":")[1].strip())
                if content_length < 0 or content_length > MAX_CONTENT_LENGTH:
                    logger.error("Invalid Content-Length: %s", content_length)
                    raise ValueError(f"Invalid Content-Length: {content_length}")

                await stream.readline()
                content = await stream.readexactly(content_length)
                data = json.loads(content.decode("utf-8"))
                self._handle_message(data, run)

                if run.protocol_terminated:
                    self._request_finalization(run, DapTerminalTrigger.DAP_TERMINATED)
                    # Continue draining stdout. The elected finalizer owns the
                    # bounded join and will cancel this observer if EOF stalls.
                    continue
            except asyncio.CancelledError:
                return
            except Exception as error:
                run.reader_error = _bounded_text(f"{error.__class__.__name__}: {error}")
                logger.exception("Error reading DAP message")
                finalizer, created = self._request_finalization(
                    run, DapTerminalTrigger.READER_FAILURE
                )
                if created:
                    await asyncio.shield(finalizer)
                return

    def _handle_message(self, data: dict[str, Any], run: _DapRun | None = None) -> None:
        """Handle one DAP message and retain bounded protocol facts."""

        try:
            message = parse_message(data)

            if isinstance(message, DAPResponse):
                logger.debug("<<< Response %s: success=%s", message.command, message.success)
                pending = run.pending if run is not None else self._pending
                future = pending.pop(message.request_seq, None)
                if future and not future.done():
                    future.set_result(message)

            elif isinstance(message, DAPEvent):
                event_name = sanitize_terminal_text(message.event, TERMINAL_EVENT_NAME_LIMIT)
                body_json = json.dumps(message.body, default=str, separators=(",", ":"))
                body_preview = _bounded_text(body_json)
                logger.debug("<<< Event %s: %s", event_name, body_preview)
                if run is not None:
                    event_seq = (
                        message.seq if type(message.seq) is int and message.seq >= 0 else None
                    )
                    run.last_dap_event = (event_seq, event_name)
                    run.last_dap_event_body_preview = body_preview
                    if message.event == "terminated":
                        run.protocol_terminated = True
                    elif message.event == "exited":
                        exit_code = message.body.get("exitCode")
                        if type(exit_code) is int:
                            run.debuggee_exit_code = exit_code

                handlers = (
                    self._event_handlers.get(message.event, [])
                    if isinstance(message.event, str)
                    else []
                )
                if not handlers:
                    logger.warning(
                        "Unhandled DAP event '%s' dropped: body_size=%d body_preview=%s",
                        event_name,
                        len(body_json.encode("utf-8")),
                        body_preview,
                    )
                for handler in handlers:
                    try:
                        handler(message)
                    except Exception:
                        logger.exception("Event handler error")

        except Exception:
            logger.exception("Error handling message, data: %s", data)

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
        return await self.send_request(Commands.DISCONNECT, {"terminateDebuggee": terminate})

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

    async def set_function_breakpoints(self, breakpoints: list[dict[str, Any]]) -> DAPResponse:
        """Set function breakpoints."""
        return await self.send_request(
            Commands.SET_FUNCTION_BREAKPOINTS,
            {"breakpoints": breakpoints},
        )

    async def set_variable(self, variables_reference: int, name: str, value: str) -> DAPResponse:
        """Set a variable's value."""
        return await self.send_request(
            Commands.SET_VARIABLE,
            {
                "variablesReference": variables_reference,
                "name": name,
                "value": value,
            },
        )

    async def set_hot_reload(self, enable: bool) -> DAPResponse:
        """Enable or disable netcoredbg Hot Reload before launch."""
        return await self.send_request("setHotReload", {"enable": enable})

    async def set_exception_breakpoints(self, filters: list[str] | None = None) -> DAPResponse:
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
        return await self.send_request(Commands.STEP_IN_TARGETS, {"frameId": frame_id})

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
