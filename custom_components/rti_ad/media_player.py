"""Media player entities for the RTI AD Series Amplifiers integration, one per zone."""

from __future__ import annotations

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import RtiAdConfigEntry
from .const import CONF_SOURCES, CONF_ZONES, SERVICE_ALL_ZONES_OFF
from .coordinator import RtiAdCoordinator
from .entity import RtiAdZoneEntity
from .protocol import ZoneStatus
from .sources import Source, normalize_sources

PARALLEL_UPDATES = 1

SUPPORTED_FEATURES = (
    MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.SELECT_SOURCE
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RtiAdConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    sources = normalize_sources(
        entry.options.get(CONF_SOURCES, entry.data[CONF_SOURCES])
    )
    zones: int = entry.options.get(CONF_ZONES, entry.data[CONF_ZONES])

    async_add_entities(
        RtiAdZoneMediaPlayer(coordinator, entry, zone, sources)
        for zone in range(1, zones + 1)
    )

    # Entity service rather than a domain service: HA resolves the target to
    # the entity objects themselves, so the call reaches the right amplifier
    # even with several configured, targeting by area or device works, and the
    # registration is torn down with the platform. Kept as a service (rather
    # than e.g. a switch) because it's one command instead of four separate
    # zone-off calls, which matters at the amplifier's 100 ms command pacing.
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_ALL_ZONES_OFF, None, "async_all_zones_off"
    )


class RtiAdZoneMediaPlayer(RtiAdZoneEntity, MediaPlayerEntity):
    """One RTI AD-Nx zone, exposed as a media player."""

    _attr_name = None  # primary entity of the zone device; no name suffix
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = SUPPORTED_FEATURES

    def __init__(
        self,
        coordinator: RtiAdCoordinator,
        entry: RtiAdConfigEntry,
        zone: int,
        sources: list[Source],
    ) -> None:
        super().__init__(coordinator, entry, zone)
        self._sources = sources

    @property
    def _status(self) -> ZoneStatus | None:
        if (data := (self.coordinator.data or {}).get(self._zone)) is not None:
            return data.status
        return None

    def _source_name(self, index: int) -> str:
        if 1 <= index <= len(self._sources):
            return self._sources[index - 1]["name"]
        return f"Source {index}"

    @property
    def state(self) -> MediaPlayerState | None:
        status = self._status
        if status is None:
            return None
        return MediaPlayerState.ON if status.power else MediaPlayerState.OFF

    @property
    def volume_level(self) -> float | None:
        if (pending := self.coordinator.pending_volume(self._zone)) is not None:
            return pending
        status = self._status
        return status.volume_level if status else None

    @property
    def is_volume_muted(self) -> bool | None:
        if (pending := self.coordinator.pending_mute(self._zone)) is not None:
            return pending
        status = self._status
        return status.mute if status else None

    @property
    def source(self) -> str | None:
        status = self._status
        return self._source_name(status.source) if status else None

    @property
    def source_list(self) -> list[str]:
        return [s["name"] for s in self._sources if s["enabled"]]

    async def async_turn_on(self) -> None:
        await self.coordinator.async_set_power(self._zone, True)

    async def async_turn_off(self) -> None:
        await self.coordinator.async_set_power(self._zone, False)

    async def async_set_volume_level(self, volume: float) -> None:
        await self.coordinator.async_set_volume(self._zone, volume)

    async def async_volume_up(self) -> None:
        await self.coordinator.async_step_volume(self._zone, 1)

    async def async_volume_down(self) -> None:
        await self.coordinator.async_step_volume(self._zone, -1)

    async def async_mute_volume(self, mute: bool) -> None:
        await self.coordinator.async_set_mute(self._zone, mute)

    async def async_select_source(self, source: str) -> None:
        # Looked up against the full list, not just the enabled subset, so
        # the physical source number sent to the amplifier is unaffected by
        # which sources are currently hidden from the dropdown.
        for index, s in enumerate(self._sources, start=1):
            if s["name"] == source:
                break
        else:
            return
        await self.coordinator.async_send_zone_command(
            self._zone, self.coordinator.client.set_source, index
        )

    async def async_all_zones_off(self) -> None:
        """Turn off every zone on this entity's amplifier."""
        await self.coordinator.async_all_zones_off()
