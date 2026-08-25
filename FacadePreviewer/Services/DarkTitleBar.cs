using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;

namespace FacadePreviewer.Services;

/// <summary>Matches a window's native title bar to App.xaml's own dark palette (Bg/Text) instead
/// of leaving Windows' default light caption, which otherwise clashes with the dark content right
/// below it. DWMWA_CAPTION_COLOR/TEXT_COLOR need Windows 11 22000+; DWMWA_USE_IMMERSIVE_DARK_MODE
/// is the older Windows 10-era fallback that only darkens the frame/border, not the exact color --
/// harmless to set both, and DwmSetWindowAttribute itself just no-ops (returns a non-zero HRESULT
/// we ignore) on older Windows builds that don't recognize a given attribute.</summary>
public static class DarkTitleBar
{
    private const int DwmwaUseImmersiveDarkMode = 20;
    private const int DwmwaCaptionColor = 35;
    private const int DwmwaTextColor = 36;

    [DllImport("dwmapi.dll", PreserveSig = true)]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref int value, int valueSize);

    /// <summary>Call from the window's SourceInitialized handler (needs a real HWND).</summary>
    public static void Apply(Window window)
    {
        IntPtr hwnd = new WindowInteropHelper(window).Handle;
        int enabled = 1;
        DwmSetWindowAttribute(hwnd, DwmwaUseImmersiveDarkMode, ref enabled, sizeof(int));

        // COLORREF is 0x00BBGGRR (reversed from the usual 0xAARRGGBB) -- App.xaml's
        // Bg (#14181A) for the caption background, Text (#E7E9E4) for the title text.
        int captionColor = 0x001A1814;
        DwmSetWindowAttribute(hwnd, DwmwaCaptionColor, ref captionColor, sizeof(int));
        int textColor = 0x00E4E9E7;
        DwmSetWindowAttribute(hwnd, DwmwaTextColor, ref textColor, sizeof(int));
    }
}
