using System.Linq;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace FacadePreviewer;

/// <summary>App-themed replacement for <see cref="MessageBox"/> -- the native MessageBox always
/// renders as a plain light-themed Windows common dialog regardless of the app's own dark
/// styling, which looks visually inconsistent next to it. This is a plain WPF Window instead, so
/// it automatically picks up App.xaml's global Window/Button styles (dark background, flat
/// buttons, app font) with no extra styling needed here.</summary>
public partial class ThemedDialog : Window
{
    public string? ResultKey { get; private set; }

    private ThemedDialog()
    {
        InitializeComponent();
    }

    /// <summary>Shows a themed dialog with custom-labeled buttons and returns the Key of whichever
    /// one was clicked, or null if the dialog was dismissed without clicking one (Alt+F4, the X
    /// button, Escape) -- callers should treat null the same as the safest/most conservative
    /// choice (e.g. "abort"), never the same as an explicit affirmative choice.</summary>
    public static string? Show(Window? owner, string title, string message, Brush titleColor,
        params (string Key, string Label, bool IsDefault)[] buttons)
    {
        var dlg = new ThemedDialog();
        if (owner != null)
        {
            dlg.Owner = owner;
            dlg.WindowStartupLocation = WindowStartupLocation.CenterOwner;
        }
        dlg.TitleText.Text = title;
        dlg.TitleText.Foreground = titleColor;
        dlg.MessageText.Text = message;

        Button? defaultButton = null;
        foreach (var (key, label, isDefault) in buttons)
        {
            var button = new Button
            {
                Content = label,
                Margin = new Thickness(8, 0, 0, 0),
                Padding = new Thickness(16, 6, 16, 6),
                MinWidth = 96,
                IsDefault = isDefault,
            };
            button.Click += (_, _) =>
            {
                dlg.ResultKey = key;
                dlg.Close();
            };
            dlg.ButtonsPanel.Children.Add(button);
            if (isDefault)
                defaultButton = button;
        }

        dlg.Loaded += (_, _) => (defaultButton ?? dlg.ButtonsPanel.Children.OfType<Button>().LastOrDefault())?.Focus();
        dlg.ShowDialog();
        return dlg.ResultKey;
    }

    /// <summary>Single-button "확인" info/error dialog -- the ThemedDialog equivalent of
    /// MessageBox.Show(..., MessageBoxButton.OK, ...).</summary>
    public static void ShowInfo(Window? owner, string title, string message, Brush titleColor)
    {
        Show(owner, title, message, titleColor, ("ok", "확인", true));
    }

    /// <summary>Yes/No confirmation dialog -- returns true only for an explicit "예" click. A "아니오"
    /// click OR dismissing without clicking either button (Alt+F4/X/Escape) both return false, per
    /// Show's own guidance to treat "no click" as the safest/most conservative choice.</summary>
    public static bool ShowConfirm(Window? owner, string title, string message, Brush titleColor,
        string yesLabel = "예", string noLabel = "아니오")
    {
        return Show(owner, title, message, titleColor, ("yes", yesLabel, true), ("no", noLabel, false)) == "yes";
    }
}
