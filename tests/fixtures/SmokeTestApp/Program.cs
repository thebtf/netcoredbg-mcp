// Smoke test app for netcoredbg-mcp DAP coverage testing.
// Each method exercises a specific debugging scenario.
// Set breakpoints, step through, evaluate expressions, check output categories.

namespace SmokeTestApp;

public class Program
{
    // Scenario 1: Breakpoint hit counting — loop hits the same line multiple times
    static int CountToTen()
    {
        var sum = 0;
        for (var i = 1; i <= 10; i++)
        {
            sum += i; // SET BREAKPOINT HERE — should be hit 10 times
        }
        return sum; // Expected: 55
    }

    // Scenario 2: Exception handling — caught exception with info
    static void ThrowAndCatch()
    {
        try
        {
            var numbers = new int[] { 1, 2, 3 };
            _ = numbers[10]; // IndexOutOfRangeException
        }
        catch (IndexOutOfRangeException ex)
        {
            Console.Error.WriteLine($"Caught exception: {ex.Message}");
        }
    }

    // Scenario 3: Output categories — stdout vs stderr
    static void OutputCategories()
    {
        Console.WriteLine("This is stdout output");
        Console.Error.WriteLine("This is stderr output");
        Console.WriteLine("Another stdout line");
        Console.Error.WriteLine("Another stderr line");
    }

    // Scenario 4: Variable inspection — different types
    static void VariableInspection()
    {
        var intVar = 42;
        var stringVar = "hello world";
        var listVar = new List<int> { 1, 2, 3, 4, 5 };
        var dictVar = new Dictionary<string, int>
        {
            ["alpha"] = 1,
            ["beta"] = 2,
            ["gamma"] = 3,
        };
        var nullableVar = (string?)null;
        var tupleVar = (Name: "test", Value: 123);

        // SET BREAKPOINT HERE — inspect all variables above
        Console.WriteLine($"int={intVar}, string={stringVar}, list.Count={listVar.Count}");
        Console.WriteLine($"dict.Count={dictVar.Count}, nullable={nullableVar ?? "null"}, tuple={tupleVar}");
    }

    // Scenario 5: Step through — multi-level call stack
    static int Outer(int x)
    {
        var mid = Middle(x + 1);
        return mid * 2;
    }

    static int Middle(int x)
    {
        var inner = Inner(x + 1); // Step into here to verify call stack depth
        return inner + 10;
    }

    static int Inner(int x)
    {
        return x * x; // 3 frames deep: Main → Outer → Middle → Inner
    }

    // Scenario 6: Long-running for quick_evaluate testing
    static void LongRunning()
    {
        var counter = 0;
        Console.WriteLine("Long-running started. Use quick_evaluate to check counter.");
        for (var i = 0; i < 30; i++)
        {
            counter = i;
            System.Threading.Thread.Sleep(200); // 200ms * 30 = ~6s — enough for quick_evaluate
            Console.WriteLine($"Tick {i}/30");
        }
        Console.WriteLine($"Long-running done. Final counter: {counter}");
    }

    // Scenario 7: Unhandled exception (for exception breakpoint testing)
    static void UnhandledException()
    {
        throw new InvalidOperationException("This is an unhandled exception for testing");
    }

    public static void Main(string[] args)
    {
        var scenario = args.Length > 0 ? args[0] : "all";

        Console.WriteLine($"=== SmokeTestApp: scenario={scenario} ===");

        switch (scenario)
        {
            case "hitcount":
                var sum = CountToTen();
                Console.WriteLine($"Sum: {sum}");
                break;

            case "exception":
                ThrowAndCatch();
                break;

            case "output":
                OutputCategories();
                break;

            case "variables":
                VariableInspection();
                break;

            case "stepping":
                var result = Outer(1);
                Console.WriteLine($"Outer(1) = {result}");
                break;

            case "longrun":
                LongRunning();
                break;

            case "unhandled":
                UnhandledException();
                break;

            case "all":
            default:
                Console.WriteLine("--- Hit Counting ---");
                sum = CountToTen();
                Console.WriteLine($"Sum: {sum}");

                Console.WriteLine("--- Exception ---");
                ThrowAndCatch();

                Console.WriteLine("--- Output Categories ---");
                OutputCategories();

                Console.WriteLine("--- Variables ---");
                VariableInspection();

                Console.WriteLine("--- Stepping ---");
                result = Outer(1);
                Console.WriteLine($"Outer(1) = {result}");

                Console.WriteLine("=== All scenarios complete ===");
                break;
        }

        Console.WriteLine("=== SmokeTestApp finished ===");
    }
}
