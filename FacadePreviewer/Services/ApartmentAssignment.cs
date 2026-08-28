using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace FacadePreviewer.Services;

/// <summary>GenerateJson이 "확정" 시점에 만들어내는 파일 하나(예:
/// previewer/data/우리아파트.json)의 내용 -- Topic은 드론까지 이미 포함된 완성된 DDS 구독
/// 토픽 문자열(예: "rt/FacadeImage/DRONE01", GenerateJson 쪽에서 조립됨), Key는 화면 표시용
/// 현장 식별자(DDS 레벨에서는 쓰이지 않음), Drone은 어느 드론이 배정됐는지 보여주기 위한
/// 정보. 이 파일 하나가 예전 "수신 토픽" 콤보박스(config/dds_topics.json)를 대체한다.</summary>
public sealed class ApartmentAssignment
{
    [JsonPropertyName("Topic")] public string Topic { get; set; } = "";
    [JsonPropertyName("Key")] public string Key { get; set; } = "";
    [JsonPropertyName("DRONE")] public string Drone { get; set; } = "";

    // GenerateJson의 "동 범위/측정 장소" 섹션에서 채워짐(관리자가 안 채웠으면 둘 다 빈 배열) --
    // MainWindow의 "동"/"측정 장소" 콤보박스를 이 값으로 채운다(MainViewModel.LoadAssignment).
    // 구버전 assignment json(이 두 필드가 아예 없는 파일)도 그대로 로드되어야 하므로 둘 다
    // 기본값을 빈 리스트로 둔다 -- Load()의 "절대 throw 안 함" 원칙과 동일.
    [JsonPropertyName("Buildings")] public List<string> Buildings { get; set; } = new();
    [JsonPropertyName("Directions")] public List<string> Directions { get; set; } = new();

    // 2026-08-28: 계약(신청서) 연동 -- GenerateJson이 계약 체결 시 미리 박아두는 값
    // (SC_YYYYMMDD_NNN 형식 contract_id + 고객명), 운용자가 여기서 직접 입력/수정하지 않음.
    // 구버전 assignment json(이 필드들 자체가 없는 파일)도 그대로 로드되도록 기본값은 빈 문자열 --
    // SendRequirements가 그대로 ""를 보내면 backend는 "계약 미연동"으로 취급(지어내지 않음).
    [JsonPropertyName("ContractId")] public string ContractId { get; set; } = "";
    [JsonPropertyName("CustomerName")] public string CustomerName { get; set; } = "";

    /// <summary>실패 시 null(파일 없음/손상 -- 다른 로더들과 동일한 "절대 throw 안 함" 관례).
    /// Topic이 비어 있으면 구독할 게 없는 것과 마찬가지라 역시 null로 취급.</summary>
    public static ApartmentAssignment? Load(string path)
    {
        try
        {
            if (!File.Exists(path))
                return null;
            var json = File.ReadAllText(path);
            var result = JsonSerializer.Deserialize<ApartmentAssignment>(json, new JsonSerializerOptions { PropertyNameCaseInsensitive = true });
            return string.IsNullOrWhiteSpace(result?.Topic) ? null : result;
        }
        catch (Exception)
        {
            return null;
        }
    }
}
