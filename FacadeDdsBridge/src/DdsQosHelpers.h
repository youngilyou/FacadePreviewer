// Shared UDPv4-only DomainParticipantQos builder -- extracted from DdsFrameSubscriber.cpp so
// FacadeStorageStatus.cpp (operator-visibility Feedback/Result/CancelRequest client, a second,
// independent DDS participant in this same DLL) gets the exact same cross-machine discovery
// fix instead of a second, easily-divergent copy of a block with a documented history of subtle
// bugs (interface whitelist handling, initial-peer port formula -- see MakeUdpOnlyQos's own
// comment in the .cpp for the full "다른 컴퓨터" discovery debugging story this came from).
#pragma once

#include <fastdds/dds/domain/qos/DomainParticipantQos.hpp>

// initial_peer_host/local_interface_ip: pass nullptr/"" to fall back to the
// FACADE_DDS_INITIAL_PEER/FACADE_DDS_INTERFACE_WHITELIST env vars. initial_peer_port <= 0 means
// "use the standard participant-index-0 metatraffic-unicast port formula" (7400 + 250*domain_id + 10).
// log_prefix: prefixes the printf lines this prints when it applies a whitelist/initial peer, so
// output stays distinguishable when more than one participant in this process uses it.
eprosima::fastdds::dds::DomainParticipantQos MakeUdpOnlyQos(
        int domain_id,
        const char* initial_peer_host,
        int initial_peer_port,
        const char* local_interface_ip,
        const char* log_prefix);
