"""Private account identity, separate from display names and operation attempts."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4


def new_profile_id() -> str:
    """Allocate opaque authority without deriving it from credentials or email."""
    return str(uuid4())


def valid_profile_id(value: object) -> bool:
    """Accept only the canonical spelling stored by this schema."""
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


@dataclass(frozen=True)
class AccountScope:
    """A durable credential profile and one runtime publication's authority.

    This is private server state, not a Ring principal or a public accessory ID.
    Operation attempts do not retire the scope of a still-active session.
    """

    profile_id: str
    generation: int

    def __post_init__(self) -> None:
        if not valid_profile_id(self.profile_id):
            raise ValueError("Invalid account profile identity")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("Invalid account publication generation")
