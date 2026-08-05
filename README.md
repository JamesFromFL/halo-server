# Halo Server

Halo Server is the headless Ring event and media backend for Halo integrations. Its daemon
owns the Ring account session and real-time event listener. A separate Textual client
administers it through an owner-only Unix socket.

## Current Status

Implemented:

- private, transactional Ring credential storage
- Ring login, two-factor login, token restore, refresh persistence, and logout
- supervised real-time Ring motion and doorbell events
- per-camera incident timing with a 30-second quiet period
- event-driven, expiring per-camera media leases with one shared Ring receiver
- bounded H.264/Opus prebuffer gated on a retained SPS/PPS/IDR boundary
- H.264 passthrough and mono 48 kHz Opus normalization for Scrypted/HomeKit
- camera inventory with an allowlisted public schema
- authenticated, versioned plugin HTTP and WebSocket APIs with event replay
- a server-owned protocol-v2 schema and fixture corpus shipped in the wheel
- revocable Scrypted pairing tokens
- digest-authenticated, on-demand loopback RTSP delivery
- per-camera validated two-way audio with bounded speaker-readiness acknowledgement
- local daemon status, login, logout, and shutdown administration
- Textual status, login, two-factor, logout, pairing, camera, and recent-event views
- the proven Halo Ring WebRTC video and audio compatibility layer

Not implemented yet:

- camera controls and server settings in the TUI
- snapshots
- TLS-protected remote plugin and media access
- operating-system service packaging

## Run From Source

Python dependencies are managed with `uv`. PyGObject, GStreamer introspection, and the
GStreamer app, RTP, Opus, and H.264 parser plugins must be installed by the operating
system. The minimum package sets for the currently tested distributions are:

```bash
# Arch Linux
sudo pacman -S --needed python-gobject gst-rtsp-server \
  gst-plugins-base gst-plugins-good gst-plugins-bad-libs

# Debian/Ubuntu
sudo apt install python3-gi gir1.2-gst-rtsp-server-1.0 \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
```

Halo Server validates `appsrc`, `queue`, `h264parse`, `rtph264pay`, and `rtpopuspay`
before binding the RTSP gateway. A missing system plugin therefore fails startup with its
element name instead of producing a camera stream that fails later.

```bash
uv venv --python /usr/bin/python3 --system-site-packages
uv sync --frozen --all-groups
uv run halo-server daemon
```

Open the TUI in another terminal:

```bash
uv run halo-server tui
```

Status and clean shutdown are also available without the TUI:

```bash
uv run halo-server status
uv run halo-server shutdown
```

The plugin API listens on `127.0.0.1:18443` and the media gateway on
`127.0.0.1:18554` by default. Both listeners intentionally accept only loopback bind
addresses. Halo Server and Scrypted must currently run in the same network namespace, such
as on the same host or in a container using host networking.

By default, credentials are stored under
`${XDG_STATE_HOME:-$HOME/.local/state}/halo-server` and the admin socket is placed under
`${XDG_RUNTIME_DIR:-/run/user/$UID}/halo-server`. Use `--state-directory` and
`--runtime-directory` after a command to override them.

## Architecture And Safety

The TUI is only an admin client. Closing it does not stop the daemon, Ring events, or an
active incident. The local admin socket validates the peer user and never returns Ring
tokens or raw Ring SDK objects.

Media sessions are event-driven or explicitly leased. Halo Server does not expose permanent
Ring camera mounts or advertise continuous Ring-to-DVR recording. One upstream Ring session
is shared by bounded downstream consumers for each camera, and every lease has both an idle
expiry and an absolute lifetime. The current absolute limits are 10 minutes for event and live
video, 5 minutes for intercom, and 30 seconds for snapshots. A client must reopen an expired
session; Halo does not silently roll a continuously held connection into permanent recording.
The authenticated capabilities API and daemon status publish these limits.

The RTSP URLs are private transport details returned only to an authenticated plugin. Their
credentials are random, rotate when plugin access is revoked, and are never displayed in
daemon status. Connecting to one starts a bounded Scrypted media lease; disconnecting releases
it. Do not configure these URLs as permanent DVR inputs.

## Development

```bash
uv lock --check
uv run --frozen pytest
uv run --frozen ruff check --no-cache src tests
uv run --frozen ruff format --check --no-cache src tests
uv run --frozen pip-audit --local --progress-spinner=off
uv build --out-dir dist
```

The project requires uv 0.11.24. CI installs the wheel into a clean environment and verifies
Git commit metadata plus SHA-256 checksums inside the build runner. Publishing and deployment
remain separate, explicitly authorized release steps.

Halo Server owns the canonical plugin wire contract under
`src/halo_server/api/protocol/v2`. Plugin repositories vendor exact copies with source-commit
and SHA-256 provenance. Breaking wire changes require a new protocol version.

## License

GPL-3.0-or-later
