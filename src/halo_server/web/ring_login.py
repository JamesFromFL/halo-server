"""Single short-lived, session-bound Ring MFA flow with owned requests."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from uuid import uuid4

from halo_server.owner_client import AdminClientError
from halo_server.web.errors import WebError, text


@dataclass
class Challenge:
    id: str
    session_id: str
    epoch: str
    email: str
    password: str
    expires_at: float
    attempts: int = 0

    def clear(self):
        self.email = self.password = ""


class RingLogin:
    def __init__(self, client, pool, state, *, clock=time.time):
        self.client, self.pool, self.state, self.clock = client, pool, state, clock
        self.challenge = None
        self.task = None
        self.closed = False
        self.expiry_task = None

    def clear(self, session_id=None):
        if self.challenge is not None and (
            session_id is None or self.challenge.session_id == session_id
        ):
            self.challenge.clear()
            self.challenge = None
            if self.expiry_task is not None and self.expiry_task is not asyncio.current_task():
                self.expiry_task.cancel()

    async def epoch(self, expected):
        snapshot = await self.state.get(force=True)
        if not snapshot["daemon"]["available"]:
            raise WebError("daemon_unavailable", "Halo daemon is unavailable", status=503)
        if snapshot["state"]["epoch"] != expected:
            raise WebError(
                "account_changed", "The Ring account changed; refresh before continuing", status=409
            )

    async def begin(self, session, email, password, epoch):
        email = text(email, maximum=320)
        password = text(password, maximum=4096)
        epoch = text(epoch, maximum=256)
        if self.closed or self.task is not None and not self.task.done():
            raise WebError("busy", "A Ring connection request is already pending", status=429)
        if self.challenge is not None and self.clock() >= self.challenge.expires_at:
            self.clear()
        if self.challenge is not None:
            raise WebError(
                "busy", "Finish or cancel the current Ring connection request", status=429
            )
        await self.epoch(epoch)
        # A concurrent login may have claimed the single flow while state was read.
        if (
            self.closed
            or self.challenge is not None
            or self.task is not None
            and not self.task.done()
        ):
            raise WebError("busy", "A Ring connection request is already pending", status=429)
        challenge = Challenge(str(uuid4()), session.id, epoch, email, password, self.clock() + 300)
        self.challenge = challenge
        self.expiry_task = asyncio.create_task(self._expire(challenge), name="web-ring-mfa-expiry")
        return await self._attempt(challenge, None)

    async def _expire(self, challenge):
        await asyncio.sleep(max(0, challenge.expires_at - self.clock()))
        if self.challenge is challenge:
            self.clear()

    async def continue_(self, session, identifier, otp):
        challenge = self.challenge
        if (
            challenge is None
            or challenge.id != identifier
            or challenge.session_id != session.id
            or self.clock() >= challenge.expires_at
        ):
            if challenge and self.clock() >= challenge.expires_at:
                self.clear()
            raise WebError(
                "challenge_expired", "Ring verification expired; start again", status=409
            )
        if self.task is not None and not self.task.done():
            raise WebError("busy", "Ring verification is pending", status=429)
        await self.epoch(challenge.epoch)
        if self.closed or self.challenge is not challenge or self.clock() >= challenge.expires_at:
            self.clear()
            raise WebError("challenge_expired", "Ring verification expired", status=409)
        if self.task is not None and not self.task.done():
            raise WebError("busy", "Ring verification is pending", status=429)
        challenge.attempts += 1
        if challenge.attempts > 5:
            self.clear()
            raise WebError(
                "challenge_expired", "Ring verification attempt limit reached", status=429
            )
        return await self._attempt(challenge, text(otp, maximum=32))

    async def _attempt(self, challenge, otp):
        async def apply():
            try:
                await self.pool.call(
                    self.client.login,
                    challenge.email,
                    challenge.password,
                    otp,
                    expected_epoch=challenge.epoch,
                )
            except AdminClientError as error:
                if error.code == "two_factor_required" and self.challenge is challenge:
                    return {
                        "status": "mfa_required",
                        "challenge_id": challenge.id,
                        "expires_at": challenge.expires_at,
                    }
                if (
                    error.code == "authentication_failed"
                    and otp is not None
                    and self.challenge is challenge
                ):
                    raise WebError(
                        "authentication_failed",
                        "Ring did not accept the verification code",
                        status=401,
                    ) from None
                self.clear()
                known = error.code in {
                    "authentication_failed",
                    "session_superseded",
                    "account_changed",
                }
                raise WebError(
                    error.code if known else "ring_unavailable",
                    "Ring connection did not complete; refresh its state before trying again",
                    status=401 if error.code == "authentication_failed" else 409 if known else 503,
                    possibly_sent=not known,
                ) from None
            except WebError:
                self.clear()
                raise
            except Exception:
                self.clear()
                raise WebError(
                    "ring_unavailable",
                    "Ring connection outcome is unknown; refresh before retrying",
                    status=503,
                    possibly_sent=True,
                ) from None
            self.clear()
            await self.state.get(force=True)
            return {"status": "connected"}

        self.task = asyncio.create_task(apply(), name="web-ring-login")
        self.task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        return await asyncio.shield(self.task)

    def cancel(self, session, identifier):
        if self.challenge is not None and (
            self.challenge.id != identifier or self.challenge.session_id != session.id
        ):
            raise WebError("not_found", "Ring verification was not found", status=404)
        self.clear(session.id)
        return {"cancelled": True, "possibly_sent": self.task is not None and not self.task.done()}

    async def close(self):
        self.closed = True
        self.clear()
        if self.expiry_task:
            self.expiry_task.cancel()
            await asyncio.gather(self.expiry_task, return_exceptions=True)
        if self.task and not self.task.done():
            await asyncio.wait((self.task,))
