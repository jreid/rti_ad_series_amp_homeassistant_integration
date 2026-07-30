"""Treble/bass number entities for the RTI AD-4x integration, one pair per zone."""

from __future__ import annotations

from typing import Literal

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import RtiAd4xConfigEntry
from .const import CONF_ZONES, DOMAIN, MAX_TONE_DB, MIN_TONE_DB, TONE_STEP_DB
from .coordinator import RtiAd4xCoordinator

PARALLEL_UPDATES = 1

ToneKind = Literal["treble", "bass"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RtiAd4xConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    zones: int = entry.options.get(CONF_ZONES, entry.data[CONF_ZONES])

    async_add_entities(
        RtiAd4xToneNumber(coordinator, entry, zone, kind)
        for zone in range(1, zones + 1)
        for kind in ("treble", "bass")
    )


class RtiAd4xToneNumber(CoordinatorEntity[RtiAd4xCoordinator], NumberEntity):
    """A zone's treble or bass control, read back from the amplifier."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "dB"
    _attr_native_min_value = MIN_TONE_DB
    _attr_native_max_value = MAX_TONE_DB
    _attr_native_step = TONE_STEP_DB
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: RtiAd4xCoordinator,
        entry: RtiAd4xConfigEntry,
        zone: int,
        kind: ToneKind,
    ) -> None:
        super().__init__(coordinator)
        self._zone = zone
        self._kind = kind
        self._attr_unique_id = f"{entry.entry_id}_zone_{zone}_{kind}"
        self._attr_translation_key = kind
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_zone_{zone}")},
            via_device=(DOMAIN, entry.entry_id),
            name=f"Zone {zone}",
        )

    @property
    def native_value(self) -> float | None:
        pending = (
            self.coordinator.pending_treble(self._zone)
            if self._kind == "treble"
            else self.coordinator.pending_bass(self._zone)
        )
        if pending is not None:
            return pending
        if (data := (self.coordinator.data or {}).get(self._zone)) is None:
            return None
        if data.tone is None:
            return None
        return data.tone.treble_db if self._kind == "treble" else data.tone.bass_db

    async def async_set_native_value(self, value: float) -> None:
        db = round(value)
        if self._kind == "treble":
            await self.coordinator.async_set_treble(self._zone, db)
        else:
            await self.coordinator.async_set_bass(self._zone, db)
