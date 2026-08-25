#include "RtmpPublisher.h"

#include <winsock2.h>
#include <ws2tcpip.h>

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <string>
#include <thread>
#include <vector>

#include "rtmp-client.h"
#include "rtmp-internal.h" // RTMP_STATE_START -- shipped in include/, not source-private

namespace {

// ---- FLV file -> tag list (fed to librtmp tag-by-tag, same shape rtmp-publish-test.cpp
// uses via flv_reader_read: tag payload IS the "FLV VideoTagHeader + AVCVIDEOPACKET" body
// rtmp_client_push_video expects, verbatim, no re-parsing needed here). ----

struct FlvTag
{
    int type; // 8=audio, 9=video, 18=script (onMetaData)
    uint32_t timestamp_ms;
    std::vector<uint8_t> payload;
};

std::vector<FlvTag> ReadFlvTags(
        const std::string& path,
        std::string& error)
{
    std::vector<FlvTag> tags;
    std::ifstream f(path, std::ios::binary);
    if (!f.is_open())
    {
        error = "cannot open '" + path + "'";
        return tags;
    }

    unsigned char header[9];
    f.read((char*)header, sizeof(header));
    if (f.gcount() != (std::streamsize)sizeof(header) || header[0] != 'F' || header[1] != 'L' || header[2] != 'V')
    {
        error = "'" + path + "' is not a valid FLV file (bad signature)";
        return tags;
    }
    uint32_t data_offset = ((uint32_t)header[5] << 24) | ((uint32_t)header[6] << 16)
                          | ((uint32_t)header[7] << 8) | (uint32_t)header[8];
    f.seekg(data_offset, std::ios::beg);

    while (f.good())
    {
        unsigned char prev_tag_size[4];
        f.read((char*)prev_tag_size, sizeof(prev_tag_size));
        if (f.gcount() != (std::streamsize)sizeof(prev_tag_size))
        {
            break; // clean EOF between tags
        }

        unsigned char tag_header[11];
        f.read((char*)tag_header, sizeof(tag_header));
        if (f.gcount() != (std::streamsize)sizeof(tag_header))
        {
            break;
        }

        int type = tag_header[0];
        uint32_t data_size = ((uint32_t)tag_header[1] << 16) | ((uint32_t)tag_header[2] << 8) | (uint32_t)tag_header[3];
        uint32_t ts24 = ((uint32_t)tag_header[4] << 16) | ((uint32_t)tag_header[5] << 8) | (uint32_t)tag_header[6];
        uint32_t ts_ext = tag_header[7];
        uint32_t timestamp_ms = (ts_ext << 24) | ts24;

        std::vector<uint8_t> payload;
        if (data_size > 0)
        {
            payload.resize(data_size);
            f.read((char*)payload.data(), data_size);
            if ((uint32_t)f.gcount() != data_size)
            {
                break; // truncated file
            }
        }

        if (type == 8 || type == 9 || type == 18) // audio / video / script
        {
            FlvTag tag;
            tag.type = type;
            tag.timestamp_ms = timestamp_ms;
            tag.payload = std::move(payload);
            tags.push_back(std::move(tag));
        }
        // any other tag type: bytes already consumed above, just skip it
    }

    return tags;
}

// ---- Winsock plumbing -- this is the first raw-socket code in previewer/FacadeDdsBridge
// (everything else is FastDDS), so unlike RtspClientPublisher (POSIX, DDS-Router/Linux) this
// is written fresh against Winsock2, same overall connect/handshake/push shape. ----

struct PublishCtx
{
    SOCKET sock = INVALID_SOCKET;
};

bool SendAll(
        SOCKET s,
        const void* data,
        size_t len)
{
    const char* p = (const char*)data;
    size_t sent = 0;
    while (sent < len)
    {
        int n = send(s, p + sent, (int)(len - sent), 0);
        if (n == SOCKET_ERROR || n <= 0)
        {
            return false;
        }
        sent += (size_t)n;
    }
    return true;
}

int ClientSend(
        void* param,
        const void* header,
        size_t len,
        const void* payload,
        size_t bytes)
{
    PublishCtx* ctx = (PublishCtx*)param;
    if (len > 0 && !SendAll(ctx->sock, header, len))
    {
        return -1;
    }
    if (bytes > 0 && !SendAll(ctx->sock, payload, bytes))
    {
        return -1;
    }
    return (int)(len + bytes);
}

bool ParseRtmpUrl(
        const std::string& url,
        std::string& host,
        unsigned short& port,
        std::string& app,
        std::string& stream)
{
    const std::string kPrefix = "rtmp://";
    if (url.compare(0, kPrefix.size(), kPrefix) != 0)
    {
        return false;
    }
    size_t pos = kPrefix.size();
    size_t slash1 = url.find('/', pos);
    if (slash1 == std::string::npos)
    {
        return false;
    }
    std::string host_port = url.substr(pos, slash1 - pos);
    size_t colon = host_port.find(':');
    if (colon == std::string::npos)
    {
        host = host_port;
        port = 1935;
    }
    else
    {
        host = host_port.substr(0, colon);
        port = (unsigned short)std::atoi(host_port.substr(colon + 1).c_str());
    }

    std::string rest = url.substr(slash1 + 1); // "app/stream"
    size_t slash2 = rest.find('/');
    if (slash2 == std::string::npos)
    {
        return false;
    }
    app = rest.substr(0, slash2);
    stream = rest.substr(slash2 + 1);
    return !host.empty() && port != 0 && !app.empty() && !stream.empty();
}

SOCKET ConnectTcp(
        const std::string& host,
        unsigned short port)
{
    struct addrinfo hints;
    std::memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;

    char port_str[16];
    std::snprintf(port_str, sizeof(port_str), "%u", port);

    struct addrinfo* res = nullptr;
    if (getaddrinfo(host.c_str(), port_str, &hints, &res) != 0 || !res)
    {
        fprintf(stderr, "[rtmp-publish] failed to resolve host '%s'\n", host.c_str());
        return INVALID_SOCKET;
    }

    SOCKET s = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (s == INVALID_SOCKET)
    {
        freeaddrinfo(res);
        return INVALID_SOCKET;
    }
    int rc = connect(s, res->ai_addr, (int)res->ai_addrlen);
    freeaddrinfo(res);
    if (rc == SOCKET_ERROR)
    {
        fprintf(stderr, "[rtmp-publish] connect to %s:%u failed\n", host.c_str(), port);
        closesocket(s);
        return INVALID_SOCKET;
    }

    BOOL nodelay = TRUE;
    setsockopt(s, IPPROTO_TCP, TCP_NODELAY, (const char*)&nodelay, sizeof(nodelay));
    return s;
}

// Feeds any bytes currently waiting on the socket into rtmp_client_input -- needed both
// during the pre-START handshake pump and periodically while streaming (so acknowledgement
// messages/onStatus replies from the server keep getting processed, matching how
// RtspClientPublisher drains its control socket during stream_frames()).
bool DrainSocket(
        SOCKET sock,
        rtmp_client_t* rtmp,
        int timeout_ms,
        bool& closed)
{
    closed = false;
    fd_set readfds;
    FD_ZERO(&readfds);
    FD_SET(sock, &readfds);
    timeval tv;
    tv.tv_sec = timeout_ms / 1000;
    tv.tv_usec = (timeout_ms % 1000) * 1000;

    int r = select(0, &readfds, nullptr, nullptr, &tv);
    if (r <= 0)
    {
        return true; // timeout or would-block -- not an error
    }

    static thread_local unsigned char buf[64 * 1024];
    int n = recv(sock, (char*)buf, sizeof(buf), 0);
    if (n <= 0)
    {
        closed = true;
        return false;
    }
    return 0 == rtmp_client_input(rtmp, buf, (size_t)n);
}

} // namespace

bool RunRtmpPublish(
        const std::string& url,
        const std::string& flv_path,
        std::atomic<bool>* cancel)
{
    std::string host;
    unsigned short port = 0;
    std::string app;
    std::string stream;
    if (!ParseRtmpUrl(url, host, port, app, stream))
    {
        fprintf(stderr, "[rtmp-publish] invalid RTMP URL '%s' -- expected rtmp://host[:port]/app/stream\n", url.c_str());
        return false;
    }

    std::string parse_error;
    std::vector<FlvTag> tags = ReadFlvTags(flv_path, parse_error);
    if (tags.empty())
    {
        fprintf(stderr, "[rtmp-publish] no usable FLV tags read from '%s': %s\n",
                flv_path.c_str(), parse_error.empty() ? "(empty)" : parse_error.c_str());
        return false;
    }
    printf("[rtmp-publish] parsed %zu FLV tag(s) from '%s'\n", tags.size(), flv_path.c_str());

    WSADATA wsa_data;
    if (WSAStartup(MAKEWORD(2, 2), &wsa_data) != 0)
    {
        fprintf(stderr, "[rtmp-publish] WSAStartup failed\n");
        return false;
    }

    bool ok = false;
    {
        PublishCtx ctx;
        ctx.sock = ConnectTcp(host, port);
        if (ctx.sock == INVALID_SOCKET)
        {
            WSACleanup();
            return false;
        }

        std::string tcurl = "rtmp://" + host + ":" + std::to_string(port) + "/" + app;

        rtmp_client_handler_t handler;
        std::memset(&handler, 0, sizeof(handler));
        handler.send = ClientSend;

        rtmp_client_t* rtmp = rtmp_client_create(app.c_str(), stream.c_str(), tcurl.c_str(), &ctx, &handler);
        if (!rtmp)
        {
            fprintf(stderr, "[rtmp-publish] rtmp_client_create failed\n");
            closesocket(ctx.sock);
            WSACleanup();
            return false;
        }

        printf("[rtmp-publish] connecting to %s (app=%s stream=%s)...\n", url.c_str(), app.c_str(), stream.c_str());
        if (0 != rtmp_client_start(rtmp, /*publish=*/0))
        {
            fprintf(stderr, "[rtmp-publish] rtmp_client_start (handshake C0/C1 send) failed\n");
            rtmp_client_destroy(rtmp);
            closesocket(ctx.sock);
            WSACleanup();
            return false;
        }

        // Pump handshake + connect/createStream/publish command exchange until the state
        // machine reaches RTMP_STATE_START ("push video/audio", per rtmp-client.h).
        auto handshake_start = std::chrono::steady_clock::now();
        constexpr int kHandshakeTimeoutSec = 10;
        bool failed = false;
        while (rtmp_client_getstate(rtmp) != RTMP_STATE_START)
        {
            if (std::chrono::duration_cast<std::chrono::seconds>(
                        std::chrono::steady_clock::now() - handshake_start).count() >= kHandshakeTimeoutSec)
            {
                fprintf(stderr, "[rtmp-publish] handshake/publish handshake did not reach START within %ds\n",
                        kHandshakeTimeoutSec);
                failed = true;
                break;
            }
            bool closed = false;
            if (!DrainSocket(ctx.sock, rtmp, 500, closed) || closed)
            {
                fprintf(stderr, "[rtmp-publish] connection closed/errored during handshake\n");
                failed = true;
                break;
            }
        }

        if (!failed)
        {
            printf("[rtmp-publish] publish accepted -- streaming %zu tag(s)...\n", tags.size());
            auto start = std::chrono::steady_clock::now();
            for (const FlvTag& tag : tags)
            {
                if (cancel && cancel->load())
                {
                    printf("[rtmp-publish] canceled -- stopping mid-stream\n");
                    failed = true;
                    break;
                }

                auto target = start + std::chrono::milliseconds(tag.timestamp_ms);
                auto now = std::chrono::steady_clock::now();
                if (target > now)
                {
                    std::this_thread::sleep_for(target - now);
                }

                int r = 0;
                if (tag.type == 9)
                {
                    r = rtmp_client_push_video(rtmp, tag.payload.data(), tag.payload.size(), tag.timestamp_ms);
                }
                else if (tag.type == 8)
                {
                    r = rtmp_client_push_audio(rtmp, tag.payload.data(), tag.payload.size(), tag.timestamp_ms);
                }
                else // 18: script/onMetaData
                {
                    r = rtmp_client_push_script(rtmp, tag.payload.data(), tag.payload.size(), tag.timestamp_ms);
                }
                if (0 != r)
                {
                    fprintf(stderr, "[rtmp-publish] push failed (type=%d, timestamp=%u)\n", tag.type, tag.timestamp_ms);
                    failed = true;
                    break;
                }

                bool closed = false;
                DrainSocket(ctx.sock, rtmp, 0, closed);
                if (closed)
                {
                    fprintf(stderr, "[rtmp-publish] connection closed by server mid-stream\n");
                    failed = true;
                    break;
                }
            }
        }

        if (!failed)
        {
            printf("[rtmp-publish] done -- all %zu tag(s) sent, stopping...\n", tags.size());
            rtmp_client_stop(rtmp);
            bool closed = false;
            DrainSocket(ctx.sock, rtmp, 500, closed); // best-effort: let FCUnpublish/deleteStream flush
            ok = true;
        }

        rtmp_client_destroy(rtmp);
        closesocket(ctx.sock);
    }

    WSACleanup();
    return ok;
}
