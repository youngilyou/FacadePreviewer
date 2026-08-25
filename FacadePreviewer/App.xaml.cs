using System.Windows;
using System.Windows.Interop;
using System.Windows.Media;

namespace FacadePreviewer;

public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        // Same fix as CheckCrackViewer's App.xaml.cs: this machine's WPF GPU
        // rendering doesn't relay over the remote session (renders fine
        // locally, solid black remotely) — force software rendering.
        RenderOptions.ProcessRenderMode = RenderMode.SoftwareOnly;
    }
}
