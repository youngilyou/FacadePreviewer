using System.Diagnostics;
using System.Management;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Media;
using FacadePreviewer.Services;
using FacadePreviewer.ViewModels;

namespace FacadePreviewer;

public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        // RunScan's subprocess (python stitch_folder.py / COLMAP) survives the app being
        // force-closed or crashing mid-scan whenever MainViewModel.Dispose() never got a chance to
        // run (Dispose() itself now kills it on a normal window close -- see its own doc comment,
        // added the same day as this). The orphan then sits there holding the capture folder's
        // output/<facade>_colmap/database.db open, so the *next* "분석 시작" fails immediately with
        // a PermissionError trying to unlink() that stale db -- reproduced repeatedly on
        // 2026-09-28 even after Dispose()'s fix, since that only covers this process's own
        // lifetime, not whatever got orphaned by an earlier crash/kill. Cleaning up any leftover
        // stitch_folder.py process at the START of every launch closes that gap regardless of how
        // the previous run ended.
        KillOrphanStitchProcesses();

        // Same fix as CheckCrackViewer's App.xaml.cs: this machine's WPF GPU
        // rendering doesn't relay over the remote session (renders fine
        // locally, solid black remotely) — force software rendering.
        RenderOptions.ProcessRenderMode = RenderMode.SoftwareOnly;

        // Stays OnExplicitShutdown for the app's entire lifetime (not just the login step) --
        // 로그아웃 closes MainWindow and loops back to a new LoginWindow, and the default
        // OnLastWindowClose would tear the whole process down the instant MainWindow closes,
        // before the next LoginWindow ever gets a chance to show (same fix CheckCrackViewer's
        // App.xaml.cs needed). Only ExitApplication-style closes and a cancelled login call
        // Shutdown() explicitly now.
        ShutdownMode = ShutdownMode.OnExplicitShutdown;

        // App.xaml's StartupUri was removed -- this method now owns picking the first window.
        // "로그인 상태 유지"(LoginViewModel.StayLoggedIn) means literally that: if the operator
        // checked it last time and that account still exists, skip LoginWindow entirely instead
        // of just pre-filling the username (unlike CheckCrackViewer's "아이디 저장", which only
        // remembers the username and still asks for a password every launch).
        var (stayLoggedIn, username) = LoginPreferencesStore.Load();
        if (stayLoggedIn && UserStore.UserExists(username))
        {
            ShowMain(username);
            return;
        }

        ShowLoginThenMain();
    }

    /// <summary>Login → MainWindow, looping back here on 로그아웃 instead of exiting the
    /// process. ShowDialog() blocks until LoginWindow closes, so this stays synchronous --
    /// MainWindow is only ever reached once a real login succeeded (LoginWindow sets
    /// DialogResult=true itself, see its LoginSucceeded handler).</summary>
    private void ShowLoginThenMain()
    {
        // LoginWindow is shown via ShowDialog() first, which would otherwise become
        // Application.MainWindow by default -- MainWindow gets reassigned explicitly in
        // ShowMain() once login is done (same fix CheckCrackViewer's App.xaml.cs needed).
        var login = new LoginWindow();
        if (login.ShowDialog() != true)
        {
            Shutdown();
            return;
        }
        ShowMain(login.ViewModel.LoggedInUser?.Username ?? "");
    }

    private void ShowMain(string username)
    {
        var main = new MainWindow();
        MainWindow = main;

        // Distinguishes "closed via 로그아웃" (loop back to a new LoginWindow) from every other
        // way MainWindow can close -- the titlebar X button, Alt+F4 -- which must still end the
        // process. Without this flag, OnExplicitShutdown (needed so a 로그아웃-triggered close
        // doesn't tear the whole app down before the next LoginWindow shows) would leave the X
        // button just closing the window and the process running invisibly with no windows at all.
        var loggingOut = false;
        if (main.DataContext is MainViewModel vm)
        {
            vm.LoggedInUsername = username;
            vm.LogoutRequested += () =>
            {
                loggingOut = true;
                // Otherwise "로그인 상태 유지" would silently log the same account back in on the
                // next full app relaunch, making 로그아웃 a no-op the moment the process restarts.
                LoginPreferencesStore.Save(false, "");
                main.Close();
                ShowLoginThenMain();
            };
        }
        main.Closed += (_, _) =>
        {
            if (!loggingOut)
                Shutdown();
        };
        main.Show();
    }

    // Process itself exposes no way to read another process's command line -- WMI's Win32_Process
    // is the standard way on Windows (same approach as manually diagnosing this via PowerShell's
    // Get-CimInstance during the 2026-09-28 session that found this bug). Matches on
    // "stitch_folder.py" specifically (this project's own distinctly-named entry point, see
    // stitch_folder.py's own header comment) rather than just "python.exe" -- this dev machine
    // routinely has several unrelated python.exe processes running (VS Code's Jedi language
    // server, conda multiprocessing workers, etc.) that must never be touched.
    private static void KillOrphanStitchProcesses()
    {
        try
        {
            using var searcher = new ManagementObjectSearcher(
                "SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name = 'python.exe' OR Name = 'python'");
            foreach (ManagementBaseObject result in searcher.Get())
            {
                using var mo = result;
                string commandLine = mo["CommandLine"] as string ?? "";
                if (!commandLine.Contains("stitch_folder.py", StringComparison.OrdinalIgnoreCase))
                    continue;

                int pid = Convert.ToInt32(mo["ProcessId"]);
                try
                {
                    using var proc = Process.GetProcessById(pid);
                    proc.Kill(entireProcessTree: true);
                }
                catch (ArgumentException) { /* already gone between the query and here */ }
                catch (InvalidOperationException) { /* already gone between the query and here */ }
            }
        }
        catch (Exception)
        {
            // WMI can be unavailable/restricted on some machines -- this cleanup is a best-effort
            // nicety, not something worth failing app startup over.
        }
    }
}
