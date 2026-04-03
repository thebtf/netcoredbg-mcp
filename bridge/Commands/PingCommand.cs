using System.Text.Json.Nodes;
using FlaUI.Core.AutomationElements;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class PingCommand
{
    public static JsonNode Handle(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        return new JsonObject
        {
            ["pong"] = true,
            ["version"] = "1.0.0"
        };
    }
}
