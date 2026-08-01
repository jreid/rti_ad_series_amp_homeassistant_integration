"""Button entity for turning off every zone on the amplifier at once."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RtiAdConfigEntry, RtiAdCoordinator

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RtiAdConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the RTI AD button platform."""
    async_add_entities([RtiAdAllZonesOffButton(entry.runtime_data, entry)])


class RtiAdAllZonesOffButton(CoordinatorEntity[RtiAdCoordinator], ButtonEntity):
    """Turns off every zone on the amplifier with a single command.

    Lives on the amp hub device, not a zone device -- it isn't specific to
    any one zone -- so it sits alongside the `all_zones_off` entity service
    (media_player.py) as a dashboard-friendly way to trigger the same
    coordinator call.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "all_zones_off"

    def __init__(self, coordinator: RtiAdCoordinator, entry: RtiAdConfigEntry) -> None:
        """Place the button on the amp hub device rather than on a zone."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_all_zones_off"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_press(self) -> None:
        """Turn off every zone on the amplifier."""
        await self.coordinator.async_all_zones_off()
