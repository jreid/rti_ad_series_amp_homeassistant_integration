"""Shared base for entities belonging to one RTI AD-Nx zone device."""

from __future__ import annotations

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import RtiAd4xConfigEntry
from .const import DOMAIN
from .coordinator import RtiAd4xCoordinator


class RtiAd4xZoneEntity(CoordinatorEntity[RtiAd4xCoordinator]):
    """Base for the media player and number entities of one zone."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RtiAd4xCoordinator,
        entry: RtiAd4xConfigEntry,
        zone: int,
        *,
        unique_id_suffix: str = "",
    ) -> None:
        super().__init__(coordinator)
        self._zone = zone
        self._attr_unique_id = f"{entry.entry_id}_zone_{zone}{unique_id_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_zone_{zone}")},
            via_device=(DOMAIN, entry.entry_id),
            name=f"Zone {zone}",
        )
