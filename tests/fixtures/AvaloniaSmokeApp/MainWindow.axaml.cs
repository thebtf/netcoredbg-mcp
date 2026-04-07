using System.Collections.ObjectModel;
using Avalonia.Controls;
using Avalonia.Interactivity;

namespace AvaloniaSmokeApp;

public record PersonRow(string Name, string Role, string Level);

public partial class MainWindow : Window
{
    private int _invokeCount;

    public ObservableCollection<PersonRow> People { get; } = new()
    {
        new("Alice", "Developer", "Senior"),
        new("Bob", "Designer", "Mid"),
        new("Charlie", "Manager", "Lead"),
        new("Diana", "Tester", "Junior"),
        new("Eve", "DevOps", "Senior"),
    };

    public MainWindow()
    {
        InitializeComponent();
        DataGrid.ItemsSource = People;
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
