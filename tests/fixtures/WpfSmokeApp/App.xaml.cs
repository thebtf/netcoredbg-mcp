using System.IO;
using System.Windows;

namespace WpfSmokeApp;

public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        if (e.Args.Length > 0 && e.Args[0] == "--root-lifecycle-control")
        {
            ShutdownMode = ShutdownMode.OnExplicitShutdown;
            if (e.Args.Length != 2 || !Path.IsPathFullyQualified(e.Args[1]) ||
                !Directory.Exists(e.Args[1]))
            {
                Console.Error.WriteLine("Root lifecycle control requires an existing absolute private directory.");
                Shutdown(2);
                return;
            }

            base.OnStartup(e);
            new RootLifecycleControl(this, e.Args[1]).Show();
            return;
        }

        StartupUri = new Uri("MainWindow.xaml", UriKind.Relative);
        base.OnStartup(e);
    }
}
