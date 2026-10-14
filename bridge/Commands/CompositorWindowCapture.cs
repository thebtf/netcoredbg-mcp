using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using Windows.Foundation;
using Windows.Foundation.Metadata;
using Windows.Graphics;
using Windows.Graphics.Capture;
using Windows.Graphics.DirectX;
using Windows.Graphics.DirectX.Direct3D11;
using Windows.Graphics.Imaging;
using WinRT;

namespace FlaUIBridge.Commands;

internal static class CompositorWindowCapture
{
    private static readonly Guid CaptureInteropId = new("3628E81B-3CAC-4C60-B7F4-23CE0E0C3356");
    private static readonly Guid CaptureItemId = new("79C3F95B-31F7-4EC2-A464-632EF5D30760");
    private static readonly Guid DxgiDeviceId = new("54EC77FA-1377-44E6-8C32-88FD5F44C84C");
    private static readonly Guid MemoryByteAccessId = new("5B0D3235-4DBA-4D44-865E-8F1D0E4FD04D");
    private const int AcquisitionDeadlineMilliseconds = 3_000;

    [DllImport("d3d11.dll", ExactSpelling = true)]
    private static extern int D3D11CreateDevice(
        IntPtr adapter, uint driverType, IntPtr software, uint flags,
        IntPtr featureLevels, uint featureLevelCount, uint sdkVersion,
        out IntPtr device, out uint featureLevel, out IntPtr immediateContext);

    [DllImport("d3d11.dll", ExactSpelling = true)]
    private static extern int CreateDirect3D11DeviceFromDXGIDevice(IntPtr device, out IntPtr projectedDevice);

    [DllImport("combase.dll", ExactSpelling = true)]
    private static extern int RoInitialize(uint initializationType);

    [DllImport("combase.dll", ExactSpelling = true)]
    private static extern void RoUninitialize();

    [DllImport("combase.dll", ExactSpelling = true, CharSet = CharSet.Unicode)]
    private static extern int WindowsCreateString(string value, uint length, out IntPtr hstring);

    [DllImport("combase.dll", ExactSpelling = true)]
    private static extern int WindowsDeleteString(IntPtr hstring);

    [DllImport("combase.dll", ExactSpelling = true)]
    private static extern int RoGetActivationFactory(IntPtr className, ref Guid interfaceId, out IntPtr factory);

    internal static CompositorCapture Capture(
        IntPtr hwnd, CaptureSnapshot expected, IScreenshotCaptureTransport transport,
        CancellationToken cancellationToken,
        Action<IntPtr, Func<bool>>? beforeAcceptanceForTesting = null)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (!OperatingSystem.IsWindowsVersionAtLeast(10, 0, 18362))
            throw new PlatformNotSupportedException("Window compositor capture requires Windows 10 build 18362 or later.");
        var initialization = RoInitialize(1);
        if (initialization < 0 && initialization != unchecked((int)0x80010106))
            Marshal.ThrowExceptionForHR(initialization);
        try
        {
            return CaptureInitialized(hwnd, expected, transport, cancellationToken, beforeAcceptanceForTesting);
        }
        finally
        {
            if (initialization >= 0)
                RoUninitialize();
        }
    }

    private static CompositorCapture CaptureInitialized(
        IntPtr hwnd, CaptureSnapshot expected, IScreenshotCaptureTransport transport,
        CancellationToken cancellationToken, Action<IntPtr, Func<bool>>? beforeAcceptanceForTesting)
    {
        if (!ApiInformation.IsTypePresent("Windows.Graphics.Capture.GraphicsCaptureItem") ||
            !ApiInformation.IsMethodPresent("Windows.Graphics.Capture.Direct3D11CaptureFramePool", "CreateFreeThreaded") ||
            !GraphicsCaptureSession.IsSupported())
            throw new PlatformNotSupportedException("Window compositor capture is unavailable on this system.");
        ScreenshotCommands.ValidateCaptureDimensions(expected.RasterWidth, expected.RasterHeight);
        EnsureTarget(hwnd, expected, transport);
        using var process = Process.GetProcessById(checked((int)expected.ProcessId));
        _ = process.SafeHandle;
        if (process.HasExited || process.StartTime.ToUniversalTime().Ticks != expected.ProcessCreationTicks ||
            checked((uint)process.SessionId) != expected.SessionId)
            throw new InvalidOperationException("Compositor capture process identity changed.");
        var stopwatch = Stopwatch.StartNew();
        CompositorCapture? result = null;
        try
        {
            using var device = CreateDevice();
            var item = CreateWindowItem(hwnd);
            var closed = 0;
            TypedEventHandler<GraphicsCaptureItem, object> onClosed = (_, _) => Interlocked.Exchange(ref closed, 1);
            try
            {
                item.Closed += onClosed;
                var itemSize = item.Size;
                EnsureSize(itemSize, expected);
                EnsureTarget(hwnd, expected, transport);
                CheckWait(stopwatch, cancellationToken, Volatile.Read(ref closed));
                using var pool = Direct3D11CaptureFramePool.CreateFreeThreaded(
                    device, DirectXPixelFormat.B8G8R8A8UIntNormalized, 1, itemSize);
                using var session = pool.CreateCaptureSession(item);
                if (ApiInformation.IsPropertyPresent("Windows.Graphics.Capture.GraphicsCaptureSession", "IsCursorCaptureEnabled"))
                    session.IsCursorCaptureEnabled = false;
                session.StartCapture();
                Direct3D11CaptureFrame? frame = null;
                while (frame is null)
                {
                    CheckWait(stopwatch, cancellationToken, Volatile.Read(ref closed));
                    frame = pool.TryGetNextFrame();
                    if (frame is null)
                        Thread.Sleep(10);
                }

                using (frame)
                {
                    CheckWait(stopwatch, cancellationToken, Volatile.Read(ref closed));
                    var contentSize = frame.ContentSize;
                    EnsureSize(contentSize, expected);
                    EnsureSize(item.Size, expected);
                    var frameTimeTicks = frame.SystemRelativeTime.Ticks;
                    if (frameTimeTicks <= 0)
                        throw new InvalidOperationException("Compositor frame has no positive native timestamp.");
                    EnsureTarget(hwnd, expected, transport);
                    var surface = frame.Surface;
                    try
                    {
                        using var softwareBitmap = Readback(surface, stopwatch, cancellationToken, ref closed);
                        CheckWait(stopwatch, cancellationToken, Volatile.Read(ref closed));
                        EnsureTarget(hwnd, expected, transport);
                        EnsureSize(item.Size, expected);
                        if (process.HasExited)
                            throw new InvalidOperationException("Compositor capture process exited.");
                        var bitmap = CopyBitmap(softwareBitmap, expected);
                        try
                        {
                            EnsureTarget(hwnd, expected, transport);
                            CheckWait(stopwatch, cancellationToken, Volatile.Read(ref closed));
                            result = new CompositorCapture(bitmap, frameTimeTicks,
                                itemSize.Width, itemSize.Height, contentSize.Width, contentSize.Height, false);
                        }
                        catch
                        {
                            bitmap.Dispose();
                            throw;
                        }
                    }
                    finally
                    {
                        ((IWinRTObject)surface).NativeObject.Dispose();
                    }
                }
                if (beforeAcceptanceForTesting is not null)
                    beforeAcceptanceForTesting(hwnd, () => Volatile.Read(ref closed) != 0);
                cancellationToken.ThrowIfCancellationRequested();
                EnsureTarget(hwnd, expected, transport);
                if (Volatile.Read(ref closed) != 0)
                    throw new InvalidOperationException("Compositor capture item closed during final target validation.");
            }
            finally
            {
                try
                {
                    item.Closed -= onClosed;
                }
                finally
                {
                    ((IWinRTObject)item).NativeObject.Dispose();
                }
            }
            return result ?? throw new InvalidOperationException("Compositor capture did not return a frame.");
        }
        catch
        {
            result?.Dispose();
            throw;
        }
    }

    private static void EnsureTarget(
        IntPtr hwnd, CaptureSnapshot expected, IScreenshotCaptureTransport transport)
    {
        if (expected.Hwnd != hwnd.ToInt64() || transport.ReadSnapshot(hwnd) != expected)
            throw new InvalidOperationException("Compositor capture target identity or physical geometry changed.");
        if (transport.ReadCaptureAffinity(hwnd) != 0)
            throw new InvalidOperationException("Compositor capture policy forbids this window.");
    }

    private static void EnsureSize(SizeInt32 size, CaptureSnapshot expected)
    {
        if (size.Width != expected.RasterWidth || size.Height != expected.RasterHeight)
            throw new InvalidOperationException("Compositor item/frame size differs from the physical window bounds.");
    }

    private static void CheckWait(Stopwatch stopwatch, CancellationToken cancellationToken, int closed)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (closed != 0)
            throw new InvalidOperationException("Compositor capture item closed.");
        if (stopwatch.ElapsedMilliseconds >= AcquisitionDeadlineMilliseconds)
            throw new TimeoutException("Compositor capture exceeded its 3-second acquisition deadline.");
    }

    private static SoftwareBitmap Readback(
        IDirect3DSurface surface, Stopwatch stopwatch, CancellationToken cancellationToken, ref int closed)
    {
        var operation = SoftwareBitmap.CreateCopyFromSurfaceAsync(surface, BitmapAlphaMode.Premultiplied);
        var resultRetrieved = false;
        try
        {
            while (operation.Status == AsyncStatus.Started)
            {
                CheckWait(stopwatch, cancellationToken, Volatile.Read(ref closed));
                Thread.Sleep(10);
            }
            var bitmap = operation.GetResults();
            resultRetrieved = true;
            try
            {
                CheckWait(stopwatch, cancellationToken, Volatile.Read(ref closed));
                return bitmap;
            }
            catch
            {
                bitmap.Dispose();
                throw;
            }
        }
        catch (Exception captureFailure)
        {
            if (!resultRetrieved)
            {
                Exception? cancellationFailure = null;
                if (operation.Status == AsyncStatus.Started)
                {
                    try
                    {
                        operation.Cancel();
                    }
                    catch (Exception ex)
                    {
                        cancellationFailure = ex;
                    }
                }
                // Cancellation is a request: retain the surface/frame/device until native copy is terminal.
                // The existing bridge process owner is the force boundary if this drain never completes.
                while (operation.Status == AsyncStatus.Started)
                    Thread.Sleep(10);
                if (operation.Status == AsyncStatus.Completed)
                    operation.GetResults().Dispose();
                if (operation.Status == AsyncStatus.Error && operation.ErrorCode is Exception drainFailure)
                    throw new AggregateException("Compositor capture failed while draining readback.", captureFailure, drainFailure);
                if (cancellationFailure is not null)
                    throw new AggregateException("Compositor readback cancellation failed.", captureFailure, cancellationFailure);
            }
            throw;
        }
        finally
        {
            operation.Close();
        }
    }

    private static IDirect3DDevice CreateDevice()
    {
        IntPtr device = IntPtr.Zero;
        IntPtr context = IntPtr.Zero;
        IntPtr dxgi = IntPtr.Zero;
        IntPtr projected = IntPtr.Zero;
        try
        {
            Marshal.ThrowExceptionForHR(D3D11CreateDevice(IntPtr.Zero, 1, IntPtr.Zero, 0x20,
                IntPtr.Zero, 0, 7, out device, out _, out context));
            if (device == IntPtr.Zero)
                throw new InvalidOperationException("D3D11 did not return a device.");
            var interfaceId = DxgiDeviceId;
            Marshal.ThrowExceptionForHR(Marshal.QueryInterface(device, ref interfaceId, out dxgi));
            if (dxgi == IntPtr.Zero)
                throw new InvalidOperationException("D3D11 did not expose its DXGI device.");
            Marshal.ThrowExceptionForHR(CreateDirect3D11DeviceFromDXGIDevice(dxgi, out projected));
            if (projected == IntPtr.Zero)
                throw new InvalidOperationException("D3D11 did not return a WinRT device.");
            return MarshalInterface<IDirect3DDevice>.FromAbi(projected);
        }
        finally
        {
            if (projected != IntPtr.Zero) Marshal.Release(projected);
            if (dxgi != IntPtr.Zero) Marshal.Release(dxgi);
            if (context != IntPtr.Zero) Marshal.Release(context);
            if (device != IntPtr.Zero) Marshal.Release(device);
        }
    }

    private static unsafe GraphicsCaptureItem CreateWindowItem(IntPtr hwnd)
    {
        const string className = "Windows.Graphics.Capture.GraphicsCaptureItem";
        IntPtr name = IntPtr.Zero;
        IntPtr factory = IntPtr.Zero;
        IntPtr item = IntPtr.Zero;
        try
        {
            Marshal.ThrowExceptionForHR(WindowsCreateString(className, (uint)className.Length, out name));
            var factoryId = CaptureInteropId;
            Marshal.ThrowExceptionForHR(RoGetActivationFactory(name, ref factoryId, out factory));
            if (factory == IntPtr.Zero)
                throw new InvalidOperationException("The window capture activation factory is unavailable.");
            var itemId = CaptureItemId;
            var createForWindow = (delegate* unmanaged[Stdcall]<IntPtr, IntPtr, Guid*, IntPtr*, int>)(*(IntPtr**)factory)[3];
            Marshal.ThrowExceptionForHR(createForWindow(factory, hwnd, &itemId, &item));
            if (item == IntPtr.Zero)
                throw new InvalidOperationException("CreateForWindow did not return a capture item.");
            return GraphicsCaptureItem.FromAbi(item);
        }
        finally
        {
            if (item != IntPtr.Zero) Marshal.Release(item);
            if (factory != IntPtr.Zero) Marshal.Release(factory);
            if (name != IntPtr.Zero) Marshal.ThrowExceptionForHR(WindowsDeleteString(name));
        }
    }

    private static unsafe Bitmap CopyBitmap(SoftwareBitmap source, CaptureSnapshot expected)
    {
        if (source.BitmapPixelFormat != BitmapPixelFormat.Bgra8 ||
            source.BitmapAlphaMode != BitmapAlphaMode.Premultiplied ||
            source.PixelWidth != expected.RasterWidth || source.PixelHeight != expected.RasterHeight)
            throw new InvalidOperationException("Compositor readback format or dimensions are invalid.");
        using var buffer = source.LockBuffer(BitmapBufferAccessMode.Read);
        using var reference = buffer.CreateReference();
        var plane = buffer.GetPlaneDescription(0);
        var rowBytes = checked(expected.RasterWidth * 4);
        if (buffer.GetPlaneCount() != 1 || plane.Width != expected.RasterWidth ||
            plane.Height != expected.RasterHeight || plane.StartIndex < 0 || Math.Abs((long)plane.Stride) < rowBytes)
            throw new InvalidOperationException("Compositor readback plane is invalid.");
        var interfaceId = MemoryByteAccessId;
        IntPtr access = IntPtr.Zero;
        try
        {
            Marshal.ThrowExceptionForHR(Marshal.QueryInterface(
                ((IWinRTObject)reference).NativeObject.ThisPtr, ref interfaceId, out access));
            if (access == IntPtr.Zero)
                throw new InvalidOperationException("Compositor readback byte access is unavailable.");
            byte* pixels = null;
            uint capacity = 0;
            var getBuffer = (delegate* unmanaged[Stdcall]<IntPtr, byte**, uint*, int>)(*(IntPtr**)access)[3];
            Marshal.ThrowExceptionForHR(getBuffer(access, &pixels, &capacity));
            var lastRow = checked((long)plane.StartIndex + (long)(plane.Height - 1) * plane.Stride);
            var firstByte = Math.Min(plane.StartIndex, lastRow);
            var endByte = checked(Math.Max(plane.StartIndex, lastRow) + rowBytes);
            if (pixels == null || firstByte < 0 || endByte > capacity)
                throw new InvalidOperationException("Compositor readback exceeds its native buffer capacity.");
            var bitmap = new Bitmap(expected.RasterWidth, expected.RasterHeight, PixelFormat.Format32bppPArgb);
            try
            {
                var target = bitmap.LockBits(new Rectangle(0, 0, bitmap.Width, bitmap.Height),
                    ImageLockMode.WriteOnly, PixelFormat.Format32bppPArgb);
                try
                {
                    if (target.Scan0 == IntPtr.Zero || Math.Abs((long)target.Stride) < rowBytes)
                        throw new InvalidOperationException("Compositor destination stride is invalid.");
                    for (var y = 0; y < bitmap.Height; y++)
                    {
                        var offset = checked((long)plane.StartIndex + (long)y * plane.Stride);
                        var destination = (byte*)target.Scan0 + checked((long)y * target.Stride);
                        Buffer.MemoryCopy(pixels + offset, destination, Math.Abs((long)target.Stride), rowBytes);
                    }
                }
                finally
                {
                    bitmap.UnlockBits(target);
                }
                return bitmap;
            }
            catch
            {
                bitmap.Dispose();
                throw;
            }
        }
        finally
        {
            if (access != IntPtr.Zero) Marshal.Release(access);
        }
    }
}
