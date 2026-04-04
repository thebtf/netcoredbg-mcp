using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Windows;
using Microsoft.Win32;

namespace WpfSmokeApp;

public partial class MainWindow : Window
{
    private readonly MainViewModel _viewModel = new();

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

    public event PropertyChangedEventHandler? PropertyChanged;

    protected void OnPropertyChanged([CallerMemberName] string? name = null)
    {
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
    }
}
