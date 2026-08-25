#include "DdsQosHelpers.h"

#include <fastdds/rtps/transport/UDPv4TransportDescriptor.hpp>
#include <fastdds/utils/IPLocator.hpp>

#include <cstdio>
#include <cstdlib>
#include <memory>
#include <sstream>
#include <string>

using namespace eprosima::fastdds::dds;

// See this function's doc history in DdsFrameSubscriber.cpp's original comment (moved here
// verbatim, only parameterized with log_prefix so two independent participants in the same
// process -- DdsFrameSubscriber's video/pose subscriber and FacadeStorageStatus's
// Feedback/Result/CancelRequest client -- print distinguishable diagnostics):
//
// Same UDPv4-only transport override as DdsVideoSubscriber (NDRONE_MULTI_VIEWER) -- Fast-DDS's
// builtin SHM transport hangs indefinitely inside create_participant() on this machine.
// FACADE_DDS_INTERFACE_WHITELIST (comma-separated IPs) restricts the transport to real LAN
// adapters on machines with virtual NICs that would otherwise get offered as unreachable
// candidate unicast locators. FACADE_DDS_INITIAL_PEER (single IP) makes this participant also
// send its own SPDP announcement via unicast directly to the peer, sidestepping multicast
// entirely -- needed on at least one real network where cross-machine multicast discovery was
// asymmetric (see previewer/CLAUDE.local.md's 2026-08-12 DDS cross-machine debugging history).
DomainParticipantQos MakeUdpOnlyQos(
        int domain_id,
        const char* initial_peer_host,
        int initial_peer_port,
        const char* local_interface_ip,
        const char* log_prefix)
{
    DomainParticipantQos qos = PARTICIPANT_QOS_DEFAULT;
    qos.transport().use_builtin_transports = false;
    auto udp = std::make_shared<eprosima::fastdds::rtps::UDPv4TransportDescriptor>();

    // Larger-than-OS-default receive buffer -- matches the RtmpVideoBridge writer-side fix (see
    // that project's MakeParticipantQos() comment): bursty sends under BEST_EFFORT QoS can
    // overflow default UDP buffers. Cheap insurance here too even though these 3 topics are
    // low-rate, small messages.
    udp->sendBufferSize = 4 * 1024 * 1024;
    udp->receiveBufferSize = 4 * 1024 * 1024;

    std::string whitelist_str = (local_interface_ip && *local_interface_ip)
            ? local_interface_ip
            : (std::getenv("FACADE_DDS_INTERFACE_WHITELIST") ? std::getenv("FACADE_DDS_INTERFACE_WHITELIST") : "");
    if (!whitelist_str.empty())
    {
        std::stringstream ss(whitelist_str);
        std::string ip;
        while (std::getline(ss, ip, ','))
        {
            if (!ip.empty())
            {
                udp->interfaceWhiteList.push_back(ip);
                printf("%s: restricting UDPv4 transport to interface '%s'\n", log_prefix, ip.c_str());
            }
        }
    }

    qos.transport().user_transports.push_back(udp);

    std::string peer_str = (initial_peer_host && *initial_peer_host)
            ? initial_peer_host
            : (std::getenv("FACADE_DDS_INITIAL_PEER") ? std::getenv("FACADE_DDS_INITIAL_PEER") : "");
    if (!peer_str.empty())
    {
        eprosima::fastdds::rtps::Locator_t peer_locator;
        peer_locator.kind = LOCATOR_KIND_UDPv4;
        peer_locator.port = initial_peer_port > 0
                ? static_cast<uint16_t>(initial_peer_port)
                : static_cast<uint16_t>(7400 + 250 * domain_id + 10);
        eprosima::fastdds::rtps::IPLocator::setIPv4(peer_locator, peer_str);
        qos.wire_protocol().builtin.initialPeersList.push_back(peer_locator);
        printf("%s: adding initial discovery peer '%s:%u'\n", log_prefix, peer_str.c_str(), peer_locator.port);
    }

    return qos;
}
