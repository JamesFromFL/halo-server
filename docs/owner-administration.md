# Owner administration

Use `halo-server tui` or the commands below from the same local user account as the daemon.
Both clients use the private Unix socket. They do not open an independent Ring session.
Closing the TUI leaves the daemon running.

The direct HomeKit path currently targets Linux. Its local synthetic/native tests are
qualified separately from real Ring camera, Apple Home hub, notification and HKSV timeline
acceptance. Those real-device checks remain unfinished.

For an explicitly coordinated stream investigation, see
[bounded stream diagnostics](engineering/bounded-stream-diagnostics.md). Its local
`stream-capture` commands collect short private media excerpts and packet timing;
they do not start a camera or change recording settings.

## Operating mode and restart

```sh
halo-server config get
halo-server config set --mode homekit
```

Modes are `ring-only`, `compatibility`, `homekit`, and `dual`. Compatibility enables the
existing plugin/loopback RTSP path; HomeKit operates directly through Halo's supervised
adapter. Dual mode enables both. Scrypted is only an optional compatibility consumer.

Configuration output separates `saved` and `applied` settings and reports `restart_required`.
Saving does not restart the daemon or silently rebuild a running integration. Stop and
restart it using the same foreground command or existing process manager when ready.
Daemon flags such as `daemon --mode homekit` override saved values for that run only.

The saved configuration also controls compatibility loopback binds:

```sh
halo-server config set --plugin-host 127.0.0.1 --plugin-port 18443 \
  --rtsp-host 127.0.0.1 --rtsp-port 18554
```

Writes use the revision observed by the client. A conflict requires a fresh decision;
clients do not retry an old intent automatically. Scripts can supply
`config set --expected-revision REVISION ...`. The TUI keeps unsaved edits and their
original revision across background refreshes; **Reload settings** discards the draft
and reads current saved truth.

## Select cameras explicitly

Sign into Ring through the TUI first. Then inspect native camera identities:

```sh
halo-server homekit list
halo-server homekit select 42 43
halo-server homekit select --none
```

`select` replaces the entire selected camera set. Camera IDs come from `list`; discovery
never automatically selects every device. Missing cameras retain their stable identities
and appear unavailable. A replacement account does not inherit another account's selection.
The TUI's Select/Deselect buttons change the highlighted camera while preserving the rest.

Status and pairing information distinguish selection, source availability, adapter readiness
and publication. A saved selection does not prove that Apple Home has paired or received media.

## Pairing and targeted reset

```sh
halo-server homekit pairing list
halo-server homekit pairing reveal 42
```

Ordinary lists/status contain no setup PIN or setup URI. `reveal` explicitly prints that
camera's setup data, so its output can remain in terminal scrollback. In the TUI, **Reveal
setup** displays the PIN/URI for at most 60 seconds. **Hide setup**, camera/account/adapter
changes, deselection, reset, logout or a detected disconnect clear the display. Clearing a
display does not revoke or rotate the underlying setup code.

Pair only a current published camera. Pairing metadata may be unknown while the adapter is
offline; unknown is not reported as a verified unpaired state. Apple Home setup uses the
revealed camera-specific code. Real Apple pairing acceptance remains a separate device gate.

A reset requires explicit confirmation:

```sh
halo-server homekit pairing reset 42 --yes
```

The TUI requires typing `RESET` for the highlighted camera. Reset removes that camera's
Apple pairing/setup material while preserving its stable Halo identity and other cameras'
pairing namespaces. The safe maintenance barrier restarts the adapter, temporarily
interrupting other native views. A completed reset requires pairing the camera again in
Apple Home.

A pending result means cleanup/recovery is unfinished. Inspect pairing status and repeat
the confirmed reset to resume its recorded operation. A script can use the exact returned
operation UUID with `--operation-id UUID`; do not invent a replacement operation to bypass
a pending reset. Clients preserve uncertainty and do not automatically repeat a reset.

## Warm HKSV recording

```sh
halo-server homekit warm 42 on
halo-server homekit warm 42 off
```

Warm recording is an explicit owner opt-in for a selected, positively identified mains-only
camera. A battery-capable camera does not qualify merely because it is plugged in, wired,
solar-powered, or reports no battery capability. Turning warm mode off remains available
when the camera is missing.

Apple Home's recording-active and recording-audio selections remain separate preferences.
The owner opt-in alone does not enable those permissions. The TUI shows mains evidence,
owner opt-in and current warm-history state. There are two global warm-source slots and
two active-recording slots, with one of each purpose per camera. A cold/rebuffering source
cannot claim four seconds of pre-event media it did not actually retain.

## Camera light and siren

```sh
halo-server camera-control 42 light on
halo-server camera-control 42 light off
halo-server camera-control 42 siren on
halo-server camera-control 42 siren off
```

Only verified device capabilities are enabled in the TUI. Siren-on uses Halo's existing
fixed 30-second limit; custom methods and durations are unavailable. Each request is bound
to the currently observed account/device generation and has a fresh operation UUID.
`accepted`, `rejected`, and `unknown` are distinct outcomes. An unknown outcome is not a
confirmed switch state and is never retried automatically.

## Diagnostics and support

```sh
halo-server doctor
halo-server doctor --probe
halo-server support --output ./halo-support.json
```

Doctor uses bounded local checks and daemon health. With no running daemon it falls back
to static local checks. `--probe` explicitly opts into local codec/adapter probes; it does
not log into Ring, wake cameras, publish accessories or contact Apple. The TUI Doctor
button uses the default checks.

Support exports use a separate sanitized whitelist, not raw status, logs or state files.
The requested destination must be a new file: exports do not overwrite an existing file
or follow a target symlink. The file is created with mode `0600` and a fixed size limit.
Pairing codes, credentials, media, signed URLs, account email and private home paths are
excluded by the daemon's report contract.

For custom paths, place common path options immediately after the top-level command:

```sh
halo-server homekit --state-directory /private/state --runtime-directory /private/run list
halo-server config --state-directory /private/state --runtime-directory /private/run get
```
