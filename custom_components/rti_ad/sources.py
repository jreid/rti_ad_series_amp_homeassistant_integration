"""The per-source name/enabled shape shared by config_flow.py and media_player.py.

A source's position in the list is its physical 1-based source number sent
to the amplifier (``SRC{nn}``); disabling a source hides it from a zone's
selectable list without disturbing that numbering.
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

_LOGGER = logging.getLogger(__name__)


class Source(TypedDict):
    """One physical input: its display name and whether zones may select it."""

    name: str
    enabled: bool


def default_source(index: int) -> Source:
    """Build an enabled source named after its 1-based physical position."""
    return {"name": f"Source {index}", "enabled": True}


def default_sources(count: int) -> list[Source]:
    """Build `count` enabled sources with default names."""
    return [default_source(i) for i in range(1, count + 1)]


def normalize_sources(raw: list[Any]) -> list[Source]:
    """Coerce config-entry data into the current shape.

    Accepts the legacy ``list[str]`` written by the old comma-separated
    field (each name becomes an enabled source) alongside the current
    ``list[Source]``, so entries created before this shape existed keep
    working without a formal migration step.
    """
    normalized: list[Source] = []
    for item in raw:
        if isinstance(item, str):
            normalized.append({"name": item, "enabled": True})
        else:
            normalized.append({"name": item["name"], "enabled": item["enabled"]})
    if not normalized:
        # The config flow always writes at least one source, so an empty
        # list here means the entry's data is corrupt or was hand-edited --
        # worth a loud note in the log even though we recover from it.
        _LOGGER.warning(
            "Config entry has no sources configured; falling back to a single "
            "default source. This shouldn't happen through normal setup -- "
            "check the entry's data for corruption."
        )
        return default_sources(1)
    return normalized


def resize_sources(existing: list[Source], count: int) -> list[Source]:
    """Pad with defaults or trim to exactly `count` entries, keeping existing values."""
    resized = list(existing[:count])
    for i in range(len(resized) + 1, count + 1):
        resized.append(default_source(i))
    return resized
