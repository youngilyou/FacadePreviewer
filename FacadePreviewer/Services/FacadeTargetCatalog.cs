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

    /// <summary>2026-10-07: the transfer window's 회사/동 lists now come only from the loaded
    /// 촬영지역 설정 file (GenerateJson output) -- one company (the contract's building name) with
    /// that file's 동 list. Empty catalog when no file is loaded or it carries no building name.</summary>
    public static FacadeTargetCatalog FromAssignment(ApartmentAssignment? assignment)
    {
        if (assignment == null || string.IsNullOrWhiteSpace(assignment.BuildingName) || assignment.Buildings.Count == 0)
            return new FacadeTargetCatalog(Array.Empty<FacadeTargetCompany>());
        return new FacadeTargetCatalog(new[]
        {
            new FacadeTargetCompany { Name = assignment.BuildingName.Trim(), Buildings = assignment.Buildings.Distinct().ToList() },
        });
    }

    /// <summary>"1000동" / " 1000 " -> "1000": the 동 number exactly as SmartCrackWeb's MySQL stores
    /// it (RequestTargets.DongNo) -- what MngData's archive `building` must carry so the analysis
    /// results can be matched back to the contract. Display keeps the "동" suffix.</summary>
    public static string NormalizeDongNo(string? value)
    {
        var v = (value ?? "").Trim();
        if (v.EndsWith("동"))
            v = v[..^1].TrimEnd();
        return v;
    }

    private sealed class CatalogDto
    {
        [JsonPropertyName("companies")] public List<FacadeTargetCompany>? Companies { get; set; }
    }
}
