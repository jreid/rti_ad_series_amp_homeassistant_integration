"""Treble/bass number entities for the RTI AD Series Amplifier integration, one pair per zone."""

from __future__ import annotations

from typing import Literal

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import RtiAdConfigEntry
from .const import CONF_ZONES, MAX_TONE_DB, MIN_TONE_DB, TONE_STEP_DB
from .coordinator import RtiAdCoordinator
from .entity import RtiAdZoneEntity

PARALLEL_UPDATES = 1

ToneKind = Literal["treble", "bass"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RtiAdConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    zones: int = entry.options.get(CONF_ZONES, entry.data[CONF_ZONES])

    async_add_entities(
        RtiAdToneNumber(coordinator, entry, zone, kind)
        for zone in range(1, zones + 1)
        for kind in ("treble", "bass")
    )


class RtiAdToneNumber(RtiAdZoneEntity, NumberEntity):
    """A zone's treble or bass control, read back from the amplifier."""

    _attr_native_unit_of_measurement = "dB"
    _attr_native_min_value = MIN_TONE_DB
    _attr_native_max_value = MAX_TONE_DB
    _attr_native_step = TONE_STEP_DB
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: RtiAdCoordinator,
        entry: RtiAdConfigEntry,
        zone: int,
        kind: ToneKind,
    ) -> None:
        super().__init__(coordinator, entry, zone, unique_id_suffix=f"_{kind}")
        self._kind = kind
        self._attr_translation_key = kind

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
