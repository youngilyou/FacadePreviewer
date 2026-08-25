#include "PublishUi.h"
#include "JpegFacadePublisher.h"

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <Windows.h>
#include <shobjidl.h>

#include <atomic>
#include <cstdio>
#include <string>
#include <thread>

namespace {

// RTMP server stays fixed (사용자 확정 사항) -- only the stream name (drone) varies, built at
// publish time from whichever DRONE0N is selected in the combo below. Same host:port used
// throughout this project's own docs/scripts (previewer/CLAUDE.local.md,
// FacadeDdsBridge/Setup-VisualStudio.ps1's F5 debug args) and the same fps the
// --publish-facade CLI mode already defaults to.
const wchar_t* const kWindowClassName = L"FacadeDdsBridgeSmokeTest_PublishUi";
const char* const kRtmpServerBase = "rtmp://192.168.100.224:1935/live/";
const double kDefaultFps = 2.0;
const int kDroneCount = 10; // DRONE01..DRONE10 -- matches GenerateJson/FacadePreviewer's own list

enum ControlId : int
{
    kIdBrowseBtn = 1001,
    kIdFolderLabel = 1002,
    kIdStatusLabel = 1003,
    kIdPublishBtn = 1004,
    kIdStopBtn = 1005,
    kIdResetBtn = 1006,
    kIdDroneCombo = 1007,
};

const UINT kWmPublishDone = WM_APP + 1;

// Single-window app -- module-level state instead of per-instance data is fine here (matches
// SmokeTest.cpp's own existing style of plain static globals for counters/state).
HWND g_hwnd = nullptr;
HWND g_browse_btn = nullptr;
HWND g_folder_label = nullptr;
HWND g_drone_combo = nullptr;
HWND g_status_label = nullptr;
HWND g_publish_btn = nullptr;
HWND g_stop_btn = nullptr;
HWND g_reset_btn = nullptr;

std::wstring g_selected_folder;
std::atomic<bool> g_cancel{false};
std::atomic<bool> g_publishing{false};
std::thread g_worker;

// CB_GETCURSEL/CB_GETLBTEXT -- returns empty if nothing selected yet.
std::string GetSelectedDrone()
{
    int idx = (int)SendMessageW(g_drone_combo, CB_GETCURSEL, 0, 0);
    if (idx == CB_ERR)
    {
        return "";
    }
    wchar_t buf[32];
    SendMessageW(g_drone_combo, CB_GETLBTEXT, (WPARAM)idx, (LPARAM)buf);
    int bytes = WideCharToMultiByte(CP_UTF8, 0, buf, -1, nullptr, 0, nullptr, nullptr);
    std::string out(bytes > 0 ? bytes - 1 : 0, '\0');
    if (bytes > 0)
    {
        WideCharToMultiByte(CP_UTF8, 0, buf, -1, out.data(), bytes, nullptr, nullptr);
    }
    return out;
}

void SetStatus(const wchar_t* text)
{
    SetWindowTextW(g_status_label, text);
}

// Idle: 폴더 선택/드론 선택/발행/리셋 enabled, 정지 disabled.
// Publishing (or stopping): 폴더 선택/드론 선택/발행/리셋 disabled, 정지 enabled until Stop is clicked.
void UpdateButtonStates()
{
    bool publishing = g_publishing.load();
    EnableWindow(g_browse_btn, !publishing);
    EnableWindow(g_drone_combo, !publishing);
    EnableWindow(g_publish_btn, !publishing);
    EnableWindow(g_reset_btn, !publishing);
    EnableWindow(g_stop_btn, publishing && !g_cancel.load());
}

void BrowseForFolder(HWND owner)
{
    IFileDialog* dialog = nullptr;
    HRESULT hr = CoCreateInstance(CLSID_FileOpenDialog, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&dialog));
    if (FAILED(hr))
    {
        wchar_t buf[128];
        swprintf_s(buf, L"폴더 선택 초기화 실패 (0x%08X)", (unsigned int)hr);
        SetStatus(buf);
        fprintf(stderr, "[publish-ui] CoCreateInstance(CLSID_FileOpenDialog) failed: 0x%08X\n", (unsigned int)hr);
        return;
    }

    DWORD options = 0;
    dialog->GetOptions(&options);
    dialog->SetOptions(options | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST);

    HRESULT show_hr = dialog->Show(owner);
    if (FAILED(show_hr) && show_hr != HRESULT_FROM_WIN32(ERROR_CANCELLED))
    {
        fprintf(stderr, "[publish-ui] IFileDialog::Show failed: 0x%08X\n", (unsigned int)show_hr);
    }
    if (SUCCEEDED(show_hr))
    {
        IShellItem* item = nullptr;
        if (SUCCEEDED(dialog->GetResult(&item)))
        {
            PWSTR path = nullptr;
            if (SUCCEEDED(item->GetDisplayName(SIGDN_FILESYSPATH, &path)))
            {
                g_selected_folder = path;
                SetWindowTextW(g_folder_label, g_selected_folder.c_str());
                SetStatus(L"대기중 (발행 준비됨)");
                CoTaskMemFree(path);
            }
            item->Release();
        }
    }
    dialog->Release();
}

// Runs on a background thread -- PublishFacadeJpegsViaRtmp blocks for the whole ffmpeg
// encode + RTMP push, which would otherwise freeze this window's message loop.
void RunPublishWorker(std::wstring folder, std::string rtmp_url)
{
    bool ok = PublishFacadeJpegsViaRtmp(folder, rtmp_url, kDefaultFps, /*keep_flv=*/false, &g_cancel);
    PostMessageW(g_hwnd, kWmPublishDone, ok ? 1 : 0, g_cancel.load() ? 1 : 0);
}

void OnPublishClick()
{
    if (g_publishing.load())
    {
        return;
    }
    if (g_selected_folder.empty())
    {
        SetStatus(L"먼저 폴더를 선택하세요");
        return;
    }
    std::string drone = GetSelectedDrone();
    if (drone.empty())
    {
        SetStatus(L"먼저 드론을 선택하세요");
        return;
    }

    std::string rtmp_url = kRtmpServerBase + drone;

    g_cancel = false;
    g_publishing = true;
    UpdateButtonStates();
    wchar_t status[128];
    swprintf_s(status, L"발행 중... (%hs)", rtmp_url.c_str());
    SetStatus(status);

    if (g_worker.joinable())
    {
        g_worker.join(); // previous run already posted kWmPublishDone, so this returns immediately
    }
    g_worker = std::thread(RunPublishWorker, g_selected_folder, rtmp_url);
}

void OnStopClick()
{
    if (!g_publishing.load())
    {
        return;
    }
    g_cancel = true;
    EnableWindow(g_stop_btn, FALSE);
    SetStatus(L"정지 중...");
}

void OnResetClick()
{
    if (g_publishing.load())
    {
        return;
    }
    g_selected_folder.clear();
    SetWindowTextW(g_folder_label, L"(폴더를 선택하세요)");
    SendMessageW(g_drone_combo, CB_SETCURSEL, (WPARAM)-1, 0);
    SetStatus(L"대기중");
}

void OnPublishDone(bool success, bool canceled)
{
    g_publishing = false;
    UpdateButtonStates();
    if (canceled)
    {
        SetStatus(L"정지됨");
    }
    else if (success)
    {
        SetStatus(L"완료");
    }
    else
    {
        SetStatus(L"실패 (콘솔 로그 확인)");
    }
}

// Blocks (briefly) until any in-progress publish actually stops -- ffmpeg's TerminateProcess
// and the RTMP loop's per-tag cancel check both resolve within ~200ms, so this doesn't hang
// the close. Without this, a still-running background thread would touch g_cancel/g_hwnd
// after the window (and eventually the process) is gone, and a joinable std::thread destroyed
// at process exit while still joinable calls std::terminate.
void StopAndJoinWorkerBeforeExit()
{
    if (g_publishing.load())
    {
        g_cancel = true;
    }
    if (g_worker.joinable())
    {
        g_worker.join();
    }
}

LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wparam, LPARAM lparam)
{
    switch (msg)
    {
    case WM_CREATE:
    {
        g_hwnd = hwnd;
        HFONT font = (HFONT)GetStockObject(DEFAULT_GUI_FONT);

        g_browse_btn = CreateWindowExW(0, L"BUTTON", L"폴더 선택...", WS_CHILD | WS_VISIBLE | WS_TABSTOP,
                                        20, 20, 120, 30, hwnd, (HMENU)(INT_PTR)kIdBrowseBtn, nullptr, nullptr);
        g_folder_label = CreateWindowExW(0, L"STATIC", L"(폴더를 선택하세요)", WS_CHILD | WS_VISIBLE,
                                          150, 26, 300, 20, hwnd, (HMENU)(INT_PTR)kIdFolderLabel, nullptr, nullptr);

        HWND drone_label = CreateWindowExW(0, L"STATIC", L"드론", WS_CHILD | WS_VISIBLE,
                                            20, 66, 60, 20, hwnd, nullptr, nullptr, nullptr);
        // CBS_DROPDOWNLIST -- 목록 중에서만 고르게(직접 입력 불가), 이 도구는 DRONE01~10 고정
        // 목록이면 충분(GenerateJson/FacadePreviewer와 동일 이름 규칙). height=200은 드롭다운이
        // 펼쳐졌을 때 목록 전체가 보이도록 하기 위함(Win32 콤보박스 관례 -- 닫혀 있을 때 실제
        // 보이는 높이는 폰트 기준으로 자동 결정됨).
        g_drone_combo = CreateWindowExW(0, L"COMBOBOX", L"", WS_CHILD | WS_VISIBLE | WS_TABSTOP | CBS_DROPDOWNLIST | WS_VSCROLL,
                                         90, 62, 150, 200, hwnd, (HMENU)(INT_PTR)kIdDroneCombo, nullptr, nullptr);
        for (int i = 1; i <= kDroneCount; ++i)
        {
            wchar_t name[16];
            swprintf_s(name, L"DRONE%02d", i);
            SendMessageW(g_drone_combo, CB_ADDSTRING, 0, (LPARAM)name);
        }

        g_status_label = CreateWindowExW(0, L"STATIC", L"대기중", WS_CHILD | WS_VISIBLE,
                                          20, 100, 430, 20, hwnd, (HMENU)(INT_PTR)kIdStatusLabel, nullptr, nullptr);
        g_publish_btn = CreateWindowExW(0, L"BUTTON", L"발행", WS_CHILD | WS_VISIBLE | WS_TABSTOP,
                                         20, 140, 120, 36, hwnd, (HMENU)(INT_PTR)kIdPublishBtn, nullptr, nullptr);
        g_stop_btn = CreateWindowExW(0, L"BUTTON", L"정지", WS_CHILD | WS_VISIBLE | WS_TABSTOP,
                                      160, 140, 120, 36, hwnd, (HMENU)(INT_PTR)kIdStopBtn, nullptr, nullptr);
        g_reset_btn = CreateWindowExW(0, L"BUTTON", L"리셋", WS_CHILD | WS_VISIBLE | WS_TABSTOP,
                                       300, 140, 120, 36, hwnd, (HMENU)(INT_PTR)kIdResetBtn, nullptr, nullptr);

        HWND controls[] = {g_browse_btn, g_folder_label, drone_label, g_drone_combo, g_status_label, g_publish_btn, g_stop_btn, g_reset_btn};
        for (HWND child : controls)
        {
            SendMessageW(child, WM_SETFONT, (WPARAM)font, TRUE);
        }
        UpdateButtonStates();
        return 0;
    }
    case WM_COMMAND:
        switch (LOWORD(wparam))
        {
        case kIdBrowseBtn: BrowseForFolder(hwnd); return 0;
        case kIdPublishBtn: OnPublishClick(); return 0;
        case kIdStopBtn: OnStopClick(); return 0;
        case kIdResetBtn: OnResetClick(); return 0;
        }
        return 0;
    case kWmPublishDone:
        if (g_worker.joinable())
        {
            g_worker.join(); // worker already returned (it just posted this message), instant
        }
        OnPublishDone(wparam != 0, lparam != 0);
        return 0;
    case WM_CLOSE:
        StopAndJoinWorkerBeforeExit();
        DestroyWindow(hwnd);
        return 0;
    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(hwnd, msg, wparam, lparam);
}

} // namespace

bool RunPublishUi()
{
    HRESULT co_result = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    bool co_owned = SUCCEEDED(co_result);

    WNDCLASSW wc{};
    wc.lpfnWndProc = WndProc;
    wc.hInstance = GetModuleHandleW(nullptr);
    wc.lpszClassName = kWindowClassName;
    wc.hCursor = LoadCursorW(nullptr, MAKEINTRESOURCEW(32512)); // IDC_ARROW -- this project doesn't
                                                                 // define UNICODE, so the plain
                                                                 // IDC_ARROW macro resolves to the
                                                                 // ANSI (LPSTR) overload and doesn't
                                                                 // match LoadCursorW's LPCWSTR param
    wc.hbrBackground = (HBRUSH)(COLOR_BTNFACE + 1);
    RegisterClassW(&wc);

    HWND hwnd = CreateWindowExW(
        0, kWindowClassName, L"FacadeDdsBridgeSmokeTest -- 발행",
        WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX,
        CW_USEDEFAULT, CW_USEDEFAULT, 520, 280,
        nullptr, nullptr, GetModuleHandleW(nullptr), nullptr);
    if (!hwnd)
    {
        if (co_owned) CoUninitialize();
        return false;
    }

    ShowWindow(hwnd, SW_SHOWNORMAL);
    UpdateWindow(hwnd);

    MSG msg;
    while (GetMessageW(&msg, nullptr, 0, 0) > 0)
    {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }

    if (co_owned)
    {
        CoUninitialize();
    }
    return true;
}
