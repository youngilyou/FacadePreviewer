using System.IO;
using System.Threading.Tasks;
using FacadePreviewer.Models;
using Microsoft.Data.Sqlite;

namespace FacadePreviewer.Services;

/// <summary>FacadePreviewer's own login accounts, stored locally in SQLite
/// (%APPDATA%\FacadePreviewer\users.db) -- same pattern as CheckCrackViewer's UserStore.cs,
/// but a completely separate database (different app, different accounts). No account
/// management screen exists yet, so this only needs to validate logins against whatever
/// EnsureCreated seeded on first run.</summary>
public static class UserStore
{
    private static readonly string DbDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "FacadePreviewer");

    private static readonly string DbPath = Path.Combine(DbDir, "users.db");

    private static string ConnectionString => $"Data Source={DbPath}";

    private static SqliteConnection OpenConnection()
    {
        Directory.CreateDirectory(DbDir);
        var connection = new SqliteConnection(ConnectionString);
        connection.Open();
        using var pragma = connection.CreateCommand();
        pragma.CommandText = "PRAGMA journal_mode=WAL;";
        pragma.ExecuteNonQuery();
        return connection;
    }

    /// <summary>Creates the table if missing, and seeds a default admin/admin123 account the
    /// very first time (so LoginWindow never needs its own account-creation flow).</summary>
    public static void EnsureCreated()
    {
        using var connection = OpenConnection();
        using (var command = connection.CreateCommand())
        {
            command.CommandText = """
                CREATE TABLE IF NOT EXISTS users (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    username       TEXT NOT NULL UNIQUE,
                    password_hash  TEXT NOT NULL,
                    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
                    last_login_at  TEXT
                );
                """;
            command.ExecuteNonQuery();
        }

        using (var countCommand = connection.CreateCommand())
        {
            countCommand.CommandText = "SELECT COUNT(*) FROM users;";
            var count = (long)(countCommand.ExecuteScalar() ?? 0L);
            if (count > 0)
                return;
        }

        using var seedCommand = connection.CreateCommand();
        seedCommand.CommandText = "INSERT INTO users (username, password_hash) VALUES ('admin', @hash);";
        seedCommand.Parameters.AddWithValue("@hash", BCrypt.Net.BCrypt.HashPassword("admin123"));
        seedCommand.ExecuteNonQuery();
    }

    public static Task<AppUser?> ValidateLoginAsync(string username, string password)
    {
        using var connection = OpenConnection();
        using var command = connection.CreateCommand();
        command.CommandText = "SELECT id, username, password_hash FROM users WHERE username = @username LIMIT 1;";
        command.Parameters.AddWithValue("@username", username.Trim());

        using var reader = command.ExecuteReader();
        if (!reader.Read())
            return Task.FromResult<AppUser?>(null);

        var id = reader.GetInt32(0);
        var passwordHash = reader.GetString(2);
        reader.Close();

        if (!BCrypt.Net.BCrypt.Verify(password, passwordHash))
            return Task.FromResult<AppUser?>(null);

        using var updateCommand = connection.CreateCommand();
        updateCommand.CommandText = "UPDATE users SET last_login_at = datetime('now') WHERE id = @id;";
        updateCommand.Parameters.AddWithValue("@id", id);
        updateCommand.ExecuteNonQuery();

        return Task.FromResult<AppUser?>(new AppUser { Id = id, Username = username.Trim() });
    }

    /// <summary>Used by App.xaml.cs's startup gating to confirm a remembered "stay logged in"
    /// username still corresponds to a real account before skipping LoginWindow entirely.</summary>
    public static bool UserExists(string username)
    {
        using var connection = OpenConnection();
        using var command = connection.CreateCommand();
        command.CommandText = "SELECT COUNT(*) FROM users WHERE username = @username;";
        command.Parameters.AddWithValue("@username", username.Trim());
        return (long)(command.ExecuteScalar() ?? 0L) > 0;
    }
}
