#pragma once

// Minimal Win32 GUI for FacadeDdsBridgeSmokeTest's "--publish-facade" feature (see
// JpegFacadePublisher.h) -- a folder picker plus 발행/정지/리셋 (publish/stop/reset) buttons,
// added directly to this exe per explicit request ("FacadeDdsBridgeSmokeTest 이곳에 ui
// 넣으시고") rather than as a separate app. Runs its own message loop; returns once the window
// is closed.
//
// RTMP URL/fps/keep_flv are fixed defaults (see PublishUi.cpp) -- the only user input is the
// folder to publish, matching the requested scope exactly (발행/정지/리셋 + 폴더 선택, nothing
// else).
bool RunPublishUi();
