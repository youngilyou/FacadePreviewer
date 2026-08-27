using System.IO;

namespace FacadePreviewer.Services;

/// <summary>Persists TransferSettingsWindow's field values to a plain .ini file next to the exe
/// (same "config" folder as facade_targets.json) so an operator's last-used server/target
/// settings survive across app restarts instead of resetting to blank every time. Loaded once at
/// window open, saved once whenever the operator clicks 전송 (regardless of whether that attempt
/// then passes validation -- so a typo is still remembered for next time, not just a successful
/// send). SessionId is deliberately NOT persisted here -- its own default (today's date/time,
/// see TransferSettingsWindow's constructor) is already the more useful "fresh per launch"
/// behavior; saving a stale one would work against that.</summary>
public static class TransferSettingsStore
{
    public record TransferSettings(
        string Host,
        string Port,
        string SshUser,
        string SshKeyPath,
        string SshPassword,
        string RemoteRoot,
        string LocalFolder,
        bool BatchMode,
        string? Company,
        string? Building,
        string? Direction);

    // Never throws -- a missing/corrupt settings file just means the dialog falls back to its
    // existing XAML default values, same as if this feature didn't exist.
    public static TransferSettings? Load(string path)
    {
        try
        {
            if (!File.Exists(path))
                return null;

            var values = new Dictionary<string, string>();
            foreach (var rawLine in File.ReadAllLines(path))
            {
                var line = rawLine.Trim();
                if (line.Length == 0 || line.StartsWith(';') || line.StartsWith('[') )
                    continue;
                var eq = line.IndexOf('=');
                if (eq <= 0)
                    continue;
                values[line[..eq].Trim()] = line[(eq + 1)..].Trim();
            }

            string Get(string key) => values.TryGetValue(key, out var v) ? v : "";

            return new TransferSettings(
                Host: Get("Host"),
                Port: Get("Port"),
                SshUser: Get("SshUser"),
                SshKeyPath: Get("SshKeyPath"),
                SshPassword: Get("SshPassword"),
                RemoteRoot: Get("RemoteRoot"),
                LocalFolder: Get("LocalFolder"),
                BatchMode: Get("BatchMode").Equals("true", StringComparison.OrdinalIgnoreCase),
                Company: values.TryGetValue("Company", out var company) ? company : null,
                Building: values.TryGetValue("Building", out var building) ? building : null,
                Direction: values.TryGetValue("Direction", out var direction) ? direction : null);
        }
        catch (IOException)
        {
            return null;
        }
        catch (UnauthorizedAccessException)
        {
            return null;
        }
    }

    // Best-effort -- a failure to save settings must never block or interrupt an actual transfer
    // in progress, so every I/O error is swallowed here rather than surfaced to the operator.
    public static void Save(string path, TransferSettings settings)
    {
        try
        {
            var dir = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(dir))
                Directory.CreateDirectory(dir);

            var lines = new[]
            {
                "[TransferSettings]",
                $"Host={settings.Host}",
                $"Port={settings.Port}",
                $"SshUser={settings.SshUser}",
                $"SshKeyPath={settings.SshKeyPath}",
                $"SshPassword={settings.SshPassword}",
                $"RemoteRoot={settings.RemoteRoot}",
                $"LocalFolder={settings.LocalFolder}",
                $"BatchMode={settings.BatchMode}",
                $"Company={settings.Company ?? ""}",
                $"Building={settings.Building ?? ""}",
                $"Direction={settings.Direction ?? ""}",
            };
            File.WriteAllLines(path, lines);
        }
        catch (IOException)
        {
        }
        catch (UnauthorizedAccessException)
        {
        }
    }
}
