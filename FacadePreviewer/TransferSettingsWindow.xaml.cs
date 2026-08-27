using System.Collections.ObjectModel;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using FacadePreviewer.Services;
using Microsoft.Win32;

namespace FacadePreviewer;

/// <summary>High-resolution facade image transfer dialog (rsync-over-ssh -> DDS-Router's
/// FacadeImageBridge watch root), plus the operator-visible tail of CrackVisionArchiveManager's
/// already-automatic zip+DB archive job (progress/cancel/completion via facade_storage_msgs --
/// see FacadeStorageStatusService). Deliberately separate from MainViewModel/MainWindow's own
/// capture+scan flow -- this is a self-contained, code-behind-driven dialog (no dedicated
/// ViewModel) since it has no state the rest of the app needs to observe.</summary>
public partial class TransferSettingsWindow : Window
{
    // Maps a local subfolder name to the canonical direction code FacadeImageBridge/main.cpp's
    // parse_hierarchy() actually requires on the wire (FRONT/BACK/LEFT/RIGHT/ROOF/OTHER, see that
    // file's own valid_directions set) -- local folders are free to use whatever naming
    // convention a given capture actually used (Korean is common here -- not every operator
    // reads English -- and this project's own sample DJI data is organized as 앞/뒤/좌/우/TOP, not
    // the English codes) as long as each one maps to one of these six wire values. There is no
    // way to recognize arbitrary Korean phrasing without *some* fixed lookup table -- a computer
    // has no innate sense that "앞" means "front" -- so this list is intentionally generous with
    // realistic variants rather than a single exact word per direction; add more here if a real
    // folder-naming convention isn't covered. StringComparer.OrdinalIgnoreCase covers
    // "front"/"Front"/"FRONT" for the English keys; Hangul keys are matched as-is (Ordinal
    // case-folding is a no-op on non-ASCII text, so one comparer safely covers both).
    private static readonly Dictionary<string, string> DirectionAliases = new(StringComparer.OrdinalIgnoreCase)
    {
        ["FRONT"] = "FRONT",
        ["앞"] = "FRONT",
        ["앞면"] = "FRONT",
        ["정면"] = "FRONT",
        ["전면"] = "FRONT",
        ["BACK"] = "BACK",
        ["뒤"] = "BACK",
        ["뒤면"] = "BACK",
        ["후면"] = "BACK",
        ["배면"] = "BACK",
        ["LEFT"] = "LEFT",
        ["좌"] = "LEFT",
        ["좌측"] = "LEFT",
        ["좌면"] = "LEFT",
        ["왼쪽"] = "LEFT",
        ["RIGHT"] = "RIGHT",
        ["우"] = "RIGHT",
        ["우측"] = "RIGHT",
        ["우면"] = "RIGHT",
        ["오른쪽"] = "RIGHT",
        ["ROOF"] = "ROOF",
        ["TOP"] = "ROOF",
        ["위"] = "ROOF",
        ["위층"] = "ROOF",
        ["옥상"] = "ROOF",
        ["지붕"] = "ROOF",
        ["상단"] = "ROOF",
        ["OTHER"] = "OTHER",
        ["기타"] = "OTHER",
        ["기타면"] = "OTHER",
    };

    // Fixed project-wide DDS convention (domain 0, ROS2 rmw_fastrtps-style "rt/" topic prefix,
    // matching every other bridge in this project -- DDS Monitor flagged facade_storage_*/
    // facade_image_* as the only topics missing it) -- matches app_config.h's
    // facade_image_domain_id/facade_storage_*_topic defaults on the MngData side exactly; not
    // something this dialog needs its own settings for.
    private const int DdsDomainId = 0;
    private const string FeedbackTopic = "rt/facade_storage_feedback";
    private const string ResultTopic = "rt/facade_storage_result";
    private const string CancelTopic = "rt/facade_storage_cancel_request";
    private const string RequirementsTopic = "rt/facade_storage_requirements";
    private const string FinalizeTopic = "rt/facade_storage_finalize";

    private enum Phase { Idle, Transferring, Storing }
    private enum RetryChoice { Restart, Resume, Abort }

    // 2026-08-27: "진행 단계" 패널의 각 행 상태 -- ThemedDialog.ShowConfirm 팝업으로 물어보던
    // "분석 시작하시겠습니까?"를 이 창 안 전용 섹션으로 옮기면서 함께 추가. Skipped는 "아니오"를
    // 선택한 경우(실패는 아니지만 그 다음 단계는 진행 안 함)에만 씀.
    private enum StageState { Pending, Active, Done, Failed, Skipped }

    // OnStorageResult에서 저장 성공을 확인한 시점부터, 운용자가 AnalysisConfirmPanel의 예/아니오를
    // 실제로 클릭할 때까지 필요한 값들을 들고 있는다 -- 예전엔 ShowConfirm이 그 자리에서 블로킹
    // 호출이라 지역 변수로 충분했지만, 인라인 버튼은 별도 Click 핸들러에서 나중에 실행되므로 필드로
    // 옮겨야 함.
    private FacadeStorageResult? _awaitingAnalysisConfirmResult;

    private readonly string _ddsHost;
    private readonly int _ddsPort;
    private readonly string _ddsLocalInterface;

    private RsyncTransferService? _transfer;
    private FacadeStorageStatusService? _storageStatus;
    // facade_analysis_msgs (domain 30) -- long-lived for this window's whole lifetime, unlike
    // _storageStatus (recreated per transfer): "분석 시작"/재시도/정지 can happen well after the
    // triggering transfer's own storage-status handle has already been disposed. Only one
    // archive_id is tracked at a time (same simplification _pendingCompany/_pendingBuilding
    // already accepts for storage status) -- starting a new analysis for a different archive
    // replaces tracking of the previous one.
    private AnalysisCommandService? _analysisCommand;
    private long? _pendingAnalysisArchiveId;
    private List<(string Direction, string LocalFolder)>? _detectedBatch;
    private Phase _phase = Phase.Idle;
    private string _pendingCompany = "";
    private string _pendingBuilding = "";
    // Set when THIS job's transfer begins -- OnStorageResult uses it to discard a stale/replayed
    // Result for an OLDER job on the same (company, building), see FacadeStorageResult's own
    // comment on why the Result topic can replay one at reader-startup.
    private long _pendingJobStartedAtEpochMs;

    // Same "config" folder facade_targets.json already lives in, next to the exe -- consistent
    // with this project's existing convention for field-laptop-portable settings (see that
    // catalog's own Load() call below), not %APPDATA% or any new location.
    private readonly string _settingsPath = Path.Combine(AppContext.BaseDirectory, "config", "transfer_settings.ini");

    // Pre-transfer review panel state. _reviewPlan mirrors OnTransferClick's own `plan` (direction
    // + local folder pairs) but is built earlier, as soon as directions are known, so the operator
    // can review before ever clicking 전송 -- see RefreshReviewPanel. _reviewItemsByDirection
    // caches generated thumbnails per direction (regenerating on every combo switch would be
    // wasteful and slow for a 50+ photo direction). _excludedFilesByDirection only holds items
    // already removed from view via "선택 제외" (OnReviewExcludeClick) -- GetExcludedFileSet unions
    // that with whatever is currently unchecked-but-still-visible, since IsIncluded alone is
    // already authoritative (see ReviewImageItem's own comment) and clicking the button is not
    // required for an exclusion to actually take effect at transfer time.
    private List<(string Direction, string LocalFolder)>? _reviewPlan;
    private readonly Dictionary<string, ObservableCollection<ReviewImageItem>> _reviewItemsByDirection = new();
    private readonly Dictionary<string, HashSet<string>> _excludedFilesByDirection = new();
    private CancellationTokenSource? _thumbnailLoadCts;
    // Currently highlighted thumbnail (see SelectReviewItem/ViewOriginalButton) -- cleared
    // whenever the review panel loads a different direction's thumbnails, since the
    // ReviewImageItem instances themselves are recreated per direction (see
    // LoadDirectionThumbnailsAsync), so a stale reference here would point at an item no longer
    // in any visible collection.
    private ReviewImageItem? _selectedReviewItem;
    private bool _isPanningOriginalPreview;
    private System.Windows.Point _originalPreviewPanStart;
    // Temp copies of a direction's included-only files, built by PrepareTransferFolder only when
    // that direction actually has an exclusion -- rsync has no notion of "everything in this
    // folder except these three files", so excluding anything means staging a filtered copy rather
    // than pointing rsync at the operator's original folder directly. Cleaned up in
    // CleanupStagingFolders (end of OnTransferClick's transfer loop, and OnClosed as a safety net).
    private readonly List<string> _stagingFoldersCreated = new();

    // 사진 검토 패널을 닫을 때 오른쪽 검토 영역과 Window 폭만 접고,
    // 다시 열 때 직전의 창 상태/크기를 그대로 복원하기 위한 UI 상태.
    // 왼쪽 설정 패널(520px)의 내용/배치는 절대 변경하지 않는다.
    private const double ReviewHiddenWindowWidth = 570.0;

    public TransferSettingsWindow(string ddsHost, int ddsPort, string ddsLocalInterface)
    {
        InitializeComponent();
        SourceInitialized += (_, _) => DarkTitleBar.Apply(this);
        _ddsHost = ddsHost;
        _ddsPort = ddsPort;
        _ddsLocalInterface = ddsLocalInterface;

        // facade_analysis_msgs on domain 30 -- a different participant/domain from the domain-0
        // one _storageStatus uses per-transfer, so it's started once here for the window's whole
        // lifetime instead. initialPeerPort 0 lets the native side compute domain 30's own
        // default port (7400+250*30+10) rather than reusing _ddsPort, which is domain 0's.
        _analysisCommand = new AnalysisCommandService();
        _analysisCommand.Dispatched += OnAnalysisDispatched;
        _analysisCommand.DispatchFailed += OnAnalysisDispatchFailed;
        _analysisCommand.JobAccepted += OnAnalysisJobAccepted;
        _analysisCommand.JobQueued += OnAnalysisJobQueued;
        _analysisCommand.JobStarted += OnAnalysisJobStarted;
        _analysisCommand.StatusUpdate += OnAnalysisStatusUpdate;
        _analysisCommand.ErrorNotify += OnAnalysisErrorNotify;
        _analysisCommand.ResultReceived += OnAnalysisResult;
        _analysisCommand.Start(domainId: 30, initialPeerHost: _ddsHost, localInterfaceIp: _ddsLocalInterface);

        // Default session id: today's date -- operator can change it, but this matches the
        // existing capture-folder naming convention (<측정장소>_yyyyMMdd_HHmmss) closely enough
        // to be a sane starting point without forcing a blank required field. Deliberately NOT
        // restored from _settingsPath (see TransferSettingsStore's own comment) -- this default
        // is already the more useful "fresh per launch" behavior.
        SessionIdTextBox.Text = DateTime.Now.ToString("yyyyMMdd_HHmmss");

        var configPath = Path.Combine(AppContext.BaseDirectory, "config", "facade_targets.json");
        var catalog = FacadeTargetCatalog.Load(configPath);
        CompanyComboBox.ItemsSource = catalog.Companies;
        if (catalog.Companies.Count > 0)
        {
            CompanyComboBox.SelectedIndex = 0;
        }
        else
        {
            CatalogWarningText.Text = $"등록된 회사/동 목록이 없습니다: {configPath}\n이 파일을 편집(회사/동 이름 추가)한 뒤 앱을 다시 시작하세요.";
            CatalogWarningText.Visibility = Visibility.Visible;
            TransferButton.IsEnabled = false;
        }


        ApplySavedSettings(catalog);

        // ApplySavedSettings restores last-used folder/direction/batch-mode from disk, and setting
        // those controls fires the same handlers a manual pick would (OnBatchModeChanged /
        // OnDirectionSelectionChanged), which call RefreshReviewPanel -- so if the restored folder
        // still exists on disk, the window would silently open already maximized with the review
        // panel expanded, before the operator ever touched 사진 검토 보기 themselves (confirmed via
        // a real repro: reopening the dialog after a previous session's transfer opened straight
        // into the wide review layout). _reviewPlan/_detectedBatch/thumbnail caches built by that
        // restore are left untouched here -- only visibility/window sizing is forced back to
        // closed, so the operator's first manual 사진 검토 보기 click still shows the restored
        // folder's content immediately (see OnReviewPanelToggleClick, which reuses _reviewPlan
        // as-is rather than rebuilding it).
        //  HideReviewPanel();
        HideReviewPanel_Start();
    }

    // Restores the operator's last-used values from _settingsPath (see TransferSettingsStore) --
    // called once at window open, after CompanyComboBox's catalog-driven defaults above so this
    // can override them where a saved value actually applies. Order matters below: LocalFolder is
    // set before BatchModeCheckBox (OnBatchModeChanged's auto-scan reads LocalFolderTextBox.Text),
    // and Company is set before Building (OnCompanySelectionChanged rebuilds BuildingComboBox's
    // ItemsSource whenever Company changes, so Building must be applied after that settles).
    private void ApplySavedSettings(FacadeTargetCatalog catalog)
    {
        var saved = TransferSettingsStore.Load(_settingsPath);
        if (saved == null)
            return;

        if (!string.IsNullOrEmpty(saved.Host))
            HostTextBox.Text = saved.Host;
        if (!string.IsNullOrEmpty(saved.Port))
            PortTextBox.Text = saved.Port;
        if (!string.IsNullOrEmpty(saved.SshUser))
            SshUserTextBox.Text = saved.SshUser;
        if (!string.IsNullOrEmpty(saved.SshKeyPath))
            SshKeyTextBox.Text = saved.SshKeyPath;
        if (!string.IsNullOrEmpty(saved.SshPassword))
            SshPasswordBox.Password = saved.SshPassword;
        if (!string.IsNullOrEmpty(saved.RemoteRoot))
            RemoteRootTextBox.Text = saved.RemoteRoot;
        if (!string.IsNullOrEmpty(saved.LocalFolder))
            LocalFolderTextBox.Text = saved.LocalFolder;

        if (catalog.Companies.Count > 0 && !string.IsNullOrEmpty(saved.Company))
        {
            var company = catalog.Companies.FirstOrDefault(c => c.Name == saved.Company);
            if (company != null)
            {
                CompanyComboBox.SelectedItem = company; // fires OnCompanySelectionChanged synchronously
                if (!string.IsNullOrEmpty(saved.Building) && company.Buildings.Contains(saved.Building))
                    BuildingComboBox.SelectedItem = saved.Building;
            }
        }

        if (!string.IsNullOrEmpty(saved.Direction))
        {
            foreach (System.Windows.Controls.ComboBoxItem item in DirectionComboBox.Items)
            {
                if (item.Content.ToString()!.StartsWith(saved.Direction, StringComparison.OrdinalIgnoreCase))
                {
                    DirectionComboBox.SelectedItem = item;
                    break;
                }
            }
        }

        // Set last -- OnBatchModeChanged's auto-scan reads LocalFolderTextBox.Text, already set above.
        if (saved.BatchMode)
            BatchModeCheckBox.IsChecked = true;
    }

    // Saves whatever the operator has currently entered, called once per 전송 click (see
    // OnTransferClick) regardless of whether that attempt then passes validation -- so a typo'd
    // value is still remembered for next time, not just a successful send.
    private void SaveCurrentSettings()
    {
        var selectedCompany = CompanyComboBox.SelectedItem as FacadeTargetCompany;
        var selectedBuilding = BuildingComboBox.SelectedItem as string;
        string? direction = null;
        if (DirectionComboBox.SelectedItem is System.Windows.Controls.ComboBoxItem selectedDirectionItem)
            direction = selectedDirectionItem.Content.ToString()!.Split(' ')[0];

        TransferSettingsStore.Save(_settingsPath, new TransferSettingsStore.TransferSettings(
            Host: HostTextBox.Text.Trim(),
            Port: PortTextBox.Text.Trim(),
            SshUser: SshUserTextBox.Text.Trim(),
            SshKeyPath: SshKeyTextBox.Text.Trim(),
            SshPassword: SshPasswordBox.Password,
            RemoteRoot: RemoteRootTextBox.Text.Trim(),
            LocalFolder: LocalFolderTextBox.Text.Trim(),
            BatchMode: BatchModeCheckBox.IsChecked == true,
            Company: selectedCompany?.Name,
            Building: selectedBuilding,
            Direction: direction));
    }

    // 세션 ID는 항상 자동 생성된 년월일시분초 값이어야 한다 (design review: "숫자만 자동 생성
    // 되어야함, 글을쓰면 팝업으로 알림, 아무것도 넣지 말라고") -- 서로 다른 방향을 같은 세션 ID로
    // 잘못 보내면 같은 facade_image_sessions 행에 흡수되어 버리는 실제 버그를 이미 겪었으므로
    // (facade_image_sessions의 PK가 session_id 단독이라 방향별로 자동 구분되지 않음), 운영자가
    // 실수로/의도적으로 값을 바꾸는 경로 자체를 막는다. SessionIdTextBox.IsReadOnly가 실제 값
    // 변경(타이핑/삭제/드래그앤드롭/IME 조합 등 모든 경로)을 막고, 아래 세 핸들러는 IsReadOnly와
    // 무관하게 여전히 발생하는 입력 시도 이벤트에 안내 팝업만 띄운다.
    private void ShowSessionIdLockedNotice()
    {
        ThemedDialog.ShowInfo(this, "세션 ID",
            "세션 ID는 자동으로 생성됩니다 (년월일시분초). 직접 입력하거나 수정할 수 없습니다.",
            (Brush)Application.Current.Resources["Warn"]);
    }

    private void OnSessionIdPreviewTextInput(object sender, System.Windows.Input.TextCompositionEventArgs e)
    {
        e.Handled = true;
        ShowSessionIdLockedNotice();
    }

    private void OnSessionIdPreviewKeyDown(object sender, System.Windows.Input.KeyEventArgs e)
    {
        var isPasteOrCut = (e.KeyboardDevice.Modifiers & System.Windows.Input.ModifierKeys.Control) != 0
            && (e.Key == System.Windows.Input.Key.V || e.Key == System.Windows.Input.Key.X);
        if (e.Key != System.Windows.Input.Key.Delete && e.Key != System.Windows.Input.Key.Back && !isPasteOrCut)
            return;
        e.Handled = true;
        ShowSessionIdLockedNotice();
    }

    private void OnSessionIdPasting(object sender, DataObjectPastingEventArgs e)
    {
        e.CancelCommand();
        ShowSessionIdLockedNotice();
    }

    private void OnCompanySelectionChanged(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        var company = CompanyComboBox.SelectedItem as FacadeTargetCompany;
        BuildingComboBox.ItemsSource = company?.Buildings;
        if (company != null && company.Buildings.Count > 0)
            BuildingComboBox.SelectedIndex = 0;
    }

    private void OnBatchModeChanged(object sender, RoutedEventArgs e)
    {
        var batchMode = BatchModeCheckBox.IsChecked == true;
        // Left enabled in both modes now (design review: 방향 combo "선택 할수 있어야 함") -- in
        // batch mode its value is never read for the actual transfer plan (see OnTransferClick,
        // which uses _detectedBatch there instead), it only drives which direction's thumbnails
        // the review panel shows, mirroring ReviewDirectionComboBox (see OnDirectionSelectionChanged
        // and OnReviewDirectionChanged/SyncLeftDirectionComboBox for the two-way sync).
        LocalFolderKicker.Text = batchMode ? "전송할 상위 폴더 (방향별 하위 폴더 포함)" : "전송할 로컬 폴더";
        DetectedDirectionsText.Visibility = Visibility.Collapsed;
        _detectedBatch = null;
        if (batchMode && Directory.Exists(LocalFolderTextBox.Text.Trim()))
            ScanAndShowDetectedDirections(LocalFolderTextBox.Text.Trim());
        else
            RefreshReviewPanel();
    }

    private void OnBrowseLocalFolder(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFolderDialog { Title = "전송할 사진 폴더 선택" };
        if (dialog.ShowDialog() != true)
            return;
        LocalFolderTextBox.Text = dialog.FolderName;
        if (BatchModeCheckBox.IsChecked == true)
        {
            ScanAndShowDetectedDirections(dialog.FolderName);
        }
        else
        {
            // Non-batch: auto-sync 방향 콤보박스 to whatever this single folder's own name implies
            // (같은 DirectionAliases 조회, 배치 모드의 하위 폴더 스캔과는 다르게 폴더 자체의 이름을
            // 본다) -- 운영자가 폴더를 고를 때마다 방향을 수동으로 다시 맞출 필요 없게.
            if (DirectionAliases.TryGetValue(Path.GetFileName(dialog.FolderName), out var canonical))
                SyncLeftDirectionComboBox(canonical);
            RefreshReviewPanel();
        }
    }

    private void OnDirectionSelectionChanged(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        // DirectionComboBox's SelectedIndex="0" in XAML fires this DURING InitializeComponent
        // (its default state differs from index 0, unlike e.g. a CheckBox left at its own default),
        // before later-declared fields like BatchModeCheckBox are assigned yet -- confirmed via a
        // real NullReferenceException. Bail out rather than null-check every control this and
        // RefreshReviewPanel touch; nothing meaningful to refresh yet this early anyway.
        if (BatchModeCheckBox == null)
            return;
        if (BatchModeCheckBox.IsChecked != true)
        {
            RefreshReviewPanel();
            return;
        }
        // Batch mode: this combo no longer feeds the transfer plan (see OnBatchModeChanged's own
        // comment), it now just mirrors ReviewDirectionComboBox -- picking a direction here that
        // was actually detected switches the review panel to it, same as picking it over there
        // would (SyncLeftDirectionComboBox does the reverse). A direction not present in this
        // batch (e.g. one of the 6 fixed choices that had no matching subfolder) has nothing to
        // switch to, so it is left alone rather than clearing the review panel.
        if (DirectionComboBox.SelectedItem is System.Windows.Controls.ComboBoxItem item
            && ReviewDirectionComboBox.ItemsSource is IEnumerable<string> directions)
        {
            var direction = item.Content.ToString()!.Split(' ')[0];
            if (directions.Contains(direction, StringComparer.OrdinalIgnoreCase))
                ReviewDirectionComboBox.SelectedItem = direction;
        }
    }

    // Scans immediate subfolders of `parentFolder` for names matching one of the accepted
    // direction aliases (see DirectionAliases) -- this IS the "필수 방향 세트" declaration
    // mechanism per the project owner's explicit direction: defined by FacadePreviewer's own
    // folder structure, not a separate admin-configured setting. _detectedBatch stores the
    // canonical (translated) direction code, not the raw folder name, since that's what actually
    // goes out on the wire (remote path + facade_storage_msgs requirements).
    private void ScanAndShowDetectedDirections(string parentFolder)
    {
        _detectedBatch = new List<(string, string)>();
        try
        {
            foreach (var dir in Directory.GetDirectories(parentFolder))
            {
                var name = Path.GetFileName(dir);
                if (DirectionAliases.TryGetValue(name, out var canonical) && Directory.EnumerateFiles(dir).Any())
                    _detectedBatch.Add((canonical, dir));
            }
        }
        catch (IOException)
        {
            // Folder became inaccessible between picking it and scanning -- leave
            // _detectedBatch empty, OnTransferClick's validation below reports it clearly.
        }

        DetectedDirectionsText.Visibility = Visibility.Visible;
        DetectedDirectionsText.Text = _detectedBatch.Count > 0
            ? $"감지된 방향 ({_detectedBatch.Count}개, 이 순서대로 한 번에 하나씩 전송됩니다): {string.Join(" → ", _detectedBatch.Select(d => d.Direction))}"
            : "이 폴더 아래에서 방향 이름과 일치하고 파일이 있는 하위 폴더를 찾지 못했습니다 (인식: FRONT/앞/정면, BACK/뒤/후면, LEFT/좌/왼쪽, RIGHT/우/오른쪽, ROOF/TOP/옥상/지붕, OTHER/기타).";

        RefreshReviewPanel();
    }

    private async void OnTransferClick(object sender, RoutedEventArgs e)
    {
        SaveCurrentSettings();

        var host = HostTextBox.Text.Trim();
        var sshUser = SshUserTextBox.Text.Trim();
        var localFolder = LocalFolderTextBox.Text.Trim();
        var selectedCompany = CompanyComboBox.SelectedItem as FacadeTargetCompany;
        var selectedBuilding = BuildingComboBox.SelectedItem as string;
        var sessionId = SessionIdTextBox.Text.Trim();
        var batchMode = BatchModeCheckBox.IsChecked == true;

        if (selectedCompany == null || string.IsNullOrEmpty(selectedBuilding))
        {
            StatusText.Text = "회사와 동을 목록에서 선택하세요 (config\\facade_targets.json 참고).";
            return;
        }
        var company = selectedCompany.Name;
        var building = selectedBuilding;

        if (string.IsNullOrEmpty(host) || string.IsNullOrEmpty(sshUser) || string.IsNullOrEmpty(localFolder) ||
            string.IsNullOrEmpty(sessionId))
        {
            StatusText.Text = "Host / SSH 사용자 / 로컬 폴더 / 세션 ID는 모두 필수입니다.";
            return;
        }
        if (!Directory.Exists(localFolder))
        {
            StatusText.Text = $"로컬 폴더가 존재하지 않습니다: {localFolder}";
            return;
        }
        if (!int.TryParse(PortTextBox.Text.Trim(), out var port) || port <= 0)
        {
            StatusText.Text = "Port가 올바르지 않습니다.";
            return;
        }

        // Catches a real, easy-to-make mistake immediately instead of only surfacing several
        // steps later as a confusing rsync auth failure: pointing this field at the extracted zip
        // FOLDER (e.g. "...\remoteRSYNC-ssh-key") instead of the actual private key FILE inside it
        // (e.g. "...\remoteRSYNC-ssh-key\id_ed25519"). When that happens, ssh silently skips
        // public-key auth entirely (can't load a directory as an identity file) and falls back to
        // password auth, which then fails -- producing the generic "Permission denied
        // (publickey,password)" message with no indication the real problem was the path itself.
        var sshKeyPathToCheck = SshKeyTextBox.Text.Trim();
        if (!string.IsNullOrEmpty(sshKeyPathToCheck))
        {
            if (Directory.Exists(sshKeyPathToCheck))
            {
                StatusText.Text = $"SSH 키 경로가 파일이 아니라 폴더를 가리키고 있습니다: {sshKeyPathToCheck}\n" +
                    "zip을 압축 풀면 생기는 폴더 안의 'id_ed25519' 파일 자체를 선택하세요.";
                return;
            }
            if (!File.Exists(sshKeyPathToCheck))
            {
                StatusText.Text = $"SSH 키 파일을 찾을 수 없습니다: {sshKeyPathToCheck}";
                return;
            }

            TightenPrivateKeyPermissions(sshKeyPathToCheck);
        }

        List<(string Direction, string LocalFolder)> plan;
        if (batchMode)
        {
            if (_detectedBatch == null || _detectedBatch.Count == 0)
            {
                StatusText.Text = "일괄 전송: 감지된 방향 하위 폴더가 없습니다. 폴더를 다시 선택하세요.";
                return;
            }
            plan = _detectedBatch;
        }
        else
        {
            // Same normalization the DDS-Router side's parse_hierarchy() applies (see
            // FacadeImageBridge/main.cpp) -- upper-cased so "front"/"Front"/"FRONT" all match
            // one of the 6 accepted values regardless of how the ComboBox text got typed/selected.
            var directionText = ((System.Windows.Controls.ComboBoxItem)DirectionComboBox.SelectedItem).Content.ToString()!;
            plan = new List<(string, string)> { (directionText.Split(' ')[0], localFolder) };
        }

        var rsyncExePath = Path.Combine(AppContext.BaseDirectory, "cygwin_rsync", "bin", "rsync.exe");
        if (!File.Exists(rsyncExePath))
        {
            StatusText.Text = $"rsync.exe를 찾을 수 없습니다: {rsyncExePath}\n" +
                "previewer/tools/Setup-Tools.bat (또는 Get-CygwinRsync.ps1)를 먼저 실행해서 설치하세요.";
            return;
        }

        _phase = Phase.Transferring;
        TransferButton.IsEnabled = false;
        CancelButton.IsEnabled = true;
        TransferProgressBar.Value = 0;
        _pendingCompany = company;
        _pendingBuilding = building;
        _pendingJobStartedAtEpochMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        ResetProgressStages();
        SetStage(StageTransferDot, StageTransferLabel, StageState.Active);
        // Review panel is a pre-transfer-only tool -- hide it once the transfer actually starts
        // (still reachable via ReviewPanelToggleButton if the operator wants to check it again).
        // HideReviewPanel() also collapses only the right-side review window area while leaving
        // the entire left settings screen unchanged.
        HideReviewPanel();

        // Storage-status DDS client is started here (not lazily in EnterStorageWaitPhase) so it's
        // already listening before the transfer even begins -- both because Requirements is sent
        // through it below, and so there's no race where an unusually fast archive completion
        // publishes Feedback/Result before this dialog started subscribing.
        _storageStatus?.Dispose();
        _storageStatus = new FacadeStorageStatusService();
        _storageStatus.FeedbackReceived += OnStorageFeedback;
        _storageStatus.ResultReceived += OnStorageResult;
        if (!_storageStatus.Start(DdsDomainId, FeedbackTopic, ResultTopic, CancelTopic, RequirementsTopic, FinalizeTopic,
                _ddsHost, _ddsPort, _ddsLocalInterface))
        {
            StatusText.Text = "저장 상태 DDS 연결 실패 -- 진행률/완료 알림 없이 계속 진행합니다.";
            _storageStatus.Dispose();
            _storageStatus = null;
        }

        // Declares this building's expected direction set to backend_core so it can auto-zip
        // once every direction has arrived (see CrackVisionArchiveManager). Additive server-side
        // (repeated declarations for the same building accumulate, not overwrite -- see that
        // class's set_building_requirements comment), so sending one direction at a time across
        // separate visits still ends up with the correct full required set. Best-effort: a
        // failure here does not block the actual photo transfer, it only means auto-archiving
        // won't fire until a later attempt successfully declares it.
        // Counts files directly under each direction's local folder (non-recursive, matching
        // ScanAndShowDetectedDirections' own convention) -- this is exactly what will actually
        // land server-side too, since FacadeImageBridge's path-depth filter already discards
        // anything nested deeper (e.g. a leftover "output/" subfolder from an earlier COLMAP run),
        // so counting only top-level files here keeps the two sides in agreement. Subtracting
        // GetExcludedCount keeps that agreement even after the review panel excludes some photos
        // -- without this, a re-declared count larger than what PrepareTransferFolder actually
        // stages below would make check_and_enqueue_if_complete wait forever for images that were
        // deliberately never going to be sent.
        var requirementCounts = plan
            .Select(p => (p.Direction, Directory.EnumerateFiles(p.LocalFolder).Count() - GetExcludedCount(p.Direction)))
            .ToList();
        var registered = _storageStatus?.SendRequirements(company, building, requirementCounts) ?? false;
        if (!registered)
            StatusText.Text = "CrackVisionDB 방향 세트 등록 실패 (계속 진행 -- 자동 zip 아카이빙만 비활성됩니다).";

        var remoteRoot = RemoteRootTextBox.Text.Trim().TrimEnd('/');
        var sshKeyPath = SshKeyTextBox.Text.Trim();
        // 2026-08-27: SSH 키 없으면 Password로(sshpass) -- 키가 있으면 항상 키가 우선(RsyncTransfer.cpp
        // Start()와 동일한 우선순위, 빈 문자열로 넘기면 그쪽에서도 키만 쓰던 기존 동작 그대로).
        var sshPassword = sshKeyPath.Length == 0 ? SshPasswordBox.Password : "";

        try
        {
            for (var i = 0; i < plan.Count; i++)
            {
                var (direction, originalFolder) = plan[i];
                // Only stages a filtered copy when this direction actually has an exclusion --
                // rsync has no "send everything except these" flag, and the common case (nothing
                // excluded) should keep pointing straight at the operator's own folder, not a copy of
                // it. See PrepareTransferFolder's own comment.
                var folder = PrepareTransferFolder(direction, originalFolder);
                // One session_id per direction -- facade_image_sessions' primary key is session_id
                // alone (one row = one direction), so a batch of N directions needs N distinct
                // session ids even though the operator only typed/kept one base value.
                var perDirectionSessionId = batchMode ? $"{sessionId}_{direction}" : sessionId;
                var remoteDest = $"{remoteRoot}/{company}/{building}/{direction}/{perDirectionSessionId}";

                var resume = false;
                while (true)
                {
                    StatusText.Text = plan.Count > 1
                        ? $"[{i + 1}/{plan.Count}] {direction} 전송 {(resume ? "재개" : "준비")} 중..."
                        : (resume ? "전송 재개..." : "전송 시작...");

                    var (exitCode, errorMessage) = await RunSingleTransferAsync(rsyncExePath, folder, sshUser, host, port,
                        sshKeyPath, sshPassword, remoteDest, resume, direction, i + 1, plan.Count);
                    if (exitCode == 0)
                        break; // this direction done -- move on to the next one in plan

                    if (errorMessage == "cancelled by user")
                    {
                        // Operator clicked Cancel during the rsync phase -- stop the whole batch
                        // quietly, they already know they cancelled it (no retry prompt).
                        _phase = Phase.Idle;
                        TransferButton.IsEnabled = true;
                        CancelButton.IsEnabled = false;
                        StatusText.Text = "전송이 취소되었습니다.";
                        SetStage(StageTransferDot, StageTransferLabel, StageState.Skipped, "취소됨");
                        return;
                    }

                    // 통신 상태 확인 후 선택: 부분 전송 지점부터 이어서 보낼지, 처음부터 다시 보낼지,
                    // 아니면 전체 전송을 중단할지. 파일은 이미 원본 그대로이므로 어느 쪽을 선택해도
                    // 데이터 손실은 없음 -- rsync 자체가 이미 온전히 전송된 파일은 다시 보내지 않음.
                    // null (다이얼로그가 버튼 클릭 없이 닫힌 경우)은 가장 안전한 선택인 "중단"과 동일하게 처리.
                    var choice = ThemedDialog.Show(this, "전송 중단 -- 재시도 방법 선택",
                        $"[{direction}] 전송이 중단되었습니다.\n원인: {errorMessage}\n\n먼저 통신(네트워크) 상태를 확인하세요.",
                        (Brush)Application.Current.Resources["Warn"],
                        ("resume", "이어서 전송", true),
                        ("restart", "처음부터 전송", false),
                        ("abort", "전체 전송 중단", false));

                    if (choice != "resume" && choice != "restart")
                    {
                        _phase = Phase.Idle;
                        TransferButton.IsEnabled = true;
                        CancelButton.IsEnabled = false;
                        StatusText.Text = $"[{i + 1}/{plan.Count}] {direction} 전송 중단됨 (전체 작업 중지).";
                        SetStage(StageTransferDot, StageTransferLabel, StageState.Failed, "중단됨");
                        return;
                    }
                    resume = choice == "resume";
                    // loop retries this same direction
                }

                // rsync exited 0, but a real repro this session showed that alone isn't quite
                // enough to fully trust: verify what actually landed matches what was sent, by size,
                // and let rsync itself re-send anything that doesn't (rsync's own default incremental
                // behavior already only re-transfers files that differ, so a plain re-run is
                // sufficient -- no special flags needed for this, unlike the resume/--partial path
                // above which is specifically for an interrupted transfer). Explicit project decision
                // to compare by SIZE only, not re-verify content via checksum -- rsync's own transfer
                // is what guarantees byte-correctness; this is just confirming nothing was silently
                // truncated/lost between rsync exiting and this check.
                const int maxSizeVerifyAttempts = 3;
                for (var verifyAttempt = 1; verifyAttempt <= maxSizeVerifyAttempts; verifyAttempt++)
                {
                    StatusText.Text = plan.Count > 1
                        ? $"[{i + 1}/{plan.Count}] {direction} 전송 크기 확인 중..."
                        : "전송 크기 확인 중...";
                    var sizesMatch = await VerifyRemoteSizesMatchAsync(rsyncExePath, folder, sshUser, host, port, sshKeyPath, sshPassword, remoteDest);
                    if (sizesMatch)
                        break;

                    if (verifyAttempt == maxSizeVerifyAttempts)
                    {
                        var choice = ThemedDialog.Show(this, "전송 크기 불일치 -- 재시도 방법 선택",
                            $"[{direction}] 일부 파일의 전송 크기와 수신 크기가 일치하지 않습니다 " +
                            $"({maxSizeVerifyAttempts}회 재전송 시도했지만 계속 불일치).\n\n먼저 통신(네트워크) 상태를 확인하세요.",
                            (Brush)Application.Current.Resources["Warn"],
                            ("retry", "다시 시도", true),
                            ("abort", "전체 전송 중단", false));
                        if (choice != "retry")
                        {
                            _phase = Phase.Idle;
                            TransferButton.IsEnabled = true;
                            CancelButton.IsEnabled = false;
                            StatusText.Text = $"[{i + 1}/{plan.Count}] {direction} 전송 중단됨 (크기 불일치, 전체 작업 중지).";
                            SetStage(StageTransferDot, StageTransferLabel, StageState.Failed, "크기 불일치");
                            return;
                        }
                        verifyAttempt = 0; // operator chose to keep trying -- reset the attempt counter
                        continue;
                    }

                    StatusText.Text = plan.Count > 1
                        ? $"[{i + 1}/{plan.Count}] {direction} 크기 불일치 감지 ({verifyAttempt}/{maxSizeVerifyAttempts}) -- 재전송 중..."
                        : $"크기 불일치 감지 ({verifyAttempt}/{maxSizeVerifyAttempts}) -- 재전송 중...";
                    await RunSingleTransferAsync(rsyncExePath, folder, sshUser, host, port, sshKeyPath, sshPassword, remoteDest,
                        resume: false, direction, i + 1, plan.Count);
                }
            }
        }
        finally
        {
            CleanupStagingFolders();
        }

        SetStage(StageTransferDot, StageTransferLabel, StageState.Done);

        // A Result can legitimately already have arrived and been fully processed by this point
        // -- e.g. a re-send of an already-archived building (see
        // CrackVisionArchiveManager::check_if_already_satisfied_by_existing_archive's own
        // comment) republishes its Result immediately upon re-declaring requirements, well before
        // this rsync loop (which still has to re-transfer files rsync's own destination cleanup
        // deleted) finishes. Skipping this block when that's already happened matters for two
        // reasons: overwriting StatusText here would silently replace an accurate "저장 완료"
        // message with a stale "대기 중"-looking one, and calling EnterStorageWaitPhase again
        // would re-disable 전송/re-enable 취소 even though the dialog is already back in its idle
        // state. StatusText is still explicitly set here (not left untouched) -- OnStorageResult's
        // own message was already overwritten by this loop's own per-direction progress updates
        // by the time execution reaches here (confirmed via a real repro: the last thing visible
        // was a stale "N/5 전송 크기 확인 중..." even though the dialog had already completed).
        if (_phase == Phase.Idle)
        {
            StatusText.Text = "이미 처리된 요청입니다 (기존 저장 결과를 그대로 사용했습니다).";
            return;
        }

        // 일괄 전송은 이미 상위 폴더 스캔 시점에 전체 방향 세트를 선언한 것이므로 팝업 없이 바로
        // finalize -- 우연히 누적 카운트가 일치하길 기다리는 옛 자동완성 로직에 더 이상 의존하지
        // 않는다(같은 테스트 건물을 여러 세션에 걸쳐 재사용하면서 누적 요구치가 실제 전송량과
        // 어긋나는 버그를 실제로 겪었다). 단일 방향 전송은 운영자에게 "이게 전부인지" 직접
        // 확인받는다 -- 아직 다른 방향을 더 보낼 계획이면 "아니오"로 대기 상태를 유지.
        if (batchMode)
        {
            _storageStatus?.SendFinalize(company, building);
            StatusText.Text = $"전체 {plan.Count}개 방향 전송 완료 ({string.Join(", ", plan.Select(p => p.Direction))}). 저장 처리 대기 중...";
            EnterStorageWaitPhase(company, building);
            return;
        }

        var isThisEverything = ThemedDialog.ShowConfirm(this, "전송 확인",
            $"{building} 모든 면 전송을 완료하셨습니까?\n\n" +
            "예: 지금까지 전송된 사진을 압축해 저장합니다.\n" +
            "아니오: 아직 보낼 방향이 남아있으면 나중에 이어서 전송하세요.",
            (Brush)Application.Current.Resources["Text2"]);
        if (!isThisEverything)
        {
            StatusText.Text = "전송 완료. 다음 방향을 이어서 보내세요.";
            _phase = Phase.Idle;
            TransferButton.IsEnabled = true;
            CancelButton.IsEnabled = false;
            _storageStatus?.Dispose();
            _storageStatus = null;
            return;
        }

        _storageStatus?.SendFinalize(company, building);
        StatusText.Text = "전송 완료. 저장 처리 대기 중...";
        EnterStorageWaitPhase(company, building);
    }

    // Same drive-letter-to-/cygdrive/ convention as RsyncTransfer.cpp's native ToCygdrivePath --
    // duplicated here in C# rather than exposed from the native DLL, since this verification step
    // is a previewer-only addition (explicit project decision) with no server-side counterpart.
    private static string ToCygdrivePath(string windowsPath)
    {
        var normalized = windowsPath.Replace('\\', '/');
        if (normalized.Length >= 2 && normalized[1] == ':')
            return $"/cygdrive/{char.ToLowerInvariant(normalized[0])}{normalized[2..]}";
        return normalized;
    }

    // Lists <name, size> for the regular files directly under remoteDir (non-recursive, matching
    // how a single direction's session folder is always flat) via the same vendored Cygwin ssh.exe
    // rsync itself uses -- no new native code, this is a previewer-only addition.
    private static async Task<Dictionary<string, long>> QueryRemoteFileSizesAsync(string rsyncExePath,
        string sshUser, string host, int port, string sshKeyPath, string sshPassword, string remoteDir)
    {
        var result = new Dictionary<string, long>();
        var rsyncDir = Path.GetDirectoryName(rsyncExePath)!;
        var sshExePath = Path.Combine(rsyncDir, "ssh.exe");
        if (!File.Exists(sshExePath))
            return result;

        // 2026-08-27: key wins if both are set (same preference order as RsyncTransfer.cpp's
        // Start()) -- only fall back to sshpass-wrapped password auth when no key was given.
        var usePasswordAuth = string.IsNullOrEmpty(sshKeyPath) && !string.IsNullOrEmpty(sshPassword);
        var sshpassExePath = Path.Combine(rsyncDir, "sshpass.exe");

        var psi = new ProcessStartInfo
        {
            FileName = usePasswordAuth ? sshpassExePath : sshExePath,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };
        if (usePasswordAuth)
        {
            if (!File.Exists(sshpassExePath))
                return result;
            // sshpass -e reads the password from SSHPASS (set below via EnvironmentVariables)
            // instead of a command-line flag, so it never appears in this process's own command
            // line (Task Manager/Process Explorer/WMI could otherwise read it there).
            psi.ArgumentList.Add("-e");
            psi.ArgumentList.Add(sshExePath);
            psi.EnvironmentVariables["SSHPASS"] = sshPassword;
        }
        psi.ArgumentList.Add("-p");
        psi.ArgumentList.Add(port.ToString());
        psi.ArgumentList.Add("-o");
        psi.ArgumentList.Add("StrictHostKeyChecking=accept-new");
        psi.ArgumentList.Add("-o");
        psi.ArgumentList.Add("UserKnownHostsFile=/dev/null");
        if (!string.IsNullOrEmpty(sshKeyPath))
        {
            psi.ArgumentList.Add("-i");
            psi.ArgumentList.Add(ToCygdrivePath(sshKeyPath));
        }
        else if (usePasswordAuth)
        {
            psi.ArgumentList.Add("-o");
            psi.ArgumentList.Add("PreferredAuthentications=password");
            psi.ArgumentList.Add("-o");
            psi.ArgumentList.Add("PubkeyAuthentication=no");
        }
        psi.ArgumentList.Add($"{sshUser}@{host}");
        // Tab-separated, size first -- a filename could in principle contain a space, so the
        // split below only ever splits on the FIRST tab rather than assuming no delimiter
        // collisions.
        psi.ArgumentList.Add($"find '{remoteDir}' -maxdepth 1 -type f -printf '%s\\t%f\\n'");

        try
        {
            using var process = Process.Start(psi);
            if (process == null)
                return result;
            var output = await process.StandardOutput.ReadToEndAsync();
            await process.WaitForExitAsync();

            foreach (var line in output.Split('\n', StringSplitOptions.RemoveEmptyEntries))
            {
                var tabIndex = line.IndexOf('\t');
                if (tabIndex <= 0)
                    continue;
                if (long.TryParse(line[..tabIndex], out var size))
                    result[line[(tabIndex + 1)..].TrimEnd('\r')] = size;
            }
        }
        catch (System.ComponentModel.Win32Exception)
        {
            // ssh.exe missing/unusable -- treated the same as "could not verify", see
            // VerifyRemoteSizesMatchAsync's own comment on why that fails open, not closed.
        }

        return result;
    }

    // Fails OPEN (returns true, "sizes match") on any verification-infrastructure problem (ssh
    // unavailable, remote command error, etc.) rather than blocking the transfer on a check that
    // itself couldn't run -- this step is a best-effort integrity improvement layered on top of
    // rsync's own exit code, not a replacement for it; rsync already reported success before this
    // ever runs.
    private static async Task<bool> VerifyRemoteSizesMatchAsync(string rsyncExePath, string localFolder,
        string sshUser, string host, int port, string sshKeyPath, string sshPassword, string remoteDest)
    {
        var localSizes = new Dictionary<string, long>();
        try
        {
            foreach (var f in Directory.EnumerateFiles(localFolder))
                localSizes[Path.GetFileName(f)] = new FileInfo(f).Length;
        }
        catch (IOException)
        {
            return true;
        }
        if (localSizes.Count == 0)
            return true;

        var remoteSizes = await QueryRemoteFileSizesAsync(rsyncExePath, sshUser, host, port, sshKeyPath, sshPassword, remoteDest);
        if (remoteSizes.Count == 0)
            return true; // could not query remote at all -- fail open, see this method's own comment

        foreach (var (name, size) in localSizes)
        {
            if (!remoteSizes.TryGetValue(name, out var remoteSize) || remoteSize != size)
                return false;
        }
        return true;
    }

    // Wraps RsyncTransferService's event-based single transfer in a Task so batch mode can
    // simply await each direction in sequence -- rsync itself is still one process per call
    // (unchanged from before), this only changes how the *dialog* drives multiple calls.
    private Task<(int ExitCode, string ErrorMessage)> RunSingleTransferAsync(string rsyncExePath, string localFolder,
        string sshUser, string host, int port, string sshKeyPath, string sshPassword, string remoteDest, bool resume,
        string direction, int index, int total)
    {
        var tcs = new TaskCompletionSource<(int, string)>();

        _transfer?.Dispose();
        _transfer = new RsyncTransferService();
        _transfer.ProgressReceived += (bytesTransferred, percent, rateMbps) =>
        {
            Dispatcher.BeginInvoke(() =>
            {
                TransferProgressBar.Value = percent;
                var mb = bytesTransferred / (1024.0 * 1024.0);
                var prefix = total > 1 ? $"[{index}/{total}] {direction} " : "";
                StatusText.Text = $"{prefix}전송 중... {percent}% ({mb:F1} MB, {rateMbps:F2} MB/s)";
            });
        };
        _transfer.TransferCompleted += (exitCode, errorMessage) =>
        {
            Dispatcher.BeginInvoke(() => tcs.TrySetResult((exitCode, errorMessage)));
        };

        var started = _transfer.Start(rsyncExePath, localFolder, sshUser, host, port, sshKeyPath, sshPassword, remoteDest, resume);
        if (!started)
        {
            _transfer.Dispose();
            _transfer = null;
            tcs.TrySetResult((-1, "rsync 프로세스를 시작하지 못했습니다."));
        }

        return tcs.Task;
    }

    // Transitions from "전송 중" to "저장 처리 중" once every planned direction has been rsync'd
    // successfully -- no separate operator confirmation click needed: the building_requirements
    // declaration (sent via DDS at the top of OnTransferClick) already declares the full expected
    // plan, so CrackVisionArchiveManager detects completion server-side automatically the moment
    // the last required image lands. This phase exists purely to give the operator visibility
    // (progress/cancel/completion) into that already-automatic process instead of it happening
    // silently. _storageStatus is already running by this point (started in OnTransferClick).
    // 2026-08-27 "진행 단계" 패널 -- Pending(회색)/Active(주황, 굵게)/Done(초록, 체크)/
    // Failed(빨강, X)/Skipped(회색, 취소선 대신 "건너뜀" 접두사 -- 실패는 아니지만 그 다음
    // 단계로 진행하지 않았다는 뜻, "아니오"를 선택했을 때만 씀).
    private static void SetStage(System.Windows.Shapes.Ellipse dot, System.Windows.Controls.TextBlock label, StageState state, string? detail = null)
    {
        var (brushKey, prefix, bold) = state switch
        {
            StageState.Active => ("Accent", "● ", true),
            StageState.Done => ("Good", "✓ ", false),
            StageState.Failed => ("Accent", "✗ ", false),
            StageState.Skipped => ("Text2", "- (건너뜀) ", false),
            _ => ("Text2", "○ ", false),
        };
        var brush = (Brush)Application.Current.Resources[brushKey];
        dot.Fill = brush;
        label.Foreground = state == StageState.Pending ? (Brush)Application.Current.Resources["Text2"] : brush;
        label.FontWeight = bold ? FontWeights.Bold : FontWeights.Normal;
        var baseText = label.Tag as string ?? label.Text; // Tag caches the original plain label the first time this runs
        if (label.Tag == null)
            label.Tag = baseText;
        label.Text = prefix + baseText + (string.IsNullOrEmpty(detail) ? "" : $" ({detail})");
    }

    // Called once per fresh 전송 click -- every stage back to Pending, confirm panel hidden,
    // stored result from a previous run (if any) cleared so a stale "예/아니오" click after a
    // brand-new transfer started can't accidentally dispatch analysis for the WRONG archive.
    private void ResetProgressStages()
    {
        ProgressStagePanel.Visibility = Visibility.Visible;
        SetStage(StageTransferDot, StageTransferLabel, StageState.Pending);
        SetStage(StageStorageDot, StageStorageLabel, StageState.Pending);
        SetStage(StageAnalysisWaitDot, StageAnalysisWaitLabel, StageState.Pending);
        SetStage(StageAnalysisRunDot, StageAnalysisRunLabel, StageState.Pending);
        SetStage(StageDoneDot, StageDoneLabel, StageState.Pending);
        AnalysisConfirmPanel.Visibility = Visibility.Collapsed;
        _awaitingAnalysisConfirmResult = null;
    }

    // "예, 분석 시작" -- 예전 ThemedDialog.ShowConfirm 팝업의 Yes 분기와 동일한 로직
    // (SendDispatchRequest 호출), 팝업 대신 인라인 버튼에서 실행되는 것만 다름.
    private void OnAnalysisStartYesClick(object sender, RoutedEventArgs e)
    {
        var result = _awaitingAnalysisConfirmResult;
        if (result == null)
            return;
        AnalysisConfirmPanel.Visibility = Visibility.Collapsed;
        SetStage(StageAnalysisWaitDot, StageAnalysisWaitLabel, StageState.Done);

        _pendingAnalysisArchiveId = result.ArchiveId;
        var sent = _analysisCommand?.SendDispatchRequest(result.ArchiveId, result.Company, result.Building,
            "", result.ImageCount, result.ZipPath, result.SizeBytes) ?? false;
        if (sent)
        {
            SetStage(StageAnalysisRunDot, StageAnalysisRunLabel, StageState.Active, $"Archive ID {result.ArchiveId}");
            StatusText.Text = $"분석 요청 전송됨 (Archive ID: {result.ArchiveId})";
        }
        else
        {
            SetStage(StageAnalysisRunDot, StageAnalysisRunLabel, StageState.Failed, "DDS 연결 실패");
            StatusText.Text = "분석 요청 전송 실패 -- DDS 연결을 확인하세요.";
        }
        _awaitingAnalysisConfirmResult = null;
    }

    // "아니오" -- 예전엔 ShowConfirm이 false를 반환하면 그냥 아무 것도 안 했음(StatusText는
    // "저장 완료: ..."로 남아있었음), 여기서도 동일하게 이후 단계는 진행하지 않고 끝냄.
    private void OnAnalysisStartNoClick(object sender, RoutedEventArgs e)
    {
        AnalysisConfirmPanel.Visibility = Visibility.Collapsed;
        SetStage(StageAnalysisWaitDot, StageAnalysisWaitLabel, StageState.Skipped);
        _awaitingAnalysisConfirmResult = null;
    }

    private void EnterStorageWaitPhase(string company, string building)
    {
        _pendingCompany = company;
        _pendingBuilding = building;
        _phase = Phase.Storing;
        TransferButton.IsEnabled = false;
        CancelButton.IsEnabled = true;
        TransferProgressBar.Value = 0;
        SetStage(StageStorageDot, StageStorageLabel, StageState.Active);

        if (_storageStatus == null)
        {
            // Already reported at connect time (top of OnTransferClick) -- nothing more to wait
            // for here, so don't leave the UI stuck showing a "저장 처리 대기 중" state that will
            // never resolve.
            _phase = Phase.Idle;
            TransferButton.IsEnabled = true;
            CancelButton.IsEnabled = false;
            SetStage(StageStorageDot, StageStorageLabel, StageState.Failed, "DDS 연결 없음");
        }
    }

    private void OnStorageFeedback(FacadeStorageFeedback feedback)
    {
        if (feedback.Company != _pendingCompany || feedback.Building != _pendingBuilding)
            return; // a different building's job -- not the one this dialog is watching
        Dispatcher.BeginInvoke(() =>
        {
            var percent = feedback.ImagesTotal > 0 ? (int)(100.0 * feedback.ImagesZipped / feedback.ImagesTotal) : 0;
            TransferProgressBar.Value = percent;
            // RECEIVING = server is still waiting for images to finish arriving/uploading (a
            // large batch can take a while to fully land even after rsync itself reports done --
            // see MngData's check_and_enqueue_if_complete for why this phase needed its own
            // visible progress instead of a static "저장 처리 대기 중" the whole time); ZIPPING =
            // every required image has arrived and the archive is actually being built now.
            var label = feedback.Status == "RECEIVING" ? "이미지 수신 확인 중" : "압축 처리 중";
            StatusText.Text = $"저장 처리 중... {label} {feedback.ImagesZipped}/{feedback.ImagesTotal}장 ({percent}%)";
        });
    }

    private void OnStorageResult(FacadeStorageResult result)
    {
        if (result.Company != _pendingCompany || result.Building != _pendingBuilding)
            return;
        // The Result topic is RELIABLE + TRANSIENT_LOCAL (see FacadeStorageResult's own comment
        // on why), so a brand-new reader for the SAME (company, building) as a previous job can be
        // replayed that OLD result at startup, before this job has produced any result of its own
        // -- confirmed via a real repro: a "저장 완료" popup for a stale archive appeared while a
        // genuinely new transfer was still only 68% through, both jobs happening to target the
        // same company/building (an easy thing to do with settings now persisted across launches,
        // see TransferSettingsStore). Discard anything timestamped before this job's own start.
        if (result.CompletedAtEpochMs < _pendingJobStartedAtEpochMs)
            return;
        Dispatcher.BeginInvoke(() =>
        {
            _phase = Phase.Idle;
            TransferButton.IsEnabled = true;
            CancelButton.IsEnabled = false;
            _storageStatus?.Dispose();
            _storageStatus = null;

            if (result.Cancelled)
            {
                StatusText.Text = $"저장 처리가 취소되었습니다 ({result.Company}/{result.Building}).";
                SetStage(StageStorageDot, StageStorageLabel, StageState.Skipped, "취소됨");
                ThemedDialog.ShowInfo(this, "저장 취소됨",
                    "저장 처리가 취소되었습니다. 원본 사진과 데이터베이스 기록은 그대로 유지됩니다.",
                    (Brush)Application.Current.Resources["Text2"]);
            }
            else if (result.Success)
            {
                var mb = result.SizeBytes / (1024.0 * 1024.0);
                StatusText.Text = $"저장 완료: {result.ImageCount}장, {mb:F1} MB ({result.ZipPath})";
                SetStage(StageStorageDot, StageStorageLabel, StageState.Done);

                // 분석 시작 여부 -- archive_id는 방금 받은 FacadeStorageResult에서 확보(도메인 0),
                // 명령 자체는 도메인 30(AnalysisCommandBridge)으로 보냄. directions_csv는
                // FacadeStorageResult에 없어서("" 전달) CheckCrackViewer 쪽 등록은 압축 해제 후
                // 실제 하위 폴더를 스캔해서 결정하므로 값이 없어도 등록 자체엔 영향 없음(원격
                // 분석 작업 창의 표시용 컬럼에만 씀) -- AnalysisCommandBridge.h의 SendDispatchRequest
                // 주석 참고. 2026-08-27: 팝업(ThemedDialog.ShowConfirm) 대신 "진행 단계" 패널의
                // "분석 대기" 행에 인라인 예/아니오 버튼으로 물어봄 -- 실제 처리는
                // OnAnalysisStartYesClick/OnAnalysisStartNoClick에서.
                _awaitingAnalysisConfirmResult = result;
                SetStage(StageAnalysisWaitDot, StageAnalysisWaitLabel, StageState.Active,
                    $"{result.Company} / {result.Building}, Archive ID {result.ArchiveId}");
                AnalysisConfirmPanel.Visibility = Visibility.Visible;
            }
            else
            {
                StatusText.Text = $"저장 실패: {result.ErrorMessage}";
                SetStage(StageStorageDot, StageStorageLabel, StageState.Failed, result.ErrorMessage);
                ThemedDialog.ShowInfo(this, "저장 실패",
                    $"저장 처리 중 오류가 발생했습니다.\n\n{result.ErrorMessage}",
                    (Brush)Application.Current.Resources["Accent"]);
            }
        });
    }

    // === facade_analysis_msgs (domain 30) event handlers -- all filtered to
    // _pendingAnalysisArchiveId, same "only the job THIS window is tracking" discipline
    // OnStorageResult already applies via _pendingCompany/_pendingBuilding. 원격 운용자(현장,
    // 이 창)는 명령을 보내고 에러가 나면 재시도/정지만 결정하면 됨 -- 정상 진행 중인 상태 표시는
    // 참고용일 뿐 별도 조작이 필요 없음(AnalysisLoadBalancer README "취소 필요 여부" 참고).

    private void OnAnalysisDispatched(AnalysisDispatched d)
    {
        if (d.ArchiveId != _pendingAnalysisArchiveId) return;
        Dispatcher.BeginInvoke(() => StatusText.Text = $"분석 배정됨: worker={d.AssignedWorkerId} (Archive ID: {d.ArchiveId})");
    }

    private void OnAnalysisDispatchFailed(AnalysisDispatchFailed d)
    {
        if (d.ArchiveId != _pendingAnalysisArchiveId) return;
        Dispatcher.BeginInvoke(() =>
        {
            StatusText.Text = $"분석 배정 실패: {d.Reason} (Archive ID: {d.ArchiveId})";
            SetStage(StageAnalysisRunDot, StageAnalysisRunLabel, StageState.Failed, d.Reason);
            ThemedDialog.ShowInfo(this, "분석 배정 실패",
                $"분석 작업을 배정할 워크스테이션을 찾지 못했습니다.\n\n사유: {d.Reason}",
                (Brush)Application.Current.Resources["Accent"]);
        });
    }

    private void OnAnalysisJobAccepted(AnalysisJobAccepted d)
    {
        if (d.ArchiveId != _pendingAnalysisArchiveId) return;
        Dispatcher.BeginInvoke(() => StatusText.Text =
            $"분석 시작됨: worker={d.WorkerId} (Archive ID: {d.ArchiveId})");
    }

    private void OnAnalysisJobQueued(AnalysisJobQueued d)
    {
        if (d.ArchiveId != _pendingAnalysisArchiveId) return;
        Dispatcher.BeginInvoke(() => StatusText.Text =
            $"분석 대기 중: worker={d.WorkerId}, 대기 순번={d.QueuePosition} (Archive ID: {d.ArchiveId})");
    }

    private void OnAnalysisJobStarted(AnalysisJobStarted d)
    {
        if (d.ArchiveId != _pendingAnalysisArchiveId) return;
        Dispatcher.BeginInvoke(() => StatusText.Text = $"분석 시작됨 (대기열에서): worker={d.WorkerId} (Archive ID: {d.ArchiveId})");
    }

    private void OnAnalysisStatusUpdate(AnalysisStatusUpdate d)
    {
        if (d.ArchiveId != _pendingAnalysisArchiveId) return;
        Dispatcher.BeginInvoke(() => StatusText.Text =
            $"분석 진행 중: {d.Stage} {d.Progress} (worker={d.WorkerId}, Archive ID: {d.ArchiveId})");
    }

    // 에러 발생 시 이곳이 유일하게 운용자 조작(재시도/정지)이 필요한 지점 -- 정상 진행 중엔
    // StatusUpdate만 참고용으로 표시되고 별도 조작 없음.
    private void OnAnalysisErrorNotify(AnalysisErrorNotify d)
    {
        if (d.ArchiveId != _pendingAnalysisArchiveId) return;
        Dispatcher.BeginInvoke(() =>
        {
            StatusText.Text = $"분석 오류: {d.Stage} -- {d.ErrorMessage} (Archive ID: {d.ArchiveId})";
            var choice = ThemedDialog.Show(this, "분석 오류",
                $"분석 중 오류가 발생했습니다.\n\n단계: {d.Stage}\n내용: {d.ErrorMessage}\n\n재시도하시겠습니까, 정지하시겠습니까?",
                (Brush)Application.Current.Resources["Accent"],
                ("retry", "재시도", true), ("stop", "정지", false));
            if (choice == "retry")
                _analysisCommand?.SendRetryRequest(d.ArchiveId);
            else if (choice == "stop")
                _analysisCommand?.SendStopRequest(d.ArchiveId);
        });
    }

    private void OnAnalysisResult(AnalysisResult d)
    {
        if (d.ArchiveId != _pendingAnalysisArchiveId) return;
        Dispatcher.BeginInvoke(() =>
        {
            StatusText.Text = d.Success
                ? $"분석 완료: worker={d.WorkerId} (Archive ID: {d.ArchiveId}) -- 결과는 사무실에서 확인하세요."
                : $"분석 실패: worker={d.WorkerId} (Archive ID: {d.ArchiveId})";
            SetStage(StageAnalysisRunDot, StageAnalysisRunLabel, d.Success ? StageState.Done : StageState.Failed);
            SetStage(StageDoneDot, StageDoneLabel, d.Success ? StageState.Done : StageState.Failed);
            ThemedDialog.ShowInfo(this, d.Success ? "분석 완료" : "분석 실패",
                d.Success
                    ? $"Archive ID {d.ArchiveId}의 분석이 완료되었습니다.\n상세 결과는 사무실 CheckCrackViewer 화면에서 확인하세요."
                    : $"Archive ID {d.ArchiveId}의 분석이 실패했습니다.",
                (Brush)Application.Current.Resources[d.Success ? "Good" : "Accent"]);
        });
    }

    // Rebuilds the review panel's plan from whatever the operator currently has configured --
    // called after every action that could change which files would actually be sent (batch scan,
    // folder browse, batch-mode toggle, non-batch direction change). A fresh plan means the
    // previous plan's exclusions/cached thumbnails no longer apply to whatever is showing now, so
    // both are cleared rather than carried forward against a different file set.
    private void RefreshReviewPanel()
    {
        List<(string Direction, string LocalFolder)> plan;
        if (BatchModeCheckBox.IsChecked == true)
        {
            if (_detectedBatch == null || _detectedBatch.Count == 0)
            {
                _reviewPlan = null;
                HideReviewPanel();
                return;
            }
            plan = _detectedBatch;
        }
        else
        {
            var folder = LocalFolderTextBox.Text.Trim();
            if (!Directory.Exists(folder) || DirectionComboBox.SelectedItem is not System.Windows.Controls.ComboBoxItem item)
            {
                _reviewPlan = null;
                HideReviewPanel();
                return;
            }
            var direction = item.Content.ToString()!.Split(' ')[0];
            plan = new List<(string, string)> { (direction, folder) };
        }

        _reviewPlan = plan;
        _excludedFilesByDirection.Clear();
        _reviewItemsByDirection.Clear();
        ReviewSelectAllCheckBox.IsChecked = true;
        ReviewDirectionComboBox.ItemsSource = plan.Select(p => p.Direction).ToList();
        ReviewDirectionComboBox.SelectedIndex = 0; // fires OnReviewDirectionChanged, loads first direction's thumbnails
        ShowReviewPanel();
    }

    private void ShowReviewPanel()
    {
        // 사진 검토 보기:
        // 기존 왼쪽 설정 화면(520px)은 그대로 유지하고 오른쪽 검토 영역을 연다.
        MainSettingsColumn.Width = GridLength.Auto;
        MainSettingsPanel.Width = 520;
        MainSettingsPanel.HorizontalAlignment = HorizontalAlignment.Left;
        ReviewPanelColumn.Width = new GridLength(1, GridUnitType.Star);

        ReviewPanelBorder.Visibility = Visibility.Visible;
        ReviewPanelToggleButton.Content = "사진 검토 숨기기";

        // 검토 화면에서는 정상적인 최대화/복원 버튼을 다시 표시한다.
        ResizeMode = ResizeMode.CanResize;

        // 검토 화면은 기존 요구대로 풀스크린(최대화).
        WindowState = WindowState.Maximized;
    }

    private void HideReviewPanel()
    {
        // 이미 숨겨진 상태이면 버튼/창 모드만 작은 창 기준으로 맞춘다.
        if (ReviewPanelBorder.Visibility != Visibility.Visible)
        {
            ReviewPanelToggleButton.Content = "사진 검토 보기";
            ResizeMode = ResizeMode.CanMinimize;
            return;
        }

        // 사진 검토 숨기기:
        // 오른쪽 검토 영역만 접고 왼쪽 설정 화면의 내용/폭/배치는 그대로 유지한다.
        ReviewPanelBorder.Visibility = Visibility.Collapsed;
        ReviewPanelColumn.Width = new GridLength(0);

        MainSettingsColumn.Width = GridLength.Auto;
        MainSettingsPanel.Width = 520;
        MainSettingsPanel.HorizontalAlignment = HorizontalAlignment.Left;

        ReviewPanelToggleButton.Content = "사진 검토 보기";

        // 작은 창으로 돌아가기 전에 Normal 상태로 전환.
        WindowState = WindowState.Normal;

        // 작은 전송 창에서는 최대화 버튼을 숨긴다.
        // CanMinimize: 최소화/닫기만 허용, 최대화 버튼은 비활성/제거되고
        // 사용자가 창 테두리를 끌어 임의로 크게 만드는 것도 막는다.
        ResizeMode = ResizeMode.CanMinimize;

        // 기존 컴팩트 폭 유지.
        Width = ReviewHiddenWindowWidth;

        // 현재 모니터 작업영역보다 높지 않게 유지.
        var workArea = SystemParameters.WorkArea;
        Height = Math.Min(Height, workArea.Height);

        // 고해상도 사진 전송 창을 현재 작업영역 중앙에 배치.
        Left = workArea.Left + (workArea.Width - Width) / 2.0;
        Top = workArea.Top + (workArea.Height - Height) / 2.0;
    }

    private void HideReviewPanel_Start()
    {     

        // 사진 검토 숨기기:
        // 오른쪽 검토 영역만 접고 왼쪽 설정 화면의 내용/폭/배치는 그대로 유지한다.
        ReviewPanelBorder.Visibility = Visibility.Collapsed;
        ReviewPanelColumn.Width = new GridLength(0);

        MainSettingsColumn.Width = GridLength.Auto;
        MainSettingsPanel.Width = 520;
        MainSettingsPanel.HorizontalAlignment = HorizontalAlignment.Left;

        ReviewPanelToggleButton.Content = "사진 검토 보기";

        // 작은 창으로 돌아가기 전에 Normal 상태로 전환.
        WindowState = WindowState.Normal;

        // 작은 전송 창에서는 최대화 버튼을 숨긴다.
        // CanMinimize: 최소화/닫기만 허용, 최대화 버튼은 비활성/제거되고
        // 사용자가 창 테두리를 끌어 임의로 크게 만드는 것도 막는다.
        ResizeMode = ResizeMode.CanMinimize;

        // 기존 컴팩트 폭 유지.
        Width = ReviewHiddenWindowWidth;

        // 현재 모니터 작업영역보다 높지 않게 유지.
        var workArea = SystemParameters.WorkArea;
        Height = Math.Min(Height, workArea.Height);

        // 고해상도 사진 전송 창을 현재 작업영역 중앙에 배치.
        Left = workArea.Left + (workArea.Width - Width) / 2.0;
        Top = workArea.Top + (workArea.Height - Height) / 2.0;
    }

    private void OnReviewPanelToggleClick(object sender, RoutedEventArgs e)
    {
        if (ReviewPanelBorder.Visibility == Visibility.Visible)
        {
            HideReviewPanel();
            return;
        }
        if (_reviewPlan == null || _reviewPlan.Count == 0)
        {
            StatusText.Text = "검토할 사진이 없습니다 (폴더/방향을 먼저 선택하세요).";
            return;
        }
        ShowReviewPanel();
    }

    private async void OnReviewDirectionChanged(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        if (ReviewDirectionComboBox.SelectedItem is not string direction || _reviewPlan == null)
            return;
        var folder = _reviewPlan.FirstOrDefault(p => p.Direction == direction).LocalFolder;
        if (string.IsNullOrEmpty(folder))
            return;
        // Keeps the left-panel 방향 combo showing whatever direction is currently being reviewed
        // -- without this, browsing a different direction here left that combo's own selection
        // stuck on whatever it last was (design review: "연결 되어야함, 머는것을 선택해도 같이
        // 선택되게"), which is especially confusing in batch mode since that combo is disabled
        // there but still visibly shows a value that could silently disagree with this one.
        SyncLeftDirectionComboBox(direction);
        await LoadDirectionThumbnailsAsync(direction, folder);
    }

    private void SyncLeftDirectionComboBox(string direction)
    {
        foreach (System.Windows.Controls.ComboBoxItem item in DirectionComboBox.Items)
        {
            if (item.Content.ToString()!.StartsWith(direction, StringComparison.OrdinalIgnoreCase))
            {
                DirectionComboBox.SelectedItem = item;
                break;
            }
        }
    }

    // Generates thumbnails on a background task (Task.Run) so a 50+ photo direction never blocks
    // the UI thread, showing ThumbnailLoadingOverlay for the duration -- per explicit project
    // direction ("별도의 task에서 실행하고 완료될 때까지 로딩중 회전 UI 전시"). Results are cached per
    // direction so switching the combo back to an already-loaded direction is instant.
    private async Task LoadDirectionThumbnailsAsync(string direction, string folder)
    {
        // The previously-selected item (if any) belonged to whichever direction was showing
        // before -- never carries over to a different direction's own set of ReviewImageItem
        // instances.
        _selectedReviewItem = null;
        ViewOriginalButton.IsEnabled = false;

        if (_reviewItemsByDirection.TryGetValue(direction, out var cached))
        {
            ThumbnailItemsControl.ItemsSource = cached;
            return;
        }

        _thumbnailLoadCts?.Cancel();
        var cts = new CancellationTokenSource();
        _thumbnailLoadCts = cts;

        ThumbnailLoadingOverlay.Visibility = Visibility.Visible;
        ThumbnailItemsControl.ItemsSource = null;

        try
        {
            var items = await Task.Run(() =>
            {
                var list = new ObservableCollection<ReviewImageItem>();
                IEnumerable<string> files;
                try
                {
                    files = Directory.EnumerateFiles(folder).OrderBy(f => f, StringComparer.OrdinalIgnoreCase);
                }
                catch (IOException)
                {
                    return list;
                }

                foreach (var file in files)
                {
                    if (cts.Token.IsCancellationRequested)
                        break;
                    // 220 matches the thumbnail column's MaxWidth (see TransferSettingsWindow.xaml)
                    // now that thumbnails resize with the window instead of staying a fixed 96px.
                    list.Add(new ReviewImageItem(file) { ThumbnailSource = TryLoadImage(file, decodePixelWidth: 220) });
                }
                return list;
            }, cts.Token);

            if (cts.Token.IsCancellationRequested)
                return;

            _reviewItemsByDirection[direction] = items;
            ThumbnailItemsControl.ItemsSource = items;
        }
        finally
        {
            if (!cts.Token.IsCancellationRequested)
                ThumbnailLoadingOverlay.Visibility = Visibility.Collapsed;
        }
    }

    // decodePixelWidth keeps a thumbnail decode cheap (full multi-MB drone photos would otherwise
    // all be decoded at native resolution just to show an 88x88 box); null decodes at full
    // resolution for the original-preview panel. Frozen so the BitmapImage can be built on
    // Task.Run's background thread and safely handed to the UI thread afterward.
    private static ImageSource? TryLoadImage(string filePath, int? decodePixelWidth)
    {
        try
        {
            var bitmap = new BitmapImage();
            bitmap.BeginInit();
            bitmap.CacheOption = BitmapCacheOption.OnLoad;
            if (decodePixelWidth.HasValue)
                bitmap.DecodePixelWidth = decodePixelWidth.Value;
            bitmap.UriSource = new Uri(filePath);
            bitmap.EndInit();
            bitmap.Freeze();
            return bitmap;
        }
        catch (Exception ex) when (ex is NotSupportedException or IOException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    // Single click highlights/selects (enables ViewOriginalButton); double click shows the
    // original directly, same as clicking that button for the just-selected thumbnail would.
    // Wired to the Border's MouseLeftButtonDown in XAML, not MouseLeftButtonUp -- confirmed via a
    // real test that checking ClickCount on the Up event never actually fired this (double-clicking
    // a thumbnail did nothing), while the standard "check ClickCount on Down" pattern does.
    private async void OnThumbnailClicked(object sender, MouseButtonEventArgs e)
    {
        if (sender is not FrameworkElement { Tag: ReviewImageItem item })
            return;

        if (e.ClickCount == 1)
        {
            SelectReviewItem(item);
            return;
        }
        if (e.ClickCount != 2)
            return;

        await ShowOriginalAsync(item);
    }

    // The 포함 checkbox lives inside the same Border as the thumbnail image -- clicking it used to
    // also bubble up and single-click-trigger the original-image load every time (confusing, and
    // wasteful: a full-resolution decode on every accidental click), which is why this needs
    // SelectReviewItem to be a no-op beyond the visual highlight rather than opening the preview.
    private void SelectReviewItem(ReviewImageItem item)
    {
        if (_selectedReviewItem == item)
            return;
        if (_selectedReviewItem != null)
            _selectedReviewItem.IsSelected = false;
        _selectedReviewItem = item;
        item.IsSelected = true;
        ViewOriginalButton.IsEnabled = true;
    }

    private void OnViewOriginalButtonClick(object sender, RoutedEventArgs e)
    {
        if (_selectedReviewItem != null)
            _ = ShowOriginalAsync(_selectedReviewItem);
    }

    private async Task ShowOriginalAsync(ReviewImageItem item)
    {
        SelectReviewItem(item);
        OriginalPreviewHint.Visibility = Visibility.Collapsed;
        OriginalPreviewScale.ScaleX = 1.0;
        OriginalPreviewScale.ScaleY = 1.0;
        OriginalPreviewTranslate.X = 0;
        OriginalPreviewTranslate.Y = 0;
        OriginalPreviewImage.Source = await Task.Run(() => TryLoadImage(item.FilePath, decodePixelWidth: null));
    }

    private void OnOriginalPreviewMouseWheel(object sender, MouseWheelEventArgs e)
    {
        if (OriginalPreviewImage.Source == null)
            return;
        var factor = e.Delta > 0 ? 1.1 : 1.0 / 1.1;
        var newScale = Math.Clamp(OriginalPreviewScale.ScaleX * factor, 0.2, 6.0);
        OriginalPreviewScale.ScaleX = newScale;
        OriginalPreviewScale.ScaleY = newScale;
        e.Handled = true;
    }

    private void OnOriginalPreviewMouseDown(object sender, MouseButtonEventArgs e)
    {
        if (OriginalPreviewImage.Source == null)
            return;
        _isPanningOriginalPreview = true;
        _originalPreviewPanStart = e.GetPosition(OriginalPreviewGrid);
        OriginalPreviewGrid.CaptureMouse();
    }

    private void OnOriginalPreviewMouseMove(object sender, MouseEventArgs e)
    {
        if (!_isPanningOriginalPreview)
            return;
        var pos = e.GetPosition(OriginalPreviewGrid);
        OriginalPreviewTranslate.X += pos.X - _originalPreviewPanStart.X;
        OriginalPreviewTranslate.Y += pos.Y - _originalPreviewPanStart.Y;
        _originalPreviewPanStart = pos;
    }

    private void OnOriginalPreviewMouseUp(object sender, MouseButtonEventArgs e)
    {
        _isPanningOriginalPreview = false;
        OriginalPreviewGrid.ReleaseMouseCapture();
    }

    private void OnReviewSelectAllChanged(object sender, RoutedEventArgs e)
    {
        if (ReviewDirectionComboBox.SelectedItem is not string direction)
            return;
        if (!_reviewItemsByDirection.TryGetValue(direction, out var items))
            return;
        var check = ReviewSelectAllCheckBox.IsChecked == true;
        foreach (var item in items)
            item.IsIncluded = check;
    }

    // Removes every currently-unchecked thumbnail from view and remembers its path in
    // _excludedFilesByDirection -- purely a decluttering/confirmation step, not what actually makes
    // exclusion take effect (unchecking a box already does that, see ReviewImageItem's own
    // comment and GetExcludedFileSet below, which counts unchecked-but-still-visible items too).
    private void OnReviewExcludeClick(object sender, RoutedEventArgs e)
    {
        if (ReviewDirectionComboBox.SelectedItem is not string direction)
            return;
        if (!_reviewItemsByDirection.TryGetValue(direction, out var items))
            return;

        if (!_excludedFilesByDirection.TryGetValue(direction, out var excludedSet))
        {
            excludedSet = new HashSet<string>();
            _excludedFilesByDirection[direction] = excludedSet;
        }

        foreach (var item in items.Where(i => !i.IsIncluded).ToList())
        {
            excludedSet.Add(item.FilePath);
            items.Remove(item);
            // Selection (highlight/원본보기) and inclusion are independent -- a highlighted
            // thumbnail can still be unchecked and removed here, which would otherwise leave
            // ViewOriginalButton enabled and pointing at an item no longer in any collection.
            if (_selectedReviewItem == item)
            {
                _selectedReviewItem = null;
                ViewOriginalButton.IsEnabled = false;
            }
        }
    }

    // Authoritative "how many photos in this direction will NOT be sent" count -- unions items
    // already moved out of view by OnReviewExcludeClick with whatever is currently unchecked but
    // still visible, since the checkbox alone is enough to exclude a photo (see ReviewImageItem's
    // own comment); clicking "선택 제외" is not required for a photo to actually be excluded.
    private int GetExcludedCount(string direction) => GetExcludedFileSet(direction).Count;

    private HashSet<string> GetExcludedFileSet(string direction)
    {
        var set = _excludedFilesByDirection.TryGetValue(direction, out var removed)
            ? new HashSet<string>(removed)
            : new HashSet<string>();
        if (_reviewItemsByDirection.TryGetValue(direction, out var items))
        {
            foreach (var item in items.Where(i => !i.IsIncluded))
                set.Add(item.FilePath);
        }
        return set;
    }

    // rsync has no "send this folder except these files" mode, so an exclusion can only be
    // enforced by pointing rsync at a filtered copy instead of the operator's real folder. Returns
    // the original folder unchanged (no copy, no cleanup needed) when nothing in it is excluded --
    // the common case, and the only one that matters when the review panel was never touched at
    // all for this direction.
    private string PrepareTransferFolder(string direction, string originalFolder)
    {
        var excluded = GetExcludedFileSet(direction);
        if (excluded.Count == 0)
            return originalFolder;

        var stagingRoot = Path.Combine(Path.GetTempPath(), "FacadePreviewer_stage", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(stagingRoot);
        foreach (var file in Directory.EnumerateFiles(originalFolder))
        {
            if (excluded.Contains(file))
                continue;
            File.Copy(file, Path.Combine(stagingRoot, Path.GetFileName(file)), overwrite: true);
        }
        _stagingFoldersCreated.Add(stagingRoot);
        return stagingRoot;
    }

    private void CleanupStagingFolders()
    {
        foreach (var dir in _stagingFoldersCreated)
        {
            try
            {
                if (Directory.Exists(dir))
                    Directory.Delete(dir, recursive: true);
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
        _stagingFoldersCreated.Clear();
    }

    private void OnBrowseSshKey(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFileDialog { Title = "SSH 개인키 선택", Filter = "모든 파일|*.*" };
        if (dialog.ShowDialog() == true)
            SshKeyTextBox.Text = dialog.FileName;
    }

    // [YYIL] A private key extracted from DDS Monitor's SSH-key-issuance zip (via Windows
    // Explorer's "압축 풀기") inherits broad NTFS ACLs from its parent folder -- Cygwin's ssh.exe
    // (matching every real OpenSSH client) translates that into POSIX-style permission bits that
    // look "too open" (confirmed via a real repro: 0750, group-readable) and REFUSES to use the
    // key at all ("WARNING: UNPROTECTED PRIVATE KEY FILE! ... This private key will be ignored."),
    // silently falling back to password auth, which then fails -- producing the exact same generic
    // "Permission denied (publickey,password)" message as every OTHER auth problem this feature
    // has hit, with nothing in that message hinting the real cause was file permissions. This will
    // hit every operator who extracts the zip via Explorer (the only supported workflow), not just
    // an edge case -- so fix it here automatically via icacls (built into every Windows install)
    // instead of requiring each operator to know to run icacls/chmod by hand. Best-effort: any
    // failure here (e.g. icacls missing, path on a filesystem that doesn't support ACL changes) is
    // silently ignored -- ssh's own permission check will surface clearly either way if this
    // didn't help, same as before this fix existed.
    private static void TightenPrivateKeyPermissions(string path)
    {
        try
        {
            var currentUser = $"{Environment.UserDomainName}\\{Environment.UserName}";
            var psi = new ProcessStartInfo
            {
                FileName = "icacls.exe",
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            // /inheritance:r (strip inherited ACEs) and /grant:r (grant explicitly, replacing any
            // existing explicit grant) are applied in the SAME icacls call so the file is never
            // left with zero valid ACEs in between the two steps.
            psi.ArgumentList.Add(path);
            psi.ArgumentList.Add("/inheritance:r");
            psi.ArgumentList.Add("/grant:r");
            psi.ArgumentList.Add($"{currentUser}:R");

            using var process = Process.Start(psi);
            process?.WaitForExit(5000);
        }
        catch (Exception ex) when (ex is System.ComponentModel.Win32Exception or InvalidOperationException)
        {
        }
    }

    private void OnCancelClick(object sender, RoutedEventArgs e)
    {
        if (_phase == Phase.Storing)
        {
            _storageStatus?.SendCancelRequest(_pendingCompany, _pendingBuilding);
            StatusText.Text = "저장 처리 취소 요청을 보냈습니다. 서버 응답을 기다리는 중...";
        }
        else
        {
            _transfer?.Cancel();
        }
    }

    protected override void OnClosed(EventArgs e)
    {
        _transfer?.Dispose();
        _transfer = null;
        _storageStatus?.Dispose();
        _storageStatus = null;
        _analysisCommand?.Dispose();
        _analysisCommand = null;
        _thumbnailLoadCts?.Cancel();
        CleanupStagingFolders();
        base.OnClosed(e);
    }
}
