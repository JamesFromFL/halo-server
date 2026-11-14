# Ring push recovery discovered during deployment qualification

Read-only server checks found seven published, paired cameras and healthy local
services, but the camera push-event connection had remained `connecting` for
about 14 hours. A bounded, content-free journal classification identified one
Firebase receiver shutdown and a `binascii.Error` with `Incorrect padding` while
decoding an incoming crypto-key header. No credentials or incoming payloads were
copied into source or fixtures.

The pinned SDK calls `_terminate()` on an unexpected receiver exception. That
sets `STOPPING`, disables listening and cancels sibling tasks; it never advances
to `STOPPED`. Halo previously recreated a receiver only for `STOPPED`, leaving
this terminated listener supervised indefinitely as connecting. Normal reconnects
use `RESETTING` and `STARTING_*`, so treating `STOPPING` as terminal is a narrow
correction.

Halo now retires and replaces either terminal state through the existing cleanup
path. Owner shutdown still prevents replacement, credentials updated by the old
listener are retained, and transient reconnect states keep their current receiver.
The SDK's stop API does not independently guarantee that all canceled tasks have
joined; this change does not invent such a guarantee or mutate SDK state.

Eight regression cases were added. Before the change, only recovery from
`STOPPING` failed (a bounded timeout), while seven control cases passed. After the
change, all **57 focused Ring/HomeKit event and journal tests passed**. Ruff lint,
formatting and diff checks passed.

The original decoding failure is a separate compatibility problem: a generated,
valid legacy Web Push fixture decrypts with padded key/salt fields, while the
pinned SDK rejects unpadded fields. Receiver recovery alone does not make those
messages deliverable. That padding compatibility follow-up must be qualified
before activating the new candidate. The retained live payload was not inspected,
so its validity beyond the observed padding failure is unverified.

Evidence is retained privately in
`work/halo-server-noncamera-target-2026-11-14/`. The first server-qualified
non-camera package remains cache-staged and inactive; it predates this fix.
