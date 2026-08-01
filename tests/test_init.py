"""Tests for the top-level integration: setup, unload, and device removal.

These drive the integration end to end -- `hass.config_entries.async_setup`/
`async_unload`/`async_update_entry` against a `FakeServer` standing in for the
amplifier -- rather than calling `async_setup_entry` as a bare function, so
platform forwarding, device registry wiring, and the update-listener-triggered
reload are all exercised.
"""

from __future__ import annotations

import pytest
from harness import FakeServer, const
from harness import coordinator as coord_mod
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rti_ad import async_remove_config_entry_device

pytestmark = pytest.mark.usefixtures("socket_enabled")


def _entry(*, host, port, zones=2, sources=None):
    return MockConfigEntry(
        domain=const.DOMAIN,
        data={
            CONF_HOST: host,
            CONF_PORT: port,
            const.CONF_ZONES: zones,
            const.CONF_SOURCES: sources or ["A", "B"],
        },
    )


def _zone_devices(device_registry, entry_id):
    return [
        device
        for device in dr.async_entries_for_config_entry(device_registry, entry_id)
        if (const.DOMAIN, entry_id) not in device.identifiers
    ]


async def test_setup_entry_creates_coordinator_and_forwards_platforms(hass):
    srv = FakeServer()
    port = await srv.start()
    entry = _entry(host="127.0.0.1", port=port, zones=2)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert isinstance(entry.runtime_data, coord_mod.RtiAdCoordinator)

    device_registry = dr.async_get(hass)
    hub = device_registry.async_get_device(identifiers={(const.DOMAIN, entry.entry_id)})
    assert hub is not None
    assert hub.manufacturer == "RTI"
    # Not derived from zone count -- there's no such SKU as "AD-2x".
    assert hub.model == "AD Series"
    assert len(_zone_devices(device_registry, entry.entry_id)) == 2

    assert len(hass.states.async_all("media_player")) == 2
    assert len(hass.states.async_all("number")) == 4  # treble + bass per zone
    assert len(hass.states.async_all("button")) == 1  # all-zones-off, on the hub

    srv.stop()


async def test_unload_entry_closes_client_and_cancels_pending(hass):
    srv = FakeServer()
    port = await srv.start()
    entry = _entry(host="127.0.0.1", port=port, zones=1)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    # Unloading a platform marks its entities unavailable rather than
    # removing their state entirely, so check the entry/coordinator side
    # of the teardown instead of hass.states.
    assert entry.state is ConfigEntryState.NOT_LOADED
    zone_1 = hass.states.get("media_player.zone_1")
    assert zone_1 is not None and zone_1.state == "unavailable"
    assert coordinator.client._writer is None, "client connection should be closed"

    srv.stop()


async def test_prune_stale_zone_devices_removes_devices_for_lowered_zone_count(hass):
    srv = FakeServer()
    port = await srv.start()
    entry = _entry(host="127.0.0.1", port=port, zones=4)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    device_registry = dr.async_get(hass)
    assert len(_zone_devices(device_registry, entry.entry_id)) == 4

    hass.config_entries.async_update_entry(entry, options={const.CONF_ZONES: 2})
    await hass.async_block_till_done()

    remaining = _zone_devices(device_registry, entry.entry_id)
    assert {next(iter(d.identifiers)) for d in remaining} == {
        (const.DOMAIN, f"{entry.entry_id}_zone_1"),
        (const.DOMAIN, f"{entry.entry_id}_zone_2"),
    }

    srv.stop()


# --------------------------------------------------------------------------
# Deleting a device by hand (async_remove_config_entry_device)
# --------------------------------------------------------------------------


async def _setup_for_removal(hass, zones=2):
    srv = FakeServer()
    port = await srv.start()
    entry = _entry(host="127.0.0.1", port=port, zones=zones)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return srv, entry, dr.async_get(hass)


async def test_the_amp_hub_device_cannot_be_deleted_while_the_entry_exists(hass):
    srv, entry, device_registry = await _setup_for_removal(hass)
    hub = device_registry.async_get_device(identifiers={(const.DOMAIN, entry.entry_id)})

    assert await async_remove_config_entry_device(hass, entry, hub) is False

    srv.stop()


async def test_a_zone_device_within_the_configured_count_cannot_be_deleted(hass):
    srv, entry, device_registry = await _setup_for_removal(hass, zones=2)
    zone_2 = device_registry.async_get_device(
        identifiers={(const.DOMAIN, f"{entry.entry_id}_zone_2")}
    )

    assert await async_remove_config_entry_device(hass, entry, zone_2) is False

    srv.stop()


async def test_a_zone_device_beyond_the_configured_count_can_be_deleted(hass):
    srv, entry, device_registry = await _setup_for_removal(hass, zones=2)
    # Stands in for a device left behind by a higher zone count -- created
    # directly, since setup prunes exactly these before the hook is reachable.
    stale = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(const.DOMAIN, f"{entry.entry_id}_zone_3")},
    )

    assert await async_remove_config_entry_device(hass, entry, stale) is True

    srv.stop()
