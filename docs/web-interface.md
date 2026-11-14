# Halo Server browser interface

The owner console uses the same visual language as Halo GTK. It manages Halo's
existing Ring connection and HomeKit cameras through the private daemon API.
Opening the console does not open cameras, request snapshots, or enable recording.

The console runs as a separate HTTPS companion. The daemon remains the owner of
Ring credentials, devices, events, media, and HomeKit pairing. Both processes run
under the same local user; the process boundary provides independent recovery,
not protection from arbitrary code running as that user.

This first web companion and its managed service lifecycle are qualified on
Linux. Physical Firefox/iPhone certificate onboarding and the live server pilot
are separate acceptance steps. The existing experimental macOS label does not
qualify this web TLS implementation there.

## Build and local setup

Build the static browser assets before packaging Halo:

```sh
cd web-ui
npm ci --ignore-scripts --no-audit --no-fund
npm run check
npm test
npm run build
cd ..
```

The lockfile pins the build dependencies. The resulting assets are bundled in
the Python package; the running interface needs no frontend Node process, CDN,
remote font, or dependency download. Existing managed release qualification
still applies to the Python daemon and HAP adapter.

The browser acceptance harness starts a temporary HTTPS companion with seven
synthetic cameras, uses the real owner API, and checks that viewing the interface
never opens media. With the pinned Playwright Chromium installed:

```sh
python scripts/check_web_browser.py --output /tmp/halo-browser-evidence
```

Use `--installed` with an isolated environment containing the built wheel to
exercise the installed package. This harness does not use a real Ring account.

Prepare loopback-only access from the local owner's terminal:

```sh
halo-server web configure
halo-server web setup
halo-server web
```

`configure` prints the HTTPS URL and certificate fingerprint. `setup` prints a
ten-minute, single-use enrollment code. Open the console, use the fixed owner
username **`admin`**, enter the enrollment code, and choose a **Halo web password**
privately in the browser. The password must contain 15–128 characters. There is
no default password, and the Halo password is separate from the Ring password.
The username is exactly lowercase `admin`; it is a public account name, not a
second secret. This console currently has one owner account.

Do not share the enrollment code or put it in a URL, screenshot, journal, or chat.
Keep using the existing Ring session when it is already connected. If you already
created a Halo web password on an earlier release, sign in with `admin` and that
same password after upgrading. No new enrollment or password reset is required.

The default HTTPS port is **18444**, and the initial address is
`https://127.0.0.1:18444`. The port can be overridden with `--port` during
configuration. Local-network binding requires explicit owner configuration, for
example:

```sh
halo-server web configure --host 192.168.1.20 --canonical-host 192.168.1.20 --allow-lan
```

Use the server's actual LAN address. The generated certificate identifies the
configured hostname/address. Establish trust using the fingerprint received
through the local terminal or your trusted SSH connection before entering any
credentials. A self-signed leaf is not automatically trusted by Firefox or
iPhone Safari. Physical-device certificate onboarding remains a deployment
acceptance check; a synthetic browser test does not qualify that workflow.
Halo does not install a local certificate authority or expose an internet service.

Changing the certificate hostname after initial setup requires the explicit
`--renew-certificate` option. This changes the fingerprint and requires renewed
browser trust. To use an existing trusted certificate instead, import the
matching PEM files locally with `halo-server web certificate --certificate
/private/server-cert.pem --private-key /private/server-key.pem`. Never place the
private key in the repository or transfer it through the browser interface.

Use `halo-server web access` to inspect the configured access information. Path
overrides belong immediately after `web`, before the subcommand. The web settings,
password verifier, and TLS key live in a separate private `web` directory under
Halo's state directory.

## Managed installations and recovery

The managed companion unit is `halo-server-web.service`. It is independent of
`halo-server.service`, so stopping the daemon does not remove the recovery UI.
Release activation must retire both owners before switching immutable releases.
The release operator preserves each unit's enabled and active state separately.
Rollback to an older release without the web command suppresses web startup and
retains its desired state for a later compatible upgrade.

Install the unit through the managed release operator's `--install-web-unit`
option. That option does not implicitly start or enable a previously inactive
companion. Complete local web configuration before deliberately enabling it.
Do not hand-edit generated managed units; custom units are refused during
managed changes rather than overwritten.

If release maintenance cannot drain an outstanding browser operation, activation
stops before changing the selected release. The console remains readable but
pauses new changes. Retry the release operation after the outstanding work settles.
To abandon maintenance, inspect the owner health socket's `pending_operations`
count; once it is zero, locally restart only `halo-server-web.service`. Do not
force a restart while a possibly-sent operation is still running. This recovery
does not restart cameras or discard pairing data.

The browser's **Restart Halo** action only restarts the managed daemon. It
requires confirmation, temporarily interrupts camera access, and verifies the
new daemon instance before reporting completion. A missing response is an
uncertain operation, not permission to repeat it. Software installation and
release activation remain owner-terminal operations.

If the web password is lost, run:

```sh
halo-server web reset --yes
```

This revokes web authentication and issues a new enrollment code. Use `admin`
and choose a replacement password privately in the browser. Recovery preserves
Ring credentials, HomeKit pairing, device selection, and recording preferences.

Adding the fixed owner username does not change the stored password format or
raise the managed release reader floor. An explicit rollback to a retained web
release from before this change restores its password-only sign-in form; use the
same current Halo password there. That older interface still requires password
authentication. Returning to this release restores the `admin` username field.
Release rollback never restores an older password or other mutable state.

## Everyday use

- **Overview** shows current daemon, Ring event, and HomeKit publication facts.
  Unknown and stale observations stay visibly unknown or stale.
- **Devices** lists current cameras and their evidenced capabilities. Selection,
  publication, pairing, source availability, and recording are separate states.
- **Integrations** manages the existing HomeKit integration. Selecting a camera
  does not mean it has been paired or that live viewing has been tested.
- **Activity** is bounded recent session activity. It clears on account change
  and daemon restart and is not a complete historical audit.
- **Diagnostics** runs bounded Halo Doctor checks and downloads an allowlisted
  support report. Advanced probes are explicit actions.
- **Settings** separates saved and applied configuration. Saving never restarts
  Halo automatically, and conflicts preserve the owner's draft.

Pairing codes require explicit reveal and recent web authentication. They are
hidden after one minute, navigation, logout, or a relevant authority change.
Hiding a code does not rotate it. Reset affects one explicitly named camera and
may temporarily interrupt the HomeKit adapter. Resume an existing pending reset
by its original operation ID rather than starting another reset.

Camera light and siren responses distinguish accepted, rejected, and unknown outcomes.
An accepted command is not proof of the physical device state. The existing
30-second siren bound remains in force. Recording controls distinguish Halo's
warm-source opt-in from Apple Home's recording and audio preferences.

## Scope

The interface manages cameras, non-camera monitoring, Halo Bridge imports,
guarded Ring Alarm mode controls, individual Ring Smart Lighting, optional lighting
groups and qualified Chime settings/actions. Brightness, additional hardware
families and other ecosystems require further qualification. It does not automatically preview or play video,
browse arbitrary files, expose raw logs, or provide browser-driven software updates.

## Halo Bridge for non-camera devices

Enable read-only monitoring in Devices, prepare the Halo Bridge import preferences,
and select supported sensors. Then use **Integrations → Halo Bridge → Connect Halo Bridge to
Apple Home** to explicitly enable publication. Saved import preferences alone do
not publish. One bridge pairing brings its selected contact, motion, leak, smoke,
carbon monoxide, Ring Alarm and qualified physical Beams light services into Apple
Home; each camera retains its own pairing. Importing an alarm or light exposes
observed state and does not enable control.

Reveal the bridge setup code after confirming the Halo password. The code is
short-lived and disappears when the page or account changes. Reset pairing only
when intentionally replacing the Apple Home association. A reset disables bridge
publication before deleting its pairing; cameras and other account identities are
preserved. An interrupted reset offers **Resume reset**, using the original
operation. After completion, remove the old bridge from Apple Home, explicitly
reconnect publication and pair again.

Unavailable or stale readings produce a HomeKit fault rather than a fabricated
closed/dry/clear/disarmed/off state. Installed and physical qualification is pending.

Open the security panel in Devices to see its observed mode, entry/exit/alarm phase,
Ring account authority and pending or uncertain command status. **Allow normal
alarm controls** is a separate, password-confirmed preference for that account and
location. It permits Home, Away and Disarmed requests from Halo and the paired
Apple Home bridge. Disabling remains available without recent password confirmation.
No mode is changed by enabling the preference, restarting, or importing a device.

Halo requires explicit Ring owner-role evidence and fresh panel state. Ring still
enforces account and subscription permissions; unknown or shared roles currently
remain read-only. Web mode changes require confirmation and recent authentication.
Only a fresh reported target mode confirms success. An uncertain result requires
checking the observed state; the same operation is never automatically replayed.

Arming cannot bypass open/faulted sensors, and Night mode is not mapped to another
mode. Panic, emergency dispatch, siren activation and active-alarm cancellation
remain blocked. Use the Ring app or keypad for an active alarm or a bypass flow.

## Individual Smart Lighting controls

Open a supported Beams light in Devices to see its actual reported state and
control authority. **Allow light controls** defaults off. Choose an On duration
of 30 seconds, 1 minute, 5 minutes or 15 minutes, then **Apply light preferences**.
The default duration is 1 minute. Enabling requires password confirmation; saving
preferences does not switch the light. The saved duration applies to both Halo
and Apple Home. Ordinary light actions do not require another password prompt.

**Return to automatic** releases Ring's manual On override. Apple Home's Off
request has this same meaning: motion rules, schedules or another controller may
keep or turn the light on. Halo shows the actual reported On/Off state separately
and does not invent an Off value or a shutoff countdown. Confirmation requires a
fresh matching state report; a return-to-automatic request can remain uncertain
while Ring still reports On. The On duration is a bounded request to Ring, not a
guaranteed physical shutoff time.

Controls require a known physical Beams model, an unambiguous bridge match,
explicit owner role, current state and the saved opt-in. Unknown or shared roles
remain read-only in this implementation. This is Halo's current qualification
boundary; Ring itself supports some shared-user lighting controls. An uncertain
command is never automatically resent. **Check outcome** retrieves the existing
operation; use newer observed state to resolve an old receipt only when Halo
offers that action. Disabling controls preserves monitoring and HomeKit identity.

Brightness is not exposed by this wave. Camera-integrated lights keep their own
interface; camera, individual and group lighting commands share one account-wide
guard against overlapping writes.

## Lighting groups

Groups appear separately from individual lights. **Allow light controls** uses
the same finite On duration preferences, but **Turn off group** sends Ring's group
Off request. It does not disable future motion, schedules or linked behavior.
The displayed state is Ring's group aggregate, not a claim that every member
agrees. Halo obtains a fresh group report to confirm a command and never copies a
group result into camera or individual-light observations.

Every member needs an exact current ownership match before group control is
available. Missing or mixed ownership remains read-only. Groups need explicit
individual inclusion in Halo Bridge even when auto-import is enabled, avoiding
automatic duplicate physical-light and group tiles. Import and control permission
are independent. Changed membership or authority invalidates an older command.

## Chimes

Open a qualified legacy Chime in Devices and enable **Allow Chime controls** after
password confirmation. Volume, snooze/clear and automatic nightlight preferences
apply only when submitted. Missing source values remain unknown. Automatic
nightlight enables Ring's ambient-light behavior; it is not a report of lamp power.
These settings are managed in Halo, with no fabricated HomeKit accessory control.

Sound tests require their own permission, a deliberate confirmation and recent
password authentication. An accepted request means Ring accepted the HTTP request,
not that playback finished. After listening, acknowledge that the sound stopped
to release Halo's session-scoped sound latch. This acknowledgement is local and
does not send another sound request. A restart cannot prove playback completion.

Saving permissions, discovery and pairing never play a sound. Submitted commands
are not automatically replayed after connection loss; use the existing operation
and current observed state to inspect an uncertain outcome. Tone changes and
newer Chime models require additional qualification.

See the [deployment and acceptance guide](noncamera-acceptance.md) for the first
server/device pilot and remaining qualification boundaries.
