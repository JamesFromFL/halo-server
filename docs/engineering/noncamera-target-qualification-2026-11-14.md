# Non-camera target qualification — November 14, 2026

Subsequent owner-login work produced a newer, locally qualified replacement package.
See [Local web owner login](web-owner-login-2026-11-14.md) before deployment.
The staging results below remain the historical record for the earlier device package.

This continues wave 9 after restoring the desktop's dedicated SSH identity.
The candidate is built for the server runtime. Cache staging, if verified below,
does not activate it or change the production release selection.

## Runtime evidence

The fresh SSH baseline reports Python 3.14.9 on x86_64, versus Python 3.14.10
on the desktop. All 13 interpreter/native library files in the selected release
matched both the server and the retained target runtime. The target runtime was
then checked beyond the production manifest's native-library census: all 77
stdlib extension objects and 18 additional native libraries were hashed remotely.
The two OpenSSL libraries differed from the desktop. Their exact server bytes
were copied into a new private local runtime, together with the other matching
libraries. The shared cached runtime and production build/verifier source were
left unchanged. The isolated runtime guard pins these inputs before building and
running installed-package probes.

This supplemental stdlib check matters because the current production manifest
covers the interpreter and release-payload native closure, not every stdlib
extension dependency. The extra receipt establishes this candidate's tested
runtime; it does not expand the production manifest schema or make future system
library updates automatically qualified.

## Existing installation

The read-only owner IPC preflight passed 14 checks. All seven expected camera
identities were selected, paired, listening and advertised. Live, snapshot,
recording, upstream and cleanup counters were zero. HKSV and owner warming were
off. The daemon and web companion were both active and enabled with no restarts,
drop-ins or pending daemon reload. Public camera pairing metadata, configuration
and known policy digests were captured without reading credentials or private
pairing files. New non-camera policies and pairing storage were absent, as
expected for the currently selected older release.

A separate push-event issue was discovered: three observations five seconds
apart reported Ring events as `connecting`. The retained connection activity
showed that state had persisted for about 14 hours. Authentication and REST
inventory were available. That combination does not establish motion/doorbell
push delivery. A service that is running and publishing cameras is not sufficient
proof that its Ring event connection is healthy.

## Repairs discovered during qualification

The bounded live journal identified an `Incorrect padding` failure decoding an
incoming push header. The SDK then left its receiver permanently `STOPPING`,
which Halo had not recognized as terminal. Recovery is now covered by both
terminal-state and owner-shutdown regressions. A per-listener compatibility
adapter accepts canonical padded/unpadded legacy Web Push headers while retaining
the SDK's authenticated decryption, credential handling and shared HTTP session.
The exact SDK versions are declared in package metadata; global SDK classes are
not patched. See [the push recovery notes](ring-push-recovery-2026-11-14.md).

Commits `4bd9f01` and `7b4bbdd` record the two repairs. The push-fix source suite
passed **4,015 tests**, with one unavailable IPv6-loopback skip and 83 existing
upstream warnings. Repository Ruff and the offline lock consistency check passed.
The observed live message contents were not inspected, and no claim is made that
synthetic decryption tests prove live Ring delivery.

## Device bridge shutdown repair

The next reproducible candidate passed all 788 installed tests and the daemon
lifecycle probe, but the separate installed-child probe caught a real device
shutdown race. A diagnostic run against that unchanged sealed package recorded
camera exit 0 and device exit -15, with no stderr. The first device termination
came from the control reader after EOF while owner shutdown was already active.
The device reader unconditionally terminated its process; the camera reader
already deferred to its stop owner's graceful-exit deadline in that situation.

The repair uses the same ownership rule for the device bridge. Unexpected channel
closure during normal operation still terminates the child, and the stop owner
retains its existing timeout, escalation and joined cleanup. The failed candidate
and diagnostic receipts remain at their original paths. It was not staged or
activated. The deterministic regression failed on the old implementation with
exit -15 while its unexpected-EOF control passed. After the repair, **135 runtime
and ownership tests passed** using the pinned Node executable, including forced
shutdown, canceled stop and ordinary channel loss. An initial run using the wrong
ambient Node version is retained separately; the corrected run used the
release-required version. Commit `3926087` records the narrow repair and its two
regressions. Final qualification uses that corrected source.

## Qualification and staging

The corrected source passed **4,017 Python tests**, with the same one unavailable
IPv6-loopback skip and 83 upstream deprecation warnings. Two independent
application wheels and two full release archives matched byte-for-byte. The
adapter passed **323 tests**. The sealed installed package passed **846 tests**:
622 device, 68 web, 12 media repair, 86 event and 58 device-runtime cases, with no
failures or skips. These selections overlap the full source suite; they are not
an additive unique-test total. Installed imports and frozen-source hashes were
checked, with no ambient dependency fallback or remaining native children.

Both actual installed HomeKit children answered hello/ping with separate pairing
owners, and device retirement preserved camera responses. Both exited zero and
released their locks; fresh test pairing stores remained empty. The installed
daemon started healthy and exited zero through the managed activation fixture.
The unit controller was injected and never called real systemctl. No real account,
accessory publication or Ring actuation was used. The new deterministic runtime
regressions also passed through the installed package and sealed Node executable.

- Source commit: `3926087fd529ff5b20de36360d248aaba093d677`.
- Release: `2b4a3ef49620b6c062cb5c171be04fdeca7040e8350fc9d742e9e2862c500600`.
- Archive SHA256: `e76ba2eef50f52c03aa9e2b8672c2e4decbc0d17acea251b1b0d40d2cbd97fc3`.
- Manifest SHA256: `c6b192f55c68fe5126709eb5a125fa557dbe03c85fac0a4230f2fe80d6cd85ea`.
- Final qualification receipt SHA256:
  `15059d6641a3594b4d581b42afa23ba7b615695eb053f867b8ab28737e1fa53f`.

An independent audit verified all 65 referenced qualification artifacts, nine
preserved failure references and 4,628 installed payload files, plus the repeated
builds and fresh server runtime proof. All 95 supplemental server hashes still
matched at the final runtime check. The failed pre-repair candidate and exact
termination trace remain intact in the reconnect work directory.

The final package was uploaded only to the private server cache at:

`/home/james/.cache/halo-server-deployment/2026-11-14-noncamera-2b4a3ef49620b6c0`

The unchanged production verifier checked the complete extracted candidate on
the actual server and also verified the currently selected release as a code
recovery option. It checked 5,091 archive entries and 376,900,296 expanded bytes.
A local staging guard initially rejected the new structured child-exit-code
report before contacting SSH; it was corrected to explicitly require integer
camera/device exit codes of zero. The failure and prior guard are preserved.
No archive, production validator or test success condition was weakened.

**The candidate is staged and ready for controlled deployment; it is not active.**
Final read-only preflight passed all 14 checks. Seven paired, selected and
published camera identities, their public pairing metadata, saved/running
configuration, policy digests, selected release, service PIDs, invocation IDs and
startup intent all matched the original baseline. Media and cleanup counters
remained zero. Private pairing contents and credentials were not read.

The running old release still reports its existing push connection as connecting;
the new repairs cannot affect it until activation. Neither physical Ring delivery
nor Apple Home behavior is claimed as tested by these package checks.

Detailed evidence is retained in the private workstation directories:

- `work/halo-server-noncamera-target-2026-11-14/`: initial server/runtime baseline.
- `work/halo-server-noncamera-reconnect-2026-11-14/`: push fixes and confirmed
  pre-repair device shutdown failure.
- `work/halo-server-noncamera-shutdown-2026-11-14/`: final source suite, immutable
  package, installed probes, qualification receipt and server staging/preservation
  receipts. No production activation or Git push was performed.

## Remaining acceptance

No physical Ring/Apple acceptance is established by this package qualification.
After a managed activation, check all seven cameras and one established live view
before enabling device monitoring. Confirm the push connection reaches connected
and a deliberate ordinary camera motion event arrives; service activity and
working snapshots alone do not prove push delivery. Import and pair a sensor-first Halo Bridge,
then qualify contact and motion events before opting into individual control
families. Cameras retain separate accessory identities. Panic and emergency
dispatch remain excluded. Alarm, lighting and Chime control permissions remain
independent opt-ins. See [the acceptance guide](../noncamera-acceptance.md).

The old release remains a code recovery option; preserving mutable state does not
mean an old bridge reader understands mappings created by a newer version. Never
restore older pairing or policy bytes merely to make a rollback run.
