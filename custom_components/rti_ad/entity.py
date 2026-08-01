"""Shared base for entities belonging to one RTI AD-Nx zone device."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import RtiAdConfigEntry
from .const import DOMAIN
from .coordinator import RtiAdCoordinator


class RtiAdZoneEntity(CoordinatorEntity[RtiAdCoordinator]):
    """Base for the media player and number entities of one zone."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RtiAdCoordinator,
        entry: RtiAdConfigEntry,
        zone: int,
        *,
        unique_id_suffix: str = "",
    ) -> None:
        """Attach the entity to its zone device under the amp hub device."""
        super().__init__(coordinator)
        self._zone = zone
        self._attr_unique_id = f"{entry.entry_id}_zone_{zone}{unique_id_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_zone_{zone}")},
            via_device=(DOMAIN, entry.entry_id),
            name=f"Zone {zone}",
        )
