#include "RsyncTransfer.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <sstream>
#include <vector>

namespace
{

// [YYIL] 2026-08-21: was `out += static_cast<char>(normalized[i])` per wchar_t -- truncates every
// UTF-16 code unit down to its low byte, which is a no-op for ASCII but silently destroys any
// non-ASCII character (Korean 회사/동 names in particular, e.g. captured from a company/building
// picked in FacadePreviewer's UI). Confirmed via a real transfer: a company/building containing
// Korean text landed on the remote host as garbage bytes in the destination folder name -- the
// files themselves transferred fine (rsync doesn't care what the path bytes mean), but the
// resulting facade_image_sessions.building value no longer matched what FacadePreviewer had
// separately registered via POST api/crackvision/building-requirements (that call goes over
// HTTP/JSON, not through this path, so it kept the correct UTF-8 text) -- so
// check_and_enqueue_if_complete never saw a match and the archive silently never triggered.
// Proper fix: encode non-ASCII text as UTF-8 (Cygwin's own default path/filename encoding),
// not by truncating UTF-16 code units.
std::string WideToUtf8(const std::wstring& wide)
{
    if (wide.empty())
        return {};
    const int len = WideCharToMultiByte(CP_UTF8, 0, wide.c_str(), -1, nullptr, 0, nullptr, nullptr);
    std::string out(len, '\0');
    WideCharToMultiByte(CP_UTF8, 0, wide.c_str(), -1, out.data(), len, nullptr, nullptr);
    if (!out.empty() && out.back() == '\0')
        out.pop_back();
    return out;
}

// Cygwin rsync.exe expects Cygwin-style POSIX paths for reliable behavior with -a
// (permissions/symlink semantics) -- "D:\foo\bar" -> "/cygdrive/d/foo/bar", the standard,
// documented Cygwin path-translation convention (same one cwRsync users have always had to
// apply manually; done here automatically so the C# caller only ever deals in native Windows
// paths).
std::string ToCygdrivePath(const std::wstring& windows_path)
{
    std::wstring normalized = windows_path;
    std::replace(normalized.begin(), normalized.end(), L'\\', L'/');

    if (normalized.size() >= 2 && normalized[1] == L':')
    {
        std::string out = "/cygdrive/";
        out += static_cast<char>(std::tolower(static_cast<unsigned char>(normalized[0])));
        out += WideToUtf8(normalized.substr(2));
        return out;
    }

    // Not a drive-letter path (e.g. a UNC path) -- pass through as-is (UTF-8 encoded) and let
    // rsync/Cygwin's own runtime attempt its usual translation; not a case this feature is
    // expected to hit (FacadePreviewer's folder picker always yields a local drive path).
    return WideToUtf8(normalized);
}

std::wstring Utf8ToWide(const std::string& utf8)
{
    if (utf8.empty())
        return {};
    const int len = MultiByteToWideChar(CP_UTF8, 0, utf8.c_str(), -1, nullptr, 0);
    std::wstring out(len, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, utf8.c_str(), -1, out.data(), len);
    if (!out.empty() && out.back() == L'\0')
        out.pop_back();
    return out;
}

// Quote one argument per Windows CreateProcess command-line rules (the same rules CommandLineToArgvW
// parses back), matching JpegFacadePublisher.cpp's own existing implementation of this for consistency.
std::wstring QuoteArg(const std::wstring& arg)
{
    if (!arg.empty() && arg.find_first_of(L" \t\n\v\"") == std::wstring::npos)
        return arg;

    std::wstring out = L"\"";
    for (size_t i = 0; i < arg.size();)
    {
        size_t backslashes = 0;
        while (i < arg.size() && arg[i] == L'\\')
        {
            ++backslashes;
            ++i;
        }
        if (i == arg.size())
        {
            out.append(backslashes * 2, L'\\');
            break;
        }
        if (arg[i] == L'"')
        {
            out.append(backslashes * 2 + 1, L'\\');
            out += L'"';
            ++i;
        }
        else
        {
            out.append(backslashes, L'\\');
            out += arg[i];
            ++i;
        }
    }
    out += L'"';
    return out;
}

// Parses one --info=progress2 line, e.g.
//   "     50,331,650  42%   12.34MB/s    0:00:03 (xfr#12, to-chk=88/103)"
// Returns false if the line doesn't look like a progress line at all (rsync also prints plain
// per-file names and summary lines with -v, which this correctly ignores).
bool ParseProgress2Line(const std::string& line, uint64_t& bytes, int& percent, double& rate_mbps)
{
    const auto percent_pos = line.find('%');
    if (percent_pos == std::string::npos)
        return false;

    // Percent: walk backward from '%' over digits.
    size_t p = percent_pos;
    while (p > 0 && std::isdigit(static_cast<unsigned char>(line[p - 1])))
        --p;
    if (p == percent_pos)
        return false;
    percent = std::atoi(line.substr(p, percent_pos - p).c_str());

    // Bytes transferred: leading run of digits/commas before the percent field.
    std::string digits;
    for (char c : line)
    {
        if (std::isdigit(static_cast<unsigned char>(c)))
            digits += c;
        else if (c == ',')
            continue;
        else if (!digits.empty())
            break;
    }
    bytes = digits.empty() ? 0ULL : std::stoull(digits);

    // Rate: "<number>(kB|MB|GB)/s" somewhere after the percent field.
    rate_mbps = 0.0;
    const auto slash_s = line.find("/s", percent_pos);
    if (slash_s != std::string::npos)
    {
        size_t start = slash_s;
        while (start > 0 && (std::isdigit(static_cast<unsigned char>(line[start - 1])) || line[start - 1] == '.' ||
                std::isalpha(static_cast<unsigned char>(line[start - 1]))))
            --start;
        const std::string token = line.substr(start, slash_s - start);
        double value = 0.0;
        char unit[8] = {};
        if (std::sscanf(token.c_str(), "%lf%7s", &value, unit) >= 1)
        {
            std::string u(unit);
            if (u == "kB")
                rate_mbps = value / 1024.0;
            else if (u == "MB")
                rate_mbps = value;
            else if (u == "GB")
                rate_mbps = value * 1024.0;
            else
                rate_mbps = value / (1024.0 * 1024.0); // assume bytes/s
        }
    }

    return true;
}

} // namespace

bool RsyncTransfer::Start(
        const std::wstring& rsync_exe_path,
        const std::wstring& local_source_dir,
        const std::string& ssh_user,
        const std::string& ssh_host,
        int ssh_port,
        const std::wstring& ssh_key_path,
        const std::string& remote_dest_root,
        bool resume,
        FacadeRsyncProgressCallback progress_cb,
        FacadeRsyncCompleteCallback complete_cb,
        void* user_data)
{
    progress_cb_ = progress_cb;
    complete_cb_ = complete_cb;
    user_data_ = user_data;
    cancelled_.store(false);

    // [YYIL] 2026-08-21: Cygwin's rsync.exe spawning a NATIVE (non-Cygwin) Windows OpenSSH
    // ssh.exe as its child breaks the rsync binary protocol stream immediately, every time
    // ("connection unexpectedly closed (0 bytes received so far)", confirmed via a real transfer
    // test independent of path encoding/compression/dry-run) -- Cygwin's and native Win32's
    // stdio-handle/pipe semantics don't interoperate cleanly for this kind of raw byte-stream
    // piping. Fixed the same way every historical Windows rsync distribution (cwRsync,
    // DeltaCopy, ...) already does: use a Cygwin-built ssh.exe instead, vendored alongside
    // rsync.exe itself (see tools/Get-CygwinRsync.ps1) -- found here by sibling path lookup
    // rather than PATH search, so it can't silently resolve to some other ssh on the machine.
    const std::wstring rsync_dir = rsync_exe_path.substr(0, rsync_exe_path.find_last_of(L"/\\") + 1);
    const std::string ssh_cygpath = ToCygdrivePath(rsync_dir + L"ssh.exe");

    std::ostringstream ssh_cmd;
    ssh_cmd << ssh_cygpath << " -p " << ssh_port << " -o StrictHostKeyChecking=accept-new"
            // Cygwin's ssh.exe resolves $HOME to /home/<Windows username> by default, which
            // doesn't exist (no Cygwin install providing that directory tree) -- without this,
            // every connection first fails to create/write ~/.ssh/known_hosts (harmless, but
            // noisy, and would silently no-op host-key persistence across runs anyway since that
            // write keeps failing). Skipping known_hosts entirely is fine here: host identity
            // isn't this feature's security boundary -- the SSH key pair already is.
            << " -o UserKnownHostsFile=/dev/null"
            // [YYIL] 2026-08-21: confirmed via a real ~1.9GB/many-hundred-file transfer that this
            // matters -- rsync spends a while up front building its file list (stat-ing and
            // checksumming every source file) before any protocol data actually flows over the
            // SSH pipe, and with no keepalive at all, a NAT/firewall/idle-connection policy on the
            // path can (and did) drop the TCP connection during that silent stretch, surfacing as
            // "connection unexpectedly closed (0 bytes received so far)" the moment rsync then
            // tries to actually use it -- indistinguishable from the real bug this same error text
            // pointed at earlier (see the Cygwin-ssh comment above) but with an entirely different
            // root cause. ServerAliveInterval makes this ssh client itself send a keepalive probe
            // every 15s and give up only after 6 consecutive unanswered ones (90s), instead of
            // staying completely silent for however long the file-list build takes.
            << " -o ServerAliveInterval=15 -o ServerAliveCountMax=6";
    if (!ssh_key_path.empty())
        ssh_cmd << " -i " << ToCygdrivePath(ssh_key_path);

    const std::string source_cygpath = ToCygdrivePath(local_source_dir) + "/";
    const std::string remote_spec = ssh_user + "@" + ssh_host + ":" + remote_dest_root + "/";

    std::wstring cmdline = QuoteArg(rsync_exe_path);
    // --mkpath: the destination is always <remote_dest_root>/<company>/<building>/<direction>/
    // <session_id>/, several directory levels deeper than remote_dest_root itself -- rsync only
    // auto-creates the final path component by default, and fails outright ("No such file or
    // directory") if the parents in between don't exist yet, which for a brand-new
    // company/building/direction combination they never do. --mkpath (rsync >= 3.2.3, present in
    // both this vendored client and every rsync version FacadeImageBridge's host has been seen
    // running) creates the whole missing chain.
    cmdline += L" -avz --mkpath --info=progress2";
    if (resume)
        cmdline += L" --partial --append-verify";
    cmdline += L" -e " + QuoteArg(Utf8ToWide(ssh_cmd.str()));
    cmdline += L" " + QuoteArg(Utf8ToWide(source_cygpath));
    cmdline += L" " + QuoteArg(Utf8ToWide(remote_spec));

    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = TRUE;

    HANDLE stdout_write = nullptr;
    if (!CreatePipe(&stdout_read_, &stdout_write, &sa, 0))
        return false;
    SetHandleInformation(stdout_read_, HANDLE_FLAG_INHERIT, 0);

    STARTUPINFOW si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdOutput = stdout_write;
    si.hStdError = stdout_write; // merged -- see header comment on error_message.
    si.hStdInput = GetStdHandle(STD_INPUT_HANDLE);

    std::vector<wchar_t> cmdline_buf(cmdline.begin(), cmdline.end());
    cmdline_buf.push_back(L'\0');

    const BOOL created = CreateProcessW(
            nullptr, cmdline_buf.data(), nullptr, nullptr, TRUE,
            CREATE_NO_WINDOW, nullptr, nullptr, &si, &process_info_);

    CloseHandle(stdout_write);
    if (!created)
    {
        CloseHandle(stdout_read_);
        stdout_read_ = nullptr;
        return false;
    }

    reader_thread_ = std::thread([this] { ReaderThreadMain(); });
    return true;
}

void RsyncTransfer::ReaderThreadMain()
{
    std::string buffer;
    std::string last_nonempty_line;
    // [YYIL] 2026-08-21: "rsync error: error in rsync protocol data stream (code 12)" is a
    // catch-all rsync prints whenever the underlying ssh connection dies for *any* reason -- a
    // dropped network, a wrong SSH key, wrong credentials, all look identical from rsync's side
    // and produce this exact same last line. Confirmed via a real repro: an operator who left the
    // SSH key field blank got exactly this generic message with no hint that the real problem was
    // authentication -- ssh's own much more specific "Permission denied (publickey,password)."
    // line appeared several lines earlier in the same output, but last_nonempty_line only ever
    // kept the final line, silently discarding it. Track the last line matching this one known,
    // clearly-actionable pattern separately and prefer it over the generic one when present.
    std::string auth_failure_line;
    char chunk[4096];

    for (;;)
    {
        DWORD bytes_read = 0;
        const BOOL ok = ReadFile(stdout_read_, chunk, sizeof(chunk), &bytes_read, nullptr);
        if (!ok || bytes_read == 0)
            break;

        buffer.append(chunk, bytes_read);

        // --info=progress2 uses \r to redraw the same line; ordinary rsync output uses \n.
        // Split on either so both styles are handled.
        size_t pos;
        while ((pos = buffer.find_first_of("\r\n")) != std::string::npos)
        {
            std::string line = buffer.substr(0, pos);
            buffer.erase(0, pos + 1);
            if (line.empty())
                continue;
            last_nonempty_line = line;
            if (line.find("Permission denied") != std::string::npos)
                auth_failure_line = line;

            uint64_t bytes = 0;
            int percent = 0;
            double rate = 0.0;
            if (progress_cb_ && ParseProgress2Line(line, bytes, percent, rate))
                progress_cb_(bytes, percent, rate, user_data_);
        }
    }

    CloseHandle(stdout_read_);
    stdout_read_ = nullptr;

    WaitForSingleObject(process_info_.hProcess, INFINITE);
    DWORD exit_code = 1;
    GetExitCodeProcess(process_info_.hProcess, &exit_code);
    CloseHandle(process_info_.hProcess);
    CloseHandle(process_info_.hThread);

    if (complete_cb_)
    {
        if (cancelled_.load())
            complete_cb_(static_cast<int>(exit_code), "cancelled by user", user_data_);
        else if (exit_code == 0)
            complete_cb_(0, "", user_data_);
        else if (!auth_failure_line.empty())
            complete_cb_(static_cast<int>(exit_code),
                    ("SSH 인증 실패 (" + auth_failure_line + ") -- SSH 키 경로를 확인하세요.").c_str(), user_data_);
        else
            complete_cb_(static_cast<int>(exit_code), last_nonempty_line.c_str(), user_data_);
    }
}

void RsyncTransfer::Cancel()
{
    if (process_info_.hProcess)
    {
        cancelled_.store(true);
        TerminateProcess(process_info_.hProcess, 1);
    }
}

RsyncTransfer::~RsyncTransfer()
{
    Cancel();
    if (reader_thread_.joinable())
        reader_thread_.join();
}
