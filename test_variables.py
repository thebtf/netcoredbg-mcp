"""Test variable inspection while stopped at breakpoint."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from netcoredbg_mcp.session import SessionManager, DebugState


async def test_variable_inspection():
    """Test that we can inspect variables while stopped at a breakpoint."""
    session = SessionManager()

    test_app = os.path.join(os.path.dirname(__file__), "test-app")
    dll_path = os.path.join(test_app, "bin", "Debug", "net6.0", "test-app.dll")
    source_path = os.path.join(test_app, "Program.cs")

    print("=" * 60)
    print("Variable Inspection Test")
    print("=" * 60)

    try:
        # Add breakpoint at line 13 BEFORE starting session
        # Line 13: Console.WriteLine($"Sum of {x} and {y} is {sum}");
        print("\n[1] Adding breakpoint at line 13 (after Add call)...")
        bp = await session.add_breakpoint(source_path, 13)
        print(f"    Breakpoint: line {bp.line}")

        # Launch debug session
        print("\n[2] Launching debug session...")
        result = await session.launch(dll_path, cwd=test_app, stop_at_entry=False)
        print(f"    Result: {result}")

        # Wait for breakpoint to be hit
        print("\n[3] Waiting for breakpoint to be hit...")
        for i in range(50):  # Wait up to 5 seconds
            await asyncio.sleep(0.1)
            if session.state.state == DebugState.STOPPED:
                print(f"    Stopped after {(i+1)*0.1:.1f}s")
                break
        else:
            print("    WARNING: Timeout waiting for breakpoint")

        print(f"    State: {session.state.state.value}")
        print(f"    Stop reason: {session.state.stop_reason}")
        print(f"    Thread ID: {session.state.current_thread_id}")

        # Get call stack while stopped
        print("\n[4] Getting call stack while stopped...")
        frames = await session.get_stack_trace()
        print(f"    Got {len(frames)} frames")

        if frames:
            frame = frames[0]
            print(f"    Frame ID: {frame.id}")
            print(f"    Function: {frame.name}")
            print(f"    Source: {frame.source}")
            print(f"    Line: {frame.line}")

            # Get scopes for this frame
            print("\n[5] Getting scopes for frame...")
            scopes = await session.get_scopes(frame.id)
            print(f"    Got {len(scopes)} scopes")

            # Get variables for each scope
            for scope in scopes:
                scope_name = scope.get("name", "unknown")
                var_ref = scope.get("variablesReference")
                print(f"\n[6] Getting variables for scope '{scope_name}' (ref={var_ref})...")

                if var_ref:
                    variables = await session.get_variables(var_ref)
                    print(f"    Variables ({len(variables)}):")
                    for var in variables:
                        print(f"      - {var.name}: {var.value} ({var.type})")

        # Step over to next line
        print("\n[7] Step over to next line...")
        step_result = await session.step_over()
        print(f"    Result: {step_result}")

        # Wait for stop
        for i in range(20):
            await asyncio.sleep(0.1)
            if session.state.state == DebugState.STOPPED:
                break

        print(f"    State after step: {session.state.state.value}")

        # Check variables again after step
        if session.state.state == DebugState.STOPPED:
            print("\n[8] Getting call stack after step...")
            frames = await session.get_stack_trace()
            if frames:
                frame = frames[0]
                print(f"    Now at line: {frame.line}")

        # Continue to finish
        print("\n[9] Continuing to finish...")
        await session.continue_execution()
        await asyncio.sleep(1.0)

        # Print captured output
        print("\n[10] Captured output:")
        for line in session.state.output_buffer:
            print(f"    {line.strip()}")

        print("\n" + "=" * 60)
        print("Test completed successfully!")
        print("=" * 60)

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await session.stop()


if __name__ == "__main__":
    asyncio.run(test_variable_inspection())
