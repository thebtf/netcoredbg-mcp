using System.ComponentModel;
using System.Runtime.InteropServices;

namespace WpfSmokeApp;

internal sealed class NativeCalibrationWindow : IDisposable
{
    private const int WmEraseBackground = 0x0014;
    private const int WmPaint = 0x000F;
    private const int SwShow = 5;
    private const int Blackness = 0x00000042;
    private const int WhiteBrush = 0;
    private const uint WsOverlappedWindow = 0x00CF0000;
    private const uint WsPopup = 0x80000000;
    private const uint WsVisible = 0x10000000;
    private const uint CalibrationMarkerColorRef = 0x00DEC012;

    private static readonly WindowProcedure Procedure = Dispatch;
    private static NativeCalibrationWindow? _active;

    private readonly bool _drawMarker;
    private readonly string _className = $"WpfSmokeApp.Calibration.{Environment.ProcessId}";
    private readonly IntPtr _instance = GetModuleHandle(null);
    private bool _classRegistered;
    private bool _disposed;
    private IntPtr _hwnd;

    public NativeCalibrationWindow(bool drawMarker)
    {
        _drawMarker = drawMarker;
        var windowClass = new WindowClass
        {
            cbSize = checked((uint)Marshal.SizeOf<WindowClass>()),
            lpfnWndProc = Marshal.GetFunctionPointerForDelegate(Procedure),
            hInstance = _instance,
            lpszClassName = _className,
        };
        if (RegisterClassEx(ref windowClass) == 0)
            throw new Win32Exception(Marshal.GetLastWin32Error());

        _classRegistered = true;
        var style = _drawMarker ? WsOverlappedWindow : WsPopup;
        _hwnd = CreateWindowEx(
            0,
            _className,
            "WPF Smoke Capture Calibration",
            style | WsVisible,
            180,
            180,
            800,
            600,
            IntPtr.Zero,
            IntPtr.Zero,
            _instance,
            IntPtr.Zero);
        if (_hwnd == IntPtr.Zero)
        {
            var error = Marshal.GetLastWin32Error();
            Dispose();
            throw new Win32Exception(error);
        }

        _active = this;
    }

    public void Show() => ShowWindow(_hwnd, SwShow);

    public void Dispose()
    {
        if (_disposed)
            return;

        _disposed = true;
        try
        {
            if (_hwnd != IntPtr.Zero && IsWindow(_hwnd) && !DestroyWindow(_hwnd))
                throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        finally
        {
            if (ReferenceEquals(_active, this))
                _active = null;
            if (_classRegistered && !UnregisterClass(_className, _instance))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            _classRegistered = false;
        }
    }

    private static IntPtr Dispatch(IntPtr hwnd, int message, IntPtr wParam, IntPtr lParam) =>
        _active?.HandleMessage(hwnd, message, wParam, lParam) ??
        DefWindowProc(hwnd, message, wParam, lParam);

    private IntPtr HandleMessage(IntPtr hwnd, int message, IntPtr wParam, IntPtr lParam)
    {
        if (message == WmEraseBackground)
            return new IntPtr(1);
        if (message == WmPaint)
        {
            Paint(hwnd);
            return IntPtr.Zero;
        }

        return DefWindowProc(hwnd, message, wParam, lParam);
    }

    private void Paint(IntPtr hwnd)
    {
        var hdc = BeginPaint(hwnd, out var paint);
        if (hdc == IntPtr.Zero)
            throw new Win32Exception(Marshal.GetLastWin32Error());

        try
        {
            if (!GetClientRect(hwnd, out var clientRect))
                throw new Win32Exception(Marshal.GetLastWin32Error());

            if (!_drawMarker)
            {
                if (!PatBlt(hdc, 0, 0, clientRect.Right - clientRect.Left,
                        clientRect.Bottom - clientRect.Top, Blackness))
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                return;
            }

            FillRect(hdc, ref clientRect, GetStockObject(WhiteBrush));
            var marker = new NativeRect { Left = 20, Top = 20, Right = 60, Bottom = 60 };
            var markerBrush = CreateSolidBrush(CalibrationMarkerColorRef);
            try
            {
                FillRect(hdc, ref marker, markerBrush);
            }
            finally
            {
                DeleteObject(markerBrush);
            }
        }
        finally
        {
            EndPaint(hwnd, ref paint);
        }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct NativeRect
    {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct WindowClass
    {
        public uint cbSize;
        public uint style;
        public IntPtr lpfnWndProc;
        public int cbClsExtra;
        public int cbWndExtra;
        public IntPtr hInstance;
        public IntPtr hIcon;
        public IntPtr hCursor;
        public IntPtr hbrBackground;
        public string? lpszMenuName;
        public string lpszClassName;
        public IntPtr hIconSm;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PaintStruct
    {
        public IntPtr hdc;
        [MarshalAs(UnmanagedType.Bool)] public bool fErase;
        public NativeRect rcPaint;
        [MarshalAs(UnmanagedType.Bool)] public bool fRestore;
        [MarshalAs(UnmanagedType.Bool)] public bool fIncUpdate;
        [MarshalAs(UnmanagedType.ByValArray, SizeConst = 32)] public byte[] rgbReserved;
    }

    private delegate IntPtr WindowProcedure(IntPtr hwnd, int message, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern ushort RegisterClassEx(ref WindowClass windowClass);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateWindowEx(
        uint exStyle, string className, string windowName, uint style, int x, int y, int width,
        int height, IntPtr parent, IntPtr menu, IntPtr instance, IntPtr param);

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr hwnd, int command);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr DefWindowProc(IntPtr hwnd, int message, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool GetClientRect(IntPtr hwnd, out NativeRect rect);

    [DllImport("user32.dll")]
    private static extern int FillRect(IntPtr hdc, ref NativeRect rect, IntPtr brush);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr BeginPaint(IntPtr hwnd, out PaintStruct paint);

    [DllImport("user32.dll")]
    private static extern bool EndPaint(IntPtr hwnd, ref PaintStruct paint);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool IsWindow(IntPtr hwnd);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool DestroyWindow(IntPtr hwnd);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool UnregisterClass(string className, IntPtr instance);

    [DllImport("gdi32.dll")]
    private static extern IntPtr GetStockObject(int objectType);

    [DllImport("gdi32.dll")]
    private static extern IntPtr CreateSolidBrush(uint colorRef);

    [DllImport("gdi32.dll")]
    private static extern bool DeleteObject(IntPtr objectHandle);

    [DllImport("gdi32.dll", SetLastError = true)]
    private static extern bool PatBlt(IntPtr hdc, int x, int y, int width, int height, int rop);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr GetModuleHandle(string? moduleName);
}
