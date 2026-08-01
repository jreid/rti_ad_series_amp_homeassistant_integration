"""The RTI AD Series Amplifiers integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_ZONES, DOMAIN
from .coordinator import RtiAdCoordinator
from .protocol import RtiAdClient

PLATFORMS = [Platform.MEDIA_PLAYER, Platform.NUMBER, Platform.BUTTON]

type RtiAdConfigEntry = ConfigEntry[RtiAdCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: RtiAdConfigEntry) -> bool:
    client = RtiAdClient(entry.data[CONF_HOST], entry.data[CONF_PORT])
    zones = entry.options.get(CONF_ZONES, entry.data[CONF_ZONES])

    coordinator = RtiAdCoordinator(hass, client, zones=list(range(1, zones + 1)))
    # The only unconditional read: everything after this is driven by command
    # replies, so the amplifier's single control port stays free.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    device_registry = dr.async_get(hass)
    # The amp device is also the via_device parent for each zone device, so it
    # must be created explicitly even though it now carries its own entity
    # (the all-zones-off button) as well.
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="RTI",
        # Not f"AD-{zones}x": zone/source counts are configured independently
        # of any real product SKU (see sources.py), so a count like 3 or 6
        # doesn't correspond to an actual RTI model number.
        model="AD Series",
    )
    _async_prune_stale_zone_devices(device_registry, entry.entry_id, zones)

    # all_zones_off is also registered by the media_player platform as an
    # entity service, for automations that already target a zone entity;
    # per-zone tone control lives on the number platform.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _async_prune_stale_zone_devices(
    device_registry: dr.DeviceRegistry, entry_id: str, zones: int
) -> None:
    """Remove zone devices (and their entities) left behind by a lower zone count."""
    valid = {(DOMAIN, f"{entry_id}_zone_{z}") for z in range(1, zones + 1)}
    for device in dr.async_entries_for_config_entry(device_registry, entry_id):
        if (DOMAIN, entry_id) in device.identifiers:
            continue  # the amp hub device itself
        if not device.identifiers & valid:
            device_registry.async_remove_device(device.id)


async def async_unload_entry(hass: HomeAssistant, entry: RtiAdConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        entry.runtime_data.async_cancel_pending()
        await entry.runtime_data.client.close()
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
