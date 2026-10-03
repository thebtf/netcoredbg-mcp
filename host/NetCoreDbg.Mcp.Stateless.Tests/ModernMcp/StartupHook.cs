internal static class StartupHook
{
    internal const string ErrorMarkerEnvironmentVariable = "NETCOREDBG_MCP_TEST_STARTUP_ERROR";

    public static void Initialize()
    {
        var marker = Environment.GetEnvironmentVariable(ErrorMarkerEnvironmentVariable);
        if (string.IsNullOrEmpty(marker))
        {
            return;
        }

        Console.Error.WriteLine(marker);
        Console.Error.Flush();
        throw new InvalidOperationException("Controlled MCP startup failure.");
    }
}
