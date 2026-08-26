using System.Windows;
using System.Windows.Input;
using FacadePreviewer.Services;
using FacadePreviewer.ViewModels;

namespace FacadePreviewer;

public partial class LoginWindow : Window
{
    public LoginViewModel ViewModel { get; }

    public LoginWindow()
    {
        InitializeComponent();
        SourceInitialized += (_, _) => DarkTitleBar.Apply(this);
        ViewModel = new LoginViewModel();
        DataContext = ViewModel;
        ViewModel.LoginSucceeded += () =>
        {
            DialogResult = true;
            Close();
        };
    }

    private void PasswordInput_PasswordChanged(object sender, RoutedEventArgs e)
    {
        if (DataContext is LoginViewModel vm)
            vm.Password = PasswordInput.Password;
        PasswordPlaceholder.Visibility = string.IsNullOrEmpty(PasswordInput.Password)
            ? Visibility.Visible
            : Visibility.Collapsed;
    }

    // IsDefault on the 로그인 button should already catch Enter, but PasswordBox focus
    // doesn't always propagate it reliably in WPF -- explicit fallback (same as CheckCrackViewer's
    // LoginWindow.xaml.cs).
    private void PasswordInput_KeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key != Key.Enter || DataContext is not LoginViewModel vm)
            return;
        if (vm.LoginCommand.CanExecute(null))
            vm.LoginCommand.Execute(null);
    }
}
