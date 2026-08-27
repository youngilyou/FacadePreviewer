// Thin wrapper around the vendored Cygwin rsync.exe (see previewer/tools/Get-CygwinRsync.ps1)
// for the facade high-resolution image transfer feature. Deliberately just a CreateProcess
// wrapper around the external rsync.exe binary, not a reimplementation/relink of rsync itself --
// keeps Cygwin fully arm's-length from this MSVC-built DLL (no shared headers/libs), per
// explicit project instruction (mixing Cygwin and Visual Studio in one build has caused real
// problems on this project before). Same pattern as VideoDecoder.cpp/JpegFacadePublisher.cpp's
// existing ffmpeg.exe CreateProcess wrapper.
#pragma once

#include <windows.h>

#include <atomic>
#include <cstdint>
#include <string>
#include <thread>

// Fires from the background reader thread, not the caller's thread -- same contract as
// DdsFrameSubscriber's callbacks (see FacadeDdsBridge.h), caller must marshal to its own UI
// thread if needed.
using FacadeRsyncProgressCallback = void (*)(uint64_t bytes_transferred, int percent, double rate_mbps, void* user_data);
// exit_code == 0 means rsync reported success. error_message is empty on success, otherwise a
// short diagnostic (last non-empty stderr line, or a CreateProcess/launch failure description).
using FacadeRsyncCompleteCallback = void (*)(int exit_code, const char* error_message, void* user_data);

class RsyncTransfer
{
public:
    RsyncTransfer() = default;
    ~RsyncTransfer();

    RsyncTransfer(const RsyncTransfer&) = delete;
    RsyncTransfer& operator=(const RsyncTransfer&) = delete;

    // rsync_exe_path: path to the vendored rsync.exe (previewer/tools/cygwin_rsync/bin/rsync.exe).
    // local_source_dir: local folder to push (must already be laid out as
    //   <company>/<building>/<direction>/<session_id>/*.jpg -- this wrapper does not build that
    //   tree, the caller/UI does, see MainViewModel's session-folder logic).
    // ssh_key_path: pass empty string to use ssh's own default key discovery (~/.ssh/*), i.e. no
    //   -i flag is added.
    // ssh_password: 2026-08-27, "SSH 키 없으면 Password로" requirement -- only consulted when
    //   ssh_key_path is empty (key always wins if both are set, matching the operator's own
    //   stated preference order). Non-interactive password auth needs sshpass (vendored alongside
    //   rsync.exe/ssh.exe, see Get-CygwinRsync.ps1) since plain ssh has no non-interactive
    //   password flag of its own; empty ssh_password with an empty ssh_key_path falls back to
    //   ssh's own default key discovery exactly as before this parameter existed.
    // remote_dest_root: destination base path on the DDS-Router host (the FacadeImageBridge
    //   watch root) -- local_source_dir's own leaf directories get rsync'd underneath it.
    // resume: false (default/original behavior) re-runs plain rsync -avz -- already-complete
    //   files are still skipped by rsync's own quick-check, only a file left mid-copy by a prior
    //   interrupted run gets fully re-sent from byte 0. true adds --partial --append-verify, so a
    //   large file interrupted mid-copy resumes from where it left off (verified byte-for-byte
    //   against the source first) instead of being fully retransmitted -- exposed as an explicit
    //   operator choice ("처음부터 전송" vs "이어서 전송") after a transfer failure, see
    //   TransferSettingsWindow's retry prompt.
    // Returns false if the process could not even be launched (rsync.exe missing, CreateProcess
    // failure) -- check GetLastError()/the completion callback is NOT fired in that case, the
    // failure is synchronous.
    bool Start(
            const std::wstring& rsync_exe_path,
            const std::wstring& local_source_dir,
            const std::string& ssh_user,
            const std::string& ssh_host,
            int ssh_port,
            const std::wstring& ssh_key_path,
            const std::wstring& ssh_password,
            const std::string& remote_dest_root,
            bool resume,
            FacadeRsyncProgressCallback progress_cb,
            FacadeRsyncCompleteCallback complete_cb,
            void* user_data);

    // Best-effort: terminates the rsync.exe process tree. Re-running Start() from the UI
    // afterward is an explicit, deliberate retry (see resume parameter above for the two ways
    // that retry can behave).
    void Cancel();

private:
    void ReaderThreadMain();

    PROCESS_INFORMATION process_info_{};
    HANDLE stdout_read_ = nullptr;
    std::thread reader_thread_;
    std::atomic<bool> cancelled_{false};

    FacadeRsyncProgressCallback progress_cb_ = nullptr;
    FacadeRsyncCompleteCallback complete_cb_ = nullptr;
    void* user_data_ = nullptr;
};
