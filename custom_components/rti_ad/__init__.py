"""The RTI AD Series Amplifier integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_ZONES, DOMAIN
from .coordinator import RtiAdConfigEntry, RtiAdCoordinator
from .protocol import RtiAdClient

PLATFORMS = [Platform.MEDIA_PLAYER, Platform.NUMBER, Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: RtiAdConfigEntry) -> bool:
    """Set up an RTI AD Series amplifier from a config entry."""
    client = RtiAdClient(entry.data[CONF_HOST], entry.data[CONF_PORT])
    zones = _configured_zones(entry)

    coordinator = RtiAdCoordinator(hass, client, zones=list(range(1, zones + 1)))
    # The only unconditional read: everything after this is driven by command
    # replies, so the amplifier's single control port stays free.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    device_registry = dr.async_get(hass)
    # The amp device is also the via_device parent for each zone device, so it
    # must be created explicitly rather than left to the entity that lives on
    # it (the all-zones-off button) to bring into existence.
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


def _configured_zones(entry: RtiAdConfigEntry) -> int:
    """Return the zone count in force, preferring an options-flow override."""
    zones: int = entry.options.get(CONF_ZONES, entry.data[CONF_ZONES])
    return zones


def _zone_device_identifiers(entry_id: str, zones: int) -> set[tuple[str, str]]:
    """Return the device identifiers of the zones a config entry currently covers."""
    return {(DOMAIN, f"{entry_id}_zone_{z}") for z in range(1, zones + 1)}


def _async_prune_stale_zone_devices(
    device_registry: dr.DeviceRegistry, entry_id: str, zones: int
) -> None:
    """Remove zone devices (and their entities) left behind by a lower zone count."""
    valid = _zone_device_identifiers(entry_id, zones)
    for device in dr.async_entries_for_config_entry(device_registry, entry_id):
        if (DOMAIN, entry_id) in device.identifiers:
            continue  # the amp hub device itself
        if not device.identifiers & valid:
            device_registry.async_remove_device(device.id)


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: RtiAdConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Decide whether the user may delete a device from this config entry.

    Lowering the zone count on reload already prunes what it orphans, but a
    device can also outlive that -- renamed, or left behind by an entry that
    was reloaded while the registry held an older shape -- so the same
    in-range test is offered on demand from the device page.

    The amp hub device is the config entry made visible: deleting the entry
    is how it goes away, so it is never removable on its own.
    """
    if (DOMAIN, config_entry.entry_id) in device_entry.identifiers:
        return False
    valid = _zone_device_identifiers(
        config_entry.entry_id, _configured_zones(config_entry)
    )
    return not device_entry.identifiers & valid


async def async_unload_entry(hass: HomeAssistant, entry: RtiAdConfigEntry) -> bool:
    """Unload a config entry, dropping pending work and freeing the control port."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        entry.runtime_data.async_cancel_pending()
        await entry.runtime_data.client.close()
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
