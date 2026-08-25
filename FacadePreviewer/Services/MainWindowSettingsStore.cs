using System.IO;

namespace FacadePreviewer.Services;

/// <summary>Persists MainWindow's connection/capture field values to a plain .ini file next to the
/// exe (same "config" folder as facade_targets.json/transfer_settings.ini) so an operator's
/// last-used DDS-Router host/port and capture location survive across app restarts instead of
/// resetting to blank every time -- same rationale and file-format convention as
/// TransferSettingsStore, just for MainViewModel's own fields. MeasurementLocation IS persisted
/// here (unlike TransferSettingsWindow's deliberately-not-persisted SessionId): unlike a session
/// id, there is no "fresher is better" default for it, and re-typing the same site name on every
/// relaunch during a single day's shoot is pure friction.</summary>
public static class MainWindowSettingsStore
{
    public record MainWindowSettings(
        string DdsRouterHost,
        string DdsRouterPort,
        string LocalInterfaceIp,
        string CaptureRootPath,
        string MeasurementLocation,
        string SelectedBuilding);

    // Never throws -- a missing/corrupt settings file just means the window falls back to its
    // existing default values, same as if this feature didn't exist.
    public static MainWindowSettings? Load(string path)
    {
        try
        {
            if (!File.Exists(path))
                return null;

            var values = new Dictionary<string, string>();
            foreach (var rawLine in File.ReadAllLines(path))
            {
                var line = rawLine.Trim();
                if (line.Length == 0 || line.StartsWith(';') || line.StartsWith('['))
                    continue;
                var eq = line.IndexOf('=');
                if (eq <= 0)
                    continue;
                values[line[..eq].Trim()] = line[(eq + 1)..].Trim();
            }

            string Get(string key) => values.TryGetValue(key, out var v) ? v : "";

            return new MainWindowSettings(
                DdsRouterHost: Get("DdsRouterHost"),
                DdsRouterPort: Get("DdsRouterPort"),
                LocalInterfaceIp: Get("LocalInterfaceIp"),
                CaptureRootPath: Get("CaptureRootPath"),
                MeasurementLocation: Get("MeasurementLocation"),
                SelectedBuilding: Get("SelectedBuilding"));
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

    // Best-effort -- a failure to save settings must never block or interrupt an actual capture in
    // progress, so every I/O error is swallowed here rather than surfaced to the operator.
    public static void Save(string path, MainWindowSettings settings)
    {
        try
        {
            var dir = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(dir))
                Directory.CreateDirectory(dir);

            var lines = new[]
            {
                "[MainWindowSettings]",
                $"DdsRouterHost={settings.DdsRouterHost}",
                $"DdsRouterPort={settings.DdsRouterPort}",
                $"LocalInterfaceIp={settings.LocalInterfaceIp}",
                $"CaptureRootPath={settings.CaptureRootPath}",
                $"MeasurementLocation={settings.MeasurementLocation}",
                $"SelectedBuilding={settings.SelectedBuilding}",
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
