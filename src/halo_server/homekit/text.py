"""Literal DTO text rules pinned to Unicode16 for private protocol v1.

Python3.11 and Node use different Unicode databases. Keeping these Cc/Cf/Cs
ranges explicit prevents their versions from changing accepted wire authority.
This module has no SDK, media-runtime or IPC imports.
"""

from __future__ import annotations

from bisect import bisect_right

FORBIDDEN_TEXT_RANGES = (
    (0x0, 0x1F),
    (0x7F, 0x9F),
    (0xAD, 0xAD),
    (0x600, 0x605),
    (0x61C, 0x61C),
    (0x6DD, 0x6DD),
    (0x70F, 0x70F),
    (0x890, 0x891),
    (0x8E2, 0x8E2),
    (0x180E, 0x180E),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x2064),
    (0x2066, 0x206F),
    (0xD800, 0xDFFF),
    (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB),
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x13430, 0x1343F),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
)
_STARTS = tuple(start for start, _ in FORBIDDEN_TEXT_RANGES)


def _permitted_character(char: str) -> bool:
    code = ord(char)
    index = bisect_right(_STARTS, code) - 1
    return index < 0 or code > FORBIDDEN_TEXT_RANGES[index][1]


def literal_text(value: object, maximum: int, *, empty: bool = True) -> bool:
    return (
        isinstance(value, str)
        and (empty or bool(value))
        and len(value) <= maximum
        and all(_permitted_character(char) for char in value)
    )


def safe_display_text(value: object, maximum: int = 256) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(char for char in value if _permitted_character(char))[:maximum]
