using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using FlaUI.Core.AutomationElements;
using FlaUI.UIA3;
using Microsoft.Win32;

namespace FlaUIBridge.Commands;

public static class SystemEventCommands
{
    private sealed record RegistryValueSnapshot(object? Value, RegistryValueKind? Kind)
    {
        public bool Exists => Kind.HasValue;
    }

    private const string ThemeChangeEventName = "theme_change";
    private const string PersonalizeRegistryPath = @"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize";
    private const string AppsUseLightThemeValueName = "AppsUseLightTheme";
    private const string SystemUsesLightThemeValueName = "SystemUsesLightTheme";
    private const uint WmSettingChange = 0x001A;
    private const uint SmtoAbortIfHung = 0x0002;
    private static readonly IntPtr HwndBroadcast = new(0xFFFF);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr SendMessageTimeout(
        IntPtr hWnd,
        uint msg,
        UIntPtr wParam,
        string lParam,
        uint fuFlags,
        uint uTimeout,
        out UIntPtr lpdwResult);

    public static JsonNode SendSystemEvent(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var eventName = @params?["event"]?.GetValue<string>()?.Trim().ToLowerInvariant()
            ?? throw new ArgumentException("Missing required parameter: event");

        if (!string.Equals(eventName, ThemeChangeEventName, StringComparison.Ordinal))
        {
            throw new ArgumentException(
                $"Unsupported event '{eventName}'. Supported events: {ThemeChangeEventName}");
        }

        var requestedMode = @params?["mode"]?.GetValue<string>()?.Trim().ToLowerInvariant() ?? "toggle";
        if (requestedMode is not ("light" or "dark" or "toggle"))
        {
            throw new ArgumentException(
                $"Unsupported mode '{requestedMode}'. Supported modes: light, dark, toggle");
        }

        using var existingKey = Registry.CurrentUser.OpenSubKey(PersonalizeRegistryPath, writable: false);
        var keyCreated = existingKey is null;
        using var key = Registry.CurrentUser.CreateSubKey(PersonalizeRegistryPath, writable: true)
            ?? throw new InvalidOperationException($"Failed to open or create registry key: {PersonalizeRegistryPath}");

        if (keyCreated)
        {
            Program.Log($"Created missing registry key: HKCU\\{PersonalizeRegistryPath}");
        }

        var currentMode = ReadCurrentMode(key);
        var targetMode = requestedMode == "toggle"
            ? (currentMode == "dark" ? "light" : "dark")
            : requestedMode;
        var previousAppsTheme = CaptureRegistryValue(key, AppsUseLightThemeValueName);
        var previousSystemTheme = CaptureRegistryValue(key, SystemUsesLightThemeValueName);

        var targetValue = targetMode == "light" ? 1 : 0;
        key.SetValue(AppsUseLightThemeValueName, targetValue, RegistryValueKind.DWord);
        key.SetValue(SystemUsesLightThemeValueName, targetValue, RegistryValueKind.DWord);
        key.Flush();

        var sendResult = SendMessageTimeout(
            HwndBroadcast,
            WmSettingChange,
            UIntPtr.Zero,
            "ImmersiveColorSet",
            SmtoAbortIfHung,
            100,
            out _);

        if (sendResult == IntPtr.Zero)
        {
            var errorCode = Marshal.GetLastWin32Error();
            var broadcastFailure = DescribeBroadcastFailure(errorCode);

            if (TryRestoreThemeValues(
                key,
                previousAppsTheme,
                previousSystemTheme,
                out var rollbackFailure))
            {
                Program.Log(
                    $"theme_change broadcast failed after registry write ({broadcastFailure}); rolled back to {currentMode}");

                return new JsonObject
                {
                    ["event"] = ThemeChangeEventName,
                    ["from"] = currentMode,
                    ["to"] = currentMode,
                    ["attempted_to"] = targetMode,
                    ["warning"] = "broadcast failed",
                    ["rolled_back"] = true
                };
            }

            Program.Log(
                $"theme_change broadcast failed after registry write ({broadcastFailure}); rollback failed: {rollbackFailure}");

            return new JsonObject
            {
                ["ok"] = false,
                ["event"] = ThemeChangeEventName,
                ["from"] = currentMode,
                ["attempted_to"] = targetMode,
                ["rollback_failed"] = true,
                ["error"] = "broadcast failed and registry rollback failed",
                ["broadcast_failure"] = broadcastFailure,
                ["rollback_error"] = rollbackFailure
            };
        }

        Program.Log($"theme_change system event: {currentMode} -> {targetMode}");

        return new JsonObject
        {
            ["event"] = ThemeChangeEventName,
            ["from"] = currentMode,
            ["to"] = targetMode
        };
    }

    private static string ReadCurrentMode(RegistryKey key)
    {
        var currentValue = key.GetValue(AppsUseLightThemeValueName);
        if (currentValue is int intValue)
        {
            return intValue == 0 ? "dark" : "light";
        }

        if (currentValue is long longValue)
        {
            return longValue == 0 ? "dark" : "light";
        }

        return "light";
    }

    private static RegistryValueSnapshot CaptureRegistryValue(RegistryKey key, string valueName)
    {
        try
        {
            return new RegistryValueSnapshot(
                key.GetValue(valueName),
                key.GetValueKind(valueName));
        }
        catch (IOException)
        {
            return new RegistryValueSnapshot(null, null);
        }
    }

    private static bool TryRestoreThemeValues(
        RegistryKey key,
        RegistryValueSnapshot appsTheme,
        RegistryValueSnapshot systemTheme,
        out string rollbackFailure)
    {
        try
        {
            RestoreRegistryValue(key, AppsUseLightThemeValueName, appsTheme);
            RestoreRegistryValue(key, SystemUsesLightThemeValueName, systemTheme);
            key.Flush();
            rollbackFailure = string.Empty;
            return true;
        }
        catch (Exception ex)
        {
            rollbackFailure = ex.Message;
            return false;
        }
    }

    private static void RestoreRegistryValue(
        RegistryKey key,
        string valueName,
        RegistryValueSnapshot snapshot)
    {
        if (!snapshot.Exists)
        {
            key.DeleteValue(valueName, throwOnMissingValue: false);
            return;
        }

        key.SetValue(valueName, snapshot.Value!, snapshot.Kind!.Value);
    }

    private static string DescribeBroadcastFailure(int errorCode)
    {
        if (errorCode == 0)
        {
            return "SendMessageTimeout returned 0 without a Win32 error code";
        }

        return new Win32Exception(errorCode).Message;
    }
}
