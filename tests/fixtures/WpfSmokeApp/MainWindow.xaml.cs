using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using Microsoft.Win32;

namespace WpfSmokeApp;

public partial class MainWindow : Window
{
    private readonly MainViewModel _viewModel = new();
    private int _dataGridAnchorIndex;
    private int _dataGridCurrentIndex;
    private bool _suppressSelectionSync;

    public MainWindow()
    {
        InitializeComponent();
        DataContext = _viewModel;
    }

    private void BtnInvoke_Click(object sender, RoutedEventArgs e)
    {
        _viewModel.InvokeCount++;
        _viewModel.StatusText = $"Invoked {_viewModel.InvokeCount}x";
    }

    private void BtnScoped_Click(object sender, RoutedEventArgs e)
    {
        _viewModel.StatusText = "Scoped button clicked";
    }

    private void BtnOpenFile_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFileDialog
        {
            Title = "Select a file",
            Filter = "All files (*.*)|*.*",
        };
        if (dialog.ShowDialog() == true)
        {
            _viewModel.StatusText = $"Selected: {dialog.FileName}";
        }
    }

    private void DataGrid_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_suppressSelectionSync || sender is not DataGrid grid || grid.SelectedIndex < 0)
        {
            return;
        }

        _dataGridAnchorIndex = grid.SelectedIndex;
        _dataGridCurrentIndex = grid.SelectedIndex;
    }

    private void DataGrid_PreviewKeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key is not (Key.Down or Key.Up))
        {
            return;
        }

        var grid = (DataGrid)sender;
        var shiftHeld = Keyboard.Modifiers.HasFlag(ModifierKeys.Shift);
        if (shiftHeld)
        {
            var direction = e.Key == Key.Down ? 1 : -1;
            _dataGridCurrentIndex = Math.Clamp(
                _dataGridCurrentIndex + direction,
                0,
                grid.Items.Count - 1);
            ApplySelectionRange(grid, _dataGridAnchorIndex, _dataGridCurrentIndex);
            e.Handled = true;
        }
        else
        {
            var direction = e.Key == Key.Down ? 1 : -1;
            _dataGridCurrentIndex = Math.Clamp(
                _dataGridCurrentIndex + direction,
                0,
                grid.Items.Count - 1);
            _dataGridAnchorIndex = _dataGridCurrentIndex;
        }

        var selectedNames = grid.SelectedItems
            .OfType<PersonRow>()
            .Select(row => row.Name)
            .ToArray();
        var selected = string.Join(",", selectedNames);
        _viewModel.StatusText = $"DataGridArrow key={e.Key} shift={shiftHeld} selected={selected}";
        Console.WriteLine(_viewModel.StatusText);
    }

    private void ApplySelectionRange(DataGrid grid, int startIndex, int endIndex)
    {
        var start = Math.Min(startIndex, endIndex);
        var end = Math.Max(startIndex, endIndex);
        _suppressSelectionSync = true;
        try
        {
            grid.SelectedItems.Clear();
            for (var index = start; index <= end; index++)
            {
                grid.SelectedItems.Add(grid.Items[index]);
            }

            if (grid.Columns.Count > 0)
            {
                grid.CurrentCell = new DataGridCellInfo(grid.Items[endIndex], grid.Columns[0]);
            }
            grid.ScrollIntoView(grid.Items[endIndex]);
        }
        finally
        {
            _suppressSelectionSync = false;
        }
    }
}

public class MainViewModel : INotifyPropertyChanged
{
    private string _statusText = "Ready";
    private bool _isFeatureEnabled;
    private int _invokeCount;

    public string StatusText
    {
        get => _statusText;
        set { _statusText = value; OnPropertyChanged(); }
    }

    public bool IsFeatureEnabled
    {
        get => _isFeatureEnabled;
        set { _isFeatureEnabled = value; OnPropertyChanged(); }
    }

    public int InvokeCount
    {
        get => _invokeCount;
        set { _invokeCount = value; OnPropertyChanged(); }
    }

    public ObservableCollection<string> Items { get; } = new()
    {
        "Item 1",
        "Item 2",
        "Item 3",
        "Item 4",
        "Item 5",
    };

    public ObservableCollection<PersonRow> People { get; } = new()
    {
        new("Alice", "Developer", "Senior"),
        new("Bob", "Designer", "Mid"),
        new("Charlie", "Manager", "Lead"),
        new("Diana", "Tester", "Junior"),
        new("Eve", "DevOps", "Senior"),
    };

    public event PropertyChangedEventHandler? PropertyChanged;

    protected void OnPropertyChanged([CallerMemberName] string? name = null)
    {
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
    }
}

public record PersonRow(string Name, string Role, string Level);
