using System;

class Program
{
    static void Main(string[] args)
    {
        Console.WriteLine("Test app started");
        
        int x = 10;
        int y = 20;
        int sum = Add(x, y);
        
        Console.WriteLine($"Sum of {x} and {y} is {sum}");
        
        for (int i = 0; i < 3; i++)
        {
            Console.WriteLine($"Loop iteration: {i}");
        }
        
        Console.WriteLine("Test app finished");
    }
    
    static int Add(int a, int b)
    {
        int result = a + b;
        return result;
    }
}
