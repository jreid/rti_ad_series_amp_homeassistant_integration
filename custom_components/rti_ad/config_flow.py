"""Config flow for the RTI AD Series Amplifiers integration."""

from __future__ import annotations

import socket
from collections.abc import Callable
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_EDIT_SOURCES,
    CONF_SOURCE_COUNT,
    CONF_SOURCES,
    CONF_ZONES,
    DEFAULT_PORT,
    DEFAULT_SOURCE_COUNT,
    DEFAULT_ZONES,
    DOMAIN,
    MAX_SOURCES,
    MAX_ZONES,
    MIN_SOURCES,
    MIN_ZONES,
)
from .protocol import RtiAd4xClient, RtiAd4xError
from .sources import Source, default_sources, normalize_sources, resize_sources

DEFAULT_NAME = "RTI AD Series Amplifier"


def _sources_schema(count: int, current: list[Source]) -> dict[Any, Any]:
    """One name/enabled field pair per source row, defaulted from `current`."""
    resized = resize_sources(current, count)
    schema: dict[Any, Any] = {}
    for i, src in enumerate(resized, start=1):
        schema[vol.Required(f"source_{i}_name", default=src["name"])] = str
        schema[vol.Required(f"source_{i}_enabled", default=src["enabled"])] = bool
    return schema


def _sources_from_input(user_input: dict[str, Any], count: int) -> list[Source]:
    """Build the final source list from a submitted sources-step form.

    A blank name falls back to a placeholder rather than erroring -- the
    only thing that actually blocks submission is every row being disabled.
    """
    return [
        {
            "name": user_input[f"source_{i}_name"].strip() or f"Source {i}",
            "enabled": user_input[f"source_{i}_enabled"],
        }
        for i in range(1, count + 1)
    ]


async def _validate_connection(host: str, port: int) -> None:
    client = RtiAd4xClient(host, port)
    try:
        await client.get_status(1)
    finally:
        await client.close()


async def _validate_or_set_error(host: str, port: int, errors: dict[str, str]) -> bool:
    """Try the connection, recording `cannot_connect` on failure.

    Shared by async_step_user and async_step_reconfigure, which otherwise
    duplicated this exact try/except.
    """
    try:
        await _validate_connection(host, port)
    except RtiAd4xError:
        errors["base"] = "cannot_connect"
        return False
    return True


async def _async_step_sources(
    flow: ConfigFlow | OptionsFlow,
    user_input: dict[str, Any] | None,
    source_count: int,
    current_sources: list[Source],
    *,
    create_entry: Callable[[list[Source]], ConfigFlowResult],
) -> ConfigFlowResult:
    """The per-source name/enabled step, identical between the config and
    options flows except for what async_create_entry is called with."""
    errors: dict[str, str] = {}

    if user_input is not None:
        sources = _sources_from_input(user_input, source_count)
        if not any(s["enabled"] for s in sources):
            errors["base"] = "no_sources_enabled"
        else:
            return create_entry(sources)

    schema = vol.Schema(_sources_schema(source_count, current_sources))
    return flow.async_show_form(step_id="sources", data_schema=schema, errors=errors)


async def _resolve_unique_id(hass: HomeAssistant, host: str, port: int) -> str:
    """Canonicalize host to a resolved IP so hostname and IP dedup to one entry.

    Connections still use the user-typed host (see async_setup_entry); this
    only affects the identity used to detect a duplicate config entry.
    """
    try:
        infos = await hass.loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ip = infos[0][4][0]
    except OSError:
        ip = host
    return f"{ip}:{port}"


def _conflicting_entry(
    hass: HomeAssistant, unique_id: str, *, exclude_entry_id: str | None = None
) -> bool:
    """Does some *other* config entry already claim this identity?

    Written directly against `hass.config_entries.async_entries` rather than
    the `_abort_if_unique_id_configured`/`_abort_if_unique_id_mismatch`
    helpers: those don't fit here, since our unique_id is derived from the
    very field (host) that reconfigure exists to let the user change -- the
    entry being reconfigured matching its *own* new identity is expected, not
    a conflict, and neither helper models that distinction correctly.
    """
    return any(
        entry.unique_id == unique_id and entry.entry_id != exclude_entry_id
        for entry in hass.config_entries.async_entries(DOMAIN)
    )


class RtiAd4xConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for RTI AD Series Amplifiers."""

    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        # Carried from async_step_user to async_step_sources. Declared here
        # (rather than only appearing as a side effect of async_step_user
        # running) so the class's state is visible up front and a step
        # reached out of order degrades to an empty/harmless default instead
        # of a bare AttributeError.
        self._title: str = ""
        self._data: dict[str, Any] = {}
        self._source_count: int = 0
        self._current_sources: list[Source] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            unique_id = await _resolve_unique_id(self.hass, host, port)
            await self.async_set_unique_id(unique_id)  # dedupes in-progress flows too
            if _conflicting_entry(self.hass, unique_id):
                return self.async_abort(reason="already_configured")
            if await _validate_or_set_error(host, port, errors):
                self._title = user_input[CONF_NAME]
                self._data = {
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_ZONES: user_input[CONF_ZONES],
                }
                self._source_count = user_input[CONF_SOURCE_COUNT]
                self._current_sources = default_sources(self._source_count)
                return await self.async_step_sources()

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                vol.Required(CONF_ZONES, default=DEFAULT_ZONES): vol.All(
                    int, vol.Range(min=MIN_ZONES, max=MAX_ZONES)
                ),
                vol.Required(CONF_SOURCE_COUNT, default=DEFAULT_SOURCE_COUNT): vol.All(
                    int, vol.Range(min=MIN_SOURCES, max=MAX_SOURCES)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_sources(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await _async_step_sources(
            self,
            user_input,
            self._source_count,
            self._current_sources,
            create_entry=lambda sources: self.async_create_entry(
                title=self._title, data={**self._data, CONF_SOURCES: sources}
            ),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let a changed IP/hostname be corrected without deleting the entry."""
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            # unique_id is derived from the host (see _resolve_unique_id), so
            # it is *expected* to change here -- that's the whole point of
            # this step. The only thing to guard against is this address
            # already belonging to a different configured amp; matching the
            # entry's own current identity is not a conflict.
            unique_id = await _resolve_unique_id(self.hass, host, port)
            if _conflicting_entry(
                self.hass, unique_id, exclude_entry_id=reconfigure_entry.entry_id
            ):
                return self.async_abort(reason="already_configured")
            if await _validate_or_set_error(host, port, errors):
                # Not async_update_reload_and_abort: __init__.py already
                # registers an update listener that reloads on any entry
                # change, so scheduling a second reload here would just be
                # the redundant path HA now warns against.
                self.hass.config_entries.async_update_entry(
                    reconfigure_entry,
                    unique_id=unique_id,
                    data={**reconfigure_entry.data, CONF_HOST: host, CONF_PORT: port},
                )
                return self.async_abort(reason="reconfigure_successful")

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=reconfigure_entry.data[CONF_HOST]): str,
                vol.Required(CONF_PORT, default=reconfigure_entry.data[CONF_PORT]): int,
            }
        )
        return self.async_show_form(
            step_id="reconfigure", data_schema=schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return RtiAd4xOptionsFlow()


class RtiAd4xOptionsFlow(OptionsFlow):
    """Allow editing zone count, source count, and per-source name/enabled after setup."""

    def __init__(self) -> None:
        super().__init__()
        # Carried from async_step_init to async_step_sources; see the
        # matching note on RtiAd4xConfigFlow.__init__.
        self._zones: int = 0
        self._source_count: int = 0
        self._current_sources: list[Source] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        current = {**self.config_entry.data, **self.config_entry.options}
        current_sources = normalize_sources(current[CONF_SOURCES])

        if user_input is not None:
            self._zones = user_input[CONF_ZONES]
            self._source_count = user_input[CONF_SOURCE_COUNT]
            self._current_sources = resize_sources(current_sources, self._source_count)
            # Skip straight to create_entry when there's nothing for the
            # sources step to actually change: the source count didn't move
            # and the user didn't ask to edit names/enabled state. Without
            # this, a zones-only edit still has to page through and resubmit
            # every source row just to leave them exactly as they were.
            if (
                self._source_count == len(current_sources)
                and not user_input[CONF_EDIT_SOURCES]
            ):
                return self.async_create_entry(
                    title="",
                    data={CONF_ZONES: self._zones, CONF_SOURCES: current_sources},
                )
            return await self.async_step_sources()

        schema = vol.Schema(
            {
                vol.Required(CONF_ZONES, default=current[CONF_ZONES]): vol.All(
                    int, vol.Range(min=MIN_ZONES, max=MAX_ZONES)
                ),
                vol.Required(CONF_SOURCE_COUNT, default=len(current_sources)): vol.All(
                    int, vol.Range(min=MIN_SOURCES, max=MAX_SOURCES)
                ),
                vol.Optional(CONF_EDIT_SOURCES, default=False): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_sources(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await _async_step_sources(
            self,
            user_input,
            self._source_count,
            self._current_sources,
            create_entry=lambda sources: self.async_create_entry(
                title="", data={CONF_ZONES: self._zones, CONF_SOURCES: sources}
            ),
        )
