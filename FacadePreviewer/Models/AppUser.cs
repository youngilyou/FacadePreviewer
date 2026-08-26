namespace FacadePreviewer.Models;

/// <summary>A logged-in operator account (Services/UserStore.cs). Deliberately minimal --
/// this app has no account-management screen yet, so there's nothing beyond the identity
/// needed to gate the login screen.</summary>
public sealed class AppUser
{
    public int Id { get; init; }
    public string Username { get; init; } = "";
}
