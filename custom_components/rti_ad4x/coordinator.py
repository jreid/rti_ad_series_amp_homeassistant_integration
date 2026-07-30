"""Data update coordinator for the RTI AD-4x integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import COMMAND_COALESCE_WINDOW, DOMAIN, POLL_FAILURE_TOLERANCE
from .protocol import (
    VOLUME_STEP_LEVEL,
    RtiAd4xClient,
    RtiAd4xError,
    ToneStatus,
    ZoneStatus,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ZoneData:
    """Everything known about one zone: playback state plus tone settings."""

    status: ZoneStatus
    tone: ToneStatus | None = None


@dataclass
class _Pending:
    """Adjustments awaiting a flush.

    ``volume_target`` is *sticky*: it survives a flush and is only dropped when
    a poll refreshes real state. That is what lets a press arriving while an
    earlier command is still on the wire accumulate from where the zone is
    heading, instead of rebasing off a cache that has not caught up yet.

    Whether work is outstanding is tracked separately (``volume_dirty``, and
    ``None`` for tone) rather than inferred from the values, so that setting
    tone to 0 dB -- flat, the most likely choice -- is expressible.
    """

    volume_target: float | None = None
    volume_dirty: bool = False
    mute: bool | None = None
    treble: int | None = None
    bass: int | None = None

    def has_work(self) -> bool:
        return (
            self.volume_dirty
            or self.mute is not None
            or self.treble is not None
            or self.bass is not None
        )


@dataclass(frozen=True)
class _Work:
    """One zone's claimed adjustments, snapshotted for sending."""

    volume: float | None
    mute: bool | None
    treble: int | None
    bass: int | None


class RtiAd4xCoordinator(DataUpdateCoordinator[dict[int, ZoneData]]):
    """Holds zone and tone state for one amplifier.

    There is no periodic polling: the amplifier has no other writer, and every
    command answers with the resulting state, so the cache stays truthful once
    read. State is fetched at setup and thereafter maintained from command
    replies; ``homeassistant.update_entity`` forces a re-read.

    A lock wraps every exchange so only one is ever in flight, and each
    operation is bracketed in a client session so the amplifier's single
    control port is released as soon as the work is done.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: RtiAd4xClient,
        zones: list[int],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,  # read on demand only; see class docstring
        )
        self.client = client
        self.zones = zones
        self._lock = asyncio.Lock()
        self._pending: dict[int, _Pending] = {z: _Pending() for z in zones}
        self._flush_task: asyncio.Task[None] | None = None
        self._flush_errors: dict[int, RtiAd4xError] = {}
        self._consecutive_failures = 0

    async def _async_update_data(self) -> dict[int, ZoneData]:
        try:
            data = await self._async_read_all()
        except RtiAd4xError as err:
            self._consecutive_failures += 1
            # Losing a race for the single control port is ordinary contention,
            # not a fault. Keep serving the last known state for a few attempts
            # rather than blanking every zone the moment someone else connects.
            if self.data and self._consecutive_failures < POLL_FAILURE_TOLERANCE:
                _LOGGER.debug(
                    "Read failed (%s/%s), keeping last known state: %s",
                    self._consecutive_failures,
                    POLL_FAILURE_TOLERANCE,
                    err,
                )
                return self.data
            raise UpdateFailed(str(err)) from err
        self._consecutive_failures = 0
        return data

    async def _async_read_all(self) -> dict[int, ZoneData]:
        async with self._lock, self.client.session():
            data: dict[int, ZoneData] = {}
            for zone in self.zones:
                status = await self.client.get_status(zone)
                # Tone is a separate query and a nice-to-have: if it fails we
                # still want the zone itself to stay available.
                try:
                    tone = await self.client.get_tone_status(zone)
                except RtiAd4xError as err:
                    _LOGGER.debug("Tone query failed for zone %s: %s", zone, err)
                    tone = None
                data[zone] = ZoneData(status=status, tone=tone)
                # Now that real state is known, stop steering toward a stale
                # target -- unless a press is still queued against it.
                pending = self._pending[zone]
                if not pending.volume_dirty:
                    pending.volume_target = None
            return data

    async def async_send_zone_command(
        self,
        zone: int,
        action: Callable[..., Awaitable[ZoneStatus]],
        *args,
    ) -> ZoneStatus:
        """Run a command that returns zone status, and publish the new state."""
        async with self._lock:
            try:
                status = await action(zone, *args)
            except RtiAd4xError as err:
                raise HomeAssistantError(f"Zone {zone}: {err}") from err
        data = dict(self.data or {})
        existing = data.get(zone)
        data[zone] = (
            replace(existing, status=status)
            if existing is not None
            else ZoneData(status=status)
        )
        self.async_set_updated_data(data)
        return status

    async def async_all_zones_off(self) -> None:
        """Turn off every zone with a single command.

        The reply (``#ZALLOFF``) carries no per-zone detail, but since nothing
        else can change this amplifier we know the outcome: every zone is off,
        everything else untouched. Applying that locally avoids re-reading all
        zones just to learn what we already know.
        """
        async with self._lock:
            try:
                await self.client.all_zones_off()
            except RtiAd4xError as err:
                raise HomeAssistantError(f"Could not turn off all zones: {err}") from err
        data = {
            zone: replace(entry, status=replace(entry.status, power=False))
            for zone, entry in (self.data or {}).items()
        }
        if data:
            self.async_set_updated_data(data)

    # -- Coalesced adjustments -------------------------------------------------
    #
    # Volume steps and tone changes are the commands a user can generate faster
    # than the amplifier's minimum command interval, by holding a button or
    # dragging a slider. Each request records a target and waits on a shared
    # debounced flush, so a burst costs one command carrying the final value
    # rather than one per press -- most of which the amplifier would drop.

    def _cached_volume_level(self, zone: int) -> float:
        if (data := (self.data or {}).get(zone)) is not None:
            return data.status.volume_level
        return 0.0

    async def async_step_volume(self, zone: int, steps: int) -> None:
        """Nudge volume by ``steps`` 1 dB increments (negative to go down)."""
        pending = self._pending[zone]
        base = (
            pending.volume_target
            if pending.volume_target is not None
            else self._cached_volume_level(zone)
        )
        pending.volume_target = max(0.0, min(1.0, base + steps * VOLUME_STEP_LEVEL))
        pending.volume_dirty = True
        await self._async_flush_soon(zone)

    async def async_set_volume(self, zone: int, level: float) -> None:
        """Set an absolute volume level; last writer within the window wins."""
        pending = self._pending[zone]
        pending.volume_target = max(0.0, min(1.0, level))
        pending.volume_dirty = True
        await self._async_flush_soon(zone)

    async def async_set_mute(self, zone: int, mute: bool) -> None:
        self._pending[zone].mute = mute
        await self._async_flush_soon(zone)

    async def async_set_treble(self, zone: int, db: int) -> None:
        self._pending[zone].treble = db
        await self._async_flush_soon(zone)

    async def async_set_bass(self, zone: int, db: int) -> None:
        self._pending[zone].bass = db
        await self._async_flush_soon(zone)

    async def async_set_power(self, zone: int, on: bool) -> None:
        """Power a zone, then apply anything that was deferred while it was off."""
        await self.async_send_zone_command(
            zone, self.client.power_on if on else self.client.power_off
        )
        if on and self._pending[zone].has_work():
            await self._async_flush_soon(zone)

    # Requests deferred until a zone is powered on are still shown to the user,
    # so a slider or tone control reflects what they asked for rather than
    # snapping back while the zone is off.

    def pending_volume(self, zone: int) -> float | None:
        pending = self._pending[zone]
        return pending.volume_target if pending.volume_dirty else None

    def pending_mute(self, zone: int) -> bool | None:
        return self._pending[zone].mute

    def pending_treble(self, zone: int) -> int | None:
        return self._pending[zone].treble

    def pending_bass(self, zone: int) -> int | None:
        return self._pending[zone].bass

    async def _async_flush_soon(self, zone: int) -> None:
        """Ensure a flush is scheduled, wait for it, then surface this zone's outcome.

        The flush task is shared across every zone with pending work in the
        same debounce window, so it can't raise for its own failures -- that
        would fail every caller sharing the window, not just the one whose
        zone actually broke. Each caller instead checks, after the shared
        wait, whether its own zone came back with an error.
        """
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = self.hass.async_create_task(self._async_flush())
        # Shielded so one caller being cancelled cannot kill the flush that
        # other callers are also waiting on.
        await asyncio.shield(self._flush_task)
        if (err := self._flush_errors.pop(zone, None)) is not None:
            raise HomeAssistantError(f"Zone {zone}: {err}") from err

    async def _async_flush(self) -> None:
        await asyncio.sleep(COMMAND_COALESCE_WINDOW)
        # Drain in a loop: anything queued while commands are on the wire is
        # picked up by the next pass instead of being silently dropped.
        while (batch := self._claim_pending()):
            await self._async_send_batch(batch)

    def _zone_is_on(self, zone: int) -> bool:
        data = (self.data or {}).get(zone)
        return data is not None and data.status.power

    def _claim_pending(self) -> dict[int, _Work]:
        """Take ownership of outstanding adjustments, leaving targets in place."""
        batch: dict[int, _Work] = {}
        for zone, pending in self._pending.items():
            if not pending.has_work():
                continue
            if not self._zone_is_on(zone):
                # While a zone is off the amplifier drops mute and tone
                # commands, and an absolute volume would switch the zone on.
                # Neither is what the user asked for, so hold the request and
                # apply it when the zone is next powered up.
                continue
            batch[zone] = _Work(
                volume=pending.volume_target if pending.volume_dirty else None,
                mute=pending.mute,
                treble=pending.treble,
                bass=pending.bass,
            )
            # volume_target is deliberately retained so further steps keep
            # accumulating from it while this batch is in flight.
            pending.volume_dirty = False
            pending.mute = None
            pending.treble = None
            pending.bass = None
        return batch

    async def _async_send_batch(self, batch: dict[int, _Work]) -> None:
        data = dict(self.data or {})
        async with self._lock, self.client.session():
            for zone, work in batch.items():
                try:
                    if work.volume is not None:
                        status = await self.client.set_volume_level(zone, work.volume)
                        data = self._merge(data, zone, status=status)
                    if work.mute is not None:
                        status = await self.client.set_mute(zone, work.mute)
                        data = self._merge(data, zone, status=status)
                    if work.treble is not None:
                        tone = await self.client.set_treble(zone, work.treble)
                        data = self._merge(data, zone, tone=tone)
                    if work.bass is not None:
                        tone = await self.client.set_bass(zone, work.bass)
                        data = self._merge(data, zone, tone=tone)
                except RtiAd4xError as err:
                    _LOGGER.exception("Failed to apply adjustments for zone %s", zone)
                    self._flush_errors[zone] = err
        self.async_set_updated_data(data)

    @staticmethod
    def _merge(
        data: dict[int, ZoneData],
        zone: int,
        *,
        status: ZoneStatus | None = None,
        tone: ToneStatus | None = None,
    ) -> dict[int, ZoneData]:
        """Fold one reply into the accumulating snapshot.

        Successive commands for a zone must build on this snapshot rather than
        on ``self.data``, or an earlier reply in the same batch gets clobbered
        by a later one reading pre-batch state.
        """
        existing = data.get(zone)
        if existing is None:
            return data if status is None else {**data, zone: ZoneData(status=status)}
        updated = existing
        if status is not None:
            updated = replace(updated, status=status)
        if tone is not None:
            updated = replace(updated, tone=tone)
        return {**data, zone: updated}

    def async_cancel_pending(self) -> None:
        """Drop any scheduled flush; called during unload."""
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
        self._flush_task = None
