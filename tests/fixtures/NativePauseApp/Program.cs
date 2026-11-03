using System.Runtime.CompilerServices;

namespace NativePauseApp;

internal static class Program
{
    private static void Main()
    {
        Console.WriteLine("NATIVE_PAUSE_READY"); // NATIVE_PAUSE_BREAKPOINT
        RunLoop();
    }

    [MethodImpl(MethodImplOptions.NoInlining | MethodImplOptions.NoOptimization)]
    private static void RunLoop()
    {
        long iteration = 0;
        while (true)
        {
            Thread.Sleep(50);
            iteration = unchecked(iteration + 1);
            if (iteration % 20 == 0)
            {
                Console.WriteLine($"NATIVE_PAUSE_HEARTBEAT iteration={iteration}");
            }
        }
    }
}
