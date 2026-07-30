"""Config flow for the RTI AD-4x integration."""

from __future__ import annotations

import socket
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_SOURCES,
    CONF_ZONES,
    DEFAULT_PORT,
    DEFAULT_SOURCES,
    DEFAULT_ZONES,
    DOMAIN,
    MAX_ZONES,
    MIN_ZONES,
)
from .protocol import RtiAd4xClient, RtiAd4xError

DEFAULT_NAME = "RTI AD-4x"


def _sources_to_string(sources: list[str]) -> str:
    return ", ".join(sources)


def _string_to_sources(value: str) -> list[str]:
    sources = [s.strip() for s in value.split(",") if s.strip()]
    if not sources:
        raise ValueError("At least one source name is required")
    return sources


async def _validate_connection(host: str, port: int) -> None:
    client = RtiAd4xClient(host, port)
    try:
        await client.get_status(1)
    finally:
        await client.close()


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
    """Handle a config flow for RTI AD-4x."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            try:
                sources = _string_to_sources(user_input[CONF_SOURCES])
            except ValueError:
                errors[CONF_SOURCES] = "no_sources"
            else:
                unique_id = await _resolve_unique_id(self.hass, host, port)
                await self.async_set_unique_id(unique_id)  # dedupes in-progress flows too
                if _conflicting_entry(self.hass, unique_id):
                    return self.async_abort(reason="already_configured")
                try:
                    await _validate_connection(host, port)
                except RtiAd4xError:
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title=user_input[CONF_NAME],
                        data={
                            CONF_HOST: host,
                            CONF_PORT: port,
                            CONF_ZONES: user_input[CONF_ZONES],
                            CONF_SOURCES: sources,
                        },
                    )

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                vol.Required(CONF_ZONES, default=DEFAULT_ZONES): vol.All(
                    int, vol.Range(min=MIN_ZONES, max=MAX_ZONES)
                ),
                vol.Required(
                    CONF_SOURCES, default=_sources_to_string(DEFAULT_SOURCES)
                ): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
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
            try:
                await _validate_connection(host, port)
            except RtiAd4xError:
                errors["base"] = "cannot_connect"
            else:
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
                vol.Required(
                    CONF_HOST, default=reconfigure_entry.data[CONF_HOST]
                ): str,
                vol.Required(
                    CONF_PORT, default=reconfigure_entry.data[CONF_PORT]
                ): int,
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
    """Allow editing zone count and source names after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        current = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            try:
                sources = _string_to_sources(user_input[CONF_SOURCES])
            except ValueError:
                errors[CONF_SOURCES] = "no_sources"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_ZONES: user_input[CONF_ZONES],
                        CONF_SOURCES: sources,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_ZONES, default=current[CONF_ZONES]): vol.All(
                    int, vol.Range(min=MIN_ZONES, max=MAX_ZONES)
                ),
                vol.Required(
                    CONF_SOURCES, default=_sources_to_string(current[CONF_SOURCES])
                ): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
