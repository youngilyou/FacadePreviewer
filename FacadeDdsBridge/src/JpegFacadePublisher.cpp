#include "JpegFacadePublisher.h"
#include "RtmpPublisher.h"
#include "FacadeDdsBridge.h"

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <Windows.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <optional>
#include <sstream>
#include <thread>
#include <vector>

namespace fs = std::filesystem;

namespace {

// [YYIL] 2026-08-13: minimal DJI XMP pose reader -- part of the pose-anchoring feature
// (previewer's own long-documented design: "ORB is primary alignment, pose is a periodic
// drift anchor/consistency check", see project memory project-dds-previewer-design and
// FacadeStitcher.cs's class doc comment). No XML/EXIF library dependency: DJI's XMP packet is
// a flat run of `drone-dji:Key="value"` attributes (confirmed by direct inspection of real
// files under facades/우), so a bounded byte-range search + substring extraction is simpler
// and more robust here than pulling in a general XMP/EXIF parser for 9 known field names.
struct DjiXmpPose
{
    bool valid = false;
    double gps_latitude_deg = 0;
    double gps_longitude_deg = 0;
    double gps_altitude_m = 0; // AbsoluteAltitude
    double gimbal_yaw_deg = 0;
    double gimbal_pitch_deg = 0;
    double gimbal_roll_deg = 0;
    double flight_yaw_deg = 0;
    double flight_pitch_deg = 0;
    double flight_roll_deg = 0;
};

// Finds `key="<number>"` within [data, data+len) and parses the number. Returns false (leaves
// out untouched) if the key isn't present -- callers treat that as "field missing", not an
// error, since not every DJI firmware/model version populates every field.
bool FindXmpDouble(
        const char* data,
        size_t len,
        const char* key,
        double& out)
{
    size_t key_len = std::strlen(key);
    for (size_t i = 0; i + key_len + 2 <= len; ++i)
    {
        if (data[i] != key[0] || 0 != std::memcmp(data + i, key, key_len))
        {
            continue;
        }
        size_t j = i + key_len;
        if (j >= len || data[j] != '=' || j + 1 >= len || data[j + 1] != '"')
        {
            continue;
        }
        j += 2;
        size_t start = j;
        while (j < len && data[j] != '"')
        {
            ++j;
        }
        if (j >= len)
        {
            return false;
        }
        try
        {
            out = std::stod(std::string(data + start, j - start));
            return true;
        }
        catch (...)
        {
            return false;
        }
    }
    return false;
}

// Reads just enough of the front of the JPEG (DJI's XMP packet is always near the file start,
// well before the actual image scan data) to find the `<x:xmpmeta>...</x:xmpmeta>` packet and
// pull out GPS + gimbal + flight attitude. Returns DjiXmpPose{valid=false} (not an exception)
// on any read/parse failure -- a missing/malformed XMP packet on one image should skip pose
// data for that one frame, not abort the whole publish.
DjiXmpPose ReadDjiXmpPose(
        const fs::path& jpeg_path)
{
    DjiXmpPose pose;

    std::ifstream f(jpeg_path, std::ios::binary);
    if (!f.is_open())
    {
        return pose;
    }
    const size_t kReadBudget = 256 * 1024; // DJI XMP packets observed well under 2KB, generous margin
    std::vector<char> buf(kReadBudget);
    f.read(buf.data(), (std::streamsize)buf.size());
    size_t read_bytes = (size_t)f.gcount();

    const char* begin_marker = "<x:xmpmeta";
    const char* end_marker = "</x:xmpmeta>";
    const char* begin = nullptr;
    for (size_t i = 0; i + std::strlen(begin_marker) <= read_bytes; ++i)
    {
        if (0 == std::memcmp(buf.data() + i, begin_marker, std::strlen(begin_marker)))
        {
            begin = buf.data() + i;
            break;
        }
    }
    if (!begin)
    {
        return pose; // no XMP found in the budget -- valid stays false
    }
    const char* search_end = buf.data() + read_bytes;
    const char* end = nullptr;
    for (const char* p = begin; p + std::strlen(end_marker) <= search_end; ++p)
    {
        if (0 == std::memcmp(p, end_marker, std::strlen(end_marker)))
        {
            end = p + std::strlen(end_marker);
            break;
        }
    }
    if (!end)
    {
        return pose;
    }

    const char* xmp = begin;
    size_t xmp_len = (size_t)(end - begin);

    bool have_lat = FindXmpDouble(xmp, xmp_len, "drone-dji:GpsLatitude", pose.gps_latitude_deg);
    // DJI's own XMP schema has a typo in this key ("Longtitude", missing the 'i') --
    // confirmed by direct inspection, not assumed; this is the real field name DJI writes.
    bool have_lon = FindXmpDouble(xmp, xmp_len, "drone-dji:GpsLongtitude", pose.gps_longitude_deg);
    bool have_alt = FindXmpDouble(xmp, xmp_len, "drone-dji:AbsoluteAltitude", pose.gps_altitude_m);
    FindXmpDouble(xmp, xmp_len, "drone-dji:GimbalYawDegree", pose.gimbal_yaw_deg);
    FindXmpDouble(xmp, xmp_len, "drone-dji:GimbalPitchDegree", pose.gimbal_pitch_deg);
    FindXmpDouble(xmp, xmp_len, "drone-dji:GimbalRollDegree", pose.gimbal_roll_deg);
    FindXmpDouble(xmp, xmp_len, "drone-dji:FlightYawDegree", pose.flight_yaw_deg);
    FindXmpDouble(xmp, xmp_len, "drone-dji:FlightPitchDegree", pose.flight_pitch_deg);
    FindXmpDouble(xmp, xmp_len, "drone-dji:FlightRollDegree", pose.flight_roll_deg);

    pose.valid = have_lat && have_lon && have_alt;
    return pose;
}

// Intrinsic Z(yaw)-Y(pitch)-X(roll) Euler -> quaternion, matching DJI's own gimbal angle
// convention (yaw: 0=North/+clockwise, pitch: 0=horizontal/-90=straight down, roll: 0=level).
// Precision here only needs to be good enough for a coarse frame-to-frame consistency check
// (see FacadeStitcher's pose-gate comment) -- not a survey-grade attitude solution.
void EulerDegToQuaternion(
        double yaw_deg,
        double pitch_deg,
        double roll_deg,
        double& qx,
        double& qy,
        double& qz,
        double& qw)
{
    const double d2r = 3.14159265358979323846 / 180.0;
    double yaw = yaw_deg * d2r, pitch = pitch_deg * d2r, roll = roll_deg * d2r;
    double cy = std::cos(yaw * 0.5), sy = std::sin(yaw * 0.5);
    double cp = std::cos(pitch * 0.5), sp = std::sin(pitch * 0.5);
    double cr = std::cos(roll * 0.5), sr = std::sin(roll * 0.5);
    qw = cr * cp * cy + sr * sp * sy;
    qx = sr * cp * cy - cr * sp * sy;
    qy = cr * sp * cy + sr * cp * sy;
    qz = cr * cp * sy - sr * sp * cy;
}

// Equirectangular local-ENU approximation relative to a caller-supplied origin -- accurate
// enough over a single facade scan's small footprint (tens of meters), not intended for
// anything beyond the relative-displacement pose-consistency check this feeds.
void GpsToLocalEnuMeters(
        double lat_deg,
        double lon_deg,
        double alt_m,
        double origin_lat_deg,
        double origin_lon_deg,
        double origin_alt_m,
        double& east_m,
        double& north_m,
        double& up_m)
{
    const double d2r = 3.14159265358979323846 / 180.0;
    const double kEarthRadiusM = 6378137.0;
    double dlat = (lat_deg - origin_lat_deg) * d2r;
    double dlon = (lon_deg - origin_lon_deg) * d2r;
    north_m = dlat * kEarthRadiusM;
    east_m = dlon * kEarthRadiusM * std::cos(origin_lat_deg * d2r);
    up_m = alt_m - origin_alt_m;
}

std::string WideToCodepage(
        const std::wstring& w,
        UINT codepage)
{
    if (w.empty())
    {
        return std::string();
    }
    int bytes = WideCharToMultiByte(codepage, 0, w.c_str(), (int)w.size(), nullptr, 0, nullptr, nullptr);
    std::string out(bytes, '\0');
    WideCharToMultiByte(codepage, 0, w.c_str(), (int)w.size(), out.data(), bytes, nullptr, nullptr);
    return out;
}

std::string WideToUtf8(
        const std::wstring& w)
{
    return WideToCodepage(w, CP_UTF8);
}

// RunRtmpPublish (RtmpPublisher.cpp) opens its flv_path argument via std::ifstream(const
// std::string&), which MSVC's runtime interprets using the process's ANSI codepage (matching
// how SmokeTest.cpp's existing --rtmp-publish mode already hands it a raw (ANSI) argv string)
// -- NOT UTF-8. Only the --keep-flv path below embeds a possibly-non-ASCII facade folder name
// into that path, so only that one call site needs this (ANSI, not UTF-8) conversion.
std::string WideToAnsi(
        const std::wstring& w)
{
    return WideToCodepage(w, CP_ACP);
}

// scan_images() (src/capture/image_catalog.py) ported: sorted *.jpg/*.jpeg under facade_dir,
// excluding anything under facade_dir/output (that subfolder holds this project's own prior
// output -- feeding it back in as source would be wrong, same reasoning as the Python version).
std::vector<fs::path> ScanFacadeJpegs(
        const fs::path& facade_dir)
{
    std::vector<fs::path> images;
    fs::path output_dir = facade_dir / L"output";
    std::error_code ec;

    for (const auto& entry : fs::recursive_directory_iterator(facade_dir, fs::directory_options::skip_permission_denied, ec))
    {
        if (!entry.is_regular_file(ec))
        {
            continue;
        }
        std::wstring ext = entry.path().extension().wstring();
        std::transform(ext.begin(), ext.end(), ext.begin(), ::towlower);
        if (ext != L".jpg" && ext != L".jpeg")
        {
            continue;
        }

        bool under_output = false;
        for (fs::path parent = entry.path().parent_path(); !parent.empty(); parent = parent.parent_path())
        {
            if (fs::equivalent(parent, output_dir, ec))
            {
                under_output = true;
                break;
            }
            if (parent == parent.parent_path())
            {
                break; // reached filesystem root
            }
        }
        if (under_output)
        {
            continue;
        }

        images.push_back(entry.path());
    }

    std::sort(images.begin(), images.end());
    return images;
}

// Locates ffmpeg next to this project the same way previewer/tools/README.md documents
// (tools/ffmpeg/bin/ffmpeg.exe, vendored there 2026-08-12 to keep it off C: -- see that
// README's "ffmpeg" section) -- computed relative to this exe's own module path, falling back
// to plain "ffmpeg.exe" (PATH search, via CreateProcess's own search behavior) if that vendored
// copy isn't present.
std::wstring FindFfmpegExe()
{
    wchar_t module_path[MAX_PATH];
    DWORD n = GetModuleFileNameW(nullptr, module_path, MAX_PATH);
    if (n == 0 || n == MAX_PATH)
    {
        return L"ffmpeg.exe";
    }

    // module_path is .../previewer/FacadeDdsBridge/build/<Config>/FacadeDdsBridgeSmokeTest.exe
    // -> up 3 levels reaches .../previewer, then down into tools/libffmpeg/bin/ffmpeg.exe.
    //
    // [YYIL] 2026-08-13: this used to point at a SEPARATE static "full_build" ffmpeg
    // (tools/ffmpeg/, ~650MB, CLI-only, GPL, libx264) purely for this JPEG->FLV test
    // encoding step -- redundant with tools/libffmpeg/ (the shared/dev build already
    // required for VideoDecoder's H.264 *decode* link, see tools/Get-FfmpegDevModule.ps1),
    // whose bin/ also happens to ship a full ffmpeg.exe. Consolidated to that one install;
    // tools/ffmpeg/ is no longer used by anything and can be deleted. Encoder had to change
    // from libx264 to libopenh264 as part of this (see EncodeConcatToFlv's own comment) since
    // tools/libffmpeg/ is deliberately the LGPL-shared build (no libx264, which is GPL).
    fs::path exe_dir = fs::path(module_path).parent_path();
    fs::path candidate = exe_dir / L".." / L".." / L".." / L"tools" / L"libffmpeg" / L"bin" / L"ffmpeg.exe";
    std::error_code ec;
    if (fs::exists(candidate, ec))
    {
        return fs::weakly_canonical(candidate, ec).wstring();
    }
    return L"ffmpeg.exe";
}

// Standard Windows argv quoting (each arg individually): pass through unquoted if it has no
// space/tab/quote, otherwise wrap in quotes doubling embedded quotes and escaping runs of
// backslashes that immediately precede a quote -- the well-known algorithm every CreateProcess
// caller needs, since the OS itself does no argv splitting (the child re-parses lpCommandLine).
std::wstring QuoteArg(
        const std::wstring& arg)
{
    if (!arg.empty() && arg.find_first_of(L" \t\"") == std::wstring::npos)
    {
        return arg;
    }

    std::wstring out = L"\"";
    size_t i = 0;
    while (i < arg.size())
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
        }
        else
        {
            out.append(backslashes, L'\\');
            out += arg[i];
        }
        ++i;
    }
    out += L'"';
    return out;
}

bool RunProcess(
        const std::wstring& exe,
        const std::vector<std::wstring>& args,
        std::atomic<bool>* cancel = nullptr)
{
    std::wstring cmdline = QuoteArg(exe);
    for (const auto& a : args)
    {
        cmdline += L' ';
        cmdline += QuoteArg(a);
    }

    printf("running: %s\n", WideToUtf8(cmdline).c_str());

    STARTUPINFOW si{};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi{};

    std::vector<wchar_t> mutable_cmdline(cmdline.begin(), cmdline.end());
    mutable_cmdline.push_back(L'\0');

    BOOL ok = CreateProcessW(nullptr, mutable_cmdline.data(), nullptr, nullptr, FALSE,
                              0, nullptr, nullptr, &si, &pi);
    if (!ok)
    {
        fprintf(stderr, "CreateProcess failed for '%s' (error %lu)\n", WideToUtf8(exe).c_str(), GetLastError());
        return false;
    }

    // Polled in short slices (rather than a single INFINITE wait) so PublishUi.cpp's "정지"
    // button can actually interrupt an in-progress ffmpeg encode -- there is no cooperative
    // way to ask ffmpeg to stop mid-encode, so cancellation here means TerminateProcess.
    bool canceled = false;
    for (;;)
    {
        DWORD wait_result = WaitForSingleObject(pi.hProcess, 200);
        if (wait_result == WAIT_OBJECT_0)
        {
            break;
        }
        if (cancel && cancel->load())
        {
            printf("canceled -- terminating '%s'\n", WideToUtf8(exe).c_str());
            TerminateProcess(pi.hProcess, 1);
            WaitForSingleObject(pi.hProcess, INFINITE);
            canceled = true;
            break;
        }
    }

    DWORD exit_code = 1;
    GetExitCodeProcess(pi.hProcess, &exit_code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    return !canceled && exit_code == 0;
}

bool WriteConcatList(
        const std::vector<fs::path>& images,
        double fps,
        const fs::path& list_path)
{
    std::ofstream out(list_path, std::ios::binary);
    if (!out.is_open())
    {
        return false;
    }

    // Same "repeat the last file with no duration" workaround as the deleted Python version --
    // ffmpeg's concat demuxer only honors 'duration' as "how long the *preceding* file shows",
    // so the final file's duration is otherwise silently dropped.
    char duration_buf[32];
    std::snprintf(duration_buf, sizeof(duration_buf), "%.6f", 1.0 / fps);

    auto write_file_line = [&out](const fs::path& p)
    {
        std::string utf8_path = WideToUtf8(fs::absolute(p).wstring());
        std::string escaped;
        escaped.reserve(utf8_path.size());
        for (char c : utf8_path)
        {
            if (c == '\'')
            {
                escaped += "'\\''";
            }
            else
            {
                escaped += c;
            }
        }
        out << "file '" << escaped << "'\n";
    };

    for (const auto& img : images)
    {
        write_file_line(img);
        out << "duration " << duration_buf << "\n";
    }
    if (!images.empty())
    {
        write_file_line(images.back());
    }
    return true;
}

} // namespace

bool PublishFacadeJpegsViaRtmp(
        const std::wstring& facade_dir,
        const std::string& rtmp_url,
        double fps,
        bool keep_flv,
        std::atomic<bool>* cancel)
{
    fs::path dir(facade_dir);
    std::error_code ec;
    if (!fs::is_directory(dir, ec))
    {
        fprintf(stderr, "not a folder: %s\n", WideToUtf8(facade_dir).c_str());
        return false;
    }
    if (cancel && cancel->load())
    {
        printf("canceled before starting\n");
        return false;
    }

    std::vector<fs::path> images = ScanFacadeJpegs(dir);
    if (images.empty())
    {
        fprintf(stderr, "no JPEGs found under %s\n", WideToUtf8(facade_dir).c_str());
        return false;
    }
    printf("found %zu JPEG(s) under %s\n", images.size(), WideToUtf8(facade_dir).c_str());

    wchar_t temp_path_buf[MAX_PATH];
    GetTempPathW(MAX_PATH, temp_path_buf);
    fs::path temp_dir = fs::path(temp_path_buf) / L"publish_facade_rtmp";
    fs::create_directories(temp_dir, ec);

    fs::path list_path = temp_dir / L"concat_list.txt";
    if (!WriteConcatList(images, fps, list_path))
    {
        fprintf(stderr, "failed to write concat list '%s'\n", WideToUtf8(list_path.wstring()).c_str());
        return false;
    }

    fs::path out_flv = keep_flv
        ? (dir / (dir.filename().wstring() + L"_rtmp_test.flv"))
        : (temp_dir / L"facade_publish.flv"); // ASCII-only temp name -- RunRtmpPublish takes a
                                               // narrow std::string, keep this one path free of
                                               // any non-ASCII the facade folder name might have

    // [YYIL] 2026-08-13: cap the encoded resolution at 1080px tall (only downscales, never
    // upscales -- 'min(1080,ih)') instead of publishing at the source JPEGs' full DJI
    // resolution (5280x3956). Root-caused a real corruption bug via this: publishing at full
    // res produced a single ~multi-MB I-frame chunked into thousands of tiny 376-byte
    // VideoTsPacket DDS samples sent in one uninterrupted burst; under this project's
    // deliberately-BEST_EFFORT DDS QoS (no retransmission, no FEC), that burst reliably lost
    // enough packets mid-frame to corrupt the H.264 decode (visually: clean vertical
    // streaking -- classic left-to-right intra-prediction cascade from a lost macroblock,
    // confirmed by dumping the raw decoded frame straight from VideoDecoder.cpp, bypassing
    // the C#/OpenCvSharp side entirely, and seeing the identical corruption there already).
    // 1080p matches this previewer's own documented design target (see project memory:
    // project-dds-previewer-design, "Resolution: 1920x1080, downsampling is fine") -- this
    // was never meant to carry full source-photo resolution end to end; the earlier
    // full-resolution test runs were an unintentional stress test, not the intended usage.
    //
    // Encoder is libopenh264, not libx264: FindFfmpegExe() now points at tools/libffmpeg/
    // (the LGPL-shared build already required for VideoDecoder's decode-side link), which
    // has libx264 (GPL) compiled out on purpose -- keeping this whole dependency LGPL avoids
    // the licensing implications of linking/shipping a GPL codec. Verified this doesn't
    // change anything downstream: libopenh264's output is standard-compliant Annex-B H.264,
    // parses/decodes through RtmpVideoBridge's AVCDecoderConfigurationRecord handling and
    // VideoDecoder's avcodec_receive_frame identically to what libx264 produced.
    std::wstring ffmpeg_exe = FindFfmpegExe();
    bool ffmpeg_ok = RunProcess(ffmpeg_exe, {
        L"-y",
        L"-f", L"concat", L"-safe", L"0", L"-i", list_path.wstring(),
        L"-r", std::to_wstring(fps),
        L"-vf", L"scale=-2:'min(1080,ih)'",
        L"-c:v", L"libopenh264", L"-pix_fmt", L"yuv420p",
        L"-an",
        L"-f", L"flv", out_flv.wstring(),
    }, cancel);
    fs::remove(list_path, ec);
    if (!ffmpeg_ok)
    {
        fprintf(stderr, cancel && cancel->load() ? "canceled during ffmpeg encode\n" : "ffmpeg failed\n");
        return false;
    }
    printf("encoded: %s (%llu bytes)\n", WideToUtf8(out_flv.wstring()).c_str(),
           (unsigned long long)fs::file_size(out_flv, ec));

    bool published = RunRtmpPublish(rtmp_url, WideToAnsi(out_flv.wstring()), cancel);

    if (!keep_flv)
    {
        fs::remove(out_flv, ec);
    }

    return published;
}
