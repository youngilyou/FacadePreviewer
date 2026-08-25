using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace FacadePreviewer.Services;

public sealed class FacadeTargetCompany
{
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("buildings")] public List<string> Buildings { get; set; } = new();
}

/// <summary>Local, pre-configured company/building name catalog (config/facade_targets.json,
/// shipped next to the .exe -- see FacadePreviewer.csproj) -- feeds TransferSettingsWindow's
/// company/building ComboBoxes so an operator can only ever pick from this list, never type a
/// free-text company/building name. Deliberate project decision: "운용자의 오타, 무작위 이름
/// 설정은 추후 문제가 있음" -- a typo'd company/building name here would silently create a
/// distinct facade_building_requirements/crackvision_archives row family from the intended one,
/// so this file (edited by whoever administers a given deployment/site, not the field operator)
/// is the single source of truth for valid names, matching the direction dropdown's existing
/// fixed FRONT/BACK/LEFT/RIGHT/ROOF/OTHER vocabulary.</summary>
public sealed class FacadeTargetCatalog
{
    public IReadOnlyList<FacadeTargetCompany> Companies { get; }

    private FacadeTargetCatalog(IReadOnlyList<FacadeTargetCompany> companies)
    {
        Companies = companies;
    }

    /// <summary>Never throws -- a missing, empty, or malformed config file yields an empty
    /// catalog (caller shows a clear "설정 파일을 확인하세요" message and disables transfer
    /// rather than falling back to free-text entry).</summary>
    public static FacadeTargetCatalog Load(string path)
    {
        try
        {
            if (!File.Exists(path))
                return new FacadeTargetCatalog(Array.Empty<FacadeTargetCompany>());

            var json = File.ReadAllText(path);
            var dto = JsonSerializer.Deserialize<CatalogDto>(json, new JsonSerializerOptions { PropertyNameCaseInsensitive = true });
            var companies = (dto?.Companies ?? new List<FacadeTargetCompany>())
                .Where(c => !string.IsNullOrWhiteSpace(c.Name))
                .ToList();
            return new FacadeTargetCatalog(companies);
        }
        catch (Exception)
        {
            // Malformed JSON, permission error, etc. -- treated the same as "missing" (see doc
            // comment above): an empty catalog is a safe, visible failure mode, never a crash.
            return new FacadeTargetCatalog(Array.Empty<FacadeTargetCompany>());
        }
    }

    private sealed class CatalogDto
    {
        [JsonPropertyName("companies")] public List<FacadeTargetCompany>? Companies { get; set; }
    }
}
