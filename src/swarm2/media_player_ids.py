"""Pure identifiers shared by media presets, discovery and providers."""

from __future__ import annotations

import re


MAX_MEDIA_PLAYER_ID_CHARACTERS = 255
MPRIS_PLAYER_PREFIX = "org.mpris.MediaPlayer2."
APPLE_MUSIC_PLAYER_ID = "com.apple.Music"

_MPRIS_PLAYER_ID = re.compile(
    r"org\.mpris\.MediaPlayer2\."
    r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*\Z"
)


def validate_media_player_id(value: object) -> str:
    """Return one supported host player ID or reject it without I/O."""

    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_MEDIA_PLAYER_ID_CHARACTERS
        or (value != APPLE_MUSIC_PLAYER_ID
            and _MPRIS_PLAYER_ID.fullmatch(value) is None)
    ):
        raise ValueError(
            "Media player must be an Apple Music bundle ID or complete MPRIS bus name"
        )
    return value


def is_mpris_player_id(value: object) -> bool:
    """Return whether *value* is one complete supported MPRIS bus name."""

    return (
        isinstance(value, str)
        and len(value) <= MAX_MEDIA_PLAYER_ID_CHARACTERS
        and _MPRIS_PLAYER_ID.fullmatch(value) is not None
    )
