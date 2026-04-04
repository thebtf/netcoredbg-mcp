"""Smoke test for DAP coverage expansion features.

Requires: netcoredbg in PATH or NETCOREDBG_PATH env var.
Build first: dotnet build tests/fixtures/SmokeTestApp -c Debug

Usage: python tests/smoke_test.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netcoredbg_mcp.session import SessionManager
from netcoredbg_mcp.session.state import Breakpoint


async def smoke_test() -> bool:
    """Run smoke tests against real netcoredbg. Returns True if all pass."""
    manager = SessionManager()

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dll = os.path.join(base, "tests", "fixtures", "SmokeTestApp", "bin", "Debug", "net8.0", "SmokeTestApp.dll")
    source = os.path.join(base, "tests", "fixtures", "SmokeTestApp", "Program.cs")

    if not os.path.exists(dll):
        print(f"ERROR: Build SmokeTestApp first: dotnet build tests/fixtures/SmokeTestApp -c Debug")
        return False

    passed = 0
    failed = 0

    def check(name: str, condition: bool, detail: str = "") -> None:
        nonlocal passed, failed
        status = "PASS" if condition else "FAIL"
        if not condition:
            failed += 1
        else:
            passed += 1
        suffix = f" — {detail}" if detail else ""
        print(f"  [{status}] {name}{suffix}")

    print("=== SMOKE TEST: DAP Coverage v0.4.0 ===\n")

    try:
        # Setup: breakpoint on line 15 (sum += i in CountToTen)
        manager.breakpoints.add(Breakpoint(file=source, line=15))
        await manager.launch(program=dll, args=["hitcount"], stop_at_entry=False)
        snapshot = await manager.wait_for_stopped(timeout=10)

        # Wait for async hit counting
        await asyncio.sleep(0.3)

        norm = manager.breakpoints._normalize_path(source)

        # Test 1: Hit counting
        print("Hit Counting:")
        hit = manager.state.hit_counts.get((norm, 15), 0)
        check("First breakpoint hit counted", hit == 1, f"hit_count={hit}")

        # Continue 2 more times
        for _ in range(2):
            manager.prepare_for_execution()
            await manager._client.continue_execution(manager.state.current_thread_id)
            await manager.wait_for_stopped(timeout=5)
            await asyncio.sleep(0.2)

        hit = manager.state.hit_counts.get((norm, 15), 0)
        check("Hit count increments correctly", hit == 3, f"hit_count={hit}")

        # Test 2: Module tracking
        print("\nModule Tracking:")
        check("Modules loaded", len(manager.state.modules) > 0, f"count={len(manager.state.modules)}")
        app_module = [m for m in manager.state.modules if "SmokeTestApp" in m.name]
        check("SmokeTestApp module found", len(app_module) > 0)

        # Test 3: Output categories
        print("\nOutput Categories:")
        stdout = [e for e in manager.state.output_buffer if e.category == "stdout"]
        check("Output buffer has entries", len(manager.state.output_buffer) > 0, f"total={len(manager.state.output_buffer)}")
        check("Stdout entries tagged", len(stdout) > 0, f"stdout={len(stdout)}")

        # Test 4: Stopped description fields exist on snapshot
        print("\nStopped Description:")
        check("Description field exists", hasattr(snapshot, "description"))
        check("Text field exists", hasattr(snapshot, "text"))

        # Test 5: Breakpoint verified + hit_count
        print("\nBreakpoint Status:")
        all_bps = manager.breakpoints.get_all()
        bp_list = []
        for _, bps in all_bps.items():
            bp_list.extend(bps)
        check("Breakpoint is verified", len(bp_list) > 0 and bp_list[0].verified)

        # Test 6: Capabilities
        print("\nCapabilities:")
        caps = manager.client.capabilities
        check("Capabilities accessible", isinstance(caps, dict) and len(caps) > 0, f"keys={len(caps)}")
        check("supportsTerminateRequest", caps.get("supportsTerminateRequest", False) is True)

        # Test 7: Terminate
        print("\nGraceful Terminate:")
        if caps.get("supportsTerminateRequest", False):
            await manager.client.terminate()
            snapshot = await manager.wait_for_stopped(timeout=5)
            check("Terminate succeeds", snapshot.state.value == "terminated", f"state={snapshot.state.value}")
        else:
            check("Terminate supported", False, "supportsTerminateRequest=False")

    except Exception as e:
        print(f"\n  [FAIL] Unexpected error: {e}")
        failed += 1
    finally:
        try:
            await manager.stop()
        except Exception:
            pass

    print(f"\n=== RESULTS: {passed} passed, {failed} failed ===")
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(smoke_test())
    sys.exit(0 if success else 1)
