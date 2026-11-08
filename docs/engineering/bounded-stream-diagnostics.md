# Bounded stream diagnostics

This opt-in owner tool observes one camera's existing live-view pipeline. Starting
it does not start Ring media, change HomeKit settings, enable HKSV, or alter codec,
jitter-buffer, filtering, pacing, or latency policy. A later ordinary viewer opens
the stream. Recording starts only after an explicit local owner command and ends
independently after at most 300 seconds.

## Why this evidence is needed

The November 7 floodlight session had continuing incoming packets while assembled
audio briefly stopped reaching the consumer. Aggregate counters cannot identify
the exact sequence gap, jitter-buffer decision, codec output, and send deadline
for the same incident. Smoothness at Apple's display also cannot be inferred from
successful server sends or receiver reports alone.

The new trace correlates these boundaries on the server's monotonic clock:

1. Received decrypted RTP payload, sequence, timestamp and codec, before assembly.
2. Audio jitter-buffer occupancy, missing slots, prefetch and release decisions.
3. Completed encoded source frames and source generation/attempt/epoch mapping.
4. Consumer reads, conversion timing, encoded output and timestamp mapping.
5. HomeKit packet preparation, scheduled sends, successful sends and RTCP reports.
6. Stream start/end and diagnostic writer loss/cleanup state.

Source sender-report RTP/NTP pairs are numeric observations; invalid or incoherent
reports do not change the production clock mapping. Stream ordinals are local to
the diagnostic capture. No account labels, camera IDs, SDP, IP addresses, SSRCs,
pairing data, tokens or SRTP keys are written into the timing/media records.

## Controlled lifecycle

Keep Ring and Apple Home live views closed. Use the daemon's local owner socket:

```sh
halo-server stream-capture start CAMERA_ID --seconds 240
halo-server stream-capture status
```

Save the returned `capture_id`. Starting while an upstream or media consumer is
active is rejected. The operation is idempotent when retried with the same ID and
parameters (`--capture-id UUID`). It records only the authorized camera/account
generation and revokes admission if that authority changes.

Collect a closed baseline, open Apple Home for about two minutes, then close it
and collect roughly 25 seconds of cleanup. Full-session metadata continues during
cleanup. A detected audio assembly hold of at least 350 ms triggers a payload
excerpt; a manual mark can select an earlier observed problem:

```sh
halo-server stream-capture mark CAPTURE_ID
halo-server stream-capture stop CAPTURE_ID
```

The first trigger preserves up to ten seconds before and after it. If no anomaly
or manual mark occurred, the live stream ending preserves its final excerpt.
Without any stream-end trigger, stopping/deadline preserves the final ten seconds
available. Only the first trigger selects an excerpt; metadata covers the entire
capture subject to the limits below. No view needs to remain open after the
requested test interval.

## Bounded storage and failure isolation

Artifacts are under the daemon state directory's `diagnostic-captures/UUID/`:

- `owner.json`: capture and release provenance, without camera/account identity.
- `events.jsonl`: timestamped, allowlisted numeric/enum metadata.
- `media.bin` and `media.jsonl`: exact encoded payload bytes and indexed metadata.
- `manifest.json`: limits, counters, trigger, final status and file SHA-256 hashes.

Directories are private (0700), files private (0600), and symbolic-link paths are
rejected. Each capture permits up to 64 MiB of media, 96 MiB of event metadata,
16 MiB of media index, and 500,000 events. The producer queue is bounded by 8 MiB
and 8,192 items; the rolling excerpt buffer is also bounded. At least 384 MiB free
space is required to start. Three retained capture directories are permitted;
archive them explicitly before collecting more. Restart never resumes a capture.

Packet callbacks do not perform filesystem I/O or wait for diagnostic queue or
authority locks. Contention, capacity loss, malformed metadata and writer errors
are counted and make `trace_complete` false. Stop fences producers before waiting
for the writer; a stuck writer reports pending cleanup and blocks another capture.
The writer has a bounded join and cannot prevent process exit. A failed diagnostic
operation must not change media admission or incident/lease housekeeping.

These files contain actual camera audio/video. Keep them outside Git and public
reports. The observer records the Ring payload as received; it cannot undo audio
processing already applied by the camera or Ring.

## Offline inspection and replay

Use the project's qualified Python environment on a private copied capture:

```sh
python -m halo_server.media.diagnostic_replay inspect /private/capture
python -m halo_server.media.diagnostic_replay replay-audio /private/capture
python -m halo_server.media.diagnostic_replay export-audio /private/capture \
  --output-directory /private/new-audio-export
```

The reader verifies private regular files, manifest hashes and media index bounds.
It carries diagnostic losses into analysis instead of treating missing evidence
as a clean stream. Audio replay feeds the captured RTP sequence and relative
arrival times through the pinned aiortc receiver without networking or sleeps.
It reports assembled-frame hashes and jitter-buffer decisions. Independent Opus
decoding exports source/output WAV files with timing sidecars; WAV sample playback
alone does not preserve or prove packet arrival timing.

An excerpt begins with an empty replay buffer, not the original receiver's entire
prior state. H264 excerpts may lack SPS/PPS or an earlier reference/keyframe and
are not guaranteed to be independently decodable. The index preserves the exact
bytes for later analysis; do not label an undecodable excerpt as corrupted output.

## Qualification boundaries

Tests cover real receiver assembly, exact media/PTS equivalence with diagnostics
disabled/enabled/throwing, scope changes, lock contention, caps, deadlines, trigger
windows, writer failures, private artifact validation and offline replay. Synthetic
producer benchmarks measure observer cost on the desktop; they do not establish
overhead on the server or smoothness in Apple Home.

The next acceptance step is one fresh closed/open/running/closed camera session,
with matching service/socket measurements. Correlate source arrival, assembly,
conversion and send intervals before changing media behavior. Apple rendering
still needs an aligned device-side observation or recording; server-side evidence
cannot show exactly what the Apple decoder displayed or played.
