"""Shared test helpers: the real `homeassistant` package plus a fake amplifier.

Home Assistant itself comes from `pytest-homeassistant-custom-component`
(see requirements_test.txt, pinned to the release built against the exact
`homeassistant` version this integration declares as its `min_ha_version`).
There is no HA stand-in here anymore -- once the real package is installed,
`custom_components/rti_ad4x`'s own `from homeassistant... import ...` lines
resolve normally, the same as they would inside a real HA install.

`FakeAmp` is unrelated to any of that: it's a fake of the physical AD-4x,
reproducing behaviour measured on real hardware (absolute volume and source
selection wake a powered-off zone; mute and tone commands are silently
dropped, tone answering with a zone-status line, which the client surfaces
as RtiAd4xZoneOffError).
"""

from __future__ import annotations

import asyncio
import time

from pytest_homeassistant_custom_component.common import MockEntityPlatform

from custom_components.rti_ad4x import (
    button,
    config_flow,
    const,
    coordinator,
    media_player,
    number,
    protocol,
)
from homeassistant.helpers.update_coordinator import UpdateFailed

__all__ = [
    "FakeServer",
    "UpdateFailed",
    "button",
    "calls_of",
    "config_flow",
    "const",
    "coordinator",
    "make_coordinator",
    "media_player",
    "number",
    "protocol",
    "run",
    "settle",
    "setup_platform_entry",
]


class FakeAmp:
    """An AD-4x stand-in reproducing the behaviour measured on real hardware.

    Notably: absolute volume and source selection wake a powered-off zone,
    while mute and tone commands are silently dropped -- tone answering with a
    zone-status line, which the client surfaces as RtiAd4xZoneOffError.
    """

    def __init__(self, zones=(1, 2), powered=True):
        self.calls: list[tuple] = []
        self.power = {z: powered for z in zones}
        self.atten = {z: 30 for z in zones}
        self.mute = {z: False for z in zones}
        self.treble = {z: 0 for z in zones}
        self.bass = {z: 0 for z in zones}
        self.source = {z: 1 for z in zones}
        self.sessions = 0
        self.session_events: list[str] = []
        self.fail_next_reads = 0
        self.fail_next_tone_reads = 0
        self.fail_next_writes = 0
        self.read_delay = 0.0
        self.first_send = asyncio.Event()

    class _Session:
        def __init__(self, amp):
            self.amp = amp

        async def __aenter__(self):
            self.amp.sessions += 1
            self.amp.session_events.append("open")

        async def __aexit__(self, *exc):
            self.amp.session_events.append("close")

    def session(self):
        return self._Session(self)

    async def close(self):
        pass

    def _status(self, zone):
        return protocol.ZoneStatus(
            zone, self.power[zone], self.mute[zone], self.source[zone], -self.atten[zone]
        )

    def _tone(self, zone):
        return protocol.ToneStatus(zone, self.treble[zone], self.bass[zone])

    async def _wire(self):
        self.first_send.set()
        if self.read_delay:
            await asyncio.sleep(self.read_delay)

    def _maybe_fail_write(self):
        if self.fail_next_writes > 0:
            self.fail_next_writes -= 1
            raise protocol.RtiAd4xError("another client is probably connected")

    async def get_status(self, zone):
        if self.fail_next_reads > 0:
            self.fail_next_reads -= 1
            raise protocol.RtiAd4xError("another client is probably connected")
        self.calls.append(("sta", zone))
        return self._status(zone)

    async def get_tone_status(self, zone):
        if self.fail_next_tone_reads > 0:
            self.fail_next_tone_reads -= 1
            raise protocol.RtiAd4xError("tone query failed")
        self.calls.append(("set", zone))
        return self._tone(zone)

    async def power_on(self, zone):
        self._maybe_fail_write()
        self.power[zone] = True
        self.calls.append(("pwr", zone, 1))
        return self._status(zone)

    async def power_off(self, zone):
        self._maybe_fail_write()
        self.power[zone] = False
        self.calls.append(("pwr", zone, 0))
        return self._status(zone)

    async def set_volume_level(self, zone, level):
        self._maybe_fail_write()
        self.power[zone] = True  # measured: absolute volume wakes the zone
        self.atten[zone] = protocol.volume_level_to_attenuation(level)
        self.calls.append(("vol", zone, self.atten[zone]))
        await self._wire()
        return self._status(zone)

    async def set_source(self, zone, source):
        self._maybe_fail_write()
        self.power[zone] = True  # measured: source selection wakes the zone
        self.source[zone] = source
        self.calls.append(("src", zone, source))
        return self._status(zone)

    async def set_mute(self, zone, mute):
        if not self.power[zone]:
            self.calls.append(("mute-dropped", zone))
            return self._status(zone)
        self._maybe_fail_write()
        self.mute[zone] = mute
        self.calls.append(("mut", zone, mute))
        return self._status(zone)

    async def set_treble(self, zone, db):
        if not self.power[zone]:
            self.calls.append(("treble-dropped", zone))
            raise protocol.RtiAd4xZoneOffError("zone is off")
        self._maybe_fail_write()
        self.treble[zone] = db
        self.calls.append(("trb", zone, db))
        return self._tone(zone)

    async def set_bass(self, zone, db):
        if not self.power[zone]:
            self.calls.append(("bass-dropped", zone))
            raise protocol.RtiAd4xZoneOffError("zone is off")
        self._maybe_fail_write()
        self.bass[zone] = db
        self.calls.append(("bas", zone, db))
        return self._tone(zone)

    async def all_zones_off(self):
        self._maybe_fail_write()
        self.calls.append(("alloff",))
        for zone in self.power:
            self.power[zone] = False


class FakeServer:
    """Serves the AD-4x line protocol over a real socket; optionally
    single-client like the real one.

    Used wherever a test needs to exercise the real `RtiAd4xClient` (as
    opposed to `FakeAmp`, which stands in for the client's own callers).
    """

    def __init__(self, single_client=True, inject_foreign=False):
        self.single_client = single_client
        self.inject_foreign = inject_foreign
        self.live = 0
        self.peak = 0
        self.accepted = 0
        self.arrivals: list[float] = []

    async def _handle(self, reader, writer):
        if self.single_client and self.live > 0:
            writer.close()
            return
        self.live += 1
        self.accepted += 1
        self.peak = max(self.peak, self.live)
        try:
            while True:
                raw = await reader.readuntil(b"\r")  # commands end in bare \r
                self.arrivals.append(time.monotonic())
                cmd = raw.decode().strip().lstrip("*")
                zone = cmd[2:4]
                if self.inject_foreign:
                    writer.write(b"#09,1,0,01,-11\r\n")
                if cmd.startswith("ZALLPWR"):
                    writer.write(b"#ZALLOFF\r\n")
                elif "SET" in cmd or "TRB" in cmd or "BAS" in cmd:
                    # Tone queries and tone-setting commands alike reply on
                    # the $ channel while the zone is on.
                    writer.write(f"${zone},+00,+06\r\n".encode())
                elif "PWR" in cmd:
                    writer.write(f"#{zone},1,0,01,-29\r\n".encode())
                    writer.write(b"#ZNON01\r\n")  # unsolicited broadcast
                else:
                    writer.write(f"#{zone},1,0,01,-29\r\n".encode())
                await writer.drain()
        except Exception:  # noqa: BLE001 - client went away
            pass
        finally:
            self.live -= 1

    async def start(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    def stop(self):
        self._server.close()


async def settle():
    """Let the server coroutine notice a client disconnect."""
    for _ in range(5):
        await asyncio.sleep(0)


def make_coordinator(hass, zones=(1, 2), powered=True):
    amp = FakeAmp(zones, powered)
    coord = coordinator.RtiAd4xCoordinator(hass, amp, list(zones))
    return coord, amp


def calls_of(amp, kind):
    return [c for c in amp.calls if c[0] == kind]


def run(coro):
    return asyncio.run(coro)


async def setup_platform_entry(hass, platform_module, domain, entry):
    """Run `platform_module.async_setup_entry` behind a real EntityPlatform.

    Direct instantiation of an entity class (what most tests here do) never
    needs this. It's only for the couple of tests that exercise
    entity_platform.async_get_current_platform() or
    async_register_entity_service(), both of which require a genuine
    EntityPlatform context that a bare call to async_setup_entry doesn't
    provide.
    """
    platform = MockEntityPlatform(
        hass, platform_name=const.DOMAIN, domain=domain, platform=platform_module
    )
    await platform.async_setup_entry(entry)
    return platform
