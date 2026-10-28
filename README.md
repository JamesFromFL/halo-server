# Halo Server

Halo Server is the headless Ring event and media backend for Halo integrations. Its daemon
owns the Ring account session and real-time event listener. A separate Textual client
administers it through an owner-only Unix socket.

## Current Status

Direct HomeKit live video/audio, talkback, prompt events, JPEG snapshots and bounded
HKSV H.264/AAC recording delivery are implemented through a supervised HAP adapter.
Explicit camera selection, protected stable pairing identities, owner configuration,
setup reveal/reset, warm-recording opt-in and diagnostics are available through the
local CLI/TUI. Native output and encrypted recording transport are tested locally;
real Apple/Ring device qualification remains pending. Verified offline release
packaging, explicit user-service installation and controlled upgrades are implemented.
The plugin/RTSP path remains available as optional compatibility mode. See the
[owner administration guide](docs/owner-administration.md) for setup and commands, and
[direct HomeKit engineering status](docs/direct-homekit-status.md) for validation boundaries.

Implemented:

- private, transactional Ring account and push-event credential storage
- Ring login, two-factor login, token restore, refresh persistence, and logout
- supervised real-time Ring motion and doorbell events
- per-camera incident timing with a 30-second quiet period
- event-driven, expiring per-camera media leases with one shared Ring receiver
- bounded H.264/Opus prebuffer gated on a retained SPS/PPS/IDR boundary
- H.264 passthrough and mono 48 kHz Opus normalization for compatibility clients
- development native HomeKit live video/audio, talkback, events and JPEG snapshots
- separate recording permissions, measured pre-roll and bounded native HKSV/HDS delivery
- camera inventory with an allowlisted public schema
- authenticated, versioned plugin HTTP and WebSocket APIs with event replay
- a server-owned protocol-v3 schema and fixture corpus shipped in the wheel
- revocable Scrypted pairing tokens
- digest-authenticated, on-demand loopback RTSP delivery
- cached-by-default JPEG snapshots with bounded explicit fresh capture
- capability-gated camera light and siren controls with nullable observed state
- per-camera validated two-way audio with bounded WebRTC readiness and activation signaling
- local daemon status, login, logout, shutdown, mode CAS and saved/applied settings
- explicit HomeKit selection, scoped setup reveal/reset and warm-recording owner opt-in
- verified owner light/siren actions and sanitized diagnostics/support exports
- Textual status, login, two-factor, logout, pairing, camera, and recent-event views
- the proven Halo Ring WebRTC video and audio compatibility layer

Not implemented yet:

- TLS-protected remote plugin and media access
- real Ring/Apple Home hub acceptance and measured end-to-end performance

## Platform Support

Halo Server currently uses owner-checked Unix sockets, POSIX file permissions, and advisory
file locking to protect Ring credentials and local administration. Platform labels reflect
that security boundary, not just whether Python can import the package.

| Platform | Validation tier | Notes |
| --- | --- | --- |
| Arch Linux | Verified | The complete suite and package gate run on the current Arch development host. |
| Ubuntu 24.04 or newer | Primary CI target | The checked-in workflow installs and validates the native media stack on Ubuntu. |
| Debian 12 or newer | Supported source target; distro CI pending | Official packages provide the required Python and media stack. |
| Fedora 42 or newer | Supported source target; distro CI pending | Official packages provide the required Python and media stack. |
| macOS (Apple Silicon and Intel) | Experimental | Uses Homebrew GStreamer and Apple's `getpeereid`; automated and real-camera acceptance on macOS are still required. |
| Native Windows | Unsupported | Windows does not provide Halo's current Unix credential, locking, or admin-socket security model. |
| Windows with WSL2 | Experimental | Halo can run inside Linux, but direct HomeKit discovery/networking and unattended operation under WSL2 remain unqualified. |

A supported source target has no known runtime or dependency blocker and has a documented
native package set. Debian and Fedora still require clean-host install and media smoke jobs
before their packages can be promoted as release-verified environments.
Current Linux verification is x86_64 only; ARM64 remains unverified on every Linux target.
The direct HomeKit worker CPU-affinity contract currently targets Linux; macOS
native delivery requires its own qualification.

Direct HomeKit publication is currently Linux-only. macOS and WSL2 labels above
cover experimental source use; they do not qualify direct HomeKit delivery there.
A native Windows port needs a designed credential store, file lock, local IPC
authentication and service installer. A loopback TCP admin port would weaken the
current owner boundary.

## Run From Source

Python dependencies are locked with `uv 0.11.24`. Install that exact version through
[Astral's versioned installer](https://docs.astral.sh/uv/getting-started/installation/) before
running project commands:

```bash
curl -LsSf https://astral.sh/uv/0.11.24/install.sh | sh
uv --version
```

PyGObject, GStreamer introspection, and the
GStreamer app, RTP, Opus, and H.264 parser plugins must be installed by the operating
system. Use the package set for the host:

```bash
# Arch Linux
sudo pacman -S --needed python-gobject gst-rtsp-server \
  gst-plugins-base gst-plugins-good gst-plugins-bad

# Fedora
sudo dnf install python3-gobject gstreamer1-rtsp-server \
  gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-bad-free

# Debian/Ubuntu
sudo apt install python3-gi gir1.2-gst-rtsp-server-1.0 \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
```

Use the distribution's own Python 3.11 through 3.14 so its PyGObject package and GStreamer
typelibs share one ABI. In particular, Ubuntu 22.04's default Python 3.10 is below Halo's
minimum; use Ubuntu 24.04 or newer instead of mixing an independently downloaded Python with
the system PyGObject package.

Halo Server validates `appsrc`, `queue`, `h264parse`, `rtph264pay`, and `rtpopuspay`
before binding the RTSP gateway. A missing system plugin therefore fails startup with its
element name instead of producing a camera stream that fails later.

```bash
uv venv --python /usr/bin/python3 --system-site-packages
uv sync --frozen --all-groups
uv run halo-server daemon
```

On experimental macOS, Homebrew's `gstreamer` formula bundles the former
GStreamer base, good, bad, RTSP server, and Python-binding formulae. The virtual environment
must inherit packages from Homebrew's matching Python interpreter:

```bash
brew install gstreamer
BREW_PYTHON="$(brew --prefix)/bin/python3"
"$BREW_PYTHON" -c \
  'import sys; assert (3, 11) <= sys.version_info < (3, 15), "Halo requires Python 3.11 through 3.14"'
"$BREW_PYTHON" -c \
  'import gi; gi.require_version("Gst", "1.0"); gi.require_version("GstRtspServer", "1.0"); from gi.repository import Gst, GstRtspServer'
uv venv --python "$BREW_PYTHON" --system-site-packages
uv sync --frozen --all-groups
uv run halo-server daemon
```

These macOS commands are a development path, not a supported unattended service install.
Before promoting macOS, CI must exercise the full suite on both architectures and live testing
must cover GStreamer RTSP, Ring media, clean shutdown, and restart persistence.

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

Snapshot requests use Ring's cached image endpoint by default and do not ask the camera for a
new image. An authenticated plugin can explicitly request a fresh snapshot for an event; that
operation may wake the camera and falls back to the cached image when Ring does not publish a
new timestamp. Snapshot work is limited to 8 seconds and 8 MiB, accepts JPEG data only, and is
always returned with `Cache-Control: no-store`.

Light and siren plugin routes accept exactly `{"on": true|false}` and are exposed for the
capabilities reported by the pinned Ring SDK, plus the exact mini-v3 override documented below.
Turning a siren on requests a bounded 30-second duration; turning it off requests zero seconds.
Calls for each camera are serialized so a rapid on/off sequence cannot complete out of order.
The only non-SDK control inference is the exact observed `stickup_cam_mini_v3` kind: its
validated 2K media profile matches Ring's Indoor Cam Plus product contract, which includes a
remote siren. Halo does not infer controls for other unknown kinds or from raw status fields.

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
the installed CLI, media dependency contract, and packaged protocol resources. The build job
records its Git commit and SHA-256 artifact checksums; publishing and deployment remain
separate, explicitly authorized release steps.

Managed offline releases bundle the locked Python dependencies, production HomeKit
adapter and official Node runtime. First installation includes an explicit user
unit; verified launchers, revision-checked atomic selection, a monotonic reader
floor and inherited lifetime locks protect upgrades and compatible code rollback.
The initial archive is qualified for its exact Linux host/interpreter/library
hashes. See [managed releases](docs/managed-releases.md) and
[activation and rollback](docs/managed-release-activation.md). Real Ring/Apple
qualification follows the [acceptance runbook](docs/homekit-acceptance.md).

Halo Server owns the canonical plugin wire contract under
`src/halo_server/api/protocol/v3`. Protocol v2 remains immutable for compatibility history.
Plugin repositories vendor exact copies with source-commit and SHA-256 provenance. Breaking wire
changes require a new protocol version.

## License

GPL-3.0-or-later
