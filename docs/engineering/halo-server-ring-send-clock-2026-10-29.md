# Relative Ring sender-clock observations

R7b established periodic incoming gaps before HomeKit conversion. It did not
establish whether Ring emitted the packets in bursts or whether delivery after
Ring's sending endpoint bunched them. This addition uses the already-negotiated
RTP abs-send-time extension when authenticated primary packets actually carry
it. It does not change negotiation, packets, buffering, media or speaker policy.

The existing pinned aiortc instance callback sees parsed authenticated RTP.
Known current primary codec payloads are admitted; RTX and unknown payloads are
excluded and break timing continuity. Audio and video each retain only one
numeric previous reference. A comparison requires consecutive sequence numbers
on the same source, a monotonic callback clock, and sender and callback intervals
within ten seconds. Missing/invalid extensions, source changes, sequence gaps,
reordering, large jumps and clock resets have explicit bounded counters. A
missing extension is unknown timing, never a sender clock of zero.

The extension is a 24-bit 6.18 clock with a 64-second wrap. Modular differences
handle its wrap; the ten-second bounds avoid inferring across ambiguous long
intervals. Fixed histograms record sender advancement, callback advancement,
and the positive difference in each direction. Separate histograms cover
callback gaps of at least 500 ms. No absolute timestamp, SSRC, packet, SDP,
address or payload is exported. Optional diagnostics cannot change the original
receiver callback outcome, and retirement clears references and status.

A long callback gap with little sender-clock advancement is evidence of
bunching after that timestamp boundary. Similar advancement at both boundaries
is consistent with a gap already present there. The timestamp may be set or
replaced by a cloud relay, so neither observation locates a physical camera
boundary or measures absolute network delay. RTP loss/repair exclusions and
extension-presence counts must accompany any conclusion. Actual extension
presence and physical burst localization remain unverified until a new pilot.

The initial review found that the callback precedes aiortc's RTX conversion and
unknown-payload rejection. A pair of consecutive RTX packets could therefore
produce misleading primary-stream timing. The explicit current-codec gate
corrected this, and the independent real-receiver counterexample now reports
excluded packets and zero comparisons. Focused tests also cover both wraps,
missing data, clock resets, lifecycle fencing, strict status sanitization and
unchanged real receiver assembly under diagnostic failures.

Primary format reference: [WebRTC Absolute Send Time](https://webrtc.googlesource.com/src/+/refs/heads/main/docs/native-code/rtp-hdrext/abs-send-time/README.md).
The installed parser and callback contract are pinned to aiortc 1.15.0.
