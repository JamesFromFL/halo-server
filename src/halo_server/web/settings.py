"""Private, revisioned web settings and explicit local HTTPS enrollment."""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import os
import re
import secrets
import ssl
import stat
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from halo_server.paths import open_private_directory
from halo_server.private_json import PrivateJsonError, PrivateJsonStore
from halo_server.web.defaults import DEFAULT_WEB_PORT


class WebSettingsError(PrivateJsonError):
    pass


class WebSettingsConflict(WebSettingsError):
    pass


def canonical_hostname(value):
    if not isinstance(value, str) or not value or len(value) > 253:
        raise WebSettingsError("Web hostname is invalid")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        if value != value.lower() or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in value.split(".")
        ):
            raise WebSettingsError("Web hostname is invalid") from None
        return value


@dataclass(frozen=True)
class WebSettings:
    version: int = 1
    revision: int = 0
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = DEFAULT_WEB_PORT
    canonical_host: str = "127.0.0.1"
    lan_enabled: bool = False
    certificate_file: str | None = None

    def __post_init__(self):
        if type(self.version) is not int or self.version != 1:
            raise WebSettingsError("Web settings version is unsupported")
        if type(self.revision) is not int or not 0 <= self.revision < 2**53:
            raise WebSettingsError("Web settings revision is invalid")
        if type(self.enabled) is not bool or type(self.lan_enabled) is not bool:
            raise WebSettingsError("Web settings flags are invalid")
        try:
            address = ipaddress.ip_address(self.host)
        except ValueError:
            raise WebSettingsError("Web bind address must be a literal IP address") from None
        if not address.is_loopback and not self.lan_enabled:
            raise WebSettingsError("LAN web access requires explicit local enablement")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise WebSettingsError("Web port is invalid")
        if canonical_hostname(self.canonical_host) != self.canonical_host:
            raise WebSettingsError("Web hostname is not canonical")
        try:
            wildcard = ipaddress.ip_address(self.canonical_host).is_unspecified
        except ValueError:
            wildcard = False
        if wildcard:
            raise WebSettingsError("Canonical web URL cannot use a wildcard address")
        if self.certificate_file is not None and (
            not isinstance(self.certificate_file, str)
            or not re.fullmatch(r"tls-[0-9a-f]{64}\.pem", self.certificate_file)
        ):
            raise WebSettingsError("Web certificate identity is invalid")
        if self.enabled and self.certificate_file is None:
            raise WebSettingsError("Enabled web access requires a certificate")

    @property
    def authority(self):
        name = f"[{self.canonical_host}]" if ":" in self.canonical_host else self.canonical_host
        return f"{name}:{self.port}" if self.port != 443 else name

    @property
    def origin(self):
        return f"https://{self.authority}"

    def as_dict(self):
        return asdict(self)


class SettingsStore(PrivateJsonStore[WebSettings]):
    def __init__(self, paths):
        super().__init__(
            web_directory(paths) / "settings.json",
            snapshot_type=WebSettings,
            decode=lambda value: WebSettings(**value),
            default=WebSettings,
            max_bytes=4096,
            label="Web settings",
            noun="web settings",
            error_type=WebSettingsError,
            conflict_type=WebSettingsConflict,
        )


def web_directory(paths):
    return Path(paths.state_directory) / "web"


def _read_file(directory, name):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size > 65536
        ):
            raise WebSettingsError("Private web certificate file is unsafe")
        data = os.read(descriptor, 65537)
        if len(data) != info.st_size:
            raise WebSettingsError("Private web certificate file changed")
        return data
    finally:
        os.close(descriptor)


def _write_file(directory, name, body):
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(body)
            stream.flush()
            os.fsync(descriptor)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)


def _certificate(paths, settings):
    directory = open_private_directory(web_directory(paths))
    try:
        bundle = _read_file(directory, settings.certificate_file)
        return _validate_certificate(bundle, settings.canonical_host)
    finally:
        os.close(directory)


def _validate_certificate(bundle, hostname):
    try:
        key = serialization.load_pem_private_key(bundle, password=None)
        cert = x509.load_pem_x509_certificate(bundle)
        encoding = serialization.Encoding.DER
        form = serialization.PublicFormat.SubjectPublicKeyInfo
        if key.public_key().public_bytes(encoding, form) != cert.public_key().public_bytes(
            encoding, form
        ):
            raise WebSettingsError("Web certificate does not match its private key")
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        try:
            matching = ipaddress.ip_address(hostname) in names.get_values_for_type(x509.IPAddress)
        except ValueError:
            matching = hostname in names.get_values_for_type(x509.DNSName)
        if (
            not matching
            or not cert.not_valid_before_utc <= datetime.now(UTC) < cert.not_valid_after_utc
        ):
            raise WebSettingsError("Web certificate does not cover the hostname or has expired")
        return cert
    except (ValueError, TypeError, x509.ExtensionNotFound) as error:
        raise WebSettingsError("Web certificate is invalid") from error


def _new_certificate(hostname):
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Halo Server")])
    try:
        san = x509.IPAddress(ipaddress.ip_address(hostname))
    except ValueError:
        san = x509.DNSName(hostname)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([san]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), False)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM) + key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _stage_certificate(paths, bundle, hostname):
    cert = _validate_certificate(bundle, hostname)
    name = "tls-" + cert.fingerprint(hashes.SHA256()).hex() + ".pem"
    directory = open_private_directory(web_directory(paths), create=True)
    try:
        # Immutable identities allow a settings CAS to select a complete key/cert pair.
        try:
            existing = _read_file(directory, name)
        except FileNotFoundError:
            if len([entry for entry in os.listdir(directory) if entry.startswith("tls-")]) >= 32:
                raise WebSettingsError("Certificate history is full; use local recovery") from None
            _write_file(directory, name, bundle)
        else:
            if existing != bundle:
                raise WebSettingsError("Certificate identity already has different private data")
    finally:
        os.close(directory)
    return name


def configure(
    paths,
    *,
    host="127.0.0.1",
    port=DEFAULT_WEB_PORT,
    canonical_host=None,
    allow_lan=False,
    renew_certificate=False,
):
    store = SettingsStore(paths)
    current = store.load()
    proposed = replace(
        current,
        host=host,
        port=port,
        canonical_host=canonical_hostname(canonical_host or host),
        lan_enabled=allow_lan,
    )
    if renew_certificate or current.certificate_file is None:
        name = _stage_certificate(
            paths, _new_certificate(proposed.canonical_host), proposed.canonical_host
        )
        proposed = replace(proposed, certificate_file=name)
    else:
        _certificate(paths, proposed)
    proposed = replace(proposed, enabled=True)
    store.save(proposed, expected_revision=current.revision)
    return access_info(paths)


def access_info(paths, settings=None):
    settings = settings or SettingsStore(paths).load()
    result = {
        "enabled": settings.enabled,
        "host": settings.host,
        "port": settings.port,
        "lan_enabled": settings.lan_enabled,
        "canonical_url": settings.origin,
        "certificate_fingerprint": None,
        "certificate_expires_at": None,
    }
    if settings.enabled:
        cert = _certificate(paths, settings)
        result.update(
            certificate_fingerprint=hashlib.sha256(
                cert.public_bytes(serialization.Encoding.DER)
            ).hexdigest(),
            certificate_expires_at=cert.not_valid_after_utc.timestamp(),
        )
    return result


def tls_context(paths, settings):
    _certificate(paths, settings)
    directory = open_private_directory(web_directory(paths))
    descriptor = -1
    try:
        _read_file(directory, settings.certificate_file)
        descriptor = os.open(
            settings.certificate_file, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(f"/proc/self/fd/{descriptor}")
        return context
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory)


def import_certificate(paths, certificate, private_key):
    """Explicit local CLI import; HTTP never accepts filesystem paths."""
    store = SettingsStore(paths)
    current = store.load()
    pieces = []
    for filename in (certificate, private_key):
        with open(filename, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise WebSettingsError("Certificate input must be a regular file")
            value = stream.read(65537)
            if len(value) > 32768:
                raise WebSettingsError("Certificate input is too large")
            pieces.append(value)
    name = _stage_certificate(paths, b"\n".join(pieces), current.canonical_host)
    store.save(replace(current, certificate_file=name), expected_revision=current.revision)
    return access_info(paths)
