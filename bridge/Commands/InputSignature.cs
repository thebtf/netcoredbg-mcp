namespace FlaUIBridge.Commands;

internal static class InputSignature
{
    // Build-stable magic value shared with netcoredbg_mcp.ui.input_signature.
    // The OS injected-input flag separates physical operator input from synthetic input;
    // this signature identifies synthetic input emitted by this runner. It is best-effort
    // against foreign synthetic injectors because another process could replay the value.
    internal const ulong RunnerInputSignatureValue = 0x4E434442UL;
    internal static readonly UIntPtr RunnerInputSignature = new(RunnerInputSignatureValue);
    internal static readonly IntPtr RunnerInputSignatureIntPtr = new(unchecked((long)RunnerInputSignatureValue));
}
