using System.Threading.Tasks;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using FacadePreviewer.Models;
using FacadePreviewer.Services;

namespace FacadePreviewer.ViewModels;

/// <summary>Backs LoginWindow -- shown before MainWindow (see App.xaml.cs). Mirrors
/// CheckCrackViewer's LoginViewModel.cs (same UserStore/BCrypt pattern, own separate
/// database), with one difference: StayLoggedIn isn't just "remember my username" -- when
/// checked, App.xaml.cs skips LoginWindow entirely on the next launch (see
/// LoginPreferencesStore's own doc comment).</summary>
public partial class LoginViewModel : ObservableObject
{
    [ObservableProperty] private string _username = "";
    [ObservableProperty] private string _password = "";
    [ObservableProperty] private string _statusText = "";
    [ObservableProperty] private bool _isBusy;
    [ObservableProperty] private bool _stayLoggedIn;

    public AppUser? LoggedInUser { get; private set; }
    public event Action? LoginSucceeded;

    public LoginViewModel()
    {
        UserStore.EnsureCreated();
        var (stayLoggedIn, username) = LoginPreferencesStore.Load();
        StayLoggedIn = stayLoggedIn;
        Username = username;
    }

    partial void OnUsernameChanged(string value) => LoginCommand.NotifyCanExecuteChanged();
    partial void OnPasswordChanged(string value) => LoginCommand.NotifyCanExecuteChanged();
    partial void OnIsBusyChanged(bool value) => LoginCommand.NotifyCanExecuteChanged();

    private bool CanLogin() => !IsBusy && !string.IsNullOrWhiteSpace(Username) && !string.IsNullOrEmpty(Password);

    [RelayCommand(CanExecute = nameof(CanLogin))]
    private async Task Login()
    {
        IsBusy = true;
        StatusText = "로그인 중…";
        try
        {
            var user = await UserStore.ValidateLoginAsync(Username, Password);
            if (user == null)
            {
                StatusText = "아이디 또는 비밀번호가 올바르지 않습니다.";
                return;
            }
            LoggedInUser = user;
            StatusText = "";
            LoginPreferencesStore.Save(StayLoggedIn, Username);
            LoginSucceeded?.Invoke();
        }
        catch (Exception ex)
        {
            StatusText = $"로그인 실패: {ex.Message}";
        }
        finally
        {
            IsBusy = false;
        }
    }
}
