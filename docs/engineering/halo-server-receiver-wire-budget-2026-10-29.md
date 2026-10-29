# Classic HomeKit codec and wire budgets

This isolated candidate separates the selected H.264 codec ceiling from local
protected-UDP pacing. It keeps original source receipts, the 500 ms new-video-AU
deadline, whole-AU reservation, actual-IDR dependency recovery, source/epoch
checks after waits, cryptographic index ownership and bounded cleanup.

## Why the policies differ

The Apple-authored classic HAP R2 specification, Table 9-10, describes the selected
maximum as codec kbps averaged over one second. It does not specify that every
RTP header, SRTP tag and IDR must be serialized at that rate. Primary document:
[classic HAP R2, public mirror](https://forum.iobroker.net/assets/uploads/files/1634848447889-apple-spezifikation-homekit.pdf),
PDF page 205. This is the classic contract; the later HEVC/multitier compatibility
guide is a different generation.

The recorded synthetic full-target 299 kbps fixture has a protected IDR of
18,942 bytes: 506.8 ms under the former codec-rate wire clock, or 75.8 ms at the
new 2 Mbps local peak. Those are local serialization models, not measured NIC,
Wi-Fi, WAN or Apple arrival times.

## Concrete policy

- Local protected-wire peak: `min(64 Mbps, max(2 Mbps, 4 * selected codec bps))`.
  This explicit bounded default is independent of the selected codec average.
  It is not a path-capacity estimate or network-performance guarantee.
- Every datagram is still paced at that finite peak. Idle time banks no tokens.
  An entire AU reserves the same send lock, so repairs cannot steal its slots.
- The dry-run reservation includes the complete protected AU, including its
  final datagram's modeled service. Maximum 4096 datagrams and 2 MiB + 128 KiB
  protected bytes, in addition to the existing 2 MiB encoded AU cap.
- Pending work, source age and modeled service must fit the original
  `received_at + 500 ms` deadline. Every actual send rechecks authority, current
  capture epoch and deadline after pacing or a blocked-socket retry. No frame
  receipts are refreshed. No new frame queue is added.
- Existing sender/session limits, 250 ms socket-retry cap, dependency recovery,
  feedback authentication, 128 feedback datagrams/s, 64 repair packets/s and
  the 512-packet / 1 MiB cache limits remain.

These are software admission and pacing bounds. They cannot prove physical
delivery by a deadline or eliminate preemption between a check and a syscall.
At most eight existing views bound the aggregate configured peak; no new
congestion estimator or automatic network capacity claim is introduced.

## Codec validation is independent

`CodecRateMonitor` records generated Annex-B access-unit bytes, including output
that is later skipped, against `(source_pts - 90000, source_pts]`. It excludes
RTP/SRTP overhead and retransmissions, and never uses receipt or departure bursts
to infer codec rate. This is source-time measurement, not a wall-time wire rate.
It has 128 metadata rows, bounded counters, explicit partial/complete coverage,
and unknown-window counts after backwards timestamps or excessive density.
Same-PTS outputs add bytes; they cannot overwrite the earlier contribution.
Actual epoch or configuration changes start a distinct measurement domain.

The observer does not enforce a dropping limit. That is deliberate staging:
existing recorded full-target / 1 s-VBV 30 fps moving fixtures have peak source
second payload rates of 176.3 and 444.3 kbps at selected 132 and 299 kbps. Nominal
ABR/VBV settings are not proof of every rolling one-second window. Repeatedly
dropping reference pictures at that boundary would create another recovery
loop. The current encoder remains unchanged in this candidate; a separately
qualified encoder change must measure its actual codec windows before claiming
selection compliance. The observer itself does not certify Home compatibility.

`video_format` retains its existing selected-configuration meaning. Additional
`actual_source_format` and `actual_output_format` expose bounded, parsed adapter
SPS metadata, with SPS-declared frame-rate rational when present. These are not
measured displayed FPS or a claim that requested format equals source format.

## Qualification and remaining limits

The candidate tests actual manager/SRTP ownership and independent picture decode,
finite peak spacing, original deadline oversleep, blocked UDP, revoked authority,
whole-AU no-index rejection, recovery and exact cached-ciphertext retransmission.
Existing synthetic video lifecycle, provenance, native moving-source and public
HAP -> real IPC -> native UDP -> independent decode tests are also exercised.

No real Ring/Home stream, server change or physical quality claim is part of this
isolated gate. The measured roughly 700 ms upstream media quiet periods precede
this sender and are not explained or repaired by changing local pacing.

Repairs intentionally remain a separate follow-up. The present cache still
inherits the original source deadline, and the feedback reader still awaits
paced repairs. A later bounded repair queue should use exact ciphertext with a
one-second successful-send cache age, separate new-byte/repair accounting and
prompt BYE handling. This candidate does not claim that original R5 repair plan
is complete.

Audio is outside this patch. The separately tested shared 1.2 s original-audio
contribution/output-age candidate trades a finite additional age budget for
continuity across measured incoming gaps, without relabeling receipt times or
raising its existing 32-packet / 40 KiB ownership limits. A narrower history-only
change would need separate contribution/expiry tests and cannot be inferred
from changing the shared maximum-age constant.
