"""DAP Client - communicates with netcoredbg process."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from collections.abc import Callable
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

    def _find_netcoredbg(self) -> str:
        """Find netcoredbg executable."""
        # Check environment variable
        env_path = os.environ.get("NETCOREDBG_PATH")
        if env_path and os.path.exists(env_path):
            return env_path

        # Check relative to this package
        package_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
        candidates = [
            os.path.join(package_dir, "bin", "netcoredbg", "netcoredbg.exe"),
            os.path.join(package_dir, "bin", "netcoredbg", "netcoredbg"),
        ]
        for path in candidates:
            if os.path.exists(path):
                return path

        # Check system PATH using shutil.which
        system_path = shutil.which("netcoredbg")
        if system_path:
            return system_path

        raise FileNotFoundError(
            "netcoredbg not found. Set NETCOREDBG_PATH environment variable."
        )

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
        logger.debug(f">>> {request.command}: {request.arguments}")
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
                "supportsProgressReporting": False,
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
        env: dict[str, str] | None = None,
        stop_at_entry: bool = False,
        just_my_code: bool = False,
    ) -> DAPResponse:
        """Launch program for debugging."""
        arguments = {
            "program": program,
            "cwd": cwd or os.path.dirname(program),
            "args": args or [],
            "env": env or {},
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

    async def step_in(self, thread_id: int) -> DAPResponse:
        """Step into function."""
        return await self.send_request(Commands.STEP_IN, {"threadId": thread_id})

    async def step_out(self, thread_id: int) -> DAPResponse:
        """Step out of function."""
        return await self.send_request(Commands.STEP_OUT, {"threadId": thread_id})

    async def pause(self, thread_id: int) -> DAPResponse:
        """Pause execution."""
        return await self.send_request(Commands.PAUSE, {"threadId": thread_id})

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

    async def variables(self, variables_reference: int) -> DAPResponse:
        """Get variables for scope/variable."""
        return await self.send_request(
            Commands.VARIABLES, {"variablesReference": variables_reference}
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
