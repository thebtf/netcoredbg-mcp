using System.Collections.ObjectModel;
using System.ComponentModel;
using System.IO;
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
    private readonly Stack<Action> _undoStack = new();
    private readonly string? _mutableFile;
    private int _dataGridAnchorIndex;
    private int _dataGridCurrentIndex;
    private bool _suppressCharacterSelection;
    private bool _suppressSelectionSync;

    public MainWindow()
    {
        InitializeComponent();
        DataContext = _viewModel;
        _mutableFile = Environment.GetEnvironmentVariable("WPF_SMOKE_MUTABLE_FILE");
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
            .OfType<CueRow>()
            .Select(row => row.Phrase)
            .ToArray();
        var selected = string.Join(",", selectedNames);
        _viewModel.StatusText = $"DataGridArrow key={e.Key} shift={shiftHeld} selected={selected}";
        Console.WriteLine(_viewModel.StatusText);
    }

    private void CharactersListBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_suppressCharacterSelection || CharactersListBox.SelectedItem is not CharacterRow character)
        {
            return;
        }

        AssignCharacter(character);
    }

    private void CharGender_Checked(object sender, RoutedEventArgs e)
    {
        if (_suppressCharacterSelection || sender is not CheckBox checkbox ||
            checkbox.DataContext is not CharacterRow character)
        {
            return;
        }

        var newValue = checkbox.IsChecked == true;
        var oldValue = !newValue;
        _undoStack.Push(() =>
        {
            _suppressCharacterSelection = true;
            try
            {
                character.IsFemale = oldValue;
            }
            finally
            {
                _suppressCharacterSelection = false;
            }
            _viewModel.GenderStatusText = $"{character.Name} {character.GenderLabel}";
            FocusCueGrid();
        });

        _viewModel.GenderStatusText = $"{character.Name} {character.GenderLabel}";
        _viewModel.StatusText = $"WpfWorkflow ToggleGender character={character.Name} state={character.GenderLabel}";
        Console.WriteLine($"WpfWorkflow ToggleGender character={character.Name} state={character.GenderLabel}");
        WriteMutableState($"gender={character.Name}:{character.GenderLabel}");
    }

    private void Undo_Click(object sender, RoutedEventArgs e)
    {
        UndoLatest();
    }

    private void AssignCharacter(CharacterRow character)
    {
        var selectedRows = CueDataGrid.SelectedItems.OfType<CueRow>().ToArray();
        if (selectedRows.Length == 0 && CueDataGrid.SelectedItem is CueRow current)
        {
            selectedRows = new[] { current };
        }

        if (selectedRows.Length == 0)
        {
            return;
        }

        var before = selectedRows
            .Select(row => (Row: row, Character: row.Character))
            .ToArray();
        _undoStack.Push(() =>
        {
            foreach (var item in before)
            {
                item.Row.Character = item.Character;
            }
            FocusCueGrid();
        });

        foreach (var row in selectedRows)
        {
            row.Character = character.Name;
        }

        _viewModel.StatusText =
            $"WpfWorkflow AssignCharacter route=ListInvoke selectedCount={selectedRows.Length} character={character.Name}";
        Console.WriteLine(_viewModel.StatusText);
        WriteMutableState($"assigned={character.Name};count={selectedRows.Length}");

        txtOutput.Focus();
    }

    private void UndoLatest()
    {
        if (_undoStack.Count == 0)
        {
            _viewModel.StatusText = "WpfWorkflow Undo route=Menu empty=True";
            Console.WriteLine(_viewModel.StatusText);
            FocusCueGrid();
            return;
        }

        _undoStack.Pop().Invoke();
        _viewModel.StatusText = "WpfWorkflow Undo route=Menu";
        Console.WriteLine(_viewModel.StatusText);
        WriteMutableState("undo=latest");
        FocusCueGrid();
    }

    private void FocusCueGrid()
    {
        CueDataGrid.Focus();
        Keyboard.Focus(CueDataGrid);
    }

    private void WriteMutableState(string value)
    {
        if (string.IsNullOrWhiteSpace(_mutableFile))
        {
            return;
        }

        try
        {
            File.WriteAllText(_mutableFile, value);
        }
        catch (IOException)
        {
            // The smoke fixture should keep running even when the optional
            // mutable-state file is locked or unavailable.
        }
        catch (UnauthorizedAccessException)
        {
        }
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
    private string _genderStatusText = "No gender change";
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

    public string GenderStatusText
    {
        get => _genderStatusText;
        set { _genderStatusText = value; OnPropertyChanged(); }
    }

    public ObservableCollection<string> Items { get; } = new()
    {
        "Item 1",
        "Item 2",
        "Item 3",
        "Item 4",
        "Item 5",
    };

    public ObservableCollection<CueRow> CueRows { get; } = new()
    {
        new("00:00:01.0", "00:00:03.0", "Narrator", "Fixture cue one"),
        new("00:00:04.0", "00:00:06.0", "Narrator", "Fixture cue two"),
        new("00:00:07.0", "00:00:09.0", "Narrator", "Fixture cue three"),
    };

    public ObservableCollection<CharacterRow> Characters { get; } = new()
    {
        new("ALICE", isFemale: false),
        new("BOB", isFemale: false),
    };

    public event PropertyChangedEventHandler? PropertyChanged;

    protected void OnPropertyChanged([CallerMemberName] string? name = null)
    {
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
    }
}

public class CueRow : INotifyPropertyChanged
{
    private string _character;

    public CueRow(string start, string end, string character, string phrase)
    {
        Start = start;
        End = end;
        _character = character;
        Phrase = phrase;
    }

    public string Start { get; }

    public string End { get; }

    public string Character
    {
        get => _character;
        set { _character = value; OnPropertyChanged(); }
    }

    public string Phrase { get; }

    public event PropertyChangedEventHandler? PropertyChanged;

    private void OnPropertyChanged([CallerMemberName] string? name = null)
    {
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
    }
}

public class CharacterRow : INotifyPropertyChanged
{
    private bool _isFemale;

    public CharacterRow(string name, bool isFemale)
    {
        Name = name;
        _isFemale = isFemale;
    }

    public string Name { get; }

    public bool IsFemale
    {
        get => _isFemale;
        set
        {
            if (_isFemale == value)
            {
                return;
            }

            _isFemale = value;
            OnPropertyChanged();
            OnPropertyChanged(nameof(GenderLabel));
        }
    }

    public string GenderLabel => IsFemale ? "female" : "male";

    public event PropertyChangedEventHandler? PropertyChanged;

    private void OnPropertyChanged([CallerMemberName] string? name = null)
    {
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
    }
}
