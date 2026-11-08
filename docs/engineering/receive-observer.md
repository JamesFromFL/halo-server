# Bounded kernel receive observer

`python -m halo_server.media.receive_observer` is a standalone opt-in diagnostic.
It opens no camera, performs no discovery, and does not use, replace, or compete
for Halo/aioice's UDP socket. It has no send path, no promiscuous mode, and no
persistent service. Running Halo never enables it automatically.

Use this to correlate kernel software receive timestamps with the existing
validated/decrypted source RTP events. The observer supplies the missing boundary
between packets reaching the server kernel and Halo's UDP/RTP callbacks. Its
timestamp is not NIC hardware or physical wire time and does not identify which
upstream hop caused a delay. Phone VPN effects on the HomeKit leg are a separate
measurement question; select the incoming Ring flow for this diagnostic.

## Selection and execution

An operator must supply the current selected ICE flow: interface, local IPv4
address/port, remote IPv4 address/port, and negotiated dynamic RTP payload types.
These must come from the actual selected transport, not a guessed camera address.
Do not select a TURN relay and label it direct SRTP. The tool does not authenticate
RTP or bind itself to an account, device or source epoch. Stop it if that source
owner retires or the selected flow changes. It never follows a new flow silently.

Validation opens no socket and creates no file. Example addresses are documentation
addresses, not discoverable defaults:

```sh
python -m halo_server.media.receive_observer \
  --interface eth0 --local-address 192.0.2.10 --local-port 40000 \
  --remote-address 198.51.100.20 --remote-port 50000 \
  --payload-type 105 --payload-type 111 --transport direct-srtp --check
```

For an explicitly authorized measurement, replace `--check` with
`--seconds 120 --output /absolute/private-parent/new-run`. The parent must exist
without symlink components, be owned by the invoking user, and not be writable by
group or others. Creation is anchored to that checked parent directory descriptor;
the final directory must not already exist. Administrator execution therefore
needs an administrator-owned parent. The
process needs Linux `CAP_NET_RAW` (or administrator execution). Missing permission
produces a finite error and exits; there is no password prompt or automatic
privilege escalation. Do not grant persistent packet-capture capabilities to a
general Python interpreter. No privileged or physical capture was performed as
part of implementing this tool.

## Filtering and resource limits

The packet socket starts with protocol zero, so receives nothing before its exact
four-tuple and payload-type BPF filter is attached and locked. Only then is it
bound to the selected Ethernet interface. It observes incoming packets only.
The kernel truncates admitted packets after the fixed twelve-byte RTP header,
using the actual IPv4 header length; media payload bytes do not enter the observer.
Inline Ethernet VLAN nesting of zero, one or two tags is supported. Hardware VLAN
offload may strip tags before observation; `vlan_depth` describes visible tags.

Supported coverage is IPv4 direct SRTP over Ethernet with configured dynamic RTP
payload types 96–127. IPv6, IP fragments, RTCP/STUN/DTLS, TURN, TCP, unsupported link
types, other flows/types and deeper VLAN encapsulation are outside coverage.
Declared TURN/TCP/IPv6 inputs fail before capture. Unsupported packets filtered
out by BPF cannot be counted; zero observations are never reported as a complete
trace. This is not a complete capture of every protocol on the selected interface.

The limits are 300 seconds, 100,000 admitted valid headers and 32 MiB of header
JSONL, plus a small fixed-schema summary. Defaults are 120 seconds and 100,000
headers. The socket receive buffer request is 256 KiB; its effective kernel size
is reported. Userspace file buffering is 64 KiB. The receive wait is at most
200 ms; normal duration completion may include that wait and final local-file
writes. Ctrl-C closes the observer and marks the result partial. Slow storage can
cause observer drops; it does not backpressure the camera socket, and drops are
reported. Kernel/filesystem system-call completion is not a hard realtime guarantee.

## Artifact interpretation

`headers.jsonl` and `summary.json` are exclusive files with mode 0600 inside a new
0700 directory. Nothing is overwritten. The summary includes byte count and
SHA-256 of the header file. It contains no IP addresses, ports, raw SSRC, keys,
payloads, camera/account identifiers or device names.

Header rows contain per-run numeric track ordinal, payload type, sequence, RTP
timestamp, marker, declared datagram sizes and kernel realtime/user-observation
monotonic timestamps. `srtp_bytes` includes SRTP authentication/trailer overhead;
it is not the decrypted media length. SSRC/type pairs are mapped in memory only.
A second SSRC with the same type explicitly marks source identity ambiguous.
Match headers to authenticated source events by type, sequence, timestamp and
time range; unvalidated headers alone do not prove camera identity or delivery.

Start, approximately one-second, and final clock anchors relate realtime to
monotonic time with a before/after uncertainty bracket. Never subtract these clock
domains directly. A detected offset step greater than 5 ms marks observation
incomplete; smaller drift and anchor uncertainty must still be considered.

Kernel packet/drop statistics, read count, kernel-minus-read difference, malformed
headers, missing/truncated ancillary timestamps, packet/byte/track limits and
termination reason remain visible. Nonzero unconsumed count may include queued
tail packets; it is not silently treated as network loss. `complete_supported_observation`
requires duration completion, at least one packet, available statistics, zero
observer loss, no unread tail and no reported identity/clock ambiguity. Even then
the declared exclusions apply; this flag is not proof of complete media delivery
or smooth Apple rendering. A file-write failure may leave an unfinished artifact
without a summary, which must not be treated as valid evidence.

The claimed coverage starts after socket setup returns and ends at
`coverage_ended_monotonic_ns`, before the final statistics read. That read has its
own before/after bracket because it snapshots counters while reception remains
enabled. Packets accepted during statistics/close cleanup are outside the claimed
coverage; `stopped_monotonic_ns` records cleanup completion, not coverage. The
kernel-minus-read count can conservatively include packets from that final bracket.
Missing final clock anchors are explicit and always prevent complete observation;
the original packet/byte limit termination reason is preserved.

Tests use synthetic Ethernet packets, an independent classic-BPF interpreter and
mock sockets. They verify exact filter selection/header truncation, VLAN/options,
loss reporting, timestamp ABI, setup ordering, limits and cleanup. A real Linux
kernel additionally accepted the emitted filter on an AF_UNIX datagram socketpair:
18 synthetic headers were selected/truncated correctly and five exclusion fixtures
were rejected. That isolated probe opened no AF_PACKET socket and received no
physical traffic. Actual AF_PACKET setup, software timestamps and the selected
flow still require qualification before relying on physical measurements; no
privileged socket test is hidden in the test suite.

Primary references: [Linux timestamping](https://docs.kernel.org/networking/timestamping.html)
and [Linux socket filtering](https://docs.kernel.org/networking/filter.html).
