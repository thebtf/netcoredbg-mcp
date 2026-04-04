using Avalonia.Controls;
using Avalonia.Interactivity;

namespace AvaloniaSmokeApp;

public partial class MainWindow : Window
{
    private int _invokeCount;

    public MainWindow()
    {
        InitializeComponent();
    }

    private void BtnInvoke_Click(object? sender, RoutedEventArgs e)
    {
        _invokeCount++;
        if (sender is Button btn)
        {
            btn.Content = $"Invoked {_invokeCount}x";
        }
        TxtOutput.Text = $"Invoked {_invokeCount}x";
    }

    private void BtnScoped_Click(object? sender, RoutedEventArgs e)
    {
        TxtOutput.Text = "Scoped button clicked";
    }
}
