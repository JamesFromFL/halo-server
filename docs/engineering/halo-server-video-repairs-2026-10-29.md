# Bounded cached video repairs

This isolated follow-up completes the distinction between fresh access units and
repairs of packets already sent. New video AUs retain their original source
receipt plus 500 ms deadline. A cached repair instead expires one second after
the packet's initial successful socket send; neither requests nor successful
repairs refresh that initial time.

## Ownership and limits

The active video owner creates one repair worker alongside its sender, RTCP
reader and report task. The reader authenticates feedback and enqueues bounded
cache identities, then continues reading. It never waits for a repair's AU lock,
pacing sleep or blocked UDP retry. BYE can therefore fence the view immediately.

The queue has at most 64 entries and 76,800 protected bytes **including the item
currently being sent or waiting**. Entries share the exact cached tuple; no media
copy or new encryption is made. Repeated requests for the same queued packet
coalesce. Clearing or fencing drops queued entries but retains the in-flight
claim until the actual worker operation settles. The existing retransmit cache
still has its 512-packet / 1 MiB bound. A pruned cache object retained by the
queue remains charged within the separate 76,800-byte queue cap.

Admission and sends have distinct rolling one-second budgets, each 64 packets /
76,800 bytes. The request budget charges accepted enqueue operations. The send
budget is checked immediately before the socket syscall and recorded only after
success, independently of queue wait and arrival boundaries. New-AU bytes do not
spend this repair budget. The finite local wire pacer and one-AU send lock remain
shared, preserving complete-AU reservations and fair admission for waiting new
AUs. There are no per-NACK tasks or unbounded waiters.

Before every actual repair send, the worker checks current scope/privacy, the
actual capture epoch, exact cache-object identity and the original cache expiry.
Waiting for the AU lock, pacing and UDP retry is bounded by that same cache
deadline and by an additional maximum one-second operation timeout. Expiry,
budget exhaustion and bounded UDP congestion drop that repair, without starting
a reference-picture recovery loop or terminating an otherwise healthy view.

Dependency damage, reconfiguration and source recovery clear queued repairs and
cached identities. STOP, BYE and boot retirement fence the queue synchronously,
close the socket and join the worker through the existing retained cleanup path.
Prepared and partially started views are covered by the same cleanup. If a
worker actually fails to retire, the view and its in-flight claim remain charged;
retry cannot report false cleanup success.

Repairs do not read, acquire or renew a source handle; update source freshness;
advance the RTP sequence or SRTP protection index; call the timeline's sample
sent hook; or refresh the cache age. Only datagram/octet send counters include a
successful repair. Diagnostics contain fixed counts/bytes/booleans, never the
sequence numbers, cache identities or ciphertext.

## Qualification

Tests use generated packets and native SRTP with owned loopback sockets. They
cover authenticated NACK after 650 ms replaying identical ciphertext, expiry
beyond one second, distinct-request storms, duplicate coalescing, inclusive
in-flight byte accounting, independent rolling request/send budgets, source
epoch/privacy/cache-identity changes after waits, repair lock timeout, new-AU
fairness, blocked-UDP BYE/STOP, retained cleanup failure, prepared cancellation
and partial task-start failure. Existing video, clock, native decode and public
HAP -> real IPC -> UDP -> independent decode tests remain part of the gate.

No physical Ring/Home session or network quality claim is made here. Recorded
physical views had zero Home NACKs, so these fixes do not establish a cause for
the roughly periodic upstream A/V quiet periods. Software checks also cannot
prove physical NIC/Apple arrival deadlines or eliminate scheduling preemption
between admission and the syscall.
