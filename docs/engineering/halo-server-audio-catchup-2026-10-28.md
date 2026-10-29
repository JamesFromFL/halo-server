# Bounded audio catchup after observed receive bursts

This is a developed and locally tested downstream repair. It has not been
installed on the paired server or qualified with Apple Home. No real audio was
recorded. The source lane's observed common audio/video arrival gaps remain a
separate investigation.

## Confirmed local loss mechanism

The new lifecycle capture read 150.48 seconds of continuous-timestamp Ring audio,
sent 103.16 seconds, and classified 45.34 seconds of generated output as expired.
It recorded 69 converter recreations. Audio and video routed/decrypted receiver
entry both had repeated quiet periods, commonly 600–800 ms about every two
seconds; source-loop wake lag peaked at 14.84 ms. These are callback-boundary
observations, not kernel receive measurements or proof of the physical cause.

The existing audio sender independently added a full negotiated packet interval
after every datagram. A fresh 600 ms burst converted to thirty 20 ms packets could
therefore take about 580 ms to finish, exceeding the unchanged 500 ms receipt
freshness limit even before conversion and scheduling time. The unchanged source
timestamps and zero source packet loss do not prevent that local expiry.

## Repair and bounds

`media/homekit_audio.py` keeps nominal pacing for ordinary small batches. When
queued duration exceeds one maximum Opus packet (120 ms), or the current head has
aged at least 100 ms since its original receipt, the sender may use twice the
nominal cadence. A 20 ms selection is bounded at 100 RTP media packets/second,
with at most three packets of accumulated credit. It never invents or changes
compressed audio, RTP source-derived timestamps, duration, gain or codec settings.
No filter, silence insertion, noise suppression, time stretch or freshness
extension was added.

An independent byte bucket accounts for actual payload plus RTP, SRTP tag, UDP
and base IP headers. Its refill is twice the negotiated media bytes/second plus
twice the nominal packet header cost; stored credit is three maximum datagrams.
At the observed IPv4 24 kbps/20 ms selection this is 11,000 IP-wire bytes/second
and 3,684 bytes of credit. IPv6 uses its larger header cost. For any time interval,
RTP media sends stay within the refill allowance plus that finite initial credit.
Periodic SRTCP sender reports keep their existing separate bounded task; the
media bucket is not a claim about all protocol traffic or incoming talkback.

A packet whose bounded admission time would exceed its own original 500 ms
receipt deadline is classified as `output_budget_rejected` without holding newer
packets behind it. This classification is separate from already-expired output.
The existing source/queue limits remain: at most 32 output owners, including the
in-flight head and reserved native response, with less than 40 KiB retained.
Native conversion, output pacing and final socket send retain current generation,
capture epoch and live-audio permission checks. Token credit is spent only after
an actual successful media send and does not reset on a same-track privacy or
epoch transition. Stop still owns and joins conversion and egress.

## Replay results

A safe fixture extracted only completed encoded-frame counts from 100 ms bins in
the fresh capture, covering its source-relative 3–15 second window. It retains no
payloads, addresses, camera identifiers or credentials. Exact original packet
arrival instants inside a bin are unavailable. The replay places each bin's
arrivals at its beginning and prepends 1.2 seconds of ordinary warmup.

The experiment generated true mono wideband SILK `0x58`/60 ms Opus in memory,
passed it through the real native converter and actual owned audio pipeline, and
received protected loopback UDP while a native video worker ran concurrently.
An independent SRTP receiver and reference Opus decoder decoded every sent packet.

| Same input timing and synthetic source | Existing pacing | Final candidate |
| --- | ---: | ---: |
| Source audio read | 12.72 s | 12.72 s |
| Generated audio sent | 8.88 s | 12.58 s |
| Expired generated output | 3.54 s | 0 s |
| Independently decoded RTP packets | 444 | 629 |
| Converter recreations | 8 | 6 |
| Concurrent native video frames | 208 | 208 |
| Highest observed RTP packets in one second | 51 | 93 |
| Highest observed IP-wire media bytes in one second | 5,656 | 10,314 |
| Largest actual inter-send gap | 802 ms | 839 ms |

All 629 candidate-generated packets were transmitted; none was budget-rejected
in this replay. Its source hash was checked before/after execution. The final
replay also ran alongside the focused native audio regression gate. Counts and
relative timing are evidence of this bounded experiment, not a universal CPU,
network or Apple rendering guarantee.

The approximately 839 ms gap remains because the replay retains the observed
upstream quiet periods. The difference between 12.72 seconds read and 12.58
seconds sent is still not represented by transmitted complete 20 ms packets
across converter starts/retirement; native pending contribution is not inferred
or claimed recovered. Source gaps, conservative native-prefix resets, video
recovery and the existing RTCP timeline still require their own validation.

## Tests and evidence

The focused gate passed 200 audio tests. New tests cover nominal spacing, complete
600 ms backlog catchup without changing compressed bytes or RTP timestamps,
packet/byte envelope bounds for IPv4/IPv6 at all negotiated packet durations and
6/24/64 kbps, impossible admission, and privacy/epoch/stop/cancellation during a
pacing wait. The native 1.2 second burst test now includes the new explicit budget
rejection category in its complete sample ledger; its freshness or correctness
assertions were not relaxed. Existing native codec, queue, trace, lifecycle,
independent SRTP and reference decoder tests remain passing.

Scratch receipts are in `evidence/audio-continuity/`: the hash-bound before/after
JSON, captured relative-count fixture, replay script, focused gate log and final
receipt. All source changes are local to the audio sender and focused tests.
