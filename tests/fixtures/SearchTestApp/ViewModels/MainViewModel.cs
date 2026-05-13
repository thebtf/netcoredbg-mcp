namespace SearchTestApp.ViewModels;

public sealed class MainViewModel
{
    public string Phrase { get; private set; } = "ready";

    public void LoadAssignedCharacter()
    {
        Phrase = "assigned";
    }
}
