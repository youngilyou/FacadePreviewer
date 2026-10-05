using System.Collections.ObjectModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using FacadePreviewer.Models;
using FacadePreviewer.Services;
using OpenCvSharp;
using OpenCvSharp.WpfExtensions;

namespace FacadePreviewer.ViewModels;

/// <summary>[YYIL] 2026-08, capture-only redesign (the user's own pivot away from all real-time
/// stitching): "실시간 하지 맙시다. 그냥 운용자가 건물 이미지 캡쳐가 마무리 되때까지 데이터
/// 베이스에 h264 decode -> 사이즈는 640x640을 작고, 일정한 크기로 해서 운용자가 UI에서 측정장소
/// 입력하면 날짜,시간 포함된 폴더 생성 후, 이곳에 .jpeg로 저장합니다. 운용자가 드론 조종을
/// 마치고-> 스캔시작(스티칭->ColMap) 한번에 실행 하는 거로 하지요."
///
/// Everything from the earlier real-time pipeline (per-frame/batch ORB+LightGlue matching,
/// RANSAC, canvas warping, ORB-SLAM3 pose-assisted placement) is gone -- this app now does
/// exactly two things:
///   1. While IsCapturing: every decoded DDS frame is throttled to ~2fps, resized to a fixed
///      640x640, and saved as a plain JPEG into a dated capture folder. No matching, no
///      warping, no live preview.
///   2. On "스캔 시작": the whole homography-chain + COLMAP-fallback pipeline runs OFFLINE, as a
///      subprocess against the finished capture folder -- previewer/tools/stitch_engine
///      (vendored Kornia LoFTR + native COLMAP CLI, see that folder's own README), never
///      pycolmap, never in-process.
///
/// DDS connectivity (DdsBridgeService) is unchanged and still real -- only what this ViewModel
/// DOES with each decoded frame changed.</summary>
public partial class MainViewModel : ObservableObject, IDisposable
{
    // Which DDS topic to subscribe to is no longer a single hardcoded constant or an
    // operator-picked combobox -- it comes from a GenerateJson-produced Topic/Key/DRONE json
    // (see LoadedAssignment/LoadAssignment/TryEnsureDdsRegistered below).

    // Capture cadence/size: previewer's own design target (CLAUDE.local.md), not a real camera
    // calibration choice -- a fixed square avoids per-drone/per-lens aspect-ratio bookkeeping
    // for a coverage-preview tool that doesn't need native resolution. 2fps -> 1fps (2026-09-28,
    // user request): halves the total frame count for a typical facade (~1000 -> ~500), which
    // halves RunScan's total SIFT/matching/mapping cost directly -- 2fps was producing more
    // frames than a ~2-3s-apart serpentine flight actually needed for adequate overlap.
    private static readonly TimeSpan CaptureInterval = TimeSpan.FromSeconds(1.0);
    // 640 -> 320 (2026-09-28, user decision): this app's RunScan output is used purely as a
    // coverage/completeness check ("면 촬영을 다했는지가 핵심" -- crack-level visual detail is
    // explicitly not a requirement here, that's a separate higher-resolution pipeline's job), so
    // there's no real quality cost to cutting SIFT's pixel count (and therefore extraction +
    // matching time) by 4x. Watch for coverage_ratio/unreachable_ratio creeping up if this ever
    // goes lower than 320 -- too few SIFT keypoints on a weak-texture wall section can fail
    // pairwise matching and falsely look like a missed spot rather than an actual gap.
    private const int CaptureSizePx = 320;

    private readonly DdsBridgeService _dds = new();

    // Guards _captureDir/_capturedFrameCount, which are written from the UI thread
    // (StartCapture/StopCapture/Reset) and read+incremented from the native DDS listener thread
    // (OnDecodedFrameReceived) -- same cross-thread-state pattern the old pipeline used for its
    // own pending-frame buffer. _lastCaptureUtc is intentionally NOT locked: only the single DDS
    // listener thread ever touches it (DdsBridgeService's own doc comment: callbacks fire
    // serialized on that one thread, never concurrently).
    private readonly object _captureLock = new();
    private string? _captureDir;
    private int _capturedFrameCount;
    private DateTime _lastCaptureUtc = DateTime.MinValue;
    // Set only by LoadExistingCaptureFolder (see its own doc comment); null means RunScan derives
    // facadeName from the 동/측정 장소 combo boxes as usual.
    private string? _loadedFacadeNameOverride;

    [ObservableProperty] private bool _isConnected;
    [ObservableProperty] private string _connectionStatusText = "HOST 연결 안 됨";

    // App.xaml.cs sets this right after login succeeds (see ShowMain).
    [ObservableProperty] private string _loggedInUsername = "";

    [ObservableProperty] private string _measurementLocation = "";
    [ObservableProperty] private string _captureRootPath = Path.Combine(AppContext.BaseDirectory, "captures");
    [ObservableProperty] private bool _isCapturing;
    [ObservableProperty] private bool _isScanning;
    [ObservableProperty] private string _statusMessage = "대기 중 — 측정 장소를 입력하고 캡처를 시작하세요";
    [ObservableProperty] private string _captureFolderText = "";
    [ObservableProperty] private int _capturedFrameCountDisplay;
    [ObservableProperty] private string _scanLogText = "";
    // Driven by parsing stitch_folder.py's own JSON log lines (see TryUpdateScanProgress) --
    // approximate checkpoint-based percent, not a real time estimate, but enough for an operator
    // to tell "still moving" from "stuck".
    [ObservableProperty] private double _scanProgressPercent;
    [ObservableProperty] private string _scanStageText = "";
    // 분석 시작 elapsed time, shown large next to the progress bar (2026-09-29, user request).
    // Ticks every second while the scan runs and keeps the final total afterwards; "" hides it.
    [ObservableProperty] private string _scanElapsedText = "";
    private readonly Stopwatch _scanStopwatch = new();
    private System.Windows.Threading.DispatcherTimer? _scanElapsedTimer;
    // Set once RunScan finishes successfully and the output analysis mosaic loads -- the scan
    // log view switches to showing this instead (see MainWindow.xaml's DataTrigger on
    // HasScanResult). Cleared by StartCapture/Reset so a fresh capture doesn't keep showing a
    // stale previous facade's result.
    [ObservableProperty] private BitmapSource? _scanResultImage;
    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(OpenScanResultCommand))]
    private bool _hasScanResult;

    // Populated after a successful scan from {facadeName}_unmatched_images.json (see
    // LoadUnmatchedImages) -- images the pipeline itself determined never contributed to the
    // stitched result (zero passing pairwise match, or reached COLMAP but failed to register),
    // as opposed to guessing from how a photo looks. ExcludeUnmatchedImagesCommand moves the
    // checked ones into the capture folder's "excluded" subfolder (same convention as
    // RemoveExcludedFrames) so a re-scan skips them. HasUnmatchedImages exists for the same
    // WPF-trigger-needs-a-plain-bool reason as HasCapturedFrames above.
    public ObservableCollection<UnmatchedImageItem> UnmatchedImages { get; } = new();
    [ObservableProperty] private bool _hasUnmatchedImages;

    // Populated live as each frame is saved (see OnDecodedFrameReceived) so the operator can
    // review/remove bad frames (blur, wrong angle, occluder) before RunScan hands the whole
    // capture folder to stitch_folder.py, from a left-sidebar list (see MainWindow.xaml) rather
    // than inside this scan-log/result panel -- design review corrected an earlier version that
    // put the review grid here. Cleared by StartCapture/Reset, same as the other per-facade
    // capture state. HasCapturedFrames exists purely because MainWindow.xaml's DataTrigger needs a
    // plain bool -- WPF triggers can't bind ObservableCollection.Count directly without a
    // converter, and this is simpler than adding one for a single use.
    public ObservableCollection<CapturedFrameItem> CapturedFrames { get; } = new();
    [ObservableProperty] private bool _hasCapturedFrames;

    // Set by double-clicking a thumbnail in the left sidebar (see MainWindow.xaml.cs's
    // OnCapturedFrameThumbnailClicked) -- renders that frame's original 640x640 directly in this
    // window's own main content area (design review: "별도 팝업이 아닙니다"), not a separate
    // window. Cleared by StartCapture/Reset so a fresh facade doesn't keep showing a stale frame.
    [ObservableProperty] private BitmapSource? _selectedFrameImage;
    [ObservableProperty] private bool _isShowingSelectedFrame;

    // DDS-Router host/port this app should discover against, entered by the operator on
    // site (현장에서 DDS-Router IP/포트가 매번 달라질 수 있음). Empty host = no override,
    // falls back to this process's FACADE_DDS_INITIAL_PEER/FACADE_DDS_INTERFACE_WHITELIST
    // env vars if set (see DdsFrameSubscriber.cpp's MakeUdpOnlyQos). Port defaults to the
    // domain-30/participant-index-0 metatraffic-unicast port (7400 + 250*domain + 10) --
    // FacadePreviewerDomainParticipant in DDS-Router's crack_inspection_analysis.yaml is domain 30,
    // matching TryEnsureDdsRegistered's _dds.Start(30, ...) below (was hardcoded 0, mismatched
    // against the router's actual participant domain -- 2026-09-28 fix).
    [ObservableProperty] private string _ddsRouterHost = "";
    [ObservableProperty] private int _ddsRouterPort = 14910;
    [ObservableProperty] private string _localInterfaceIp = "";
    // 2026-10-05: DDS domain on screen (was hardcoded 30 here and 0 in TransferSettingsWindow).
    // Used for video capture AND the transfer window's storage/analysis DDS, so both talk to the
    // router's FacadePreviewerDomainParticipant. Changing it resets Port to that domain's default
    // participant-0 metatraffic port (7400 + 250*domain + 10); Port can still be edited after.
    [ObservableProperty] private int _ddsDomainId = 30;
    partial void OnDdsDomainIdChanged(int value)
    {
        if (value >= 0 && value <= 232)
            DdsRouterPort = 7400 + 250 * value + 10;
    }

    // GenerateJson이 만든 Topic/Key/DRONE json(예: previewer/data/우리아파트.json) 하나를
    // "불러오기"로 읽어들인 결과 -- 예전 "수신 토픽" 콤보박스(config/dds_topics.json,
    // DdsTopicCatalog)를 대체한다. Topic은 GenerateJson 쪽에서 이미 드론까지 포함해서 만들어
    // 주므로(예: "rt/FacadeImage/DRONE01") previewer는 그 값을 그대로 구독 토픽으로 쓴다 --
    // Topic+Drone을 여기서 다시 조합하지 않는다. 실제 _dds.Start(...)는 "캡처 시작" 클릭 시
    // TryEnsureDdsRegistered가 호출한다(불러오기 자체는 구독을 시작하지 않음, 별도 "등록"
    // 버튼은 없앰 -- 항상 캡처 시작 바로 전에만 눌렀으므로 하나로 합침).
    [ObservableProperty] private ApartmentAssignment? _loadedAssignment;
    // Path.GetFullPath resolves the ".." segments -- OpenFileDialog's InitialDirectory (via
    // WPF's underlying IFileDialog/shell-item resolution) throws ArgumentException on an
    // unresolved path containing literal ".." components (confirmed: crashed the whole app the
    // first time this was tried without GetFullPath).
    private readonly string _assignmentDefaultDir = FindUpward("data") ?? Path.Combine(AppContext.BaseDirectory, "data");

    // Looks for <relativePath> next to the exe, then in each parent folder. A fixed "..\..\..\.."
    // only matched bin\<Config>\net9.0-windows; Visual Studio's x64 platform builds to
    // bin\x64\<Config>\net9.0-windows, one level deeper, and the scan then couldn't find
    // tools\stitch_engine (2026-10-05). Also covers an installed copy with tools\ beside the exe.
    // Returns a full path (no ".." segments, which OpenFileDialog's InitialDirectory rejects).
    private static string? FindUpward(string relativePath)
    {
        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir != null; dir = dir.Parent)
        {
            string candidate = Path.Combine(dir.FullName, relativePath);
            if (File.Exists(candidate) || Directory.Exists(candidate))
                return candidate;
        }
        return null;
    }

    // 촬영현장(회사/단지)/동/측정장소(방향) 그룹 -- 같은 config/facade_targets.json을
    // TransferSettingsWindow와 공유(FacadeTargetCatalog), 이 앱은 배포당 하나의 현장만 다루므로
    // 회사 목록 중 첫 번째(보통 유일한) 항목을 읽기전용으로 표시하고("수정 불가"), 그 현장의
    // 동 목록만 콤보박스로 고른다. "불러오기" 버튼으로 수동 재로드 가능(admin이 파일을 고친
    // 뒤 앱 재시작 없이 반영하려는 용도) -- 시작 시 한 번은 자동으로도 로드된다.
    [ObservableProperty] private string _siteDisplayName = "(불러오기를 눌러 현장을 불러오세요)";
    public ObservableCollection<string> Buildings { get; } = new();
    [ObservableProperty] private string? _selectedBuilding;

    // TransferSettingsWindow의 방향 콤보(FRONT/BACK/LEFT/RIGHT/ROOF/OTHER)와 동일한 고정 어휘이
    // 기본값 -- MeasurementLocation은 자유 텍스트가 아니라 이 목록 중 하나. GenerateJson이 만든
    // assignment json에 Directions가 실려있으면(그 아파트의 실제 측정 장소, "정면 꺾임" 같은
    // 임의 이름 포함 가능) LoadAssignment가 이 목록을 그걸로 교체한다 -- ObservableProperty라야
    // 런타임에 교체 가능(예전엔 생성자에서만 값이 정해지는 읽기전용 필드였음).
    [ObservableProperty] private IReadOnlyList<string> _directionOptions = new[] { "FRONT", "BACK", "LEFT", "RIGHT", "ROOF", "OTHER" };

    private readonly string _facadeTargetsPath = Path.Combine(AppContext.BaseDirectory, "config", "facade_targets.json");

    [ObservableProperty] private int _sensorFramesReceived;
    [ObservableProperty] private int _videoPacketsReceived;

    // Same "config" folder facade_targets.json/transfer_settings.ini already live in (see
    // TransferSettingsWindow's own _settingsPath) -- not %APPDATA% or any new location.
    private readonly string _settingsPath = Path.Combine(AppContext.BaseDirectory, "config", "main_window_settings.ini");

    public MainViewModel()
    {
        // DdsBridgeService's events fire on the native DDS listener thread
        // (see its own doc comment) -- every handler below must dispatch to
        // the UI thread before touching any ObservableProperty.
        _dds.SensorFrameReceived += OnSensorFrameReceived;
        _dds.VideoPacketReceived += OnVideoPacketReceived;
        _dds.DecodedFrameReceived += OnDecodedFrameReceived;

        LoadFacadeTargets();
        ApplySavedSettings();
    }

    // BrowseCaptureRoot와 동일한 관례 -- 파일 선택 다이얼로그도 ViewModel의 RelayCommand가
    // 직접 띄운다(previewer는 순수 MVVM을 엄격히 지키지 않고, 단순 파일/폴더 다이얼로그는
    // ViewModel에서 바로 연다).
    [RelayCommand]
    private void LoadAssignment()
    {
        string initialDir = Directory.Exists(_assignmentDefaultDir) ? _assignmentDefaultDir : AppContext.BaseDirectory;
        var dialog = new Microsoft.Win32.OpenFileDialog
        {
            Title = "촬영지역 설정(Topic/Key/DRONE json) 불러오기",
            Filter = "JSON 파일 (*.json)|*.json",
            InitialDirectory = initialDir,
        };
        if (dialog.ShowDialog() != true)
            return;

        var assignment = ApartmentAssignment.Load(dialog.FileName);
        if (assignment == null)
        {
            StatusMessage = "촬영지역 설정 파일을 읽을 수 없습니다 (형식 확인 필요)";
            return;
        }
        LoadedAssignment = assignment;

        // GenerateJson이 이 파일에 동 범위/측정 장소를 실어보냈으면 "동"/"측정 장소" 콤보박스도
        // 이걸로 채운다 -- 구버전 assignment json(둘 다 없는 파일)은 Buildings/DirectionOptions
        // 자체 로드(LoadFacadeTargets/생성자 기본값)를 그대로 두고 건드리지 않는다.
        if (assignment.Buildings.Count > 0)
        {
            Buildings.Clear();
            foreach (var b in assignment.Buildings)
                Buildings.Add(b);
            SelectedBuilding = Buildings.FirstOrDefault();
        }
        if (assignment.Directions.Count > 0)
        {
            DirectionOptions = assignment.Directions;
            MeasurementLocation = DirectionOptions.FirstOrDefault() ?? "";
        }

        StatusMessage = $"불러옴: Topic={assignment.Topic}, Key={assignment.Key}, DRONE={assignment.Drone}";
    }

    // "DDS ROUTER에 등록" -- previewer 자체 구독만 시작한다(사용자 확정 사항 -- 원격
    // rtmp_video_bridge_streams.txt는 건드리지 않음, 그 파일은 배포 관리자가 미리 준비해 둔다는
    // 전제). LoadedAssignment.Topic은 GenerateJson이 이미 드론까지 포함해서 만든 완성된
    // 문자열이라 여기서 다시 조합하지 않고 그대로 video 토픽으로 쓴다. sensor 토픽은
    // _dds.Start의 필수 인자라 형식만 맞춰 채움 -- 실제 그 토픽에 발행하는 쪽은 없다(기존
    // "수신 토픽" 방식에서도 마찬가지였음).
    // 등록(구독 시작)과 캡처 시작을 매번 순서대로 같이 눌러야 했던 것 -- 사용자 요청으로 별도
    // 버튼을 없애고 캡처 시작 안에서 자동으로 등록하도록 합침(TryEnsureDdsRegistered). 이미
    // 등록돼 있으면(IsConnected) 아무것도 다시 하지 않는다 -- _dds.Start를 두 번 호출하지 않음.
    private bool TryEnsureDdsRegistered()
    {
        if (IsConnected)
            return true;
        if (LoadedAssignment == null)
        {
            StatusMessage = "먼저 촬영지역 설정을 불러오세요";
            return false;
        }

        string videoTopic = LoadedAssignment.Topic;
        string sensorTopic = $"{LoadedAssignment.Topic}/sensor";
        string peerDesc = string.IsNullOrWhiteSpace(DdsRouterHost) ? "peer override 없음(env var 사용)" : $"peer {DdsRouterHost}:{DdsRouterPort}";
        ConnectionStatusText = $"DDS 구독 시작됨 ({LoadedAssignment.Drone}, {videoTopic}, domain {DdsDomainId}, {peerDesc}) — 수신 대기 중";
        IsConnected = true; // "subscribing" -- StartAsync doesn't report publisher-matched yet, see DdsFrameSubscriber
        // Domain 30 -- matches FacadePreviewerDomainParticipant in DDS-Router's
        // crack_inspection_analysis.yaml. Was hardcoded 0 (2026-09-28 fix): the real
        // RtmpVideoBridge for this DRONE01 stream publishes into the router on domain 30, so a
        // domain-0 participant here could never discover it regardless of topic/type correctness.
        _dds.Start(DdsDomainId, sensorTopic, videoTopic, DdsRouterHost, DdsRouterPort, LocalInterfaceIp);
        return true;
    }

    // 생성자에서 앱 시작 시 한 번만 호출된다(재로드 버튼은 제거됨 -- 사용자 요청: "불러오기
    // 버튼 제거", 이제 동/측정장소는 보통 "촬영지역 설정 불러오기..."의 assignment json이
    // 갈아치우므로 이 버튼의 존재 이유였던 수동 재로드 시나리오가 옅어짐). 이 앱은 배포당
    // 하나의 현장만 다루므로 회사 목록의 첫 번째(보통 유일한) 항목만 쓴다 -- 여러 회사가
    // 있으면 그중 첫 항목, 파일이 비었거나 없으면 안내 메시지로 표시(크래시 없음,
    // FacadeTargetCatalog 자체가 이미 그렇게 동작).
    private void LoadFacadeTargets()
    {
        var catalog = FacadeTargetCatalog.Load(_facadeTargetsPath);
        var site = catalog.Companies.FirstOrDefault();

        Buildings.Clear();
        if (site == null)
        {
            SiteDisplayName = "(설정 파일을 확인하세요 -- config\\facade_targets.json)";
            SelectedBuilding = null;
            return;
        }

        SiteDisplayName = site.Name;
        foreach (var building in site.Buildings)
            Buildings.Add(building);
        if (SelectedBuilding == null || !Buildings.Contains(SelectedBuilding))
            SelectedBuilding = Buildings.FirstOrDefault();
    }

    // Restores the operator's last-used connection/capture values from _settingsPath (see
    // MainWindowSettingsStore) so re-typing the same DDS-Router host/site name on every relaunch
    // during a single day's shoot isn't necessary -- called once at startup, before anything else
    // touches these fields.
    private void ApplySavedSettings()
    {
        var saved = MainWindowSettingsStore.Load(_settingsPath);
        if (saved == null)
            return;

        if (!string.IsNullOrEmpty(saved.DdsRouterHost))
            DdsRouterHost = saved.DdsRouterHost;
        // Domain before Port: setting the domain resets Port to its default, then a saved Port wins.
        if (int.TryParse(saved.DdsDomainId, out var domain) && domain >= 0)
            DdsDomainId = domain;
        if (int.TryParse(saved.DdsRouterPort, out var port) && port > 0)
            DdsRouterPort = port;
        if (!string.IsNullOrEmpty(saved.LocalInterfaceIp))
            LocalInterfaceIp = saved.LocalInterfaceIp;
        if (!string.IsNullOrEmpty(saved.CaptureRootPath))
            CaptureRootPath = saved.CaptureRootPath;
        if (!string.IsNullOrEmpty(saved.MeasurementLocation))
            MeasurementLocation = saved.MeasurementLocation;
        // Same "must still exist" caution as before -- LoadFacadeTargets() (called
        // just before this) already defaulted SelectedBuilding to the first entry, so this only
        // overrides that default when the saved value is still valid for the freshly-loaded site.
        if (!string.IsNullOrEmpty(saved.SelectedBuilding) && Buildings.Contains(saved.SelectedBuilding))
            SelectedBuilding = saved.SelectedBuilding;
        if (AnalysisProfiles.Contains(saved.AnalysisProfile))
            AnalysisProfile = saved.AnalysisProfile;
    }

    // 분석 모드: which stitch_engine config 분석 시작 runs with. "PC" = config/pipeline.yaml (RTX 4080
    // desktop); "노트북" = config/pipeline.laptop.yaml (pipeline.yaml + speed overrides for the
    // RTX 4050 / 16GB field laptop), passed as `stitch_folder.py --profile laptop`. Chosen by the
    // operator, not auto-detected; saved right away so it sticks across restarts.
    public IReadOnlyList<string> AnalysisProfiles { get; } = new[] { "PC", "노트북" };
    [ObservableProperty] private string _analysisProfile = "PC";
    partial void OnAnalysisProfileChanged(string value) => SaveCurrentSettings();

    // Saves whatever the operator currently has entered -- called once per 캡처 시작 click (see
    // StartCapture), regardless of whether that attempt then passes validation, so a not-yet-valid
    // attempt (e.g. blank 측정 장소) still remembers the rest for next time, same convention as
    // TransferSettingsWindow's own SaveCurrentSettings.
    private void SaveCurrentSettings()
    {
        MainWindowSettingsStore.Save(_settingsPath, new MainWindowSettingsStore.MainWindowSettings(
            DdsRouterHost: DdsRouterHost,
            DdsRouterPort: DdsRouterPort.ToString(),
            DdsDomainId: DdsDomainId.ToString(),
            LocalInterfaceIp: LocalInterfaceIp,
            CaptureRootPath: CaptureRootPath,
            MeasurementLocation: MeasurementLocation,
            SelectedBuilding: SelectedBuilding ?? "",
            AnalysisProfile: AnalysisProfile));
    }

    // "저장 폴더 위치의 경로는 바꿀 수 있게 버튼을 넣어 경로 설정 하게 하시요" -- native folder
    // picker (Microsoft.Win32.OpenFolderDialog, .NET 8+ WPF) instead of hand-typing a path.
    [RelayCommand]
    private void BrowseCaptureRoot()
    {
        var dialog = new Microsoft.Win32.OpenFolderDialog
        {
            Title = "캡처 저장 위치 선택",
            InitialDirectory = Directory.Exists(CaptureRootPath) ? CaptureRootPath : AppContext.BaseDirectory,
        };
        if (dialog.ShowDialog() == true)
            CaptureRootPath = dialog.FolderName;
    }

    [RelayCommand]
    private void StartCapture()
    {
        if (IsCapturing)
            return;
        SaveCurrentSettings();
        if (!TryEnsureDdsRegistered())
            return; // TryEnsureDdsRegistered already set StatusMessage
        if (string.IsNullOrWhiteSpace(SelectedBuilding))
        {
            StatusMessage = "동을 선택하세요";
            return;
        }
        if (string.IsNullOrWhiteSpace(MeasurementLocation))
        {
            StatusMessage = "측정 장소를 선택하세요";
            return;
        }

        // New capture, new facade -- don't keep showing the previous facade's rendered result.
        ScanResultImage = null;
        HasScanResult = false;
        ScanLogText = "";
        _scanLogLines.Clear();
        ScanElapsedText = "";
        CapturedFrames.Clear();
        HasCapturedFrames = false;
        SelectedFrameImage = null;
        IsShowingSelectedFrame = false;

        // 동+방향을 함께 넣어야 같은 현장 안 여러 동을 캡처해도 폴더명만 보고 구분된다 --
        // RunScan의 facadeName도 동일한 조합으로 다시 계산하므로(아래 참고) 반드시 같이 바꿀 것.
        string safeLocation = SanitizeForFolderName($"{SelectedBuilding}_{MeasurementLocation}");
        string timestamp = DateTime.Now.ToString("yyyyMMdd_HHmmss");
        string dir = Path.Combine(CaptureRootPath, $"{safeLocation}_{timestamp}");
        Directory.CreateDirectory(dir);

        lock (_captureLock)
        {
            _captureDir = dir;
            _capturedFrameCount = 0;
        }
        _loadedFacadeNameOverride = null;
        _lastCaptureUtc = DateTime.MinValue;
        CapturedFrameCountDisplay = 0;
        CaptureFolderText = dir;

        // DDS 구독 자체는 바로 위 TryEnsureDdsRegistered()가 시작해 놓았다 -- 여기서부터는 그
        // 위에서 흘러들어오는 프레임을 디스크에 저장하는 것만 담당(IsCapturing이
        // OnDecodedFrameReceived 안에서 저장 여부를 가르는 게이트, _dds.Start 자체는 다시
        // 호출하지 않음).
        IsCapturing = true;
        StatusMessage = $"캡처 중 — {dir}";
    }

    [RelayCommand]
    private void StopCapture()
    {
        if (!IsCapturing)
            return;
        _dds.Stop();
        IsCapturing = false;
        IsConnected = false;
        ConnectionStatusText = "HOST 연결 안 됨";
        StatusMessage = $"캡처 중지됨 — {_capturedFrameCount}장 저장됨";

        // Catch-up call: mops up whatever tail of frames landed since the last periodic
        // IncrementalExtractionFrameInterval trigger, so clicking "분석 시작" right after this
        // still gets the full benefit of the already-extracted features instead of missing the
        // last few. Fire-and-forget same as the periodic trigger -- RunScan's own extraction call
        // is still correct (just slower) if this hasn't finished by the time the operator clicks.
        string? dirForCatchUp;
        lock (_captureLock)
            dirForCatchUp = _captureDir;
        if (dirForCatchUp != null)
            RunIncrementalExtraction(dirForCatchUp);
    }

    // Points RunScan at a capture folder from a PAST session (this process's
    // own _captureDir is in-memory only -- StartCapture always makes a brand-new timestamped
    // folder, so reopening the app or a crash/close mid-scan otherwise leaves no way back into an
    // already-fully-captured folder except re-flying the drone). Added 2026-09-28 after exactly
    // that happened: RunScan's subprocess got killed along with an orphan-process cleanup, leaving
    // 998 already-captured frames on disk with no in-app path to analyze them.
    [RelayCommand]
    private async Task LoadExistingCaptureFolder()
    {
        var dialog = new Microsoft.Win32.OpenFolderDialog
        {
            Title = "기존 촬영 폴더 불러오기 (분석/커버리지 미리보기용)",
            InitialDirectory = Directory.Exists(CaptureRootPath) ? CaptureRootPath : AppContext.BaseDirectory,
        };
        if (dialog.ShowDialog() != true)
            return;
        string dir = dialog.FolderName;

        // Picking the capture folder's own "output" subfolder (where the result tif lives) is the
        // natural thing to do when looking for a past result -- treat it as its parent capture
        // folder (2026-09-30, user picked BACK\output and got "no jpg images").
        if (string.Equals(Path.GetFileName(dir), "output", StringComparison.OrdinalIgnoreCase)
            && Directory.GetFiles(dir, "*_analysis_colmap.tif").Length > 0
            && Directory.GetParent(dir) is { } parentDir)
        {
            dir = parentDir.FullName;
        }

        // 2026-09-28: was "frame_*.jpg" only (this app's own live-capture naming) -- broadened to
        // any .jpg/.jpeg so a folder of real SD-card photos (e.g. DJI_0023.JPG, no "frame_" prefix
        // at all) can be loaded too, not just this app's own captures. Windows file matching is
        // already case-insensitive, so "*.jpg" alone also matches ".JPG"; ".jpeg" added for parity
        // with stitch_engine's own scan_images() extension set.
        string[] files = Directory.GetFiles(dir, "*.jpg")
            .Concat(Directory.GetFiles(dir, "*.jpeg"))
            .Distinct()
            .OrderBy(f => f, StringComparer.Ordinal)
            .ToArray();
        if (files.Length == 0)
        {
            StatusMessage = $"불러오기 실패 — {dir}에 jpg 이미지가 없습니다";
            return;
        }

        if (IsCapturing)
            StopCapture();

        // RunScan names its output "{facadeName}_analysis.tif" from
        // SanitizeForFolderName($"{SelectedBuilding}_{MeasurementLocation}") at capture time -- this
        // folder's own name is exactly that same string with StartCapture's "_yyyyMMdd_HHmmss"
        // suffix appended, so stripping that suffix back off recovers the original facadeName
        // regardless of whatever's currently selected in the 동/측정 장소 combo boxes.
        string folderBase = Path.GetFileName(dir);
        var m = System.Text.RegularExpressions.Regex.Match(folderBase, @"^(?<facade>.+)_\d{8}_\d{6}$");
        string recoveredFacadeName = m.Success ? m.Groups["facade"].Value : folderBase;

        ScanResultImage = null;
        HasScanResult = false;
        ScanLogText = "";
        _scanLogLines.Clear();
        ScanElapsedText = "";
        SelectedFrameImage = null;
        IsShowingSelectedFrame = false;
        CapturedFrames.Clear();

        lock (_captureLock)
        {
            _captureDir = dir;
            _capturedFrameCount = files.Length;
        }
        CaptureFolderText = dir;
        CapturedFrameCountDisplay = files.Length;
        _loadedFacadeNameOverride = recoveredFacadeName;
        StatusMessage = $"불러오는 중... 0/{files.Length}장 — {dir}";

        // Decode+resize off the UI thread (998 JPEGs synchronously here was enough to make the
        // window look unresponsive/like nothing had happened -- same class of issue as the scan-log
        // O(n^2) freeze). Each thumbnail is frozen on this background thread before being handed to
        // the UI thread to add, same pattern OnDecodedFrameReceived already uses for live capture,
        // and added one at a time so the sidebar visibly fills in rather than staying empty until
        // everything is ready (2026-09-28).
        const int ThumbnailSizePx = 120;
        int loaded = 0;
        await Task.Run(() =>
        {
            foreach (string path in files)
            {
                using var mat = Cv2.ImRead(path, ImreadModes.Color);
                if (mat.Empty())
                    continue;
                using var thumbMat = new Mat();
                Cv2.Resize(mat, thumbMat, new OpenCvSharp.Size(ThumbnailSizePx, ThumbnailSizePx));
                BitmapSource thumbnail = thumbMat.ToBitmapSource();
                thumbnail.Freeze();
                int doneSoFar = Interlocked.Increment(ref loaded);
                Application.Current.Dispatcher.BeginInvoke(() =>
                {
                    CapturedFrames.Add(new CapturedFrameItem(path) { ThumbnailSource = thumbnail });
                    HasCapturedFrames = true;
                    StatusMessage = $"불러오는 중... {doneSoFar}/{files.Length}장 — {dir}";
                });
            }
        });
        StatusMessage = $"불러옴 — {dir} ({CapturedFrames.Count}장, facade_id={recoveredFacadeName})";

        // A folder analyzed earlier already has its result -- show it right away instead of
        // requiring another full 분석 시작 just to look at it (2026-09-30).
        string outputDir = Path.Combine(dir, "output");
        string existingResult = Path.Combine(outputDir, $"{recoveredFacadeName}_analysis_colmap.tif");
        if (File.Exists(existingResult) && LoadScanResultImage(existingResult, out _))
        {
            HasScanResult = true;
            LoadUnmatchedImages(outputDir, recoveredFacadeName, dir);
            StatusMessage += $" — 이전 분석 결과 표시 ({File.GetLastWriteTime(existingResult):yyyy-MM-dd HH:mm})";
        }
    }

    // "스캔시작(스티칭->ColMap) 한번에 실행" -- runs previewer/tools/stitch_engine/stitch_folder.py
    // as a subprocess against the finished capture folder (same subprocess pattern
    // CheckCrackViewer's own "▶ 실행" button uses for tools/stitch_folder.py in the main repo).
    [RelayCommand]
    private async Task RunScan()
    {
        if (IsScanning)
            return;

        string? dir;
        lock (_captureLock)
            dir = _captureDir;
        if (dir == null || !Directory.Exists(dir))
        {
            StatusMessage = "캡처된 폴더가 없습니다 — 먼저 캡처를 시작하세요";
            return;
        }

        if (IsCapturing)
            StopCapture();

        IsScanning = true;
        IsShowingSelectedFrame = false;
        // Hide the previous result (e.g. the one 기존 폴더 불러오기 shows) so the live log is visible
        // while this run works; the new result replaces it when the scan finishes.
        ScanResultImage = null;
        HasScanResult = false;
        HasUnmatchedImages = false;
        ScanLogText = "";
        _scanLogLines.Clear();
        ScanElapsedText = "";
        ScanProgressPercent = 0;
        ScanStageText = "시작 중...";
        StatusMessage = "스캔 시작 — 스티칭 + CM 파이프라인 실행 중...";

        string engineDir = FindUpward(Path.Combine("tools", "stitch_engine", "stitch_folder.py")) is { } found
            ? Path.GetDirectoryName(found)!
            : Path.Combine(AppContext.BaseDirectory, "tools", "stitch_engine");
        string scriptPath = Path.Combine(engineDir, "stitch_folder.py");
        // StartCapture의 폴더명 조합(동+방향)과 반드시 동일해야 stitch_folder.py의
        // <facade_name>_analysis.tif 등 출력 파일명이 실제 캡처 폴더명과 일치한다.
        // LoadExistingCaptureFolder로 불러온 경우 동/측정 장소 드롭다운이 그 폴더를 만들 때 값과
        // 다를 수 있으므로, 폴더명 자체에서 복원한 값을 우선 사용.
        string facadeName = _loadedFacadeNameOverride ?? SanitizeForFolderName($"{SelectedBuilding}_{MeasurementLocation}");

        if (!File.Exists(scriptPath))
        {
            StatusMessage = $"stitch_folder.py를 찾을 수 없습니다 — {scriptPath}";
            IsScanning = false;
            return;
        }

        StartScanElapsedTimer();

        var psi = new ProcessStartInfo
        {
            FileName = "python",
            WorkingDirectory = engineDir,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        psi.ArgumentList.Add(scriptPath);
        psi.ArgumentList.Add(dir);
        psi.ArgumentList.Add(facadeName);
        psi.ArgumentList.Add("--profile");
        psi.ArgumentList.Add(AnalysisProfile == "노트북" ? "laptop" : "pc");

        try
        {
            using var proc = new Process { StartInfo = psi, EnableRaisingEvents = true };
            proc.OutputDataReceived += (_, e) => AppendScanLog(e.Data);
            proc.ErrorDataReceived += (_, e) => AppendScanLog(e.Data);
            proc.Start();
            // Tracked so Dispose() (see MainWindow.xaml.cs's OnClosing) can kill this if the window
            // closes mid-scan -- otherwise `using var proc` above only releases the .NET wrapper
            // when this method's scope unwinds normally, it does NOT kill the OS process, and
            // closing the app while `await WaitForExitAsync()` below is still pending tears down
            // this whole method without ever reaching that unwind. The orphaned python.exe (COLMAP)
            // then keeps running (and keeps a lock on the capture folder's output/ subfolder,
            // blocking deletion in Explorer) with nothing left to report back to -- found 2026-09-28
            // when the user tried to delete a capture folder right after closing the app mid-scan.
            _scanProcess = proc;
            proc.BeginOutputReadLine();
            proc.BeginErrorReadLine();
            await proc.WaitForExitAsync();
            StopScanElapsedTimer();
            string elapsed = ScanElapsedText;

            if (proc.ExitCode == 0)
            {
                ScanProgressPercent = 100;
                ScanStageText = "완료";
                string outputDir = Path.Combine(dir, "output");
                string analysisPath = Path.Combine(outputDir, $"{facadeName}_analysis_colmap.tif");
                if (LoadScanResultImage(analysisPath, out string? loadError))
                {
                    HasScanResult = true;
                    StatusMessage = $"스캔 완료 ({elapsed}) — {analysisPath}";
                }
                else
                {
                    StatusMessage = $"스캔 완료 ({elapsed}) — {outputDir} (결과 이미지 표시 실패: {loadError})";
                }
                LoadUnmatchedImages(outputDir, facadeName, dir);
            }
            else
            {
                ScanStageText = $"실패 (exit code {proc.ExitCode})";
                StatusMessage = $"스캔 실패 (exit code {proc.ExitCode}, {elapsed}) — 로그 확인";
            }
        }
        catch (Exception ex)
        {
            StatusMessage = $"스캔 실행 실패 — {ex.Message}";
            AppendScanLog($"[오류] {ex}");
        }
        finally
        {
            StopScanElapsedTimer();
            IsScanning = false;
            _scanProcess = null;
        }
    }

    private void StartScanElapsedTimer()
    {
        _scanStopwatch.Restart();
        ScanElapsedText = FormatElapsed(TimeSpan.Zero);
        _scanElapsedTimer ??= new System.Windows.Threading.DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        _scanElapsedTimer.Tick -= OnScanElapsedTick;
        _scanElapsedTimer.Tick += OnScanElapsedTick;
        _scanElapsedTimer.Start();
    }

    // Idempotent: RunScan calls it right after the process exits and again in finally.
    private void StopScanElapsedTimer()
    {
        if (!_scanStopwatch.IsRunning)
            return;
        _scanStopwatch.Stop();
        _scanElapsedTimer?.Stop();
        ScanElapsedText = FormatElapsed(_scanStopwatch.Elapsed);
    }

    private void OnScanElapsedTick(object? sender, EventArgs e) =>
        ScanElapsedText = FormatElapsed(_scanStopwatch.Elapsed);

    private static string FormatElapsed(TimeSpan t) =>
        t.TotalHours >= 1 ? $"{(int)t.TotalHours}:{t.Minutes:00}:{t.Seconds:00}" : $"{t.Minutes:00}:{t.Seconds:00}";

    // Loads the stitched analysis mosaic (.tif, potentially very large) via OpenCvSharp rather
    // than WPF's own BitmapImage -- this codebase already standardized on
    // OpenCvSharp.WpfExtensions.ToBitmapSource() for exactly this Mat->WPF-Image conversion
    // (see the deleted FacadeStitcher's own use of it) and OpenCvSharp's TIFF codec handles the
    // multi-page/large-canvas output stitch_folder.py produces more predictably than WIC's.
    // Path of the result currently shown (set by LoadScanResultImage), for OpenScanResult.
    private string? _scanResultPath;

    // Grid drawn over the result (2026-09-30, user request) so a missed spot can be named by cell
    // ("C4"). Built with the result's own aspect ratio so the overlay Image lines up with it.
    [ObservableProperty] private BitmapSource? _scanGridOverlay;
    [ObservableProperty] private bool _showScanGrid = true;
    // stitch_engine renders the result at 100 px per meter of wall
    // (rectification.facade_plane_from_reconstruction's px_per_m default).
    private const double ResultPxPerMeter = 100.0;
    private const double GridSpacingMeters = 5.0;

    private static BitmapSource BuildGridOverlay(int resultWidth, int resultHeight)
    {
        // Drawn at a reduced size (same aspect ratio) -- a full-size BGRA overlay of a ~6000px
        // result would be ~90MB for a few lines of text.
        double scale = Math.Min(1.0, 2400.0 / Math.Max(resultWidth, resultHeight));
        int w = Math.Max(1, (int)Math.Round(resultWidth * scale));
        int h = Math.Max(1, (int)Math.Round(resultHeight * scale));
        double step = GridSpacingMeters * ResultPxPerMeter * scale;

        using var overlay = new Mat(h, w, MatType.CV_8UC4, Scalar.All(0));
        var color = new Scalar(0, 230, 255, 220); // BGRA: yellow, mostly opaque
        int thickness = Math.Max(1, (int)Math.Round(w / 1200.0));
        int cols = (int)Math.Ceiling(w / step);
        int rows = (int)Math.Ceiling(h / step);
        for (int c = 0; c <= cols; c++)
        {
            int x = Math.Min(w - 1, (int)Math.Round(c * step));
            Cv2.Line(overlay, new OpenCvSharp.Point(x, 0), new OpenCvSharp.Point(x, h - 1), color, thickness);
        }
        for (int r = 0; r <= rows; r++)
        {
            int y = Math.Min(h - 1, (int)Math.Round(r * step));
            Cv2.Line(overlay, new OpenCvSharp.Point(0, y), new OpenCvSharp.Point(w - 1, y), color, thickness);
        }

        double fontScale = Math.Max(0.3, step / 180.0);
        for (int r = 0; r < rows; r++)
        {
            for (int c = 0; c < cols; c++)
            {
                string label = GridColumnLabel(c) + (r + 1);
                OpenCvSharp.Size size = Cv2.GetTextSize(label, HersheyFonts.HersheySimplex, fontScale, thickness, out _);
                var origin = new OpenCvSharp.Point((int)Math.Round(c * step) + 4 * thickness,
                                                   (int)Math.Round(r * step) + size.Height + 4 * thickness);
                Cv2.PutText(overlay, label, origin, HersheyFonts.HersheySimplex, fontScale, color, thickness, LineTypes.AntiAlias);
            }
        }

        BitmapSource bitmap = overlay.ToBitmapSource();
        bitmap.Freeze();
        return bitmap;
    }

    // 0 -> A, 25 -> Z, 26 -> AA, ...
    private static string GridColumnLabel(int index)
    {
        string label = "";
        for (int i = index; i >= 0; i = i / 26 - 1)
            label = (char)('A' + i % 26) + label;
        return label;
    }

    // "결과 열기" (2026-09-30): opens the result tif in the Windows default image viewer, where it can
    // be zoomed to check small missed spots -- the in-app view only fits the whole facade on screen.
    [RelayCommand(CanExecute = nameof(HasScanResult))]
    private void OpenScanResult()
    {
        if (_scanResultPath == null || !File.Exists(_scanResultPath))
        {
            StatusMessage = "열 결과 파일이 없습니다";
            return;
        }
        try
        {
            Process.Start(new ProcessStartInfo(_scanResultPath) { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            StatusMessage = $"결과 열기 실패 — {ex.Message}";
        }
    }

    private bool LoadScanResultImage(string path, out string? error)
    {
        error = null;
        if (!File.Exists(path))
        {
            error = "파일 없음";
            return false;
        }
        try
        {
            using Mat mat = Cv2.ImRead(path, ImreadModes.Color);
            if (mat.Empty())
            {
                error = "이미지 디코드 실패";
                return false;
            }
            BitmapSource bitmap = mat.ToBitmapSource();
            bitmap.Freeze(); // cross-thread-safe + required before handing to the UI-bound property
            ScanResultImage = bitmap;
            _scanResultPath = path;
            ScanGridOverlay = BuildGridOverlay(mat.Width, mat.Height);
            return true;
        }
        catch (Exception ex)
        {
            error = ex.Message;
            return false;
        }
    }

    private sealed class UnmatchedImagesReportDto
    {
        [JsonPropertyName("never_matched")] public List<string> NeverMatched { get; set; } = new();
        [JsonPropertyName("colmap_registration_failed")] public List<string> ColmapRegistrationFailed { get; set; } = new();
    }

    // Reads {facadeName}_unmatched_images.json (written by src/pipeline/runner.py, see
    // 2026-09-08 CLAUDE.local.md entry) and populates UnmatchedImages so an operator can act on
    // exactly the images the pipeline itself determined never contributed to the stitched
    // result, instead of guessing from how a photo looks. Missing file / parse failure just
    // leaves the panel empty rather than surfacing as a scan failure -- this is a secondary,
    // best-effort diagnostic on top of an already-successful scan.
    private void LoadUnmatchedImages(string outputDir, string facadeName, string sourceDir)
    {
        UnmatchedImages.Clear();
        string path = Path.Combine(outputDir, $"{facadeName}_unmatched_images.json");
        if (!File.Exists(path))
        {
            HasUnmatchedImages = false;
            return;
        }
        try
        {
            var dto = JsonSerializer.Deserialize<UnmatchedImagesReportDto>(
                File.ReadAllText(path), new JsonSerializerOptions { PropertyNameCaseInsensitive = true });
            if (dto == null)
            {
                HasUnmatchedImages = false;
                return;
            }

            const int ThumbnailSizePx = 120; // same convention as OnDecodedFrameReceived's CapturedFrameItem thumbnails
            void AddItems(IEnumerable<string> stems, string reason)
            {
                foreach (string stem in stems)
                {
                    string? filePath = Directory.EnumerateFiles(sourceDir)
                        .FirstOrDefault(f => Path.GetFileNameWithoutExtension(f).Equals(stem, StringComparison.OrdinalIgnoreCase));
                    if (filePath == null)
                        continue;

                    BitmapSource? thumbnail = null;
                    try
                    {
                        using Mat mat = Cv2.ImRead(filePath, ImreadModes.Color);
                        if (!mat.Empty())
                        {
                            using var thumbMat = new Mat();
                            Cv2.Resize(mat, thumbMat, new OpenCvSharp.Size(ThumbnailSizePx, ThumbnailSizePx));
                            thumbnail = thumbMat.ToBitmapSource();
                            thumbnail.Freeze();
                        }
                    }
                    catch (Exception)
                    {
                        // Best-effort thumbnail -- still list the entry (name + reason) even if
                        // this particular file can't be decoded for a preview.
                    }
                    UnmatchedImages.Add(new UnmatchedImageItem(filePath, reason) { ThumbnailSource = thumbnail });
                }
            }
            AddItems(dto.NeverMatched, "1차 매칭 실패");
            AddItems(dto.ColmapRegistrationFailed, "COLMAP 등록 실패");
            HasUnmatchedImages = UnmatchedImages.Count > 0;
        }
        catch (Exception ex)
        {
            AppendScanLog($"[경고] 미매칭 이미지 목록 로드 실패 — {ex.Message}");
            HasUnmatchedImages = false;
        }
    }

    // "제외" button on the 미매칭 이미지 panel -- same move-to-"excluded"-subfolder convention as
    // RemoveExcludedFrames (never deletes), so a subsequent RunScan's directory-wide glob skips
    // these files. Operates on IsSelected (checked = exclude), default true since being in this
    // list already means the pipeline flagged the image as unused.
    [RelayCommand]
    private void ExcludeUnmatchedImages()
    {
        string? dir;
        lock (_captureLock)
            dir = _captureDir;
        if (dir == null)
            return;
        string excludedDir = Path.Combine(dir, "excluded");

        foreach (var item in UnmatchedImages.Where(i => i.IsSelected).ToList())
        {
            try
            {
                Directory.CreateDirectory(excludedDir);
                string dest = Path.Combine(excludedDir, Path.GetFileName(item.FilePath));
                if (File.Exists(item.FilePath) && !File.Exists(dest))
                    File.Move(item.FilePath, dest);
                UnmatchedImages.Remove(item);
            }
            catch (IOException)
            {
                // Best-effort, matching RemoveExcludedFrames' own convention -- leaves this one
                // in the list so the operator can see it wasn't actually excluded and try again.
            }
            catch (UnauthorizedAccessException)
            {
            }
        }
        HasUnmatchedImages = UnmatchedImages.Count > 0;
    }

    // 2026-09-21 사용자 요청, CapturedFrames 쪽과 동일한 이유·동일한 토글 방식 -- 여기는
    // IsSelected가 반대 극성(체크=제외 대상)이라 "전체 선택" 다음 클릭은 "전부 제외 대상으로
    // 체크"가 되지만, 버튼 자체의 동작(지금 상태의 반대로 일괄 세팅)은 왼쪽과 동일하다.
    [RelayCommand]
    private void ToggleSelectAllUnmatchedImages()
    {
        bool allSelected = UnmatchedImages.Count > 0 && UnmatchedImages.All(i => i.IsSelected);
        bool next = !allSelected;
        foreach (var item in UnmatchedImages)
            item.IsSelected = next;
    }

    // Bounded to the last MaxScanLogLines lines -- a COLMAP run over ~1000 frames emits several
    // thousand lines total, and the old `ScanLogText += line + "\n"` reallocated and copied the
    // *entire* accumulated string on every single line (classic O(n^2) growth). That queued up
    // Dispatcher.BeginInvoke callbacks faster than the UI thread could drain them as the log grew,
    // which is what looked like the whole app locking up partway through a scan (2026-09-28).
    private readonly Queue<string> _scanLogLines = new();
    // Set only while RunScan's subprocess is actually running -- see its own comment on why
    // Dispose() needs this to avoid orphaning the stitch_folder.py/COLMAP child on window close.
    private Process? _scanProcess;
    private const int MaxScanLogLines = 1000;

    private void AppendScanLog(string? line)
    {
        if (line == null)
            return;
        TryUpdateScanProgress(line);
        Application.Current.Dispatcher.BeginInvoke(() =>
        {
            _scanLogLines.Enqueue(line);
            while (_scanLogLines.Count > MaxScanLogLines)
                _scanLogLines.Dequeue();
            ScanLogText = string.Join("\n", _scanLogLines) + "\n";
        });
    }

    // stitch_folder.py (previewer/tools/stitch_engine) emits one JSON object per log line
    // (src/common/logging.py's JsonFormatter) with a "stage" field matching the pipeline's own
    // state-machine (CLAUDE.local.md #32). MATCH_GEOMETRY/PREVIEW_UPDATED additionally carry a
    // "progress": "i/total" field for their per-pair/per-preview loops -- the two stages that
    // actually take most of the wall-clock time, so they're the only ones interpolated instead
    // of jumping straight to a fixed checkpoint. Percentages below are hand-picked checkpoints,
    // not a real time estimate -- good enough for "still moving" vs. "stuck", which is all an
    // operator needs from a progress bar here.
    private void TryUpdateScanProgress(string line)
    {
        JsonDocument doc;
        try
        {
            doc = JsonDocument.Parse(line);
        }
        catch (JsonException)
        {
            return; // not a JSON log line (stray print()/traceback text) -- ignore
        }

        using (doc)
        {
            if (!doc.RootElement.TryGetProperty("stage", out var stageProp) || stageProp.ValueKind != JsonValueKind.String)
                return;
            string stage = stageProp.GetString()!;

            string? progressText = doc.RootElement.TryGetProperty("progress", out var progProp) && progProp.ValueKind == JsonValueKind.String
                ? progProp.GetString()
                : null;
            double progressFraction = 0;
            if (progressText != null)
            {
                // COLMAP_MAPPING_PROGRESS sends "~85/216" (approximate) -- strip the "~".
                var parts = progressText.TrimStart('~').Split('/');
                if (parts.Length == 2 && double.TryParse(parts[0], out double done) && double.TryParse(parts[1], out double total) && total > 0)
                    progressFraction = Math.Min(1.0, done / total);
            }

            // 2026-10-05: the COLMAP-only path (colmap.mode: always, what 분석 시작 runs now) emits
            // its own stages; only the old H-chain stages were mapped, so the bar sat at
            // "5% -- 이미지 메타데이터 읽는 중" for the whole run. Percent ranges follow the measured
            // laptop timing on FRONT: extract ~1.5 min, LoFTR ~9, mapping ~11-15, mosaic ~2.
            double? colmapPercent = stage switch
            {
                "PHOTO_SPACING" => 3,
                "COLMAP_EXTRACT" => 4,
                "COLMAP_LOFTR_PROGRESS" => 8 + progressFraction * 37,
                "COLMAP_MAPPING" => 45,
                "COLMAP_MAPPING_PROGRESS" => 45 + progressFraction * 45,
                "COLMAP_ONLY" => 90,
                "GPS_MISMATCH_DETECTED" or "OFF_WALL_DETECTED" => 92,
                "GAP_FILL" => 97,
                _ => null,
            };
            string? colmapLabel = stage switch
            {
                "PHOTO_SPACING" => "겹치는 사진 정리 중",
                "COLMAP_EXTRACT" => "사진 정보 읽는 중",
                "COLMAP_LOFTR_MATCH" or "COLMAP_LOFTR_PROGRESS" => $"사진 매칭 중 (GPU){(progressText != null ? $" {progressText}" : "")}",
                "COLMAP_MAPPING" or "COLMAP_MAPPING_ATTEMPT" => "카메라 위치 계산 중",
                "COLMAP_MAPPING_PROGRESS" => $"카메라 위치 계산 중 ({progressText})",
                "COLMAP_ONLY" or "GPS_MISMATCH_DETECTED" or "OFF_WALL_DETECTED" => "벽면 정사영상 만드는 중",
                "GAP_FILL" => "빈 곳 채우는 중",
                _ => null,
            };
            if (colmapLabel != null)
            {
                Application.Current.Dispatcher.BeginInvoke(() =>
                {
                    // never move backwards (e.g. "~218/216" overshoot, repeated MATCH events)
                    if (colmapPercent is double cp && cp > ScanProgressPercent)
                        ScanProgressPercent = cp;
                    ScanStageText = colmapLabel;
                });
                return;
            }

            double? percent = stage switch
            {
                "METADATA_PARSED" => 2,
                "PAIR_GRAPH_BUILT" => 10,
                "MATCH_GEOMETRY" => 10 + progressFraction * 55,
                "GEOMETRY_SOLVED" => 65,
                "PREVIEW_UPDATED" => 65 + progressFraction * 20,
                "STITCHED" => 85,
                "COLMAP_FALLBACK" => 92,
                "RECTIFIED_COLMAP" => 98,
                "DONE" => 100,
                _ => null, // NEEDS_MANUAL_REVIEW/FAILED_GEOMETRY/etc: keep whatever percent we're already at
            };

            string label = stage switch
            {
                "METADATA_PARSED" => "이미지 메타데이터 읽는 중",
                "PAIR_GRAPH_BUILT" => "이미지 쌍 구성 중",
                "MATCH_GEOMETRY" => $"특징점 매칭 중 ({progressText})",
                "GEOMETRY_SOLVED" => "기하 정합 완료",
                "PREVIEW_UPDATED" => $"스티칭 중 ({progressText})",
                "STITCHED" => "스티칭 완료",
                "NEEDS_MANUAL_REVIEW" => "보정 실행 중...",
                "COLMAP_FALLBACK" => "CM 보정 완료",
                "RECTIFIED_COLMAP" => "CM 기반 재정렬 완료",
                "DONE" => "완료",
                "FAILED_GEOMETRY" => "실패 — 통과한 이미지 쌍 없음",
                _ => ScanStageText,
            };

            Application.Current.Dispatcher.BeginInvoke(() =>
            {
                if (percent is double p)
                    ScanProgressPercent = p;
                ScanStageText = label;
            });
        }
    }

    [RelayCommand]
    private void Reset()
    {
        if (IsCapturing)
            StopCapture();

        lock (_captureLock)
        {
            _captureDir = null;
            _capturedFrameCount = 0;
        }
        _loadedFacadeNameOverride = null;
        CapturedFrameCountDisplay = 0;
        CaptureFolderText = "";
        ScanLogText = "";
        _scanLogLines.Clear();
        ScanElapsedText = "";
        ScanResultImage = null;
        HasScanResult = false;
        CapturedFrames.Clear();
        HasCapturedFrames = false;
        SelectedFrameImage = null;
        IsShowingSelectedFrame = false;
        MeasurementLocation = "";
        SensorFramesReceived = 0;
        VideoPacketsReceived = 0;
        StatusMessage = "초기화됨 — 다음 면 촬영 대기";
    }

    private void OnSensorFrameReceived(SensorFrame frame)
    {
        Application.Current.Dispatcher.BeginInvoke(() => SensorFramesReceived++);
    }

    private void OnVideoPacketReceived(VideoPacket packet)
    {
        Application.Current.Dispatcher.BeginInvoke(() => VideoPacketsReceived++);
    }

    // Runs on the native DDS listener thread (see DdsBridgeService's own doc comment) -- must
    // stay reasonably cheap, but unlike the old pipeline there's no separate worker thread to
    // hand off to: capture-only has no live preview to protect from stalling, and resize+JPEG
    // encode of one 640x640 frame at a throttled ~2fps cadence is fast enough to do inline here.
    private void OnDecodedFrameReceived(DecodedVideoFrame frame)
    {
        if (!IsCapturing)
            return;

        DateTime now = DateTime.UtcNow;
        if (now - _lastCaptureUtc < CaptureInterval)
            return;
        _lastCaptureUtc = now;

        string? dir;
        int index;
        lock (_captureLock)
        {
            dir = _captureDir;
            if (dir == null)
                return;
            index = _capturedFrameCount++;
        }

        using var mat = Mat.FromPixelData((int)frame.Height, (int)frame.Width, MatType.CV_8UC3, frame.Bgr, (int)frame.Stride);
        using var resized = new Mat();
        Cv2.Resize(mat, resized, new OpenCvSharp.Size(CaptureSizePx, CaptureSizePx));

        string path = Path.Combine(dir, $"frame_{index:D5}.jpg");
        Cv2.ImWrite(path, resized, new ImageEncodingParam(ImwriteFlags.JpegQuality, 90));

        // Thumbnail generated from the already-resized 640x640 Mat (no re-decode from disk),
        // downscaled further to keep a long capture session's memory footprint small -- a full
        // capture can run to hundreds/thousands of frames at ~2fps, and keeping every one at full
        // 640x640 in a bound ObservableCollection would add up fast. Frozen so it can be created
        // on this native DDS thread and safely handed to the UI thread below.
        const int ThumbnailSizePx = 120;
        using var thumbMat = new Mat();
        Cv2.Resize(resized, thumbMat, new OpenCvSharp.Size(ThumbnailSizePx, ThumbnailSizePx));
        BitmapSource thumbnail = thumbMat.ToBitmapSource();
        thumbnail.Freeze();

        Application.Current.Dispatcher.BeginInvoke(() =>
        {
            CapturedFrameCountDisplay = index + 1;
            StatusMessage = $"캡처 중 — {index + 1}장 저장됨 ({dir})";

            var item = new CapturedFrameItem(path) { ThumbnailSource = thumbnail };
            CapturedFrames.Add(item);
            HasCapturedFrames = true;
        });

        MaybeTriggerIncrementalExtraction(dir, index);
    }

    // Fires roughly every IncrementalExtractionFrameInterval captured frames so
    // pycolmap.extract_features runs periodically DURING capture instead of all being deferred to
    // RunScan -- see colmap_runner.py's 2026-09-28 "reuse an already-populated database.db"
    // comment for the full design (user request: 2fps was producing "너무 많아서" too many frames
    // for RunScan to churn through in one go at the end). Guarded by Interlocked so an overlapping
    // trigger (a slow prior call still running when the next multiple lands) just skips instead of
    // running two pycolmap.extract_features calls against the same database.db concurrently --
    // SQLite is single-writer, and that's the exact "database is locked" class of bug already hit
    // once today.
    private const int IncrementalExtractionFrameInterval = 10;
    private int _incrementalExtractionInFlight; // 0/1, guarded via Interlocked, not a bool (see above)

    private void MaybeTriggerIncrementalExtraction(string capturedDir, int frameIndexJustSaved)
    {
        if (frameIndexJustSaved == 0 || (frameIndexJustSaved + 1) % IncrementalExtractionFrameInterval != 0)
            return;
        RunIncrementalExtraction(capturedDir);
    }

    // Also called from StopCapture (see there) to mop up whatever tail of frames landed since the
    // last periodic trigger, so "분석 시작" clicked right after stopping still gets the full
    // benefit instead of missing the last <IncrementalExtractionFrameInterval frames.
    private void RunIncrementalExtraction(string capturedDir)
    {
        if (Interlocked.CompareExchange(ref _incrementalExtractionInFlight, 1, 0) != 0)
            return; // a previous call is still running -- the next periodic trigger (or the
                     // StopCapture catch-up call) will pick up whatever this one missed

        string facadeName = _loadedFacadeNameOverride
            ?? SanitizeForFolderName($"{SelectedBuilding}_{MeasurementLocation}");
        string workspaceDir = Path.Combine(capturedDir, "output", $"{facadeName}_colmap");
        string engineDir = FindUpward(Path.Combine("tools", "stitch_engine", "incremental_extract.py")) is { } found
            ? Path.GetDirectoryName(found)!
            : Path.Combine(AppContext.BaseDirectory, "tools", "stitch_engine");
        string scriptPath = Path.Combine(engineDir, "incremental_extract.py");
        if (!File.Exists(scriptPath))
        {
            Interlocked.Exchange(ref _incrementalExtractionInFlight, 0);
            return;
        }

        var psi = new ProcessStartInfo
        {
            FileName = "python",
            WorkingDirectory = engineDir,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        psi.ArgumentList.Add(scriptPath);
        psi.ArgumentList.Add(capturedDir);
        psi.ArgumentList.Add(workspaceDir);

        _ = Task.Run(async () =>
        {
            try
            {
                using var proc = new Process { StartInfo = psi };
                proc.Start();
                // Drain both pipes so a slow/verbose run can't deadlock on a full OS pipe buffer,
                // even though nothing here displays the output -- this is a quiet background
                // helper the operator never watches directly; RunScan's own pass through the same
                // pycolmap call is what surfaces progress in the visible scan log.
                var stdoutTask = proc.StandardOutput.ReadToEndAsync();
                var stderrTask = proc.StandardError.ReadToEndAsync();
                await proc.WaitForExitAsync();
                await Task.WhenAll(stdoutTask, stderrTask);
            }
            catch
            {
                // Best-effort only -- RunScan's own extraction call is the fallback of record if
                // this failed for any reason (missing pycolmap, disk issue, etc.), so a failure
                // here must never surface as a capture-time error to the operator.
            }
            finally
            {
                Interlocked.Exchange(ref _incrementalExtractionInFlight, 0);
            }
        });
    }

    /// <summary>Raised by LogoutCommand -- App.xaml.cs subscribes to this to close MainWindow and
    /// loop back to a fresh LoginWindow without shutting the whole process down (same pattern as
    /// CheckCrackViewer's MainViewModel).</summary>
    public event Action? LogoutRequested;

    [RelayCommand]
    private void Logout() => LogoutRequested?.Invoke();

    // "선택 제외" button: checkbox polarity matches TransferSettingsWindow's own review panel for
    // consistency across the app (checked = included/keep, default true) -- so this processes
    // every currently-UNCHECKED frame, same as that window's own OnReviewExcludeClick, rather than
    // reacting live to each checkbox toggle. Moves each into the capture directory's "excluded"
    // subfolder (never deletes -- RunScan's directory-wide glob, see its own comment, simply never
    // sees anything under there) and removes it from the visible list.
    [RelayCommand]
    private void RemoveExcludedFrames()
    {
        string? dir;
        lock (_captureLock)
            dir = _captureDir;
        if (dir == null)
            return;
        string excludedDir = Path.Combine(dir, "excluded");

        foreach (var item in CapturedFrames.Where(i => !i.IsIncluded).ToList())
        {
            try
            {
                Directory.CreateDirectory(excludedDir);
                string dest = Path.Combine(excludedDir, Path.GetFileName(item.FilePath));
                if (File.Exists(item.FilePath) && !File.Exists(dest))
                    File.Move(item.FilePath, dest);
                CapturedFrames.Remove(item);
            }
            catch (IOException)
            {
                // Best-effort, matching this project's existing convention for non-critical file
                // housekeeping (e.g. TransferSettingsWindow's CleanupStagingFolders) -- leaves this
                // one frame in the list (not removed) so the operator can see it wasn't actually
                // excluded and try again, rather than silently losing track of it.
            }
            catch (UnauthorizedAccessException)
            {
            }
        }
    }

    // 2026-09-21 사용자 요청("좌/우 썸네일 리스트 모든 내용 선택/미선택 체크 버튼 필요", 이후
    // "전체 선택/전체 해제 하나의 버튼으로"): 캡처 세션 하나가 수백~수천 장까지 가는 게
    // 흔해서, "이 세션은 거의 다 버리고 몇 장만 남긴다"/"거의 다 남기고 몇 장만 뺀다" 같은
    // 전부-먼저 시나리오에서 매번 하나씩 클릭하는 게 비현실적 -- IsIncluded 값만 일괄 세팅
    // (파일은 안 건드림, 실제 이동은 여전히 RemoveExcludedFramesCommand가 함). 버튼 하나로
    // 토글: 지금 전부 체크돼 있으면 전체 해제, 아니면(하나도 없거나 일부만) 전체 선택 --
    // "다음 클릭이 뭘 할지"가 항상 "지금 상태와 반대"로 예측 가능하다.
    [RelayCommand]
    private void ToggleSelectAllCapturedFrames()
    {
        bool allIncluded = CapturedFrames.Count > 0 && CapturedFrames.All(i => i.IsIncluded);
        bool next = !allIncluded;
        foreach (var item in CapturedFrames)
            item.IsIncluded = next;
    }

    private static string SanitizeForFolderName(string s)
    {
        foreach (char c in Path.GetInvalidFileNameChars())
            s = s.Replace(c, '_');
        return s.Trim();
    }

    public void Dispose()
    {
        _dds.SensorFrameReceived -= OnSensorFrameReceived;
        _dds.VideoPacketReceived -= OnVideoPacketReceived;
        _dds.DecodedFrameReceived -= OnDecodedFrameReceived;
        _dds.Dispose();

        // Kill any still-running scan subprocess (python stitch_folder.py / COLMAP) rather than
        // orphaning it -- see _scanProcess's doc comment.
        try
        {
            if (_scanProcess is { HasExited: false } proc)
                proc.Kill(entireProcessTree: true);
        }
        catch (InvalidOperationException) { /* already exited between the check and Kill() */ }
    }
}
