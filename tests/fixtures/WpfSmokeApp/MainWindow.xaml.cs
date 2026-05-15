using System.Collections.ObjectModel;
using System.ComponentModel;
using System.IO;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using Microsoft.Win32;

namespace WpfSmokeApp;

public partial class MainWindow : Window
{
    private readonly MainViewModel _viewModel = new();
    private readonly Stack<Action> _undoStack = new();
    private readonly string? _mutableFile;
    private int _dataGridAnchorIndex;
    private int _dataGridCurrentIndex;
    private Point? _dragStartPoint;
    private CueRow? _dragSourceRow;
    private string _lastEdgeScrollDirection = "none";
    private int _lastEdgeScrollFirstVisible = -1;
    private int _lastEdgeScrollLastVisible = -1;
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

    private void CueDataGrid_PreviewMouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        _dragStartPoint = e.GetPosition(CueDataGrid);
        _dragSourceRow = FindCueRowFromEventSource(e.OriginalSource);
        ResetEdgeScrollEvidence();
    }

    private void CueDataGrid_PreviewMouseMove(object sender, MouseEventArgs e)
    {
        if (e.LeftButton != MouseButtonState.Pressed ||
            _dragStartPoint is not { } startPoint ||
            _dragSourceRow is not { } sourceRow)
        {
            return;
        }

        var currentPoint = e.GetPosition(CueDataGrid);
        if (Math.Abs(currentPoint.X - startPoint.X) < SystemParameters.MinimumHorizontalDragDistance &&
            Math.Abs(currentPoint.Y - startPoint.Y) < SystemParameters.MinimumVerticalDragDistance)
        {
            return;
        }

        DragDrop.DoDragDrop(CueDataGrid, sourceRow, DragDropEffects.Move);
        _dragStartPoint = null;
        _dragSourceRow = null;
    }

    private void CueDataGrid_DragOver(object sender, DragEventArgs e)
    {
        if (e.Data.GetData(typeof(CueRow)) is not CueRow sourceRow)
        {
            return;
        }

        e.Effects = DragDropEffects.Move;
        var direction = ScrollCueGridNearEdge(e.GetPosition(CueDataGrid));
        if (direction is null)
        {
            return;
        }

        var (firstVisible, lastVisible) = GetVisibleCueRange();
        _lastEdgeScrollDirection = direction;
        _lastEdgeScrollFirstVisible = firstVisible;
        _lastEdgeScrollLastVisible = lastVisible;
        _viewModel.StatusText =
            $"WpfWorkflow DragEdgeScroll direction={direction} sourceIdentity={sourceRow.Phrase} firstVisible={firstVisible} lastVisible={lastVisible}";
        Console.WriteLine(_viewModel.StatusText);
        e.Handled = true;
    }

    private void CueDataGrid_Drop(object sender, DragEventArgs e)
    {
        if (e.Data.GetData(typeof(CueRow)) is not CueRow sourceRow)
        {
            return;
        }

        var targetRow = FindCueRowFromEventSource(e.OriginalSource);
        if (targetRow is null)
        {
            _viewModel.StatusText = $"WpfWorkflow DragReorder blocked sourceIdentity={sourceRow.Phrase} targetIdentity=<none>";
            Console.WriteLine(_viewModel.StatusText);
            return;
        }

        MoveCueRow(sourceRow, targetRow);
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

    private void MoveCueRow(CueRow sourceRow, CueRow targetRow)
    {
        var rows = _viewModel.CueRows;
        var sourceIndex = rows.IndexOf(sourceRow);
        var targetIndex = rows.IndexOf(targetRow);
        if (sourceIndex < 0 || targetIndex < 0)
        {
            _viewModel.StatusText =
                $"WpfWorkflow DragReorder blocked sourceIdentity={sourceRow.Phrase} targetIdentity={targetRow.Phrase}";
            Console.WriteLine(_viewModel.StatusText);
            return;
        }

        if (sourceIndex != targetIndex)
        {
            rows.Move(sourceIndex, targetIndex);
        }

        var edgeScrollDirection = _lastEdgeScrollDirection;
        var edgeFirstVisible = _lastEdgeScrollFirstVisible;
        var edgeLastVisible = _lastEdgeScrollLastVisible;
        ResetEdgeScrollEvidence();

        CueDataGrid.SelectedItem = sourceRow;
        CueDataGrid.ScrollIntoView(sourceRow);
        var orderFingerprint = string.Join(">", rows.Select(row => row.Phrase));
        _viewModel.StatusText =
            $"WpfWorkflow DragReorder sourceIdentity={sourceRow.Phrase} targetIdentity={targetRow.Phrase} edgeScrollDirection={edgeScrollDirection} edgeFirstVisible={edgeFirstVisible} edgeLastVisible={edgeLastVisible} orderFingerprint={orderFingerprint}";
        Console.WriteLine(_viewModel.StatusText);
        WriteMutableState($"drag-reorder={sourceRow.Phrase}->{targetRow.Phrase};order={orderFingerprint}");
    }

    private string? ScrollCueGridNearEdge(Point point)
    {
        var scrollViewer = FindVisualChild<ScrollViewer>(CueDataGrid);
        if (scrollViewer is null || CueDataGrid.ActualHeight <= 0)
        {
            return null;
        }

        var bottomEdgeThreshold = Math.Max(20.0, CueDataGrid.ActualHeight * 0.18);
        var topEdgeThreshold = Math.Max(48.0, CueDataGrid.ActualHeight * 0.32);
        if (point.Y >= CueDataGrid.ActualHeight - bottomEdgeThreshold &&
            scrollViewer.VerticalOffset < scrollViewer.ScrollableHeight)
        {
            scrollViewer.ScrollToVerticalOffset(
                Math.Min(scrollViewer.ScrollableHeight, scrollViewer.VerticalOffset + 1));
            CueDataGrid.UpdateLayout();
            return "down";
        }

        if (point.Y <= topEdgeThreshold && scrollViewer.VerticalOffset > 0)
        {
            scrollViewer.ScrollToVerticalOffset(Math.Max(0, scrollViewer.VerticalOffset - 1));
            CueDataGrid.UpdateLayout();
            return "up";
        }

        return null;
    }

    private (int First, int Last) GetVisibleCueRange()
    {
        var first = -1;
        var last = -1;
        for (var index = 0; index < CueDataGrid.Items.Count; index++)
        {
            if (CueDataGrid.ItemContainerGenerator.ContainerFromIndex(index) is not DataGridRow row)
            {
                continue;
            }

            try
            {
                var bounds = row
                    .TransformToAncestor(CueDataGrid)
                    .TransformBounds(new Rect(new Size(row.ActualWidth, row.ActualHeight)));
                if (bounds.Bottom < 0 || bounds.Top > CueDataGrid.ActualHeight)
                {
                    continue;
                }
            }
            catch (InvalidOperationException)
            {
                continue;
            }

            if (first < 0)
            {
                first = index;
            }
            last = index;
        }

        return (first, last);
    }

    private void ResetEdgeScrollEvidence()
    {
        _lastEdgeScrollDirection = "none";
        _lastEdgeScrollFirstVisible = -1;
        _lastEdgeScrollLastVisible = -1;
    }

    private static T? FindVisualChild<T>(DependencyObject parent)
        where T : DependencyObject
    {
        for (var index = 0; index < VisualTreeHelper.GetChildrenCount(parent); index++)
        {
            var child = VisualTreeHelper.GetChild(parent, index);
            if (child is T typed)
            {
                return typed;
            }

            var descendant = FindVisualChild<T>(child);
            if (descendant is not null)
            {
                return descendant;
            }
        }

        return null;
    }

    private CueRow? FindCueRowFromEventSource(object source)
    {
        var current = source as DependencyObject;
        while (current is not null)
        {
            if (current is DataGridRow { Item: CueRow cueRow })
            {
                return cueRow;
            }

            current = VisualTreeHelper.GetParent(current);
        }

        return null;
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
        new("00:00:10.0", "00:00:12.0", "Narrator", "Fixture cue four"),
        new("00:00:13.0", "00:00:15.0", "Narrator", "Fixture cue five"),
        new("00:00:16.0", "00:00:18.0", "Narrator", "Fixture cue six"),
        new("00:00:19.0", "00:00:21.0", "Narrator", "Fixture cue seven"),
        new("00:00:22.0", "00:00:24.0", "Narrator", "Fixture cue eight"),
        new("00:00:25.0", "00:00:27.0", "Narrator", "Fixture cue nine"),
        new("00:00:28.0", "00:00:30.0", "Narrator", "Fixture cue ten"),
        new("00:00:31.0", "00:00:33.0", "Narrator", "Fixture cue eleven"),
        new("00:00:34.0", "00:00:36.0", "Narrator", "Fixture cue twelve"),
        new("00:00:37.0", "00:00:39.0", "Narrator", "Fixture cue thirteen"),
        new("00:00:40.0", "00:00:42.0", "Narrator", "Fixture cue fourteen"),
        new("00:00:43.0", "00:00:45.0", "Narrator", "Fixture cue fifteen"),
        new("00:00:46.0", "00:00:48.0", "Narrator", "Fixture cue sixteen"),
        new("00:00:49.0", "00:00:51.0", "Narrator", "Fixture cue seventeen"),
        new("00:00:52.0", "00:00:54.0", "Narrator", "Fixture cue eighteen"),
        new("00:00:55.0", "00:00:57.0", "Narrator", "Fixture cue nineteen"),
        new("00:00:58.0", "00:00:60.0", "Narrator", "Fixture cue twenty"),
        new("00:00:61.0", "00:00:63.0", "Narrator", "Fixture cue twenty-one"),
        new("00:00:64.0", "00:00:66.0", "Narrator", "Fixture cue twenty-two"),
        new("00:00:67.0", "00:00:69.0", "Narrator", "Fixture cue twenty-three"),
        new("00:00:70.0", "00:00:72.0", "Narrator", "Fixture cue twenty-four"),
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
