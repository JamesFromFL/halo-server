"""Local password enrollment, bounded KDF work and opaque browser sessions."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
import time
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, replace
from uuid import uuid4

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

from halo_server.private_json import PrivateJsonError, PrivateJsonStore
from halo_server.web.defaults import OWNER_USERNAME
from halo_server.web.errors import WebError, text
from halo_server.web.settings import web_directory

COOKIE = "__Host-HaloSession"
MAX_SESSIONS = 8
MAX_ANONYMOUS = 32
IDLE_SECONDS = 1800
ABSOLUTE_SECONDS = 43200
RECENT_SECONDS = 300
SETUP_SECONDS = 600
PHC = re.compile(r"\$argon2id\$v=19\$m=65536,t=3,p=1\$[A-Za-z0-9+/]{22}\$[A-Za-z0-9+/]{43}")


class AuthStoreError(PrivateJsonError):
    pass


class AuthConflict(AuthStoreError):
    pass


@dataclass(frozen=True)
class AuthRecord:
    version: int = 1
    revision: int = 0
    verifier: str | None = None
    setup_digest: str | None = None
    setup_expires_at: float = 0.0

    def __post_init__(self):
        if type(self.version) is not int or self.version != 1:
            raise AuthStoreError("Web authentication version is unsupported")
        if type(self.revision) is not int or not 0 <= self.revision < 2**53:
            raise AuthStoreError("Web authentication revision is invalid")
        if self.verifier is not None and (
            not isinstance(self.verifier, str) or not PHC.fullmatch(self.verifier)
        ):
            raise AuthStoreError("Web password verifier is invalid")
        if self.setup_digest is not None and (
            not isinstance(self.setup_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.setup_digest)
        ):
            raise AuthStoreError("Web setup verifier is invalid")
        if (
            type(self.setup_expires_at) not in {int, float}
            or not 0 <= self.setup_expires_at < 2**53
        ):
            raise AuthStoreError("Web setup expiry is invalid")
        if self.verifier is not None and self.setup_digest is not None:
            raise AuthStoreError("Configured web authentication cannot be claimed again")

    def as_dict(self):
        return asdict(self)


class AuthStore(PrivateJsonStore[AuthRecord]):
    def __init__(self, paths):
        super().__init__(
            web_directory(paths) / "authentication.json",
            snapshot_type=AuthRecord,
            decode=lambda value: AuthRecord(**value),
            default=AuthRecord,
            max_bytes=4096,
            label="Web authentication",
            noun="web authentication",
            error_type=AuthStoreError,
            conflict_type=AuthConflict,
        )


def issue_setup(paths, *, reset=False):
    """Explicit owner CLI recovery; never touch Ring or HomeKit private state."""
    store = AuthStore(paths)
    code = secrets.token_urlsafe(32)
    expires = time.time() + SETUP_SECONDS

    def enroll(current):
        if current.verifier is not None and not reset:
            raise AuthStoreError("Web authentication already exists; use explicit recovery")
        return replace(current, verifier=None, setup_digest=_digest(code), setup_expires_at=expires)

    store.update(enroll)
    return {"setup_code": code, "expires_at": expires}


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _owner_matches(value):
    # A public single-owner identifier; the password remains the secret. Keep
    # the v1 credential record readable by retained recovery releases.
    return (
        isinstance(value, str)
        and len(value) <= 64
        and value.isascii()
        and hmac.compare_digest(value, OWNER_USERNAME)
    )


def _password(value):
    return text(value, minimum=15, maximum=128).encode("utf-8")


def _derive(value):
    return Argon2id(
        salt=secrets.token_bytes(16), length=32, iterations=3, lanes=1, memory_cost=65536
    ).derive_phc_encoded(value)


def _verify(value, verifier):
    try:
        Argon2id.verify_phc_encoded(value, verifier)
        return True
    except (InvalidKey, ValueError):
        return False


@dataclass
class Session:
    id: str
    key: str
    csrf: str
    authenticated: bool
    created: float
    touched: float
    reauthenticated_at: float
    created_at: float


class RateLimiter:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.peers = OrderedDict()
        self.global_attempts = deque()

    def admit(self, peer):
        now = self.clock()
        for key, (last, _) in tuple(self.peers.items()):
            if now - last >= 60:
                del self.peers[key]
        while self.global_attempts and now - self.global_attempts[0] >= 60:
            self.global_attempts.popleft()
        attempts = self.peers.get(peer, (now, deque()))[1]
        while attempts and now - attempts[0] >= 30:
            attempts.popleft()
        if len(attempts) >= 5 or len(self.global_attempts) >= 20 or len(self.peers) >= 256:
            raise WebError(
                "rate_limited", "Please wait before trying authentication again", status=429
            )
        attempts.append(now)
        self.peers[peer] = (now, attempts)
        self.global_attempts.append(now)


class Authentication:
    def __init__(self, paths, *, clock=time.monotonic, wall_clock=time.time):
        self.store = AuthStore(paths)
        self.clock, self.wall_clock = clock, wall_clock
        self.record = AuthRecord()
        self.sessions = {}
        self.rates = RateLimiter(clock=clock)
        self.kdf = asyncio.Semaphore(2)
        self.tasks = set()
        self.refresh_task = None
        self.closed = False
        self.on_revoke = lambda _session: None

    async def _owned(self, function, *args, **kwargs):
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self.tasks.add(task)
        task.add_done_callback(self._finished)
        return await asyncio.shield(task)

    def _finished(self, task):
        self.tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def refresh(self):
        async def read():
            record = await self._owned(self.store.load)
            if record.revision < self.record.revision:
                self.revoke_all()
                raise AuthStoreError("Web authentication state moved backwards")
            if record.revision != self.record.revision:
                self.revoke_all()
                self.record = record
            self._expire()

        # The shared task owns the file read even if all HTTP waiters disconnect.
        if self.refresh_task is None or self.refresh_task.done():
            self.refresh_task = asyncio.create_task(read(), name="web-auth-refresh")
            self.tasks.add(self.refresh_task)
            self.refresh_task.add_done_callback(self._finished)
        await asyncio.shield(self.refresh_task)

    def _expire(self):
        now = self.clock()
        for key, session in tuple(self.sessions.items()):
            duration = ABSOLUTE_SECONDS if session.authenticated else SETUP_SECONDS
            if now - session.created >= duration or now - session.touched >= IDLE_SECONDS:
                self.revoke(key)

    def revoke(self, key):
        session = self.sessions.pop(key, None)
        if session is not None:
            self.on_revoke(session.id)

    def revoke_all(self):
        for key in tuple(self.sessions):
            self.revoke(key)

    def get(self, token, *, authenticated=False, touch=True):
        self._expire()
        session = (
            self.sessions.get(_digest(token))
            if isinstance(token, str) and len(token) <= 128
            else None
        )
        if session is None or authenticated and not session.authenticated:
            if authenticated:
                raise WebError("authentication_required", "Sign in to Halo Server", status=401)
            return None
        if touch:
            session.touched = self.clock()
        return session

    def create(self, *, authenticated=False):
        if self.closed:
            raise WebError("unavailable", "The web console is stopping", status=503)
        self._expire()
        limit = MAX_SESSIONS if authenticated else MAX_ANONYMOUS
        current = [row for row in self.sessions.values() if row.authenticated is authenticated]
        if len(current) >= limit:
            if authenticated:
                raise WebError(
                    "session_capacity", "The browser session limit has been reached", status=429
                )
            self.revoke(min(current, key=lambda row: row.touched).key)
        token = secrets.token_urlsafe(32)
        now = self.clock()
        session = Session(
            str(uuid4()),
            _digest(token),
            secrets.token_urlsafe(32),
            authenticated,
            now,
            now,
            now if authenticated else -RECENT_SECONDS,
            self.wall_clock(),
        )
        self.sessions[session.key] = session
        return session, token

    def view(self, session):
        return {
            "authenticated": session.authenticated,
            "setup_required": self.record.verifier is None,
            "csrf_token": session.csrf,
            "reauthenticated": session.authenticated
            and self.clock() - session.reauthenticated_at < RECENT_SECONDS,
            "session_id": session.id,
        }

    def recent(self, session):
        if self.clock() - session.reauthenticated_at >= RECENT_SECONDS:
            raise WebError(
                "recent_auth_required", "Confirm your Halo password for this action", status=403
            )

    def csrf(self, session, value):
        if (
            session is None
            or not isinstance(value, str)
            or not value.isascii()
            or not hmac.compare_digest(session.csrf, value)
        ):
            raise WebError(
                "csrf_rejected", "Refresh the page before trying this action", status=403
            )

    async def _work(self, function, *args):
        if self.closed or self.kdf.locked():
            raise WebError("busy", "Authentication is busy; please try again shortly", status=429)
        await self.kdf.acquire()

        async def run():
            try:
                return await asyncio.to_thread(function, *args)
            finally:
                self.kdf.release()

        task = asyncio.create_task(run())
        self.tasks.add(task)
        task.add_done_callback(self._finished)
        return await asyncio.shield(task)

    async def authenticate(self, session, password, peer, *, username):
        self.rates.admit(peer)
        encoded = _password(password)
        record = self.record
        # A wrong username must not bypass password verification or throttling.
        verified = record.verifier is not None and await self._work(
            _verify, encoded, record.verifier
        )
        if not verified or not _owner_matches(username):
            raise WebError(
                "authentication_failed", "Halo authentication was not accepted", status=401
            )
        await self.refresh()
        if self.record.revision != record.revision or session.key not in self.sessions:
            raise WebError(
                "authentication_changed", "Web authentication changed; sign in again", status=401
            )
        self.revoke(session.key)
        return self.create(authenticated=True)

    async def setup(self, session, code, password, peer, *, username):
        self.rates.admit(peer)
        encoded = _password(password)
        record = self.record
        if (
            not _owner_matches(username)
            or record.verifier is not None
            or record.setup_digest is None
            or self.wall_clock() >= record.setup_expires_at
            or not isinstance(code, str)
            or len(code) > 128
            or not hmac.compare_digest(record.setup_digest, _digest(code))
        ):
            raise WebError("setup_rejected", "Setup code is invalid or expired", status=401)
        verifier = await self._work(_derive, encoded)
        if self.wall_clock() >= record.setup_expires_at or session.key not in self.sessions:
            raise WebError("setup_rejected", "Setup code or browser session expired", status=401)
        try:
            saved = await self._owned(
                self.store.save,
                replace(record, verifier=verifier, setup_digest=None, setup_expires_at=0),
                expected_revision=record.revision,
            )
        except AuthConflict:
            raise WebError(
                "setup_rejected", "Setup state changed; refresh the page", status=409
            ) from None
        await self.refresh()
        if self.record != saved:
            raise WebError(
                "authentication_changed", "Web authentication changed; sign in again", status=401
            )
        return self.create(authenticated=True)

    async def change_password(self, session, current_password, password, peer):
        self.recent(session)
        self.rates.admit(peer)
        old, new = _password(current_password), _password(password)
        record = self.record
        if record.verifier is None or not await self._work(_verify, old, record.verifier):
            raise WebError(
                "authentication_failed", "Halo authentication was not accepted", status=401
            )
        verifier = await self._work(_derive, new)
        if session.key not in self.sessions:
            raise WebError("authentication_required", "Sign in again", status=401)
        saved = await self._owned(
            self.store.save, replace(record, verifier=verifier), expected_revision=record.revision
        )
        await self.refresh()
        if self.record != saved:
            raise WebError(
                "authentication_changed", "Web authentication changed; sign in again", status=401
            )
        return self.create(authenticated=True)

    def list_sessions(self, current):
        self._expire()
        return {
            "sessions": [
                {
                    "session_id": row.id,
                    "current": row.id == current.id,
                    "created_at": row.created_at,
                }
                for row in self.sessions.values()
                if row.authenticated
            ]
        }

    async def close(self):
        self.closed = True
        self.revoke_all()
        if self.tasks:
            await asyncio.wait(tuple(self.tasks))
