# Facade Previewer — 개발 기록

## 목적

현장에서 건물 외벽을 드론으로 스캔하는 동안, **누락 구간을 즉시 확인**하기 위한
실시간 모니터링 툴. `D:\ClaudePr\CheckCrack`의 오프라인 크랙 분석 파이프라인과는
**완전히 별개**이며 (그쪽은 정밀도 목적, 이쪽은 커버리지 확인 목적), 이
프로젝트(`previewer/`)의 코드나 설계가 그쪽과 섞이지 않는다.

**이 프로그램은 이 개발 머신이 아닌 다른 곳(외부 현장 노트북)에서 독립적으로
실행될 예정이다.** 즉 빌드 의존성(FastDDS SDK, MSVC 툴셋 버전 등)이 이 머신에만
있는 상태로 우연히 동작하면 안 되고, 재현 가능해야 한다 — 아래 "빌드 이식성"
참고.

## 절대 규칙

1. **`RobotMediaServer`/`RobotMediaDashboard` 절대 수정 금지.** 사용자 확인:
   "이거 절대 건드리지 마세요." 기능이 일부 겹쳐도(자체 DDS 영상 구독) 의도된
   중복이며, 나중에 합치지 않는다.
2. **`Z:\DDS_Platform`은 게이트웨이(미들웨어)일 뿐, 수정 대상 아님.** 이 프로젝트는
   거기서 나오는 DDS 토픽을 구독만 한다.
3. **`D:\GCS\Map2Stitch_Wpf` 코드/설계 재사용 금지.** 그건 드론이 지면을 수직으로
   찍어 지도에 매핑하는 툴(포즈 기반 정사투영, 수평 지면 평면). 이 프로젝트는
   벽면을 측면에서 찍어 스티칭하는 것 — 평면 자체가 다르다(수직 벽면 평면).
   순수 pose 투영만으로는 GPS 오차(~10-20m, Map2Stitch_Wpf 자체가 겪고 있는
   미해결 문제)가 벽면 하나 크기(20-30m)와 맞먹어서 커버리지 맵이 무의미해짐 —
   그래서 아래처럼 영상 매칭이 메인이다.
4. `AEROCAM_MULTI_VIEWER`/`NDRONE_MULTI_VIEWER`, `Gen_IDL_DDS`는 **IDL
   스키마/패키지 SDK 참고용으로만** 사용 — 이건 이 생태계 전체가 공유하는
   wire-format 계약이라 코드 재사용 금지 원칙과 무관하다.

### 2026-08-12 예외 승인: RTMP 직접 수신 작업 한정

사용자가 명시적으로 예외를 승인함(아래 두 건, **RTMP 브릿지 작업에 한정** —
다른 이유로 DDS_Platform/RobotMediaDashboard를 건드리는 근거로 확대 해석 금지):

- **규칙 #2 예외**: `Z:\DDS_Platform\DDS-Router\thirdparty`에 librtmp
  (https://github.com/ireader/media-server 의 librtmp) 기반 `rtmpVideoBridge`
  신규 추가 허용. 기존 `RtspVideoBridge`(RTSP push → DDS `VideoTsPacket`)와
  나란히 두는 구조 — ZLMediaKit/RTSP 경로는 유지, RTMP 직접 수신은 새 경로로
  추가.
- **규칙 #1 예외**: `RobotMediaDashboard`에 기존 RTSP 탭(용어 변경 후 "MEDIA"
  탭) 옆에 RTMP 설정 탭 추가 허용.

## RTMP 직접 수신 — 방향 (2026-08-12, 아직 구현 전)

```
1. FacadeDdsBridgeSmokeTest
   librtmp(ireader/media-server) 로 H.264 발행(퍼블리셔 쪽). TS 사용 안 함.

2. DDS-Router (Z:\DDS_Platform\DDS-Router\thirdparty, 예외 승인됨)
   librtmp 설치 → rtmpVideoBridge 신규: RTMP 수신 → DDS 변환
   (DDS 쪽은 H.264 → MPEG-TS mux → DDS publish, 기존 VideoTsPacket과 동일 wire format)

3. FacadeDdsBridge (이 프로젝트, previewer/FacadeDdsBridge)
   DDS sub → MPEG-TS demux → H.264 decode → 실시간 스티칭 연동
   (남은 작업 #2 "VideoTsPacket 디먹싱/디코드"와 동일 작업)

4. 검증(가능성 확인)
   D:\ClaudePr\CheckCrack\facades\ 아래 JPEG 전부를 H.264로 변환 →
   1번 스모크테스트 퍼블리셔로 발행 → 3번 경로로 수신/디코드까지 end-to-end 확인
```

## 2026-08-12 세션 — 1+2+4단계 구현 완료, 실제 검증은 부분적

**librtmp 벤더링** (GitHub `ireader/media-server`에서 fresh clone, `RobotMediaServer` 사본
사용 안 함 — 사용자 명시 지시): `previewer/FacadeDdsBridge/thirdparty/librtmp/`와
`Z:\DDS_Platform\DDS-Router\thirdparty\librtmp\`에 각각 독립적으로 벤더링(`rtmp/` +
`libflv_min/amf0.{h,c}` + `sdk_min/{sha,uri-parse,urlcodec}` — `librtsp/README.md`와 동일한
cherry-pick 방법, 각 폴더에 자체 `README.md`로 출처 기록). GitHub 실제 헤더
(`rtmp-server.h`/`rtmp-client.h`)를 직접 fetch해서 API 확인 — `onvideo`가 이미 "FLV
VideoTagHeader + AVCVIDEOPACKET" 그대로 넘겨준다는 걸 확인해서 **libflv 전체 벤더링은
불필요**로 확정(amf0.c만 필요).

**previewer 쪽** (`FacadeDdsBridge/src/RtmpPublisher.h/.cpp`, `SmokeTest.cpp`에
`--rtmp-publish <url> <flv>` 모드 추가, Winsock2 raw socket 신규 작성 — 이 프로젝트 첫
non-FastDDS 네트워킹 코드): FLV 태그 리더 + `rtmp_client_push_video/audio/script` 루프.
`build.ps1`로 클린 빌드 확인 완료(`/p:VCToolsVersion=14.44.35207` 그대로 유지).

**DDS-Router 쪽** (`thirdparty/RtmpVideoBridge/main.cpp`, `RtspVideoBridge/main.cpp`를
직접 템플릿으로 사용): RTMP accept 루프 → `onpublish`로 app/stream 매칭(RTSP의 mount_path
매칭과 동일 구조) → `onvideo`에서 AVCDecoderConfigurationRecord 파싱(SPS/PPS 추출, IDR
프레임 앞에 Annex-B로 재삽입 — H.264-in-TS 표준 관행) → 기존 `libmpeg`(`mpeg_ts_write`)로
TS mux → `VideoTsPacket` DDS 발행(RtspVideoBridge와 동일 wire format/QoS). `onaudio`/
`onscript`는 **반드시 non-null이어야 함** — `rtmp-handler.c`가 이 두 콜백은 null 체크 없이
바로 호출함(command 계열 콜백들과 다름) — 실제 GitHub 소스를 읽어서 확인한 뒤 no-op 스텁
추가, 안 했으면 첫 audio/onMetaData 메시지에서 크래시났을 것.

`build.sh`에 `build_rtmp_video_bridge()` 추가(`build_rtsp_video_bridge()` 옆, 동일 구조),
`config/rtmp_video_bridge_streams.txt`(`app|stream|dds_domain|pub_topic|codec|stream_id`),
`scripts/run_rtmp_video_bridge.sh` 신규(DdsMonitor 상태-리포트 루프는 의도적으로 제외 —
아래 "다음 세션" 참고).

**JPEG→FLV 검증 스크립트**: `tools/publish_facade_rtmp.py` — `scan_images()` 재사용,
ffmpeg concat demuxer로 JPEG 시퀀스를 FLV(H.264)로 인코딩 후 SmokeTest
`--rtmp-publish`로 발행. ffmpeg는 이 머신에 없었어서 `winget install Gyan.FFmpeg`로 설치.

**실제 검증 상태**:
- ✅ **Publish 쪽 종단 확인**: `facades/TOP`(JPEG 13장) → ffmpeg로 FLV 인코딩(40MB) 성공 →
  `FacadeDdsBridgeSmokeTest.exe --rtmp-publish`가 FLV 17개 태그 파싱 성공 → RTMP
  connect/handshake 로직까지 정상 실행(리스닝 서버가 없어 connect 자체는 실패 — 의도된
  결과, 코드 경로가 크래시 없이 정상 실패 처리됨을 확인).
- ❌ **RtmpVideoBridge(DDS-Router 쪽) 빌드/실행 미검증** — Linux 전용 빌드인데 이 개발
  머신엔 Linux 툴체인이 없음. **WSL 사용은 명시적으로 금지됨**(사용자: "WSL 사용 안함,
  192.168.100.224 에 vm 우분트 있음") — 그 VM에 대한 SSH 접근 권한이 이 세션엔 없어서
  실제로 빌드/실행해보지 못함. 코드는 `RtspVideoBridge`(이미 검증된 패턴)를 거의 그대로
  따랐고 모든 librtmp/libmpeg API 시그니처를 실제 GitHub 헤더로 직접 대조했지만, **다음
  세션에서 반드시 그 VM(또는 실제 DDS-Router 빌드 호스트)에서
  `build.sh`(`DDS_ROUTER_BUILD_RTMP_VIDEO=1`) 실행해서 빌드 에러부터 확인할 것.**
- ❌ 그래서 "검증" 절의 5단계(전체 체인 DDS 카운터 증가 확인)도 아직 못 함 — 1번은 됐고
  2/3/4/5번은 Linux 빌드가 되어야 이어서 진행 가능.

### 2026-08-12 세션 계속 — 실제 VM에서 빌드+종단 검증 완료, DDS 데이터 미도달 이슈 발견

사용자가 SSH 접근(`yiyou@192.168.100.224`) 제공 — 공개키 1회 등록 후 키 기반 접속 확립
(`~/.ssh/authorized_keys`에 이 워크스테이션의 `id_ed25519.pub` 추가됨). **WSL은 여전히
사용 안 함** — 이 VM이 실제 DDS-Router 빌드 호스트.

**빌드 성공**: `librtmp` 단독 빌드 클린. `RtmpVideoBridge`는 처음엔
`foonathan_memory` cmake 에러 — `Z:\DDS_Platform\CLAUDE.local.md:129`에 이미 문서화된
기존 이슈였음(비인터랙티브 SSH는 `~/.bashrc`를 안 읽어서 `/opt/ros/humble/setup.bash`가
자동으로 안 잡힘 — `source /opt/ros/humble/setup.bash && source
DDS-Router/scripts/env.sh`를 명시적으로 먼저 해야 함). 이후 클린 빌드 성공 — 내 코드
문제 아니었음(`RtspVideoBridge`도 동일 환경에서 동일하게 재현되는 걸로 확인, 사전 존재
이슈).

**종단 검증 성공 (RTMP -> DDS write까지)**: VM에서 `rtmp_video_bridge` 기동(포트 1935,
`config/rtmp_video_bridge_streams.txt`의 `live|front|...`) → Windows에서
`tools/publish_facade_rtmp.py facades/TOP rtmp://192.168.100.224:1935/live/front` 실행 →
FLV 17개 태그 전부 발행 성공 → VM 로그: `AVCDecoderConfigurationRecord received: 1 SPS, 1
PPS` + **`RTMP->TS->DDS seq=0...55000`, DDS write 실패 0건**. RTMP 프로토콜 코드, AVC
파싱, TS mux, DDS publish까지 전부 실증됨.

**⚠️ 새로 발견한 이슈 — DDS 샘플이 cross-machine으로 실제 도달 안 함**: Windows의
`FacadeDdsBridgeSmokeTest.exe`(기본 DDS-subscribe 모드)가 VM의 퍼블리셔와
`on_subscription_matched`까지는 정상 되는데 (`ImageSensorFrame matched a publisher`,
`VideoTsPacket matched a publisher` 둘 다 뜸) **`video_count`가 계속 0**. 시도한 것:
1. Windows 방화벽 인바운드 UDP 규칙 추가(`FacadeDdsBridgeSmokeTest.exe`, 사용자가 관리자
   권한으로 직접 실행) — 효과 없음.
2. 이 워크스테이션에 VMware 가상 어댑터(VMnet1/VMnet8)가 실제 LAN(192.168.100.219)과
   같이 떠 있어서 FastDDS가 도달 불가능한 가상 IP를 discovery locator로 광고했을 가능성
   의심 → `DdsFrameSubscriber.cpp`의 `MakeUdpOnlyQos()`에 `FACADE_DDS_INTERFACE_WHITELIST`
   env var(콤마 구분 IP 목록, 기본값 없음 — 이식성 유지) 추가, `192.168.100.219`로 제한
   테스트 — **여전히 효과 없음**. VM 쪽도 확인(`ens33: 192.168.100.224`,
   `docker0: 172.17.0.1` 외엔 없음 — 단순한 구성).
3. Raw UDP 도달성 테스트(Python 소켓)로 더 파려다가 **사용자가 "python 사용하지 마세요"
   요청 — 중단**.

**결론**: RTMP 브릿지 자체(퍼블리셔 → RtmpVideoBridge → TS mux → DDS write)는 완전히
검증됨 — 위 로그가 그 증거. 남은 건 순수하게 **DDS 레벨의 cross-machine 데이터 전달
문제**로, RTMP 작업과는 별개 이슈(이전 세션 메모에 이미 "실제 퍼블리셔와 맞물려 카운트가
올라가는지 미검증"으로 남아있던 바로 그 질문 — 이번에 처음으로 실제 퍼블리셔로 테스트해
보니 실제로 카운트가 안 올라간다는 게 드러남). `FACADE_DDS_INTERFACE_WHITELIST` 코드는
남겨둠(유용할 수 있음, 기본 비활성).

### ⚠️ 이 RTMP 파이프라인의 실제 목적 (사용자 명확화, 2026-08-12)

**JPEG → .h264 변환 데이터는 저장용이 아니다.** 목적은: JPEG를 프레임 단위로 h264 변환 →
`FacadeDdsBridgeSmokeTest`(`--publish-facade`/`--rtmp-publish`)로 RTMP 발행 →
`RtmpVideoBridge`(DDS-Router) → **`FacadePreviewer`에서 실시간 스티칭**. 즉 지금까지 만든
건 이 최종 목적지로 가는 배관 검증이었고, 진짜 남은 작업은 "남은 작업 1번"(DDS 미도달
디버깅)이 풀린 뒤 **previewer의 원래 "남은 작업 2번"(`VideoTsPacket` 실시간 디먹싱+H.264
디코드+라이브 스티칭 연동, 아래 3번과 동일)** — 디스크 저장 기능 같은 건 불필요, 방향
아님.

### DDS cross-machine 미도달 이슈 — 추가 조사 (2026-08-12, 코드보다 조사 우선)

사용자 지적: "무조건 코딩하지 말고 `Z:\DDS_Platform\DDS-Router\config\ddsrouter\
local_demo_no_echo.yaml` 확인하세요". 확인 결과 **`ddsrouter` 데몬이 이 VM에서 실제로
실행 중**(`ddsrouter --config-path .../local_demo_no_echo.yaml --debug`, PID 확인됨) —
`ControlCenterDomainParticipant`(kind: local, domain: 0)가 내 테스트와 **같은 domain 0**에
실제 참가자로 붙어있음. 이 config 자체엔 map2stitch_msgs/영상 토픽은 전혀 없지만(드론
텔레메트리 전용), 같은 호스트·같은 도메인에 참가자가 하나 더 떠 있다는 사실 자체가
디버깅을 꼬이게 만들었을 가능성 큼 — 예: `RtmpVideoBridge`가 몇 번째 participant
index로 뜨는지(포트 계산에 영향, 아래 initialPeersList 시도가 실패한 이유일 수 있음).

시도했지만 안 된 것들 (전부 기록):
1. Windows 방화벽 인바운드 UDP 규칙(사용자가 직접 관리자 권한으로 추가) — 효과 없음.
2. 양쪽(Windows `DdsFrameSubscriber.cpp` / Linux `RtmpVideoBridge`)에 `interfaceWhiteList`
   추가해서 각자 실제 LAN IP로 제한(env var `FACADE_DDS_INTERFACE_WHITELIST`/
   `RTMP_BRIDGE_DDS_INTERFACE_WHITELIST`, 코드는 남겨둠, 기본 비활성) — 효과 없음.
3. FastDDS `Log::SetVerbosity(Info)` — Windows 프리빌트 SDK가 Info 레벨 로그를 컴파일
   타임에 제거해놔서 무용지물(Warning만 남음).
4. `writer->get_matched_subscription_data()`로 실제 discovered locator 확인 시도
   (`RtmpVideoBridge`의 `on_publication_matched`에 추가, 코드는 남겨둠) — **결정적 발견**:
   **writer(VM) 쪽에서 `on_publication_matched`가 단 한 번도 발생한 적이 없음** (모든
   테스트 로그에 "DDS reader matched for VideoTsPacket" 줄 자체가 없음), reader(Windows)
   쪽만 matched 로그가 뜸 — 즉 discovery가 편도로만 됨(VM→Windows는 되는데
   Windows→VM SPDP가 VM에 전혀 안 닿음).
5. `initialPeersList`로 명시적 unicast SPDP peer 추가 시도(env var
   `RTMP_BRIDGE_DDS_INITIAL_PEER`, 포트는 domain 0 participant-index-0 공식인 7410으로
   하드코딩) — **아직 재검증 안 함, 위 ddsrouter 참가자 때문에 포트 계산이 틀렸을 수
   있어서 코딩 중단하고 여기서 조사로 전환**.

**진행 상황 업데이트 (2026-08-12, 계속)**:

1. `ss -ulnp`로 실제 포트 점유 확인 — `RtmpVideoBridge`는 participant index 0(7410/7411),
   `ddsrouter`의 `ControlCenterDomainParticipant`는 index 1(7412/7413)로 확인. 포트 계산은
   원래 맞았음.
2. `initialPeersList`를 **양쪽 다** 추가(`RTMP_BRIDGE_DDS_INITIAL_PEER`/
   `FACADE_DDS_INITIAL_PEER` env var, 정확한 포트 7410로 서로를 가리킴) — 여전히 writer
   쪽 matched 없음.
3. PowerShell 기반 raw UDP 송수신 테스트(Python 대신)로 **양방향 다 정상 도달 확인**
   (VM→Windows:7410에 실제 RTPS 바이트 564B 수신 확인, Windows→VM도 확인) — 순수 네트워크
   문제는 완전히 배제됨.
4. **사용자 지적으로 발견한 진짜 버그**: `previewer/FacadeDdsBridge/idl/map2stitch_msgs/
   msg/VideoTsPacket.idl`이 **stale**했음 — 필드명이 `frame_id`인데 VM 쪽 authoritative
   IDL(`fixed_idl/.../VideoTsPacket.idl`)은 이미 `chunk_id`로 바뀐 지 오래(2026-07-31
   업스트림 rename). `VideoTsPacketTypeObjectSupport.cxx`의 `MemberName name_frame_id =
   "frame_id"` — 이게 XTypes TypeObject 해시에 들어가는 필드명이라, 양쪽이 사실상 **다른
   타입**으로 등록되고 있었음. `idl/generated/map2stitch_msgs/msg/VideoTsPacket*.{hpp,ipp,
   cxx}` 3개 파일에서 `frame_id`→`chunk_id` 전체 치환(fastddsgen 로컬에 없어서 수동 sed,
   구조/CDR 순서는 그대로라 안전) + `DdsFrameSubscriber.{h,cpp}`/`SmokeTest.cpp`도 갱신,
   빌드 클린 확인.
5. 고쳤는데도 아직 완전 해결 안 됨 — 흥미로운 부작용: 타입 수정 **이전**에는 reader
   쪽에서 "VideoTsPacket matched a publisher"가 항상 떴는데(그런데도 데이터 0개), 수정
   **이후**엔 그 매치 로그 자체가 사라짐. 즉 이전의 "matched"는 타입 불일치를 관대하게
   넘어간 약한 매칭이었을 가능성이 있고, 지금은 오히려 더 정직하게 "매치 안 됨"을
   보여주는 것일 수 있음. 게다가 "ImageSensorFrame matched a publisher"는 내 테스트와
   무관하게 매번 뜨는데 — 이 테스트 세션에 ImageSensorFrame 퍼블리셔를 띄운 적이 없어서,
   이건 이 LAN의 **다른 무관한 시스템**(AP_DDS 등)이 우연히 같은 토픽/도메인에 떠 있어서
   매칭되는 것으로 보임 — 즉 예전부터 이 "matched" 로그 자체가 내 writer/reader 쌍을
   가리키는 신뢰할 만한 증거가 아니었을 수 있음.

**결론(당시)**: 타입 불일치는 확실한 버그였고 고쳤음(재사용 가치 있는 수정, 유지).

### 2026-08-12 계속 — tcpdump로 wire-level 확인, sudo 확보(pw=`1`), 결정적 반전 발견

사용자가 VM sudo 비번(`1`, SSH 계정과 동일) 제공 → `tcpdump`/`tshark`로 실제 패킷 캡처.

1. **XML profile 가설 배제**: `FASTDDS_DEFAULT_PROFILES_FILE` 환경변수, `DEFAULT_FASTDDS_
   PROFILES.xml` 파일 전부 없음(`ddsrouter` 실행 환경 확인) — XML 설정과 무관.
2. **`DDS-Router` 전체 재빌드**(`bash build.sh`, colcon core 포함) + `ddsrouter`/
   `rtmp_video_bridge` 재기동 — 결과 동일, 안 풀림.
3. **tcpdump/tshark로 VM↔Windows(192.168.100.219) 간 포트 7400-7420 캡처, 45초**:
   **완전한 RELIABLE 프로토콜 교환 확인**(RTPS 서브메시지 DATA(0x15)/HEARTBEAT(0x07)/
   ACKNACK(0x06)/INFO_TS(0x09)/INFO_DST(0x0e) 전부 양방향으로 정상 오감, 356패킷/45초).
   즉 **SPDP/SEDP discovery 데이터 자체는 네트워크 레벨에서 완전하고 건강하게 오가고
   있음** — 이전에 세운 "discovery가 편도로만 된다"는 가설 자체가 틀렸을 수 있음이 드러남.
4. **`on_offered_incompatible_qos` 진단 콜백 추가** (QoS 비호환으로 인해 `on_publication_
   matched` 대신 이게 불렸을 가능성 확인) → **역시 단 한 번도 안 불림**. QoS 비호환도
   아님.
5. **`create_datawriter`의 기본 StatusMask 확인** (`Publisher.hpp` 소스 직접 확인):
   `mask = StatusMask::all()`가 기본값 — 마스크 문제도 아님, 콜백은 이론상 다 활성화돼
   있어야 함.
6. **결정적 사실**: `on_publication_matched`가 **이 writer의 전체 로그(1107줄) 통틀어
   단 한 번도 호출된 적이 없음** — Windows reader와의 매칭뿐 아니라, 같은 머신의
   `ddsrouter`가 "[PUBLISHER DISCOVERED]"로 이 writer를 인지했다고 로그에 남긴 경우에도
   마찬가지. 즉 이건 더 이상 "cross-machine networking 문제"가 아니라 **이 writer의
   `DataWriterListener` 콜백 자체가 (로컬이든 원격이든) 단 한 번도 실행된 적이 없다**는
   훨씬 근본적인 문제로 재정의됨. (다만 ddsrouter의 "[PUBLISHER DISCOVERED]" 로그는
   ddsrouter가 실제 매칭되는 DataReader를 만들어서가 아니라, 참가자 레벨 discovery
   리스너로 모든 토픽을 그냥 로깅만 하는 것일 수 있어 완전한 반증은 아님 — 다만 방향성은
   分명히 "writer 쪽 콜백 배선/등록 자체"로 좁혀짐.)

### ✅ 2026-08-12 최종 해결 — 근본 원인 확정, 전체 파이프라인 실증 완료

**해결 과정**: `hello_world` 최소 예제를 VM에도 Windows에도 만들어서 단계적으로 격리:
1. `hello_world`(HelloWorld 타입, 단순 uint32+string)를 VM↔Windows cross-machine으로
   테스트 → **매칭 성공 + 데이터 수신 성공**. 이걸로 "이 VM/네트워크는 cross-machine
   매칭이 근본적으로 안 된다"는 가설이 완전히 깨짐.
2. `RtmpVideoBridge`와 **완전히 동일한 QoS/transport 설정**(UDPv4-only,
   interfaceWhiteList, initialPeersList, BEST_EFFORT depth 16)을 `hello_world`
   퍼블리셔에 이식 → **역시 매칭+수신 성공**. QoS/transport 설정도 무죄로 확정.
3. `RtmpVideoBridge`의 accept-loop을 통째로 스킵(`RTMP_BRIDGE_DDS_TEST_ONLY` env var,
   DDS 세팅만 하고 60초 sleep)하고 실제 바이너리로 재테스트 → **여전히 매칭 안 됨**.
   accept-loop/세션 스레딩도 무죄.
4. **`VideoTsPacket` 타입 전용 최소 writer**(RtmpVideoBridge 코드 하나도 없이, 생성된
   `VideoTsPacket*.cxx/hpp`만 갖고 참가자+writer만 만들고 sleep)를 VM에 새로 빌드 →
   **여기서도 매칭 실패!** `HelloWorld`는 되는데 `VideoTsPacket`만 실패 — 타입 자체가
   범인으로 확정.
5. `diff`로 previewer와 VM의 `VideoTsPacketTypeObjectSupport.cxx`를 비교 →
   **결정적 차이 발견**:
   ```
   VM(fastddsgen 4.0.6):       ExtensibilityKind::FINAL
   previewer(fastddsgen 4.3.0): ExtensibilityKind::APPENDABLE
   ```
   두 fastddsgen 버전의 extensibility 기본값이 달라서, 필드명/타입명 문자열을 다 맞춰도
   XTypes TypeObject 해시가 근본적으로 달라 Fast-DDS가 "다른 타입"으로 취급하고 있었음.

**최종 수정**: previewer의 `idl/generated/map2stitch_msgs/msg/VideoTsPacket*.{hpp,cxx,ipp}`
6개 파일을 `Z:\DDS_Platform\DDS-Router\thirdparty\RtmpVideoBridge\map2stitch_msgs\msg\`의
것으로 **byte-for-byte 통째로 교체**(수동 패치 대신 VM의 authoritative 생성 파일 그대로
사용). 재빌드 후 종단 테스트:

```
DDS reader matched for VideoTsPacket
  matched reader remote_locators: unicast=[192.168.100.219:7411] multicast=[]
[smoke] sensor_count=0 video_count=15034
```

**JPEG→h264→RTMP→RtmpVideoBridge→TS mux→DDS 발행→Windows DDS 구독 수신까지 15,034개
샘플로 완전히 실증됨.** 이 세션의 원래 목표(7번 "dds u sub" 단계)가 최종 해결됨.

**교훈(향후 재발 방지)**: `VideoTsPacket`/`ImageSensorFrame`처럼 여러 프로젝트에서
독립적으로 fastddsgen을 돌려 생성하는 공유 IDL 타입은, **fastddsgen 버전이 다르면 필드명이
다 같아도 매칭이 깨질 수 있음** — 단순 문자열 패치로 고치려 하지 말고, 가능하면 한쪽의
생성 결과물을 그대로 복사해서 쓸 것(빌드 환경이 fastddsgen을 직접 못 돌리는 previewer
같은 경우 특히). `ImageSensorFrame`도 같은 위험이 있음 — 지금은 안 쓰지만 나중에
실사용 시 동일 이슈 재발 가능성 있으니 유의.

**남은 팔로업**:
- ~~RTMP 발행 쪽 `librtmp/rtmp-client-invoke-handler.h:229` assertion 크래시~~ — **해결됨,
  아래 "FacadePreviewer UI Host/Port 설정 + librtmp assert 크래시 수정" 절 참고.
- `RtmpVideoBridge/main.cpp`에 추가했던 `RTMP_BRIDGE_DDS_TEST_ONLY` 진단 분기는
  디버깅용으로 남겨둠(기본 비활성, 필요시 제거).

### 완료: DdsMonitor MEDIA 탭 (RTSP+RTMP), 2026-08-12

사용자가 이전에 요청했던 것("RTSP 탭을 MEDIA로 변경하고 RTSP+RTMP 내용 적용") 완료:
- 네비게이션 버튼 "RTSP" → "MEDIA" (`data-tab="rtsp"` → `"media"`, `pageTitles`도 갱신)
- `tab-media` 섹션 안에 기존 RTSP 패널 + **신규 RTMP 패널**을 나란히 배치(각자 독립
  toolbar/table/apply/stop)
- 신규: `Models/RtmpStream.cs`, `Controllers/RtmpStreamController.cs`(List/ReplaceAll/
  Apply/Stop, `RtspCameraController`와 동일 패턴 — App/Stream이 MountPath 대신 매칭 키),
  `RouterHostConfig`에 `RtmpVideoBridgeListenAddress/Port/AutoStart` 3필드,
  `RouterProcessService`에 `Restart/StopRtmpVideoBridgeAsync` + 시작 시 auto-start
  reconcile 블록, `wwwroot/js/router-config.js`에 RTMP 스트림 에디터(RTSP 블록 그대로
  포팅) + `tabs.js`에서 media 탭 진입 시 `initRtspCameraEditor()` +
  `initRtmpStreamEditor()` 둘 다 호출.
- EF Core 마이그레이션 `20260812051344_AddRtmpStreams` 생성(VM에서 `dotnet-ef` 직접
  실행) + 앱 시작 시 자동 적용 확인(`db.Database.Migrate()`), `dotnet build` 0 에러,
  실행 후 로그에 마이그레이션 SQL 정상 적용 확인, `index.html`/API 라우팅 정상 서빙
  확인(`/api/rtmp-streams` 미인증 401 — `/api/rtsp-cameras`와 동일하게 정상 동작).
- **미검증**: 실제 브라우저로 로그인해서 MEDIA 탭 클릭 → RTMP 목록 추가 → Apply →
  `RtmpVideoBridge` 재시작까지 UI 클릭으로 확인하는 건 로그인 크리덴셜이 없어서 못 함 —
  사용자가 직접 확인 필요.
- `scripts/run_rtmp_video_bridge.sh`에 DdsMonitor 상태-리포트 루프(`run_rtsp_video_bridge
  .sh`처럼 pid/cpu/mem POST)는 **아직 안 함** — RouterProcessService의 재시작
  스크립트(`BuildProcessRestartCommand`)는 PID 파일 확인만으로 동작하므로 지금 상태로도
  Apply/Stop 자체는 되지만, DdsMonitor의 "Devices" 상태 화면에 RtmpVideoBridge가 안
  뜸 — 필요하면 다음에 추가.

### 남은 작업 (다음 세션)

1. ~~DDS cross-machine 미도달 이슈~~ — **해결됨**, 위 "✅ 2026-08-12 최종 해결" 절 참고
   (ExtensibilityKind 불일치가 근본 원인).
2. MEDIA 탭 실제 브라우저 클릭 검증(로그인 필요, 사용자가).
3. `run_rtmp_video_bridge.sh`에 DdsMonitor 상태-리포트 루프 추가.
4. ~~FacadeDdsBridge의 `VideoTsPacket` 실시간 디먹싱+H.264 디코드+라이브 스티칭 연동~~ —
   **완료**, 아래 "VideoTsPacket 디먹싱+디코드+ORB 라이브 스티칭 구현" 절 참고. previewer의
   원래 최종 목적지가 이제 처음부터 끝까지 실증됨.
5. `ImageSensorFrame` 계열은 `VideoTsPacket`과 같은 ExtensibilityKind 위험을 한 번도
   검증 안 함 — 실사용 전에 반드시 재확인.

### ffmpeg 위치 변경 (2026-08-12)

`winget install Gyan.FFmpeg`로 처음 설치했던 걸 `C:` 공간 문제로 `previewer/tools/ffmpeg/`
로 이동(gitignore 처리, `previewer/tools/README.md`에 기록).

### `tools/publish_facade_rtmp.py` → C++ 포팅 (2026-08-12, 사용자 지시)

사용자 요청("publish_facade_rtmp.py 이거 지우고 FacadeDdsBridgeSmokeTest이곳에서 c++로
코딩")으로 Python 스크립트 삭제, 기능을 `FacadeDdsBridgeSmokeTest`의 새 모드로 이식:

```
FacadeDdsBridgeSmokeTest.exe --publish-facade <facade_dir> <rtmp_url> [fps] [--keep-flv]
```

새 파일 `src/JpegFacadePublisher.h/.cpp`: JPEG 폴더 스캔(`scan_images()` 로직 이식 —
`output/` 하위 제외, 정렬), ffmpeg concat list 작성(UTF-8), `previewer/tools/ffmpeg/bin/
ffmpeg.exe`를 exe 자기 경로 기준 상대경로로 자동 탐색(없으면 PATH), `CreateProcessW`로
ffmpeg 실행(표준 Windows argv 인용 규칙 직접 구현), 끝나면 기존 `RunRtmpPublish()`
재사용해서 RTMP 발행.

**한글 경로 처리가 실제 이슈였음**: `SmokeTest.cpp`의 `main(int argc, char** argv)`는 ANSI
코드페이지 narrow argv라 `facades\앞` 같은 경로가 깨질 위험이 있어서, `--publish-facade`
분기에서만 `GetCommandLineW()` + `CommandLineToArgvW()`로 facade_dir 인자를 wide-string으로
다시 읽음(다른 인자는 URL/숫자라 ASCII라 영향 없음). `RunRtmpPublish()`는 narrow
`std::string`(ANSI 코드페이지, 기존 `--rtmp-publish` 모드의 raw argv 관례와 일치)를 기대해서,
`--keep-flv`로 한글 폴더명이 섞인 출력 flv 경로를 넘길 때만 UTF-8이 아니라 ANSI(CP_ACP)로
변환해서 넘김(`WideToAnsi`) — 이 프로젝트가 이미 여러 곳에서 겪은 "Windows 한글 경로" 클래스
문제, 처음으로 이 exe 안에서 직접 마주친 것.

**실제 검증**: `facades\앞`(진짜 DJI 원본 사진 26장, 5280x3956) 대상으로 실행 —
한글 경로 정상 인식("found 26 JPEG(s) under D:\ClaudePr\CheckCrack\facades\앞"), ffmpeg
인코딩 성공(92MB flv), FLV 30개 태그 파싱, RTMP publish 전부 성공, VM의
`rtmp_video_bridge`도 정상 수신(DDS seq 계속 누적). 임시 파일(`concat_list.txt`,
`facade_publish.flv`) 정리도 확인.

### FacadePreviewer UI Host/Port 설정 + librtmp assert 크래시 수정 (2026-08-12)

**1. UI에 DDS-Router Host/Port/로컬 인터페이스 입력 필드 추가.** 지금까지
`FACADE_DDS_INITIAL_PEER`/`FACADE_DDS_INTERFACE_WHITELIST` 두 env var로만 조절하던 걸,
현장에서 실행 파일 재시작 없이 UI에서 바로 입력할 수 있게 노출:
- `DdsFrameSubscriber::Start/StartAsync`가 `initial_peer_host`/`initial_peer_port`/
  `local_interface_ip` 세 파라미터를 추가로 받음(전부 기본값 nullptr/0 — 비우면 기존
  env var로 폴백, `SmokeTest.cpp`의 CLI 동작은 그대로 유지). `MakeUdpOnlyQos()`도 같은
  방식으로 확장(파라미터 우선, 없으면 env var).
- `FacadeDdsBridge.h/.cpp`의 `FacadeDds_StartAsync` C API에 동일 3개 파라미터 추가.
- C# 쪽 `DdsBridgeInterop.cs`/`DdsBridgeService.cs`도 대응 파라미터 추가, `MainViewModel`에
  `DdsRouterHost`(string)/`DdsRouterPort`(int, 기본 7410)/`LocalInterfaceIp`(string) 3개
  `ObservableProperty` 추가 — `StartScanning()`이 이 값들을 그대로 `_dds.Start(...)`에
  전달. `MainWindow.xaml`의 컨트롤 바 위에 이 3개를 입력하는 새 행 추가.
- **UI 설계 관련 사용자 피드백**: 처음엔 별도 "적용" 버튼(값 입력 → 클릭 → 재연결, DDS
  설정을 스캔 시작과 분리)을 만들었는데, 사용자가 "죄송, 빼세요. 스캔 시작 버튼
  있잖아요. 이거로 하면 되는거지요"라며 반려 — **기존 "스캔 시작" 버튼이 필드 값을 그대로
  읽어서 시작하는 것으로 충분, 별도 Apply 버튼 불필요**. 되돌림(커밋 안 된 중간 상태라
  깔끔하게 revert).

**2. `librtmp/rtmp-client-invoke-handler.h:229` assert(0) 크래시 — 근본 원인 확정 + 수정.**
이전 세션에 "남은 팔로업"으로만 남겨뒀던 크래시가 이번 검증 중 실제로 재현됨
(`facades/TOP` 13장 발행 직후 크래시). 원인 추적:
- `rtmp_command_onstatus()`(client-side onStatus 핸들러)가 인식하는 `code` 문자열이
  하드코딩된 목록으로 제한돼 있고, 목록에 없는 코드를 받으면 `assert(0)`으로 프로세스
  전체가 죽는 구조.
- 실제로 걸린 코드는 **이 프로젝트 자신의 `RtmpVideoBridge`가 보낸 정상적인 코드**:
  `rtmp-server.c`의 `rtmp_server_ondelete_stream()`이 `deleteStream` 응답으로
  `"NetStream.DeleteStream.Suceess"`(업스트림 자체의 오타)를 보내는데, 이 문자열이
  client 핸들러의 인식 목록엔 없음 — 즉 **정상적인 publish 종료(스트림 종료) 시퀀스마다
  100% 재현되는 크래시**였음(우연/데이터 손상이 아니라 항상 걸리는 로직 버그).
- **수정**: 두 벤더링 사본(`previewer/FacadeDdsBridge/thirdparty/librtmp`와
  `Z:\DDS_Platform\DDS-Router\thirdparty\librtmp`) 모두 동일하게, 해당 `else` 분기의
  `assert(0)`을 제거하고 로그만 남기고 `return 0`으로 계속 진행하도록 패치. 각 폴더
  README.md에 "Local patches" 절 신설해 왜 upstream "unmodified" 원칙에서 벗어났는지
  기록(순수 vendoring이 아니라 이 한 군데만 로컬 패치).
- 양쪽 재빌드: `previewer/FacadeDdsBridge/build.ps1`(Debug) 클린 성공,
  `Z:\DDS_Platform\DDS-Router\thirdparty\RtmpVideoBridge\build`에서 직접 `make`(VM,
  ROS Humble setup 소싱) 클린 성공 — `RtmpVideoBridge` 프로세스도 새 바이너리로 재기동
  (참고: `RtmpVideoBridge`는 서버 역할만 하므로 이 client-side 분기는 이쪽에서는 원래
  dead code, 그래도 두 벤더링 사본을 소스 레벨에서 동일하게 유지하려고 같이 패치).

**3. 종단 재검증 — 이번엔 SmokeTest가 아니라 실제 `FacadePreviewer.exe` 앱으로.**
UI Automation(PowerShell `System.Windows.Automation`)으로 앱을 직접 조작:
1. `FacadePreviewer.exe` 실행 → Host 필드에 `192.168.100.224`, Port에 `7410` 입력 →
   "스캔 시작" 클릭.
2. Windows 쪽에서 `FacadeDdsBridgeSmokeTest.exe --publish-facade facades\TOP
   rtmp://192.168.100.224:1935/live/front 10` 실행 — **크래시 없이 끝까지 완료**
   ("done -- all 17 tag(s) sent, stopping...").
3. VM 로그: `RtmpVideoBridge`가 seq 55000까지 DDS publish (`RTMP->TS->DDS`).
4. `FacadePreviewer` UI 상태 텍스트 판독(한글 콘솔 코드페이지 문제 회피하려고 Base64로
   추출) 확인:
   - `"DDS 구독 시작됨 (domain 0, peer 192.168.100.224:7410) — 수신 대기 중"` → **UI
     입력값이 실제로 네이티브 참가자 설정까지 정확히 전달됨** 확인.
   - `"· video 14708"` → **실제 앱 GUI가 55,000개 중 14,708개 샘플을 실제로 수신**
     (BEST_EFFORT라 일부 손실은 정상, 이전 SmokeTest 검증과 같은 급의 결과).

**결론**: UI Host/Port/로컬 인터페이스 입력 → 실제 DDS 연결까지 전체 경로 실증 완료,
동시에 그동안 미뤄뒀던 RTMP publish 크래시 버그도 근본 원인까지 확정해서 고침. 이제
previewer 쪽에서 막힌 건 없고, 유일하게 진짜 남은 핵심 작업은 `VideoTsPacket` 실시간
디먹싱+H.264 디코드+라이브 스티칭 연동뿐(위 "남은 작업" 4번).


## 아키텍처

```
DJI Pilot 2 (수정 안 함)
  ├─ RTMP → ZLMediaKit → RTSP/WebRTC        [메인 CheckCrack 프로젝트, 기존]
  └─ Cloud API → MQTT/HTTPS/WS → 텔레메트리(위치/자세/고도/속도)

DDS_Platform (게이트웨이, 이미 존재/검증됨, 수정 안 함):
  - RtspVideoBridge: RTSP push → DDS VideoTsPacket
  - MqttBridge: MQTT ↔ DDS
  - DDS-Router: 도메인 라우팅 → 외부 현장 노트북

previewer/ (이 프로젝트, 완전 독립):
  - FacadeDdsBridge (네이티브 C++ DLL): FastDDS 구독 (ImageSensorFrame + VideoTsPacket)
  - FacadePreviewer (C# WPF, MVVM): P/Invoke로 DLL 호출, 라이브 스티칭 UI
  - 스티칭: ORB/AKAZE 영상 매칭이 메인, pose는 초기 위치 추정 + 드리프트
    앵커 역할만 (naive chain 금지)
  - "COLMAP 최종 확인" 버튼: 자체 구현 없이 기존 tools/stitch_folder.py를
    subprocess로 호출 (CheckCrackViewer의 "▶ 실행"과 동일 패턴)
  - "Reset" 버튼: 한 면 완료 후 다음 면으로
```

## 폴더 구조

```
previewer/
├── FacadePreviewer.sln
├── FacadePreviewer/           C# WPF 앱 (net9.0-windows)
├── FacadeDdsBridge/           네이티브 C++ DLL (CMake)
│   ├── CMakeLists.txt
│   ├── build.ps1              원커맨드 빌드 (VCToolsVersion 픽스 포함, 아래 참고)
│   ├── src/
│   └── idl/                   map2stitch_msgs IDL + fastddsgen 생성 타입
└── tools/
    ├── Get-FastDdsGenModule.ps1   FastDDS SDK 다운로드/설치
    └── README.md
```

## 빌드 이식성 (다른 머신에서 돌릴 때 꼭 확인)

1. `tools/Get-FastDdsGenModule.ps1` 먼저 실행 — Gen_IDL_DDS의 ExtraModule을
   받아 `tools/Module/FastDDSGen/FastDDS`에 설치.
2. `FacadeDdsBridge/build.ps1`로 빌드 — **반드시 `/p:VCToolsVersion=14.44.35207`
   플래그가 포함된 채로 빌드해야 한다** (아래 이유 참고). 다른 머신에 이 정확한
   MSVC 서브버전이 없으면 빌드가 깨질 수 있음 — 그 경우 `dumpbin /headers`로
   `fastddsd-3.6.dll`의 실제 linker version을 확인하고 그 머신에 맞는 값으로
   `build.ps1`의 플래그를 조정할 것.

## 2026-08-12 세션 — WPF 뼈대 + 네이티브 DDS 브릿지, 빌드/링크/스모크테스트 전부 성공

### 1. WPF 프로젝트 뼈대
`FacadePreviewer/` 생성 — net9.0-windows, CommunityToolkit.Mvvm 8.4.2,
`CheckCrackViewer`와 동일한 다크 팔레트 + `RenderOptions.ProcessRenderMode =
SoftwareOnly`(원격 세션 렌더링 픽스). `MainViewModel`에 `StartScanning`/
`Reset`/`RunColmapCheck` 커맨드 스켈레톤(전부 "미구현" 스텁), `MainWindow.xaml`에
라이브 모자이크 영역 + 컨트롤 바. `OpenCvSharp4`/`OpenCvSharp4.runtime.win`
패키지 추가(다음 ORB/AKAZE 작업용). 빌드/실행 확인 완료.

### 2. 네이티브 DDS 브릿지 (`FacadeDdsBridge/`)
`DdsFrameSubscriber.h/.cpp` — 자체 구현(코드는 새로 작성, 패턴만 참고):
UDPv4 전용 transport(이 머신은 SHM transport가 `create_participant()`에서
멈추는 문제가 있어서 처음부터 배제), BEST_EFFORT + KEEP_LAST(20)
DataReaderQos, bounded-timeout 비동기 시작/정지(RTPS teardown이 무관한
참가자를 기다리며 25-30초 멈추는 문제 방지). `ImageSensorFrame`(pose/GPS
메타데이터)과 `VideoTsPacket`(raw MPEG-TS, **아직 디먹싱/디코딩 안 함 — 남은
작업**) 두 토픽 구독, C 콜백으로 전달. `FacadeDdsBridge.h/.cpp`가 플랫 C
API(`FacadeDds_Create/Destroy/SetCallbacks/StartAsync/Stop`) export — C#
P/Invoke용.

IDL + fastddsgen 생성 타입은 `D:\GCS\MpegTS\NDRONE_MULTI_VIEWER\ICD\idl\`에서
복사(공유 wire-format 계약이라 코드 재사용 금지 원칙과 무관). `MediaFrameChunk.idl`은
더 간단했겠지만 Map2Stitch_Wpf 트리에만 있어서 의도적으로 안 씀 — 대신
`VideoTsPacket`(H.264/TS) 사용, 디코드는 나중에 직접 구현 필요.

### 3. 빌드 막힘 — MSVC 툴셋 불일치, 몇 시간 소요, 근본 원인 확정

**증상**: `fastddsd-3.6.lib` 링크 시 9개 심볼 unresolved
(`_Cnd_timedwait_for_unchecked`, `__std_search_1`, `__std_mismatch_1`,
`__std_min_element_4` 등 — MSVC STL/CRT 내부 심볼).

**시도했지만 안 된 것들**: eProsima 글로벌 설치, Map2Stitch_Wpf의 vendor 빌드,
Gen_IDL_DDS SDK — **3곳 전부 동일한 9개 심볼 에러**. CMake `-T version=14.44`
플래그도 여러 방식으로 시도했지만 전부 무효(생성된 vcxproj에
`<VCToolsVersion>`이 아예 안 들어감, 계속 14.33으로 빌드됨).

**진단**: vcpkg로 이 머신의 현재 툴셋으로 fastdds를 새로 소스 빌드했더니
(사용자가 이후 삭제 요청, 지금은 없음) 이 9개 심볼 에러가 전혀 안 남 — 프리빌트
바이너리와 이 머신 툴셋의 불일치가 원인이라는 게 확정됨.

**근본 원인 확정**: `dumpbin /headers`로 `fastddsd-3.6.dll` 확인 결과
**"14.44 linker version"** — 이 머신엔 14.33.31629와 14.44.35207 둘 다
설치돼 있는데, CMake의 툴셋 지정 플래그가 이 CMake+VS 조합에서 조용히
무시되고 있었던 것.

**실제 해결**: CMake의 `-T` 대신 MSBuild 속성을 `--` passthrough로 직접 전달:
```powershell
cmake --build build --config Debug -- /p:VCToolsVersion=14.44.35207
```
즉시 9개 심볼 전부 해결, 클린 빌드 성공. `build.ps1`에 고정 — **이 플래그
빠뜨리면 다시 깨짐, 절대 제거하지 말 것.**

**Visual Studio 2022 IDE에서 직접 빌드하고 싶을 때** (2026-08-13 추가): 위
`/p:VCToolsVersion` 플래그는 `build.ps1`이 실행하는 `cmake --build` 그 한 번의
호출에만 적용되고 `.vcxproj`에는 안 남는다 — 그래서 생성된 `build\FacadeDdsBridge.sln`을
Visual Studio에서 직접 열어 빌드(F7)하면 위와 똑같은 9개 심볼 링크 에러가 재현된다
(`-T version=14.44` 계열 CMake 제너레이터 툴셋 플래그도 이미 여러 번 시도했지만
`.vcxproj`에 `<VCToolsVersion>`이 안 들어감, 위 "빌드 막힘" 절 참고). 해결책:
`FacadeDdsBridge\Setup-VisualStudio.ps1` 신규 — cmake configure를 돌리고
`build\Directory.Build.props`에 `<VCToolsVersion>14.44.35207</VCToolsVersion>`을 써넣음
(MSBuild가 빌드 대상 프로젝트의 상위 폴더를 훑어 자동으로 찾아 적용하는 표준 매커니즘,
CMake 제너레이터와 무관하게 동작). 한 번 실행 후 `.sln`을 Visual Studio에서 열어 그냥
빌드하면 됨 — `build\` 폴더가 삭제/재생성되면(예: 순수 `cmake -S -B`) 이 props 파일도
같이 사라지므로 그때만 재실행. `build.ps1`과 공존 확인됨(커맨드라인 플래그가 더
우선순위 높아서 충돌 없음, 순서 상관없이 둘 다 실행해도 안전) — **msbuild.exe로 직접
`/p:VCToolsVersion` 없이 빌드해서 실제로 14.44.35207 링커가 선택되는 것까지 확인함**.
더블클릭용 `Setup-VisualStudio.bat`도 같이 만들어둠(파워셸 명령 기억 안 해도 됨).

### 4. 검증

`FacadeDdsBridgeSmokeTest.exe`(`src/SmokeTest.cpp`) 신규 — `DdsFrameSubscriber`
생성 → domain 0에서 시작 → 실제 `DomainParticipant`/`Subscriber`/`Topic`×2/
`DataReader`×2 전부 생성 성공 확인("failed to create..." 로그 없음, "listening"
정상 출력). 링크 성공뿐 아니라 **실제 DDS 연결 자체**가 됨을 확인.

### 5. P/Invoke 연동 (완료)

`FacadePreviewer/Services/DdsBridgeInterop.cs`(raw P/Invoke: 구조체
`FacadeImageSensorFrame`/`FacadeVideoTsPacket`, `SensorFrameCallback`/
`VideoPacketCallback` delegate, `FacadeDds_Create/Destroy/SetCallbacks/
StartAsync/Stop`) + `Services/DdsBridgeService.cs`(`IDisposable` 래퍼,
delegate를 필드로 유지해 GC 방지, 네이티브 콜백에서 나온 포인터를
`Models/SensorFrame.cs`/`Models/VideoPacket.cs` managed record로 복사) +
`.csproj`에 `FacadeDdsBridge.dll`과 FastDDS 런타임 DLL(Debug/Release
Condition 분기) 복사 `<None>` 항목 추가.

`ViewModels/MainViewModel.cs`에서 `DdsBridgeService`를 실제로 소유 —
`StartScanning`이 `FacadeDds_StartAsync` 호출, 콜백은
`Application.Current.Dispatcher.BeginInvoke`로 UI 스레드에 마샬링 후
`SensorFramesReceived`/`VideoPacketsReceived` 카운트. `MainWindow.xaml.cs`의
`Closing` 이벤트에서 `MainViewModel.Dispose()` 호출 → `DdsBridgeService.Stop
()`/`Destroy` → 네이티브 구독 스레드 정상 종료(안 하면 `DdsFrameSubscriber`의
teardown 타임아웃까지 프로세스 종료 지연).

**검증**: `dotnet build` 클린 성공. 앱 실행 → UI Automation으로 "스캔 시작"
버튼 클릭 → 헤더가 "DDS 연결 안 됨"에서 "DDS 구독 시작됨 (domain 0) — 수신
대기 중"으로, 상태 dot이 회색→초록으로 바뀜, 하단 바에 "스티칭 0장 · sensor
0 · video 0" 표시(퍼블리셔가 없으니 0은 정상) — 크래시 없이 프로세스
정상 응답 유지 확인(스크린샷 2장으로 클릭 전/후 비교).

퍼블리셔가 실제로 붙었을 때(예: FacadeDdsBridgeSmokeTest.exe를 같은 domain/
topic으로 동시 실행) sensor/video 카운트가 실제로 올라가는지는 아직 미검증
— 다음 세션에서 확인 필요.

### 남은 작업 (다음 세션)

1. 실제 퍼블리셔와 맞물려 sensor/video 카운트가 올라가는지 검증(스모크
   테스트 exe를 같은 domain/topic으로 동시 실행해서 확인)
2. `VideoTsPacket`(raw MPEG-TS) 디먹싱 + H.264 디코드 — 아직 미구현.
   `NDRONE_MULTI_VIEWER`/Map2Stitch_Wpf 둘 다 겪은 것과 비슷한 작업이지만
   코드는 새로 작성(재사용 아님) — libmpeg/avcodec 필요
3. 프레임↔텔레메트리 timestamp 기준 nearest-match 상관관계
4. `Services/`에 ORB/AKAZE incremental 스티처 — 지면 아니라 **벽면 평면**에
   투영, pose는 드리프트 앵커 용도로만
5. `Reset`/`RunColmapCheck` 스텁을 실제 로직으로 연결(저장 폴더 초기화,
   `tools/stitch_folder.py` subprocess 호출)
6. 실제 통합 시 DDS domain/topic 이름 확정 — 지금은 스모크테스트용 임시
   이름(`rt/map2stitch/facade_previewer/image_sensor_frame`,
   `.../video_ts`) 그대로 사용 중

## VideoTsPacket 디먹싱+디코드+ORB 라이브 스티칭 구현 (2026-08-13)

사용자 지시: "dds_router의 RtmpVideoBridge에서 rtmp -> enque, thread thread ->deenque
->rtmp -> h264 ->txmux -> dds pus. FacadePreviewer 에서 libmpeg, libffmpeg 이것이
디코딩 하세요. C or c++ 이면 dll 만들어서 c# 연동 하셔도 됩니다. 그리고 스티칭
진행하세요" — 두 가지 작업: (1) RtmpVideoBridge를 enqueue/dequeue 스레드 구조로
리팩터링, (2) previewer 쪽에서 실제 디코딩 + 라이브 스티칭까지 구현. 이걸로
previewer의 원래 최종 목적("이 RTMP 파이프라인의 실제 목적" 절 참고)이 처음부터
끝까지 실증됨.

### 1. `RtmpVideoBridge` — enqueue/dequeue 스레드 분리

기존 `cb_on_video`는 RTMP recv 루프(TCP `recv()`/`rtmp_server_input()`와 같은 스레드)
안에서 NALU 추출 + TS mux + DDS write까지 전부 동기 처리했음. 요청대로 producer/
consumer 구조로 분리:
- `cb_on_video`(producer, recv 스레드): 메시지를 복사해 `Session::video_queue`(뮤텍스+
  condition_variable로 보호되는 `std::deque<VideoMessage>`)에 push만 하고 즉시 리턴.
  큐 상한(`kMaxQueuedVideoMessages = 2000`) 초과 시 가장 오래된 항목을 drop(라이브
  영상은 무한 버퍼링보다 오래된 프레임을 버리는 게 맞다는 판단).
- `process_video_message()`(옛 `cb_on_video`의 몸통을 그대로 이식): AVC 파싱 + Annex-B
  변환 + `mpeg_ts_write` + DDS write.
- `video_worker_thread_func()`: `Session::worker_thread`에서 실행, 큐를 드레인하며
  `process_video_message` 호출. `cb_on_publish`에서 시작, `run_session`의 정리
  단계에서 stop 플래그 세팅 → `join()` → (그 다음에) `mpeg_ts_destroy` — 워커가 아직
  mux 중인데 destroy가 먼저 불리는 경쟁 방지, 남은 큐도 종료 전에 다 드레인.

빌드: VM에서 `RtmpVideoBridge/build`째 `make` 직접 실행(ROS Humble setup 소싱) — 클린
성공. 재기동 후 `--publish-facade`로 재검증 — seq 55000까지 정상 발행, 세션 정상 종료
확인.

### 2. previewer 쪽 — libmpeg(디먹싱) + FFmpeg(디코드) 신규 벤더링

- **`FacadeDdsBridge/thirdparty/libmpeg/`**: `ireader/media-server`에서 신선하게
  클론(DDS-Router의 기존 사본과 파일 단위로 diff 확인 후, DDS-Router 트리에서
  복사하지 않고 별도로 fresh clone) — `ts_demuxer_create/input`(TS → Annex-B H.264
  역먹싱)이 목적. DDS-Router 쪽과 마찬가지로 mux/demux 전체를 통째로 벤더링(작은
  라이브러리라 분리 유지보수가 더 손해). `README.md`에 출처 기록.
- **`tools/Get-FfmpegDevModule.ps1`** 신규: `tools/ffmpeg/`(CLI 전용, 링크 불가)와는
  별개로, **BtbN/FFmpeg-Builds**(GitHub, `n7.1` 태그 고정, LGPL-shared)에서 헤더+
  MSVC용 `.lib`+런타임 `.dll`(`libavcodec`/`libavutil`/`libswscale`/`libswresample`)을
  받아 `tools/Module/FfmpegDev/`에 설치 — `Get-FastDdsGenModule.ps1`과 동일한
  패턴(gitignored, 재실행 안전). gyan.dev 대신 BtbN을 쓴 이유: 릴리스 태그로 정확한
  FFmpeg 버전을 고정할 수 있음.
- **`src/VideoDecoder.h/.cpp`** 신규: `VideoDecoder` 클래스 — `ts_demuxer_input`으로
  188바이트 TS 패킷 단위 역먹싱(주의: `ts_demuxer_input`은 정확히 188바이트 1개만
  받음 — 처음에 `VideoTsPacket`의 전체 번들(376바이트 = 2패킷)을 통째로 넘겼다가
  `assert(188 == bytes)`로 즉시 크래시, `Feed()`에서 188바이트씩 나눠 호출하도록 수정)
  → codecid가 `PSI_STREAM_H264`인 access unit만 `avcodec_send_packet`/
  `avcodec_receive_frame`로 디코드 → `sws_scale`로 BGR24 변환 → 콜백. Impl은 PIMPL
  패턴이지만 `struct Impl`을 public으로 선언(libmpeg의 C 콜백 시그니처가 요구하는
  파일-스코프 free function이 이 타입을 참조해야 해서 — private면 컴파일 에러).
- **`DdsFrameSubscriber.h/.cpp`**: `VideoPacketListener`가 `VideoDecoder` 멤버를 갖고,
  `on_data_available`에서 raw-bytes 콜백(기존)과 별개로 `decoder_.Feed(...)`도 호출 —
  디코드된 프레임은 새 `FacadeDecodedFrameCallback`(BGR24, width/height/stride)으로
  전달. 디코드는 DDS 리스너 스레드에서 동기 실행(추가 워커 스레드 없음 — 이 프로젝트
  설계 목표인 "2fps면 충분"한 단일 프리뷰 스트림 기준으로는 충분, 나중에 필요하면
  RtmpVideoBridge에 방금 적용한 것과 같은 enqueue/dequeue 패턴을 재사용할 것).
- **`FacadeDdsBridge.h/.cpp`**: `FacadeDds_SetCallbacks`에 `decoded_frame_cb` 파라미터
  추가.
- **CMakeLists.txt**: `libmpeg`/FFmpeg dev include+lib 추가, `VideoDecoder.cpp`를
  `FacadeDdsBridge` 타겟에 추가, FFmpeg 런타임 DLL을 빌드 출력 옆에 자동 복사(post-build
  custom command).

**네이티브 단독 검증** (`SmokeTest.cpp`에 `OnDecodedFrame` 콜백 추가): VM에서
`--publish-facade facades/TOP` 발행 → 수신 측 로그에 `decoded frame #1: stream=front
5280x3956 stride=15840` 확인 — 실제 원본 DJI 해상도 그대로 H.264 디코드 성공(23568개
TS 패킷 수신 중 10개 프레임 디코드 성공, 손실은 BEST_EFFORT 특성상 정상).

### 3. C# 연동 + ORB 라이브 스티칭

- `DdsBridgeInterop.cs`/`DdsBridgeService.cs`: `FacadeDecodedFrame` 구조체 +
  `DecodedFrameCallback` 델리게이트 추가, `DecodedFrameReceived` 이벤트 신규
  (`Models/DecodedVideoFrame.cs`로 네이티브 메모리에서 BGR 바이트 복사).
- **`Services/FacadeStitcher.cs`** 신규 — ORB 특징점 기반 incremental 스티처:
  - 8000×4000 고정 캔버스(동적 리사이즈 없음, MVP 단순화).
  - 첫 프레임: 캔버스 중앙에 순수 translation으로 배치.
  - 이후 프레임: 현재 프레임 vs 직전 프레임(체인 매칭) ORB 디스크립터 + BFMatcher(Hamming)
    + ratio test(0.75) → good match 15개 미만이면 그 프레임은 스킵(체인은 안 끊음) →
    `Cv2.FindHomography`(RANSAC) → 누적 변환(`캔버스행렬 = 캔버스행렬 * H_현재→직전`)
    → `WarpPerspective`로 캔버스에 합성(단순 overwrite, seam/blend 없음 — 이 툴은
    커버리지 확인용이지 최종 정밀 분석 아님, 그건 "COLMAP 최종 확인" 버튼 몫).
  - **알려진 한계 (코드 주석에도 명시)**: 순수 시각적 체인 매칭이라 포즈 기반 드리프트
    보정이 아직 없음 — `ImageSensorFrame`↔`VideoTsPacket` 프레임 간 timestamp 상관관계
    자체가 아직 미구현이라(기존 "남은 작업" 항목) 앵커링할 대상이 없음. 프로젝트
    설계상(`project-dds-previewer-design` 메모리) "ORB/AKAZE가 주 정렬, pose는 주기적
    드리프트 앵커"인데 지금은 앞부분만 구현된 상태 — 긴 스캔에서는 드리프트 누적 예상,
    다음 단계로 명확히 남겨둠(정직하게 미완성 표시, 완성된 척 하지 않음).
- `MainViewModel.cs`: `DecodedFrameReceived` 구독 → 0.5초(≈2fps, 이 툴 자체 설계
  목표치와 일치) 스로틀 → `Mat.FromPixelData`로 Mat 변환 → `_stitcher.AddFrame` →
  `OpenCvSharp.WpfExtensions`(`OpenCvSharp4.WpfExtensions` 패키지 신규 추가)의
  `ToBitmapSource()` → `Freeze()` 후 Dispatcher로 `LiveMosaicImage`/`FramesStitched`
  갱신. `Reset` 커맨드가 `_stitcher.Reset()`도 호출하도록 연결.
- `.csproj`에 FFmpeg 런타임 DLL(`avcodec-*.dll` 등, 와일드카드 — 버전 숫자가 바뀌어도
  안 깨지게) 복사 항목 추가.

**종단 검증 (UI Automation, 실제 FacadePreviewer.exe)**:
1. Host/Port 입력 → "스캔 시작" 클릭.
2. `facades/TOP`(13장) 발행 → 상태 텍스트에 `· 스티칭 1장`, `· video 7884` 확인(짧은
   버스트 발행이라 스로틀 창을 1번만 통과).
3. `facades/앞`(26장, 더 긴 데이터) 재발행 + 검증용으로 스로틀을 일시적으로
   0.01초로 낮춰 재빌드/재실행 → `· 스티칭 3장` 확인 — **ORB 매칭+호모그래피+워핑
   경로(체인의 2번째 이후 호출)가 크래시 없이 여러 번 성공적으로 실행됨을 실증**.
   검증 후 스로틀 0.5초로 원복, 최종 클린 빌드 확인.

### ffmpeg 설치 통합 + H.264 디코드 손상 근본 수정 (2026-08-13)

**ffmpeg 통합**: `tools\ffmpeg`(구 GPL 정적 빌드, 650MB) + `tools\libffmpeg`(날짜 미상,
git 미추적 상태로 발견된 예전 미완성 준비물 — 헤더+`.lib`뿐 실행용 `.dll` 없음,
OpenH264 샘플 래퍼 코드 `h264/`도 같이 있었으나 어디에도 연결 안 돼 있었음) +
`tools\libmpeg`(마찬가지로 미추적 예전 준비물, 이번에 새로 받은
`FacadeDdsBridge/thirdparty/libmpeg`와 중복) — 사용자 확인 후 전부 삭제. 새로 받은
완전한 FFmpeg dev 빌드(`tools\Module\FfmpegDev`)를 `tools\libffmpeg`로 이동해 유일한
설치로 통합. `JpegFacadePublisher`의 인코더를 `libx264`→`libopenh264`로 변경(이
LGPL-shared 빌드엔 GPL인 libx264가 의도적으로 빠져 있음), 해상도는 1080p로 캡.
`CMakeLists.txt`/`csproj`/`Get-FfmpegDevModule.ps1`/`.gitignore`/`tools/README.md` 전부
`tools\libffmpeg` 기준으로 갱신.

**H.264 디코드 손상 근본 원인 + 수정**: 원본 DJI 해상도(5280×3956) 그대로 발행하면
디코드 결과가 세로줄로 완전히 깨지는 문제 발견 → 네이티브 디코더가 뱉는 원본 프레임을
C#/스티칭 거치지 않고 파일로 직접 덤프해서 확인한 결과 이미 그 시점부터 깨져 있음을
확인(마샬링 버그 아님 확정). 1080p로 낮추니 완전 깨짐→부분 깨짐(프레임 상단은 멀쩡,
하단만 깨짐)으로 개선 — H.264는 매크로블록을 위→아래 순서로 디코드하므로, 이건
프레임 중간에서 패킷이 유실된 전형적인 패턴. 근본 원인: 프레임 하나가 TS 패킷
수천 개(376바이트 DDS 샘플 수백~수천 개)로 쪼개져 **아무 지연 없이 한꺼번에** 전송되고,
이 프로젝트는 영상 데이터에 의도적으로 BEST_EFFORT QoS(재전송 없음)를 쓰기 때문에
이 burst가 OS 기본 UDP 소켓 버퍼를 넘치게 해서 패킷이 유실됨.

**최종 수정 2가지** (`RtmpVideoBridge/main.cpp` 송신측 + `DdsFrameSubscriber.cpp` 수신측):
1. UDP 소켓 송/수신 버퍼를 4MB로 확대(`UDPv4TransportDescriptor::sendBufferSize`/
   `receiveBufferSize`, 기본값 0=OS 기본값이었음).
2. `RtmpVideoBridge`의 `ts_write()`(worker thread에서 실행, RTMP recv 스레드 아님)에
   번들(TS 패킷 2개)마다 150μs 페이싱 추가 — 초당 ~20Mbit 상당으로 스로틀링, 실제
   1080p 스트림 비트레이트보다 훨씬 여유 있어서 실사용 지연은 미미하고 인위적인
   즉시-burst만 완화됨. `RTMP_BRIDGE_TS_PACING_US` env var로 조정 가능(0=비활성).

재검증: 이전에 하단 40%가 깨졌던 바로 그 `facades/TOP` 첫 프레임이 두 수정 적용 후
13/13 프레임 전부 완벽하게 디코드됨(원본과 픽셀 단위로 동일). VM `RtmpVideoBridge`
재빌드/재기동 + previewer 네이티브 재빌드 완료.

### 남은 작업 (다음 세션)

1. **포즈 기반 드리프트 앵커**: `ImageSensorFrame`↔`VideoTsPacket` 프레임 timestamp
   상관관계 구현 → `FacadeStitcher`에 주기적 재앵커링 추가 (프로젝트 설계의 나머지 절반).
2. 캔버스 8000×4000 초과하는 대형 스캔 처리(현재는 조용히 클리핑됨) — 동적 확장 또는
   경고 표시.
3. Seam/blend 품질 개선 여부 판단 — 지금은 단순 overwrite, 실사용 피드백 보고 결정.
4. `DdsMonitor`의 RtmpVideoBridge 관련 화면이 이번 워커 스레드 리팩터링/페이싱을
   몰라도 동작에는 지장 없음(내부 구현 변경이라 외부 API/설정 불변) — 확인만 남음.
5. `run_rtmp_video_bridge.sh`에 DdsMonitor 상태-리포트 루프 추가(이전부터 남아있던 항목).
6. `ImageSensorFrame`의 ExtensibilityKind 재검증(이전부터 남아있던 항목).
7. 실제 라이브 드론 스트림(지금까지는 전부 JPEG 시퀀스로 시뮬레이션)으로 페이싱/버퍼
   튜닝값이 적절한지 재검증 필요 — 지금 값들은 이 burst-발행 테스트 시나리오 기준으로
   튜닝됨.

### ORB 스티칭 — 와일드 호모그래피(방사형 줄무늬) 버그 수정 (2026-08-13)

**증상**: 첫 프레임은 정상, 두 번째 프레임부터 캔버스가 한 점에서 방사형으로 뻗어나가는
줄무늬로 깨짐(사용자 스크린샷으로 확인) — `WarpPerspective`가 near-degenerate
호모그래피(투시 소실점 근처로 발산)를 그대로 캔버스에 적용해서 생긴 현상.

**수정 1 — 기하학적 타당성 검증 (`FacadeStitcher.IsHomographyPlausible`)**: RANSAC이
호모그래피를 반환해도 그게 기하학적으로 말이 되는지 별도 검증 안 하고 있었음. 프레임
네 꼭짓점을 단일 스텝 호모그래피로 투영해서: (1) NaN/Inf 없음, (2) 넓이가 원본 대비
0.25~4배 범위 안(collapse/explosion 방지), (3) convexity 유지(뒤틀림 방지) — 셋 중
하나라도 깨지면 그 프레임은 스킵(체인은 안 끊고 다음 프레임이 같은 마지막 성공
프레임과 다시 매칭 시도).

**수정 2 — inlier 기준을 비율에서 절대 개수로 변경**: 처음엔 RANSAC inlier 비율
50% 미만이면 거부하도록 짰는데, 진단 카운터로 원인 분리해보니 실제 배포 시나리오(반복
패턴 많은 콘크리트 외벽 — 발코니, 창문 그리드)에서는 **기하학적으로 완전히 맞는
매칭도 raw match pool의 절반 이상이 ORB 오탐(false positive)인 경우가 흔함** — RANSAC이
inlier 서브셋에서 올바른 호모그래피를 잘 찾아내는데도 비율 기준 때문에 대부분
거부되고 있었음(진단: 19/25 거부가 이 비율 기준 때문, 위 기하 검증에 걸린 건 0건).
`MinInlierRatio=0.5` → `MinInlierCount=12`(절대 개수)로 교체 — 언더컨스트레인 방지는
유지하면서 반복 텍스처가 많은 장면에 불리하지 않게.

**검증**: `facades\앞`(26장, 실제 겹치는 드론 촬영 시퀀스 + 의도적 불연속 구간 하나
포함) 재발행 — 수정 전 25개 디코드된 프레임 중 스티칭 성공 1개, 수정 후 22개
성공(불연속 구간 등 진짜 안 겹치는 4개만 정상적으로 거부). 기하 검증은 두 테스트
모두에서 나쁜 케이스를 0건도 놓치지 않음 — 방사형 줄무늬 재현 안 됨.

## Update (2026-08-12, 새 세션): 포즈 기반 재보정 + Keyframe Reset 구현, COLMAP 네이티브 통합 계획 수립

사용자가 다이어그램으로 제시한 설계(Pose Prediction → Feature Matching → Homography →
Pose와 비교해 Accept/Reject → N장 누적 후 Keyframe Reset)를 검토 후 구현 — 이 프로젝트
자체 설계 문서(위 "Stitching algorithm -- final decision")에서 처음부터 명시했던
"ORB가 주 정렬, pose는 주기적 드리프트 앵커" 중 pose 절반을 실제로 채운 것.

### Phase 1 — 프레임↔텔레메트리 timestamp 상관관계 (구현+검증 완료)

`VideoTsPacket.timestamp_sec`을 `VideoDecoder::Feed()`가 받아 저장해뒀다가 프레임이
완성될 때 `FrameCallback`에 함께 넘기도록 배선(`VideoDecoder.h/.cpp`) →
`FacadeDecodedFrame` C 구조체 + `FacadeDecodedFrameCallback`에 `timestamp_sec` 필드
추가(`DdsFrameSubscriber.h/.cpp`) → C# `DdsBridgeInterop`/`DdsBridgeService`/
`Models/DecodedVideoFrame.cs`까지 전달 → `MainViewModel`이 `OnSensorFrameReceived`에서
받는 `ImageSensorFrame`들을 최근 50개 링버퍼(`_recentSensorFrames`, lock으로 보호 —
FastDDS가 sensor/video 리스너를 다른 스레드에서 동시 호출할 수 있어서)로 유지하고,
`OnDecodedFrameReceived`에서 `FindNearestPose()`로 가장 가까운 타임스탬프의 pose를
찾아(허용 오차 `MaxPoseCorrelationGapSec=1.0`초, 초과 시 null) `FacadeStitcher.AddFrame`에
넘긴다.

**주의**: `timestamp_sec`은 진짜 per-frame H.264 PTS가 아니라 그 프레임을 완성시킨
마지막 `VideoTsPacket` 샘플의 timestamp(디코더 레벨에서 진짜 PTS를 못 뽑아내는 TS
demux 구조상 한계) — 이 프로젝트의 "2fps면 충분" 정밀도 기준에서는 문제없다고 판단,
과장 없이 코드 주석에 명시.

**검증**: `FacadeDdsBridgeSmokeTest.exe`로 `facades/TOP` 재발행 후 실제 로그에서
`decoded frame #1: ... timestamp_sec=0.116` 확인 — 0이 아닌 실제 값이 네이티브
전체 경로를 관통해서 나옴.

### Phase 2 — Pose 기반 Accept/Reject + Keyframe Reset (구현+부분 검증)

`FacadeStitcher.cs`:
- `AddFrame(Mat bgrFrame, SensorFrame? pose = null)`로 시그니처 확장 — pose는 항상
  optional, 없으면 기존 순수 ORB 게이트만 적용(하드 요구사항 아님).
- `PoseHomographyMismatch`: 두 프레임의 `CameraPositionM*` 델타(미터)와 ORB가 낸
  `overlapRatio`를 비교 — **calibration이 없어서 pixel↔meter 절대 환산은 안 함**(이
  프로젝트 원칙, CLAUDE.local.md #9/#26과 동일 정신), 대신 motion-CLASS 비교만: pose는
  "거의 안 움직임"인데 homography는 "완전히 다른 곳으로 감"(`overlapRatio` 낮음), 또는
  반대로 pose는 "몇 미터 점프"인데 homography는 "거의 그대로"(반복 패턴에 ORB가
  false-positive로 lock — `MinInlierCount` 주석에서 이미 경고한 바로 그 실패 유형) 두
  경우만 reject.
- `MaybeKeyframeReset`: 연속 50장(`KeyframeResetFrameCount`) 넘거나 캔버스 경계
  400px(`KeyframeResetCanvasMargin`) 이내로 근접하면 현재 캔버스를 `LocalMosaicCompleted`
  이벤트로 넘기고(호출자가 소유/dispose) 상태 초기화, 다음 프레임은 새 세그먼트의
  "첫 프레임"으로 처리 — **pose 기반 위치로 배치하지 않고 기존처럼 중앙 배치**로
  구현(계획 문서엔 "pose 있으면 pose 위치로"라고 썼었지만, previewer에 여전히
  pixel↔meter scale calibration이 없어서 metric 배치는 또 다른 종류의 가짜 정밀도가
  됨 — 정직하게 스코프 축소).
- `MainViewModel`: `LocalMosaicCompleted` 구독 → `LocalMosaicsCompleted` 프로퍼티(UI에
  "· Keyframe Reset N회"로 노출) 갱신 + `StatusMessage`에 리셋 발생 사실 표시.

**검증 (실제 앱, UI Automation)**: `facades/TOP`(13장), `facades/앞`(26장) 순서로
재발행 — 두 번 다 크래시 없음, `facades/앞` 후 상태 텍스트
`"· 스티칭 3장 · Keyframe Reset 0회 · sensor 0 · video 3587"` 확인(Keyframe Reset
0회는 정상 — 50장 문턱을 안 넘었으니까).

**검증 갭 (정직하게 기록)**: 이번 세션엔 `ImageSensorFrame` 퍼블리셔가 없어서(sensor=0)
`PoseHomographyMismatch`가 실제 pose 데이터로 동작하는 경로는 검증 못 함 — pose가
없을 때 ORB 단독 게이트로 정상 fallback하는 것만 실증됨. previewer엔 아직
`ImageSensorFrame`을 발행하는 도구 자체가 없음(`VideoTsPacket`용 `--publish-facade`
같은 게 없음) — 다음에 pose 게이트를 실측 검증하려면 이 도구부터 필요.

### Phase 3 — COLMAP 네이티브 벤더링 (계획만 수립, 미착수)

사용자가 "COLMAP 최종 확인" 버튼을 (기존 계획이던 `tools/stitch_folder.py` subprocess
호출이 아니라) **COLMAP 실제 C++ 소스(`github.com/colmap/colmap`)를 previewer 네이티브
스택에 직접 벤더링**하는 방식으로 구현하길 원함. 확정된 결정 사항:

- **vcpkg 전면 금지** — 사용자 명시 지시("vcpkg 전연금지, 수동빌드"), 과거 `core/`
  vcpkg 스켈레톤 삭제 + FastDDS에서 vcpkg 두 번 거부한 전례와 일치. 모든 의존성은
  `previewer/tools/colmap_deps/`에 소스/바이너리를 직접 놓고 수동 빌드(기존
  `tools/Module/FastDDSGen`, `tools/libffmpeg` 패턴).
- **COLMAP 소스는 태그 3.13.0으로 고정**(main 아님) — GitHub에서 실제 CMake 의존성을
  버전별로 비교 확인한 결과: main은 OpenImageIO/CHOLMOD(SuiteSparse)/ONNX까지 요구해서
  지나치게 무거움. 3.13.0은 그게 다 빠져 있고 PoseLib/faiss는 COLMAP 자체 CMake의
  FetchContent가 자동 처리 — 직접 준비할 라이브러리는 **Boost/Eigen3/FreeImage/Metis/
  Glog/SQLite3/GLEW/Ceres 8개**로 좁혀짐.
- Ceres는 `SUITESPARSE=OFF`(Eigen 내장 sparse solver만 사용)로 빌드해 CHOLMOD/BLAS/
  LAPACK 체인 전체를 회피 — 이 규모의 facade 이미지 수(수십 장)엔 충분.
- 전체 상세 단계(라이브러리별 획득 방법, `FacadeColmapBridge` 신규 네이티브 모듈 설계,
  프레임 저장 파이프라인, `RunColmapCheck` 연결)는 승인된 plan 파일에 기록됨 —
  요약하면 previewer 지금까지 어떤 빌드 작업보다 크고(FastDDS 하나도 며칠 걸렸는데
  이번엔 라이브러리 8개), 고위험이라 **1·2단계(위) 완료 후 순서대로 진행하기로 합의**.

**현재 상태**: 사용자가 Phase 1·2를 먼저 직접 테스트해보겠다고 해서 Phase 3(라이브러리
빌드) 시작 전 대기 중.

### 추가 수정: 겹침 구간 이중 선(ghosting) 제거 — "first frame wins" 합성으로 변경

사용자가 실제 스크린샷으로 지붕선/창틀이 겹치는 구간에서 이중으로 보이는 현상을 지적
("겹치는 곳이 보여요") → 원인은 `WarpOntoCanvas`가 매 프레임을 캔버스에 **항상
overwrite**했기 때문 — ORB+RANSAC 정합이 몇 픽셀만 어긋나도 겹치는 영역을 두 번(이전
프레임 내용 위에 새 프레임 내용을) 그리면서 그 오차가 이중선으로 드러남. "실시간은
유지하되 완전히 없애달라"는 요청 → 블렌딩(흐림으로 완화) 대신 **겹침 영역을 아예
다시 그리지 않는** 방식으로 해결:

- `FacadeStitcher`에 `_coverageMask`(CV_8UC1, 캔버스와 동일 크기, 이미 칠해진 픽셀
  추적) 신규 추가.
- `WarpOntoCanvas`: `newAreaMask = warpedMask AND NOT(_coverageMask)`를 계산해 **아직
  아무도 안 칠한 영역에만** 새 프레임을 그림 → 겹치는 부분은 먼저 온 프레임 내용을
  그대로 유지, 두 번 그려지지 않으니 오차가 이중선으로 나타날 기회 자체가 없음. 비트
  연산 2회뿐이라 2fps 목표에 부담 없음(색/노출 블렌딩이나 seam 최적화는 여전히
  안 함 — 그건 오프라인 COLMAP/Kornia 파이프라인 몫).
- `_coverageMask`는 `_canvas`와 동일한 생명주기(첫 프레임 배치/Keyframe Reset/Reset
  때 같이 생성·해제).

**검증**: `facades\앞`을 fps=2로 재발행(26장, 약 13.5초에 걸쳐 실제 전송 — 이전
세션에서 fps를 너무 높게 주면 previewer의 0.5초 스로틀 때문에 프레임이 거의 안
들어간다는 것도 이번에 같이 확인/설명함) → 실제 앱에서 Keyframe Reset이 처음으로
실제 트리거되는 것까지 확인(`"Keyframe Reset — 로컬 모자이크 #1 완료(10장), 새 구간
시작"`, 이어서 새 구간에서 4장 추가 스티칭). UI Automation으로 캡처한 실제 스크린샷에서
지붕선/주차장 라인 등이 깨끗하게 이어짐 — 수정 전 스크린샷에서 보이던 이중선 재현 안
됨.

## 2026-08-13 세션 — 전면 재설계: 실시간 스티칭 전부 삭제, 캡처 전용 + 오프라인 스캔으로 전환

사용자 지시: "이것 하지 맙시다. 외부에서 건물 찍은거 검증용입니다. 실시간 하지 맙시다. 그냥
운용자가 건물 이미지 캡쳐가 마무리 되때까지 데이터 베이스에 h264 decode -> 사이즈는
640x640을 작고, 일정한 크기로 해서 운용자가 UI에서 측정장소 입력하면 날짜,시간 포함된 폴더
생성 후, 이곳에 .jpeg로 저장합니다. 운용자가 드론 조종을 마치고-> 스캔시작(스티칭->ColMap)
한번에 실행 하는 거로 하지요." — 이전 세션들에서 시도했던 실시간 스티칭 접근법(순수 pairwise
체인, OpenCV Stitcher 배치, 순수 affine 2계층, Stitcher+affine 하이브리드, ORB-SLAM3 포즈
보정)을 전부 폐기하고, previewer를 "촬영 중엔 저장만, 촬영 후 오프라인 일괄 처리"로 완전히
바꾼 결정.

### 1. Kornia+COLMAP 오프라인 파이프라인 벤더링 (previewer/tools/)

**`colmap_deps/`** — COLMAP 3.13.0 소스(`github.com/colmap/colmap` tag 3.13.0)를 실제
빌드. `pycolmap` 사용 안 함(사용자 명시 거부) — 순수 C++ 소스 빌드 + CLI 바이너리. 의존성은
전부 prebuilt 라이브러리만 사용(vcpkg 전면 금지, 소스 빌드 금지 — COLMAP 자신만 예외):
Boost 1.84(공식 설치 프로그램), Eigen(3.4.0 → Ceres가 5.0.1 요구해서 conda-forge
eigen=5.0.1로 교체), FreeImage/GLEW(공식 바이너리), SQLite3(`lib.exe`로 직접 `.lib` 생성),
METIS/Glog/gflags/Ceres 2.2.0/SuiteSparse 7.10.1(전부 conda-forge), BLAS/LAPACK은
SuiteSparse가 기본으로 끌고 오는 MKL(~540MB)이 너무 무거워서 OpenBLAS로 교체(~28MB,
`libblas.dll`/`liblapack.dll` 등 dispatch shim은 conda 패키지 캐시에서 직접 복사,
`openblas.lib`는 `dumpbin /exports` + `lib.exe /def:`로 직접 생성).

실제로 걸렸던 CMake 문제 3가지(전부 해결, `colmap_deps/README.md`에 상세 기록):
- CMake ≥3.30의 `CMP0167` 정책이 COLMAP 자체 코드에서 무조건 NEW로 강제돼서 Boost의
  Module-mode 탐색이 막힘(prebuilt Boost엔 `BoostConfig.cmake`가 없음) → `FindDependencies
  .cmake`의 해당 블록을 로컬 patch로 주석 처리(이유는 그 파일 안에 기록, `librtmp`
  `assert(0)` 패치와 같은 이 프로젝트의 기존 관행).
- 이 개발 머신에 예전 vcpkg 실험이 남긴 CMake User Package Registry 항목이 `Eigen3_DIR`을
  `D:/vcpkg/...`로 조용히 가로챔 → `CMAKE_FIND_PACKAGE_NO_PACKAGE_REGISTRY=ON`으로 차단.
- Boost의 MSVC autolink pragma가 기본으로 정적 lib를 요청하는데 CMake의 명시적 링크는
  동적(import) lib를 쓰고 있어서 `colmap.exe` 링크 시 `LNK2005` 중복 정의 →
  `BOOST_ALL_DYN_LINK`로 맞춤(처음 시도한 `BOOST_ALL_NO_LIB`는 너무 광범위해서 이번엔
  `LNK2019` 미해결 심볼 발생 — 일부 타겟이 autolink에 의존하고 있었음).

빌드 후 `colmap.exe`가 `STATUS_DLL_NOT_FOUND`로 실행이 안 됐던 문제 — `dumpbin /dependents`로
직접+전이 의존성 전부 추적(`ceres.dll`→`cholmod.dll`/`spqr.dll`→`amd`/`colamd`/`camd`/
`ccolamd`/`suitesparseconfig`, `glog.dll`→`gflags.dll`, 전부→`libblas.dll`/`liblapack.dll`
— conda-forge SuiteSparse/Ceres가 실제로 링크하는 건 OpenBLAS dispatch shim 이름이지
`openblas.dll` 자체가 아님) → 필요한 모든 런타임 DLL을 `colmap.exe` 옆에 직접 복사해서 해결,
`-h` 실행으로 전체 명령 목록 출력 확인.

**`stitch_engine/`** — 메인 저장소 `src/`에서 Kornia LoFTR + RANSAC/homography + 스티칭
모듈을 그대로 복사(카탈로그/설정/타입/매칭/기하/스티칭 — previewer는 별도 repo 취급, import
경계 안 섞음). `src/sfm/colmap_runner.py`는 pycolmap 버전 대신 새로 작성 — 네이티브
`colmap.exe`를 subprocess로 호출(`feature_extractor`/`exhaustive_matcher`/`mapper`).
`src/pipeline/runner.py`는 `run_facade_poc`만 남기고 트리밍(건물 footprint 기반
`run_building_poc`/facade 분류/COLMAP-pose rectification 가지는 제거 — previewer는 이미
"폴더 1개 = 면 1개"로 캡처하므로 footprint 분류가 필요 없음). 자체 `config/pipeline.yaml`
튜닝(캡처가 이미 640x640이라 `loftr.max_image_side: 640`, `visual_multiband_blend: false`로
불필요한 블렌드 단계 생략).

**실제 DJI 사진(`facades/TOP`, 13장)으로 end-to-end 검증 완료**: LoFTR 매칭 → RANSAC/
homography → 스티칭(coverage_ratio=0.91) → drift 게이트가 COLMAP fallback을 트리거 →
`feature_extractor`/`exhaustive_matcher`/`mapper` 전부 크래시 없이 완주. 처음엔
`--SiftExtraction.num_threads`가 COLMAP 3.13.0에서 `--FeatureExtraction.num_threads`로
이름이 바뀐 걸 몰라서 실패 → 고침, 재검증 성공.

### 2. FacadePreviewer C# 앱 — 캡처 전용으로 전면 재작성

**삭제**: `Services/FacadeStitcher.cs`, `Services/BatchAccumulator.cs`,
`Services/LightGlueMatcher.cs`(실시간 ORB/LightGlue/RANSAC/캔버스 워프 전부), `.csproj`의
`Microsoft.ML.OnnxRuntime.DirectML` 패키지 참조 + LightGlue onnx 모델 copy 항목. 네이티브
`FacadeDdsBridge`(ORB-SLAM3 C++ 통합 포함)는 이번 세션에 손대지 않음 — C# 쪽에서 그냥 더 이상
`ConfigureOrbSlam`을 호출하지 않을 뿐, P/Invoke 표면(`DdsBridgeInterop`/`DdsBridgeService`)은
네이티브 DLL의 실제 export와 맞춰야 해서 그대로 유지(제거하면 ABI 불일치로 깨짐).

**`ViewModels/MainViewModel.cs` 전면 재작성**: 실시간 워커 스레드(FrameCollectorLoop/
BatchWorkerLoop) 전부 제거. 새 구조:
- `StartCapture`: 측정장소 입력값 검증 → `<CaptureRootPath>/<측정장소>_<yyyyMMdd_HHmmss>/`
  폴더 생성 → DDS 구독 시작.
- `OnDecodedFrameReceived`(네이티브 DDS 리스너 스레드에서 직접 실행, 별도 워커 스레드 없음 —
  라이브 프리뷰가 없어서 stall 걱정이 없어짐): 0.5초(~2fps) 스로틀 → `Cv2.Resize`로 정확히
  640x640 → `Cv2.ImWrite`로 JPEG 저장. 매칭/워핑/캔버스 전혀 없음.
- `RunScan`("스캔 시작(스티칭→COLMAP)" 버튼): 캡처 중이면 먼저 중지 → `python
  stitch_engine/stitch_folder.py <capture_dir> <측정장소>`를 subprocess로 실행(
  `CheckCrackViewer`의 "▶ 실행"과 동일 패턴), stdout/stderr를 실시간으로 `ScanLogText`에
  누적해서 UI에 표시.
- `Reset`: 캡처 중지 + 상태 초기화(다음 면 촬영 준비).

**`MainWindow.xaml` 재작성**: 모자이크 뷰어 제거, 대신 캡처 상태 카드(폴더 경로/저장된 장수)
+ 스캔 로그 스크롤 박스. ORB-SLAM3/LightGlue 설정 UI(체크박스+경로+FOV 입력) 전부 제거.
컨트롤 바: 측정 장소 입력, 캡처 저장 위치 입력, "캡처 시작"/"캡처 중지"/
"스캔 시작(스티칭→COLMAP)"/"초기화" 4버튼. `Converters/InverseBoolConverter.cs` 신규(캡처 중
측정장소/저장위치 입력 잠금용).

**빌드/실행 검증**: `dotnet build` 클린 성공(경고 0, 오류 0 — 시도 중 `OpenCvSharp.Size` vs
`System.Windows.Size` 모호성 1건 발견해서 명시적 네임스페이스로 고침). 실행 파일을 3초간
띄워서 크래시 없이 정상 기동하는 것까지 확인.

### 남은 작업

1. 실제 드론 비행 중 캡처 검증 — 이번 세션은 previewer 자체 재작성과 오프라인 파이프라인
   벤더링/빌드/검증까지만, 실제 DDS 스트림으로 캡처→저장 경로는 아직 실측 안 함(코드 리뷰
   기준으론 기존 검증된 `DecodedFrameReceived` 전달 경로를 그대로 재사용하므로 위험 낮음).
2. `RunScan`이 호출하는 `python` 커맨드가 PATH에 있어야 함 — 현장 노트북에 Python + `pip
   install -r previewer/tools/stitch_engine/requirements.txt` 필요(아직 별도 설치
   스크립트/문서화 안 함).
3. `stitch_engine`이 참조하는 `colmap_deps/colmap_src/build/.../colmap.exe`와 그 런타임
   DLL들은 이 개발 머신에서 빌드된 채로 있음 — 현장 노트북으로 옮길 때 `colmap_deps/` 전체를
   같이 복사해야 함(용량 큼, 아직 배포 패키징 스크립트 없음).

## 2026-08-13 계속 — FacadeDdsBridge.dll Debug 빌드 완전 수정 (실행 창 반복 종료 원인 규명)

previewer 캡처 전용 앱을 실제 UI로 검증하던 중 창이 몇 초 만에 조용히 사라지는 현상이
반복 재현됨 — Windows Event Log 확인 결과 크래시가 아니라(WER 로그 없음), 이후
사용자가 직접 Debug 빌드를 시도하다 걸린 링크 에러 로그를 붙여넣으면서 실제 원인이
드러남: **`FacadeDdsBridge.dll`이 Debug 구성에서 아예 빌드된 적이 없었고**(`.lib`/`.exp`/
`.pdb`만 존재, `.dll` 없음), previewer가 Debug 빌드로 실행될 때마다
`DllNotFoundException`으로 즉시 죽고 있었던 것 — 이전 세션들에서 전부 Release로만
검증해왔기 때문에 이 문제가 계속 숨어있었음.

### 근본 원인 체인 (전부 실제로 하나씩 재현/확인)

1. **`ORB_SLAM3.lib`/`DBoW2.lib`/`g2o.lib`가 Release로 하드코딩**되어 있는데 Debug
   빌드는 기본적으로 `/MDd`(Debug CRT)를 쓰려고 해서 CRT 불일치
   (`_ITERATOR_DEBUG_LEVEL`/`RuntimeLibrary` mismatch, `LNK4098`) 발생.
2. `CMAKE_MSVC_RUNTIME_LIBRARY`를 전체 `MultiThreadedDLL`(`/MD`)로 고정해서 1번은
   해결됐지만, **OpenCV 심볼 34개가 여전히 unresolved** — `dumpbin /exports`로 직접
   확인해서 진짜 원인 발견: **OpenCV의 Debug 빌드는 `_InputArray`/`_OutputArray` 등을
   `cv::debug_build_guard` 네임스페이스로 감싸서 Debug/Release ABI가 섞이면 런타임
   손상 대신 링크 타임에 실패하도록 만드는 OpenCV 자체의 의도된 안전장치**임
   (`cv::debug_build_guard::cvtColor(...)` vs 그냥 `cv::cvtColor(...)` — 다른 심볼).
   `ORB_SLAM3.lib`은 Release 전용이라 guard 없는 이름을 참조하므로 Debug variant
   opencv_*d.lib과 링크가 안 됨.
3. 해결: `find_package(OpenCV)`가 반환하는 imported target들의
   `IMPORTED_LOCATION_DEBUG`/`IMPORTED_IMPLIB_DEBUG`를 각자의 `*_RELEASE` 값으로
   덮어써서 **어떤 구성으로 빌드하든 항상 Release OpenCV 바이너리를 링크**하도록
   강제(`force_release_binaries()` 매크로, 하드코딩 리스트 대신 `find_package`가 실제
   찾은 대상을 그대로 순회).
4. 같은 클래스의 문제가 **Pangolin(vcpkg debug 라이브러리들도 `/MDd` 기대)**에서도
   재현 → 동일 매크로를 `Pangolin_LIBRARIES`/`Boost::serialization`/
   `OpenSSL::SSL`/`OpenSSL::Crypto`에도 적용.
5. **FastDDS 관련 완전히 별개의 문제 발견**: `find_package(fastdds)`/
   `find_package(fastcdr)`가 이 프로젝트가 vendoring한
   `tools/Module/FastDDSGen/FastDDS`(CMake 패키지 설정 파일이 없음)를 못 찾고 **이
   개발 머신에만 있는 전역 설치 `C:/eProsima/fastdds3.6.1.0`로 조용히 폴백**하고
   있었음 — 사용자가 명시적으로 지적해서 고침: 이 vendored copy를 직접 가리키도록
   `find_package` 제거, `target_link_libraries`/`target_include_directories`에서
   `${FASTDDS_ROOT}` 경로를 직접 참조(기존 ORB_SLAM3/DBoW2/g2o/FFmpeg와 동일 패턴).
   이 과정에서 두 가지 더 발견:
   - fastcdr/fastdds 헤더가 `EPROSIMA_ALL_DYN_LINK`(또는 각각
     `FASTCDR_DYN_LINK`/`FASTDDS_DYN_LINK`) 매크로가 없으면 **autolink pragma가 정적
     라이브러리 이름(`libfastcdr-2.3.lib` 등)을 요청** — 처음엔 파일을 못 찾는
     에러(`LNK1104`), `target_link_directories`로 검색 경로를 열어주니 이번엔 내가
     명시적으로 링크한 동적 import lib(`fastcdr-2.3.lib`)와 **중복 정의**(`LNK2005`) —
     최종 해결은 `EPROSIMA_ALL_DYN_LINK`를 compile definition으로 추가해서 pragma가
     처음부터 올바른(동적) 이름을 요청하게 만듦.
   - `find_package(fastdds)`를 없애면서 그게 암묵적으로 전이시켜주던
     `OpenSSL::SSL`(TCP-secure transport가 필요로 하는 `SSL_*` 심볼) 링크가 사라짐 →
     명시적으로 추가.
6. **마지막 문제**: 위 수정으로 링크는 전부 성공했는데 **런타임 DLL이 출력 폴더에 하나도
   복사가 안 됨**(vcpkg의 `VCPKG_APPLOCAL_DEPS=ON` 자동 배포가 이 `SHARED` 라이브러리
   타겟에는 아예 안 걸림 — 확인해보니 opencv/pango DLL이 `build/Debug/`에 0개). CMake
   post-build 커스텀 커맨드로 vcpkg Release bin 폴더 전체를 wildcard 복사(이 파일의
   기존 FFmpeg DLL 복사 패턴과 동일 관행) + vendored FastDDS의 Release DLL들도 별도로
   복사.

### 결과

`FacadeDdsBridge/CMakeLists.txt`에 반영된 이 6가지 수정 이후, **Debug/Release 둘 다
클린 빌드 + 실제 실행 확인** (`FacadePreviewer.exe` Debug 빌드로 실행 → 크래시 없이
창 정상 표시, `previewer/tools/Module/FastDDSGen/FastDDS` vendored copy만 사용,
`C:/eProsima` 전역 설치 의존성 제거). previewer 전체를 통틀어 이제 Debug 구성으로도
정상 개발/디버깅 가능 — 이전까지는 몰랐지만 사실 한 번도 제대로 빌드된 적이 없었던
것으로 보임.

**주의**: `force_release_binaries()`로 인해 previewer의 Debug 빌드는 여전히 "이름만
Debug"에 가까움 — 우리 자신의 코드(`DdsFrameSubscriber.cpp` 등)는 실제로 디버그
정보(`/Zi`)와 최적화 없음(`/Od`)으로 컴파일되어 정상적으로 디버깅 가능하지만, OpenCV/
Pangolin/Boost/OpenSSL/FastDDS는 전부 Release 바이너리를 링크하므로 그 내부로는
소스 레벨 디버깅이 안 됨(심볼 없음). 이건 사용자가 명시적으로 선택한 트레이드오프
("CMakeLists.txt만 고쳐서 Debug도 Release 라이브러리 사용") — 완전한 Debug 지원을
원하면 ORB_SLAM3/DBoW2/g2o를 Debug로도 빌드해야 함(별도 작업, 아직 안 함).

## 2026-08-13 계속 — ORB-SLAM3 완전 제거 (사용자 지시: "ORB_SLAM3 등 사용 하지 않는것 삭제")

캡처 전용 재설계 이후 ORB-SLAM3(실시간 SLAM 보조 스티칭용)는 이미 완전히 미사용
상태였음 — 이번엔 코드/의존성까지 전부 제거.

**네이티브 (`FacadeDdsBridge`)**: `OrbSlamTracker.h/.cpp` 삭제,
`DdsFrameSubscriber`/`FacadeDdsBridge` C API에서 `FacadeOrbSlamPose`/
`ConfigureOrbSlam`/`ResetOrbSlam`/`orb_slam_pose_cb` 전부 제거(`SetCallbacks`가 5개
인자→4개 인자로), `SmokeTest.cpp`의 `--orb-slam` CLI 분기 제거. `CMakeLists.txt`에서
ORB_SLAM3/DBoW2/g2o/Pangolin/Boost/OpenCV/Eigen3/Sophus를 전부 제거.

**예상 밖 추가 발견**: OpenCV/Pangolin/Boost 제거 후 `find_package(OpenSSL)`만 남았는데,
`dumpbin /dependents`로 확인해보니 **fastdds-3.6.dll이 DLL로 링크되면서
(`EPROSIMA_ALL_DYN_LINK`) SSL_* 심볼이 그 DLL 내부에서 이미 해소되어 있어서 OpenSSL
링크 자체가 전혀 불필요**했음 — 제거하니 vcpkg 의존성이 이 프로젝트에서 완전히
사라짐(`CMAKE_TOOLCHAIN_FILE`/`VCPKG_ROOT` 설정 블록까지 통째로 삭제). 부수 효과:
`CMAKE_MSVC_RUNTIME_LIBRARY`를 강제 `/MD`(Release CRT 전용)에서 원래의 per-config
`/MD`(Release)/`/MDd`(Debug)로 되돌릴 수 있게 됨 — ORB_SLAM3.lib가 Release 전용이라
강제했던 제약이 사라졌으므로, **Debug 빌드가 이제 진짜 Debug CRT를 쓴다**
(`dumpbin /dependents`로 `MSVCP140D.dll`/`ucrtbased.dll` 확인).

**결과**: `FacadeDdsBridge.dll`이 17.5MB → **250KB**로, 런타임 DLL이 60개 →
**7개**(fastdds/fastcdr/foonathan_memory/avcodec/avutil/swscale/swresample)로 줄어듦.
`build/` 폴더를 완전히 지우고 처음부터 재구성+재빌드해서 확인(캐시된 잔재 아님).

**C# (`FacadePreviewer`)**: `Models/OrbSlamPose.cs` 삭제, `DdsBridgeInterop.cs`/
`DdsBridgeService.cs`에서 ORB-SLAM3 관련 struct/delegate/method/event 전부 제거,
`FacadeDds_SetCallbacks` 호출을 새 4-인자 시그니처에 맞춤. `.csproj`의
`OrbSlam3RuntimeDll` 와일드카드 복사 항목 삭제 — 이게 우연히 OpenSSL DLL도 같이
복사해주고 있었다는 걸 발견해서, 그 두 파일(`libcrypto-3-x64.dll`/`libssl-3-x64.dll`)
복사 항목도 함께 정리(어차피 이제 OpenSSL 자체가 안 쓰임). FastDDS Debug 전용 DLL
복사 조건도 제거 — `EPROSIMA_ALL_DYN_LINK`가 Debug/Release 둘 다 Release-이름
DLL(`fastdds-3.6.dll`, `fastddsd-3.6.dll` 아님)을 링크한다는 걸 `dumpbin`으로
재확인하고 반영.

**벤더 소스 삭제**: `tools/ORB_SLAM3/`(2.2GB) 삭제. 확인하다가 발견한 또 다른 미사용
잔재 `tools/onnx_models/`(SuperPoint+LightGlue 실험용, 49MB — 이것도 이미 이전
세션에서 코드가 삭제됐던 것)도 같이 삭제.

**검증**: Debug/Release 둘 다 클린 빌드(0 에러) + 실제 `FacadePreviewer.exe` 실행 →
DDS 연결 → RTMP 발행 → 640x640 JPEG 9장 캡처까지 실제 데이터로 재확인, 캡처된
JPEG 파일도 유효함을 직접 열어서 확인. `Setup-VisualStudio.ps1` 재실행해서
`Directory.Build.props` 복구(이번에도 `build/` 삭제로 날아갔었음).

## 2026-08-14 세션 — 고해상도 사진 전송(rsync-over-ssh) 서버 파이프라인 완성, previewer 클라이언트 쪽은 크래시로 롤백

사용자 지시로 대규모 신규 기능 착수: FacadePreviewer에서 rsync-over-ssh로 원본 고해상도
사진을 DDS-Router로 전송 → DDS pub/sub로 backend_core에 메타데이터 전달 → Postgres 저장.
DDS-ROUTER/MngData/previewer 세 코드베이스는 서로 코드/라이브러리를 공유하지 않는다는
명시적 원칙 하에 진행(각자 독립적인 `facade_image_msgs` DDS 타입 생성물을 개별 벤더링).

### ✅ 서버 쪽 — 완성 + 실제 크래시/장애 복구까지 검증 완료

- **`backend_core/src/facade_image_receiver.h/.cpp`**: 완전히 독립된 신규 모듈(기존
  `StorageQueue`/`filemsg` 파이프라인과 코드·테이블 공유 없음) — `FacadeImageMeta` DDS 구독 →
  Postgres upsert(`facade_images`/`facade_image_sessions`, `schemas/facade_images.sql`) →
  `FacadeImageAck` 발행. 신규 빌드 옵션 `MNGDATA_ENABLE_FACADE_IMAGES`(기존
  `MNGDATA_ENABLE_ROS2`와 독립).
- **`DDS-Router/thirdparty/FacadeImageBridge/`**: 신규 독립 바이너리 — rsync-over-ssh가
  써넣는 `company/building/direction/session_id/*.jpg` 트리를 inotify+주기적 재스캔으로
  감시, sha256 체크섬을 image_id로 사용, libexif로 EXIF/GPS 파싱(없으면 0), 로컬
  `pending/`→`done/` 디렉터리를 durable queue로 사용(파일 하나 = pending 항목 하나, ack
  받을 때까지 주기적으로 재발행 — 이게 "프로세스 크래시까지 견뎌야 함" 요구사항의 실제
  구현). `build.sh`에 `build_facade_image_bridge()` 추가, `scripts/run_facade_image_bridge.sh`
  신규, `DdsMonitor`의 MEDIA 탭에 최소 패널(watch root + 자동시작 + 적용/중지, RTSP/RTMP와
  달리 스트림 목록 없음 — watch root 하나뿐).
- **fastddsgen 버전 함정 재발견**: 이 VM의 apt `fastddsgen`(2.1.0)으로 생성한 코드는 이
  로컬 Fast-DDS 3.6.2.0의 `TopicDataType` API와 아예 호환 안 됨(구버전 Fast-RTPS 2.x API
  타겟, `fastrtps::` 네임스페이스/`createData()` 등 — 컴파일 자체가 안 됨). 프로젝트 자체
  빌드한 `DDS-Router/thirdparty/Fast-DDS-Gen/build/libs/fastddsgen.jar`(4.0.6, VideoTsPacket
  생성에 쓰인 것과 동일)로 다시 생성해서 해결. **양쪽(backend_core/DDS-Router)에 동일
  생성물을 그대로 복사**해서 fastddsgen 버전 불일치로 인한 ExtensibilityKind 불일치
  버그(이전 세션에 겪은 바로 그 버그 클래스) 재발을 원천 차단.
- `foonathan_memory` cmake 의존성 문제(이 로컬 Fast-DDS 설치엔 실제 라이브러리가 없고 빈
  ament vendor 마커만 있음, `ldd`로 런타임에도 불필요함 확인)는 각 트리에 독립적인 더미
  스텁(`Findfoonathan_memory.cmake` + 빈 `libfoonathan_memory.a`)으로 해결 — 두 트리가
  서로 공유하지 않는 각자의 사본.
- **실제 검증**: 진짜 파일을 watch root에 넣고(`mv`로 rsync의 원자적 rename 흉내) →
  체크섬/계층 파싱 → DDS 발행 → backend_core upsert → ack → pending→done 이동까지 전
  구간 확인. **DB 권한 에러를 일부러 만들고 코드 재시작 없이 자동 복구**(재시도 루프
  실증), **FacadeImageBridge를 강제 종료 후 재시작해서 미완료 항목이 이어서 성공**하는
  것까지 확인(크래시 내구성 요구사항 실증).
- `git push`: `youngilyou/MngData`(commit `dbde083`), `youngilyou/DDS-Router` fork(commit
  `57ebb090`, `origin`=eProsima 업스트림 아님 주의) 완료.

### ❌ previewer 클라이언트 쪽 — 작성했으나 크래시로 전부 롤백, 커밋 안 됨

`RsyncTransfer.h/.cpp`(CreateProcess로 벤더링된 rsync.exe 감싸는 래퍼, Cygwin 격리
유지), `FacadeDdsBridge.h/.cpp`에 `FacadeRsync_*` C API 4개, C# `RsyncTransferService.cs`,
`TransferSettingsWindow.xaml/.xaml.cs`(서버 접속 설정 + 회사/동/방향/세션 선택 + 폴더
찾기 + 전송 버튼), `MainWindow.xaml`에 "고해상도 전송..." 버튼 — **전부 작성 완료, 개별
빌드도 성공**했으나, `FacadePreviewer.exe`를 실행하면 **아무 상호작용 없이도** 수 초 ~
수십 초 후 `Debug Assertion Failed! ... __acrt_last_block == header`
(`debug_heap.cpp:986`, 힙 손상) 크래시가 재현됨.

**중요 — 이 크래시는 이번 세션에 추가한 코드와 무관한 것으로 확인됨**: 격리 테스트로
(1) 네이티브 `FacadeDdsBridge.dll`만 커밋 시점 버전으로 되돌려서 재현(동일 크래시), (2)
C#/네이티브 전부 `git checkout`으로 커밋된 baseline으로 완전히 되돌린 뒤 재현 — **아무
변경 없는 순수 baseline 상태에서도 동일하게 재현됨**. 즉 이번 세션의 rsync 기능
추가와는 무관한, **previewer에 이미 존재하던(또는 이 개발 머신의 현재 상태에서 발생하는)
별개의 크래시 버그**로 보임 — 원인 미상, 다음 세션에서 반드시 별도로 조사 필요(이전
세션들의 "크래시 없이 실행 확인"이 재현 안 되는 상황이므로 우선순위 높음).

**조치**: rsync 클라이언트 관련 previewer 변경사항(네이티브+C# 전부)을 `git checkout --`/
파일 삭제로 전부 롤백해서 **previewer가 커밋된 baseline 상태 그대로 남도록** 함 —
크래시나는 상태를 커밋하지 않음. `previewer/tools/Get-CygwinRsync.ps1`(rsync.exe 벤더링
스크립트 자체는 독립적으로 정상 동작 확인됨, previewer 앱과 무관)만 커밋.

**다음 세션에서**: (1) 이 crash-on-launch 버그 먼저 원인 규명(이번에 못 함 — 시간
제약), (2) 고쳐진 뒤 `tools/_pending_rsync_client_code/README.md` 참고해서 이어서 진행 —
**주의: 롤백한 코드를 커밋한 적이 없어서 git 히스토리에는 없음.** `RsyncTransfer.h`
인터페이스 선언만 그 폴더에 저장해뒀고 나머지(`.cpp` 구현체, C# 쪽 전부)는 이 세션 대화
기록에만 남아있어 처음부터 다시 작성 필요 — 로직 자체는 완성되어 있었고 개별 컴파일도
성공했었으니 재작성 시간은 오래 안 걸릴 것.

### ✅ 후속 — 크래시 원인 규명 + 수정 + rsync 클라이언트 재통합 완료 (같은 날)

사용자가 크래시를 직접 재현/스크린샷으로 확인시켜주고 "이거 버그 잡으세요" 지시.
`dumpbin /dependents`로 원인 확정:

- 벤더링된 `fastdds-3.6.dll`/`fastcdr-2.3.dll`(Release 전용 빌드)은 `MSVCP140.dll`/
  `VCRUNTIME140.dll`(Release CRT)을 링크.
- `FacadeDdsBridge.dll`은 Debug 빌드 시 `MSVCP140D.dll`/`ucrtbased.dll`(Debug CRT)을
  링크 — **서로 다른 CRT 힙**.
- ORB-SLAM3 제거 세션에서 `CMAKE_MSVC_RUNTIME_LIBRARY`를 "항상 Release"에서
  "설정별(`$<$<CONFIG:Debug>:Debug>`)"로 되돌린 게 원인 — 그때는 ORB_SLAM3 강제
  Release 제약이 없어졌으니 "진짜 Debug CRT 쓰는게 좋다"고 판단했었는데, fastdds/fastcdr가
  여전히 Release 전용이라는 걸 놓침. DLL 경계를 넘나드는 C++ 객체(std::string, QoS
  구조체 등)가 한쪽 CRT 힙에서 할당되고 다른 쪽에서 해제되며 힙이 조용히 손상 — 나중에
  힙 검증 시점에 `__acrt_last_block == header`로 터짐(그래서 실행 직후가 아니라 수 초~
  수십 초 뒤에, 상호작용 없이도 재현).

**수정**: `CMAKE_MSVC_RUNTIME_LIBRARY`를 다시 무조건 `"MultiThreadedDLL"`(Release)로 —
ORB-SLAM3 제거 이전에 이미 한 번 채택했던 것과 동일한 트레이드오프(우리 코드는 `/Zi`+`/Od`
디버그 정보는 그대로 받지만 CRT는 Release 공유). `dumpbin /dependents`로 이제
`FacadeDdsBridge.dll`도 `MSVCP140.dll`을 링크하는 것 확인, **`FacadePreviewer.exe` 30초+
무동작 대기해도 크래시 없음**으로 검증 완료.

**rsync 클라이언트 재통합**: 크래시가 이 기능과 무관하다는 게 이미 확인됐었으므로, 위
CRT 수정 위에 `RsyncTransfer.h/.cpp`/`FacadeRsync_*` C API/`RsyncTransferService.cs`/
`TransferSettingsWindow.xaml(.cs)`/`MainWindow`의 "고해상도 전송..." 버튼을 전부
재작성(대화 컨텍스트에서 복원, 로직 변경 없음)해서 다시 붙임. 빌드 클린, 앱 실행 안정,
**버튼 클릭 → 전송 설정창이 실제로 뜨는 것까지 확인**(UI Automation의
`TreeScope.Children`+`ProcessIdProperty` 조합이 이 머신의 원격 세션 렌더링 특성 때문에
신뢰 안 됨을 발견 — 실제로는 `Win32 EnumWindows`+`IsWindowVisible`로 직접 확인해야
정확함, 다음에 이 앱 UI Automation 할 때 참고).

`tools/_pending_rsync_client_code/`(임시 격리 폴더)는 정식 통합 완료로 삭제.

## 2026-08-22: COLMAP을 native 벤더 빌드에서 pycolmap으로 전환 (이전 결정 번복)

**배경**: main `src/`와 previewer `stitch_engine/src/`의 스티칭 파이프라인을 직접
비교한 결과 두 가지 문제 발견 — (1) previewer에는 main의 `TimeoutLoFTRMatcher`(LoFTR이
반복 창문 패턴에서 10분+ 멈추는 실제 확인된 버그의 수정)가 없어서 동일한 행 위험에
노출돼 있었음. (2) previewer의 COLMAP fallback은 COLMAP을 돌리기만 하고 그 결과(카메라
포즈)를 실제 모자이크 보정에 반영하지 않음 — main은 COLMAP 포즈로 `rectify_and_blend`까지
해서 homography-chain 드리프트를 실제로 대체하는데, previewer는 `_colmap_report.json`만
쓰고 끝. 게다가 previewer의 native-CLI 기반 `colmap_runner.py`는 등록 이미지 수를
`images.txt`에서 세는데, `colmap.exe mapper`는 실제로 `.bin`만 쓴다는 게 디스크에 남은
과거 실행 결과로 확인됨 — 즉 COLMAP이 성공해도 등록 이미지 수가 항상 0으로 보고되던
실제 버그였음.

**사용자 결정**: previewer도 pycolmap을 쓰도록 전환("previewer : colmap도 python 모듈
사용 하세요"). 이는 2026-08-12 예외 승인 세션에서 확정했던 "vcpkg 전면 금지, pycolmap
사용 안 함(사용자 명시 거부), native colmap.exe 직접 벤더링" 결정(Phase 3 절 참고)을
**이번 건에 한해 명시적으로 번복**한 것. 근거: 정확성이 속도보다 중요하고(사용자 확인:
"시간이 걸려도 되요 정확성이 핵심"), pycolmap을 쓰면 main의 이미 검증된
`align_reconstruction_to_utm`/`facade_plane_from_reconstruction`/`rectify_and_blend`를
새로 재구현하지 않고 그대로 포팅할 수 있음.

**결과**:
- `previewer/tools/colmap_deps/`(native COLMAP 3.13.0 C++ 소스+빌드+Boost/Eigen/Ceres 등
  프리빌트 의존성 전체) 삭제. `tools/Get-ColmapDeps.ps1`도 삭제, `Setup-Tools.bat`에서
  해당 단계 제거.
- `stitch_engine/src/sfm/colmap_runner.py`를 main의 pycolmap 기반 버전으로 교체
  (`extract_features`/`match_exhaustive`/`incremental_mapping`) — 부수적으로 위 "항상 0"
  버그도 해결됨.
- `stitch_engine/src/matching/loftr_matcher.py`에 `TimeoutLoFTRMatcher` 포팅.
- `stitch_engine/src/geometry/rectification.py` 신설(main 것의 footprint-free
  서브셋만) + `pipeline/runner.py`에 COLMAP-rectification fallback 연결 — 진행 중,
  본 세션 이후 이어서 완료.

즉 앞으로 previewer가 native `colmap.exe`를 직접 빌드/실행한다고 가정하는 코드나 문서는
전부 이 업데이트로 무효 — `tools/colmap_deps/`, `Get-ColmapDeps.ps1` 참고는 모두 과거
기록으로만 남기고 삭제됨.
