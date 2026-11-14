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

## Legacy Web Push header compatibility

The original decoding failure is a separate compatibility problem. A generated,
valid legacy Web Push fixture decrypts with padded key/salt fields, while the
pinned SDK rejects unpadded fields with the same `Incorrect padding` exception.
The retained live payload was not inspected, so its validity beyond the observed
padding failure remains unverified.

Halo now creates an instance-local Firebase receiver through a small Ring
listener factory. The adapter accepts canonical padded or unpadded Base64URL
headers, validates a 65-byte uncompressed P-256 public point and a 16-byte salt,
and supplies canonical padding to the original SDK decryption routine. ECDH and
authenticated AES-GCM decryption remain unchanged. Invalid header shapes are
rejected with fixed, bounded logging; the SDK acknowledges those messages so a
malformed header cannot indefinitely poison redelivery. Ciphertext authentication
failures still fail decryption. No payload, key, salt or identifier is logged.

This deliberately uses the reviewed private contracts of `ring-doorbell==0.9.14`
and `firebase-messaging==0.4.5`. Both are direct exact dependencies, including in
wheel metadata, and the factory rejects incompatible versions before touching
private fields. The existing listener owns startup, refreshed credentials and
cleanup; the receiver shares its existing HTTP session. Neither SDK is globally
monkeypatched. Future SDK upgrades require requalifying this adapter.

Twenty-nine new tests cover all padded/unpadded combinations with real generated
encrypted messages, the unmodified SDK failure, malformed encodings, invalid EC
points, tampered ciphertext, wrong keys/salts, bounded warnings, acknowledgement,
subsequent valid delivery and unsupported versions. A real SDK start/stop test
proves that it reuses the preinstalled receiver and preserves credentials and
shared-session ownership. Independent review also exercised that lifecycle.
The focused adapter tests and lint/format checks passed. The full source suite
then passed **4,015 tests**, with one IPv6-loopback skip and 83 existing upstream
deprecation warnings. `uv lock --check --offline` passed with all 76 locked
packages unchanged apart from the root direct-dependency declaration.
Installed-release results are recorded with the final target qualification.

These are historical `aesgcm` headers, not modern `aes128gcm` body framing:

- [RFC 7515 section 2](https://www.rfc-editor.org/rfc/rfc7515.html#section-2)
  defines Base64URL without trailing padding;
  [Appendix C](https://www.rfc-editor.org/rfc/rfc7515.html#appendix-C) describes
  deterministic restoration of that padding.
- [HTTP encrypted-content draft 02](https://datatracker.ietf.org/doc/html/draft-ietf-httpbis-encryption-encoding-02#section-1.1)
  adopts that encoding for legacy headers; section 3.1 specifies the salt length.
- [Web Push encryption draft 03 section 5](https://datatracker.ietf.org/doc/html/draft-ietf-webpush-encryption-03#section-5)
  specifies the uncompressed P-256 point representation.

Evidence is retained privately in
`work/halo-server-noncamera-target-2026-11-14/` and
`work/halo-server-noncamera-reconnect-2026-11-14/`. The first server-qualified
non-camera package remains cache-staged and inactive; it predates these fixes.
The replacement candidate must pass target qualification before activation.
Synthetic tests establish compatibility and recovery, not live Ring delivery.
