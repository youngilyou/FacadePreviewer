using System.IO;
using System.Text.Json;

namespace FacadePreviewer.Services;

/// <summary>%APPDATA%\FacadePreviewer\login_prefs.json -- remembers whether the operator
/// checked "로그인 상태 유지" (stay logged in), and if so, which username. This is stronger
/// than CheckCrackViewer's "아이디 저장" (which only pre-fills the username field, still
/// requiring a password every launch): here, StayLoggedIn=true makes App.xaml.cs skip
/// LoginWindow entirely on the next launch. The password is never stored either way.</summary>
public static class LoginPreferencesStore
{
    private static readonly string SettingsDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "FacadePreviewer");

    private static readonly string SettingsPath = Path.Combine(SettingsDir, "login_prefs.json");

    public static (bool StayLoggedIn, string Username) Load()
    {
        if (!File.Exists(SettingsPath))
            return (false, "");
        try
        {
            var prefs = JsonSerializer.Deserialize<LoginPrefs>(File.ReadAllText(SettingsPath));
            return prefs is { StayLoggedIn: true } ? (true, prefs.Username) : (false, "");
        }
        catch (JsonException)
        {
            return (false, "");
        }
    }

    public static void Save(bool stayLoggedIn, string username)
    {
        Directory.CreateDirectory(SettingsDir);
        var prefs = new LoginPrefs { StayLoggedIn = stayLoggedIn, Username = stayLoggedIn ? username : "" };
        File.WriteAllText(SettingsPath, JsonSerializer.Serialize(prefs));
    }

    private sealed class LoginPrefs
    {
        public bool StayLoggedIn { get; set; }
        public string Username { get; set; } = "";
    }
}
