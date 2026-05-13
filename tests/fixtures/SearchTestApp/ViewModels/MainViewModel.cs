namespace SearchTestApp.ViewModels;

public sealed class MainViewModel
{
    private int _loadCount;

    public string Phrase { get; private set; } = "ready";
    public string ActiveControlName => "CueInputPanel";

    public void LoadAssignedCharacter()
    {
        _loadCount++;
        Phrase = "assigned";
    }
}
