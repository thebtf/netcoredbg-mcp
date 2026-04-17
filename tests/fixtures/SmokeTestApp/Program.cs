// Smoke test app for netcoredbg-mcp DAP coverage + UI tools testing.
// Each method exercises a specific debugging scenario.
// "gui" scenario launches a WinForms window for UI automation testing.

using System.Windows.Forms;

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

    // Scenario 8: GUI with invoke button, checkbox, nested panels for UI tools testing
    static void GuiScenario()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);

        var form = new Form
        {
            Text = "SmokeTestApp GUI",
            Width = 400,
            Height = 400,
            StartPosition = FormStartPosition.CenterScreen,
        };

        // Panel for scoped search testing (root_id)
        var panel = new Panel
        {
            Name = "settingsPanel",
            Dock = DockStyle.Top,
            Height = 200,
            BorderStyle = BorderStyle.FixedSingle,
        };
        panel.AccessibleName = "settingsPanel";

        // Button for ui_invoke testing
        var invokeBtn = new Button
        {
            Name = "btnInvoke",
            Text = "Invoke Me",
            Location = new System.Drawing.Point(20, 20),
            Width = 120,
            Height = 30,
        };
        invokeBtn.AccessibleName = "btnInvoke";
        var invokeCount = 0;
        invokeBtn.Click += (_, _) =>
        {
            invokeCount++;
            invokeBtn.Text = $"Invoked {invokeCount}x";
        };

        // CheckBox for ui_toggle testing
        var checkBox = new CheckBox
        {
            Name = "chkEnabled",
            Text = "Enable Feature",
            Location = new System.Drawing.Point(20, 60),
            Width = 150,
            Checked = false,
        };
        checkBox.AccessibleName = "chkEnabled";

        // Second button inside panel for scoped search
        var scopedBtn = new Button
        {
            Name = "btnScoped",
            Text = "Scoped Button",
            Location = new System.Drawing.Point(20, 100),
            Width = 120,
            Height = 30,
        };
        scopedBtn.AccessibleName = "btnScoped";

        // TextBox for ui_read_text testing
        var textBox = new TextBox
        {
            Name = "txtOutput",
            Text = "Initial text",
            Location = new System.Drawing.Point(20, 140),
            Width = 200,
            ReadOnly = true,
        };
        textBox.AccessibleName = "txtOutput";

        // Open-second-window button inside the panel — opens a modeless
        // sibling top-level form so the smoke test can verify the full
        // multi-window flow (get_tree -> switch_window -> find_element
        // inside the new window) against a real second top-level window.
        // Form.Show() (not ShowDialog) returns immediately, so the bridge
        // is free to serve subsequent queries.
        Form? secondWindow = null;
        var openSecondBtn = new Button
        {
            Name = "btnOpenSecond",
            Text = "Open Second",
            // Positioned beside scopedBtn (20,100) and outside the column
            // used by actionBtnInside (160,100) to avoid visual overlap.
            Location = new System.Drawing.Point(20, 165),
            Width = 120,
            Height = 30,
        };
        openSecondBtn.AccessibleName = "btnOpenSecond";
        openSecondBtn.Click += (_, _) =>
        {
            if (secondWindow is { IsDisposed: false })
            {
                secondWindow.Activate();
                return;
            }

            // No AccessibleName override — we want the WinForms Text "Create
            // collection" to become the UIA Name so the smoke test can
            // target the window by title (which is how real agents will
            // address modal dialogs).
            secondWindow = new Form
            {
                Text = "Create collection",
                Width = 360,
                Height = 180,
                StartPosition = FormStartPosition.CenterParent,
                FormBorderStyle = FormBorderStyle.FixedDialog,
                MinimizeBox = false,
                MaximizeBox = false,
            };

            var input = new TextBox
            {
                Name = "dlgInput",
                Location = new System.Drawing.Point(20, 20),
                Width = 300,
            };
            input.AccessibleName = "dlgInput";

            var closeBtn = new Button
            {
                Name = "dlgClose",
                Text = "Close",
                Location = new System.Drawing.Point(140, 70),
                Width = 80,
            };
            closeBtn.AccessibleName = "dlgClose";
            closeBtn.Click += (_, _) => secondWindow?.Close();

            secondWindow.FormClosed += (_, _) => secondWindow = null;
            secondWindow.Controls.Add(input);
            secondWindow.Controls.Add(closeBtn);
            secondWindow.Show();
        };

        panel.Controls.AddRange(new Control[] { invokeBtn, checkBox, scopedBtn, textBox, openSecondBtn });

        // Duplicate-named button inside panel for root_id scoping test
        var actionBtnInside = new Button
        {
            Name = "btnAction",
            Text = "Action (Inside)",
            Location = new System.Drawing.Point(160, 100),
            Width = 120,
            Height = 30,
        };
        actionBtnInside.AccessibleName = "btnAction";
        panel.Controls.Add(actionBtnInside);

        // Same-named button outside panel — root_id="settingsPanel" should NOT find this one
        var actionBtnOutside = new Button
        {
            Name = "btnAction",
            Text = "Action (Outside)",
            Location = new System.Drawing.Point(20, 260),
            Width = 120,
            Height = 30,
        };
        actionBtnOutside.AccessibleName = "btnAction";

        // Button outside panel (for disambiguation with scoped search)
        var outerBtn = new Button
        {
            Name = "btnOuter",
            Text = "Outer Button",
            Location = new System.Drawing.Point(20, 220),
            Width = 120,
            Height = 30,
        };
        outerBtn.AccessibleName = "btnOuter";

        // Open File Dialog button for ui_file_dialog testing
        var fileDialogBtn = new Button
        {
            Name = "btnOpenFile",
            Text = "Open File...",
            Location = new System.Drawing.Point(160, 220),
            Width = 120,
            Height = 30,
        };
        fileDialogBtn.AccessibleName = "btnOpenFile";
        fileDialogBtn.Click += (_, _) =>
        {
            using var dialog = new OpenFileDialog
            {
                Title = "Select a file",
                Filter = "All files (*.*)|*.*",
            };
            if (dialog.ShowDialog() == DialogResult.OK)
            {
                textBox.Text = $"Selected: {dialog.FileName}";
            }
        };

        // DataGrid for select/read testing
        var dataGrid = new DataGridView
        {
            Name = "dataGrid",
            Location = new System.Drawing.Point(20, 300),
            Width = 350,
            Height = 120,
            AutoSizeColumnsMode = DataGridViewAutoSizeColumnsMode.Fill,
            SelectionMode = DataGridViewSelectionMode.FullRowSelect,
            MultiSelect = true,
            ReadOnly = true,
            AllowUserToAddRows = false,
        };
        dataGrid.AccessibleName = "dataGrid";
        dataGrid.Columns.Add("Name", "Name");
        dataGrid.Columns.Add("Role", "Role");
        dataGrid.Columns.Add("Level", "Level");
        dataGrid.Rows.Add("Alice", "Developer", "Senior");
        dataGrid.Rows.Add("Bob", "Designer", "Mid");
        dataGrid.Rows.Add("Charlie", "Manager", "Lead");
        dataGrid.Rows.Add("Diana", "Tester", "Junior");
        dataGrid.Rows.Add("Eve", "DevOps", "Senior");

        // Drag-reorder ListBox for ui_drag smoke test (engram #79).
        // Record MouseDown, wait for MouseMove to cross the system drag
        // threshold, then start DoDragDrop so the smoke test proves ui_drag
        // crosses the real platform threshold instead of turning every click
        // into an unconditional drag.
        var dragList = new ListBox
        {
            Name = "dragList",
            Location = new System.Drawing.Point(420, 60),
            Size = new System.Drawing.Size(160, 160),
            AllowDrop = true,
        };
        dragList.AccessibleName = "dragList";
        dragList.Items.AddRange(new object[] { "Alpha", "Beta", "Gamma", "Delta", "Epsilon" });

        int? dragSourceIndex = null;
        System.Drawing.Point? dragStartPos = null;
        dragList.MouseDown += (_, e) =>
        {
            if (e.Button != MouseButtons.Left)
            {
                dragSourceIndex = null;
                dragStartPos = null;
                return;
            }

            var srcIdx = dragList.IndexFromPoint(e.Location);
            dragSourceIndex = srcIdx >= 0 ? srcIdx : null;
            dragStartPos = dragSourceIndex.HasValue ? e.Location : null;
        };
        dragList.MouseMove += (_, e) =>
        {
            if (e.Button != MouseButtons.Left || dragSourceIndex is not int srcIdx || dragStartPos is null)
            {
                return;
            }

            var dragSize = SystemInformation.DragSize;
            var dragBounds = new System.Drawing.Rectangle(
                dragStartPos.Value.X - (dragSize.Width / 2),
                dragStartPos.Value.Y - (dragSize.Height / 2),
                dragSize.Width,
                dragSize.Height);

            if (dragBounds.Contains(e.Location))
            {
                return;
            }

            dragStartPos = null;
            dragList.DoDragDrop(dragList.Items[srcIdx], DragDropEffects.Move);
        };
        dragList.MouseUp += (_, _) =>
        {
            dragSourceIndex = null;
            dragStartPos = null;
        };
        dragList.DragEnter += (_, e) =>
        {
            if (e.Data?.GetDataPresent(typeof(string)) == true)
                e.Effect = DragDropEffects.Move;
        };
        dragList.DragOver += (_, e) =>
        {
            if (e.Data?.GetDataPresent(typeof(string)) == true)
                e.Effect = DragDropEffects.Move;
        };
        dragList.DragDrop += (sender, e) =>
        {
            var pt = dragList.PointToClient(new System.Drawing.Point(e.X, e.Y));
            var dstIdx = dragList.IndexFromPoint(pt);
            if (dragSourceIndex is int srcIdx && srcIdx >= 0 && dstIdx >= 0 && dstIdx != srcIdx)
            {
                var item = dragList.Items[srcIdx];
                dragList.Items.RemoveAt(srcIdx);
                if (dstIdx > srcIdx)
                {
                    dstIdx -= 1;
                }
                dragList.Items.Insert(dstIdx, item);
            }
            dragSourceIndex = null;
            dragStartPos = null;
        };

        // Multi-select ListBox for ui_hold_modifiers smoke test (engram #81).
        // SelectionMode=MultiExtended enables Ctrl+click discontiguous selection,
        // which requires persistent Ctrl-hold across discrete click calls.
        var multiSelectList = new ListBox
        {
            Name = "multiList",
            Location = new System.Drawing.Point(420, 240),
            Size = new System.Drawing.Size(160, 160),
            SelectionMode = SelectionMode.MultiExtended,
        };
        multiSelectList.AccessibleName = "multiList";
        multiSelectList.Items.AddRange(new object[] { "One", "Two", "Three", "Four", "Five" });

        form.Width = 620;
        form.Height = 470;
        form.Controls.Add(panel);
        form.Controls.Add(outerBtn);
        form.Controls.Add(actionBtnOutside);
        form.Controls.Add(fileDialogBtn);
        form.Controls.Add(dataGrid);
        form.Controls.Add(dragList);
        form.Controls.Add(multiSelectList);

        Console.WriteLine("GUI scenario started. Close the window to exit.");
        Application.Run(form);
    }

    [STAThread]
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

            case "gui":
                GuiScenario();
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
