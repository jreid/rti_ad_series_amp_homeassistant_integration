"""Protocol-level tests.

Most assertions here pin down behaviour that was established by probing a real
AD-4x, and would be easy to "simplify" back into a bug otherwise.
"""

from __future__ import annotations

import asyncio
import socket
import time

from harness import const, protocol as p, run

# --------------------------------------------------------------------------
# Volume scaling
# --------------------------------------------------------------------------


def test_attenuation_range_matches_hardware():
    # VOL70 reads back -70 dB on the real unit; VOL78 and above are rejected.
    assert const.MAX_ATTENUATION_DB == 70
    assert p.volume_level_to_attenuation(1.0) == 0
    assert p.volume_level_to_attenuation(0.0) == 70


def test_volume_level_clamps_outside_unit_range():
    assert p.volume_level_to_attenuation(1.5) == 0
    assert p.volume_level_to_attenuation(-0.5) == 70


def test_one_volume_step_is_exactly_one_db():
    for db in range(1, 70):
        level = p.ZoneStatus(1, True, False, 1, -db).volume_level
        up = p.volume_level_to_attenuation(min(1.0, level + p.VOLUME_STEP_LEVEL))
        down = p.volume_level_to_attenuation(max(0.0, level - p.VOLUME_STEP_LEVEL))
        assert up == db - 1, f"up from -{db} gave -{up}"
        assert down == db + 1, f"down from -{db} gave -{down}"


def test_volume_steps_clamp_at_the_rails():
    def step(db, n):
        level = p.ZoneStatus(1, True, False, 1, -db).volume_level
        return p.volume_level_to_attenuation(
            max(0.0, min(1.0, level + n * p.VOLUME_STEP_LEVEL))
        )

    assert step(0, 1) == 0, "already loudest; must not wrap"
    assert step(70, -1) == 70, "already quietest; must not wrap"


# --------------------------------------------------------------------------
# Tone encoding and parsing
# --------------------------------------------------------------------------


def test_tone_encoding_matches_reference_table():
    cases = {0: 0, 2: 2, 12: 12, -2: 22, -6: 26, -12: 32}
    for db, expected in cases.items():
        assert p.db_to_tone_command_value(db) == expected, db


def test_tone_encoding_snaps_to_two_db_steps_and_clamps():
    assert p.db_to_tone_command_value(5) == 4
    assert p.db_to_tone_command_value(99) == 12
    assert p.db_to_tone_command_value(-99) == 32
    # The amplifier accepts illegal values silently, so nothing odd may escape.
    for db in range(-12, 13):
        assert p.db_to_tone_command_value(db) % 2 == 0, db


def test_tone_reply_puts_bass_before_treble():
    # Verified on hardware: treble +8 with bass -2 reads back as $01,-02,+08.
    tone = p._parse_tone_status_line("$01,-02,+08")
    assert tone.treble_db == 8
    assert tone.bass_db == -2


def test_tone_command_answered_with_zone_status_means_zone_is_off():
    try:
        p._parse_tone_status_line("#01,0,0,01,-40", "treble")
    except p.RtiAd4xZoneOffError as err:
        assert "zone is off" in str(err)
    else:
        raise AssertionError("expected RtiAd4xZoneOffError")


def test_malformed_replies_are_rejected():
    for bad in ("garbage", "$01,+00", "$01,a,b"):
        try:
            p._parse_tone_status_line(bad)
        except p.RtiAd4xError:
            pass
        else:
            raise AssertionError(f"{bad!r} should have been rejected")


# --------------------------------------------------------------------------
# Zone status parsing
# --------------------------------------------------------------------------


def test_status_line_parsing():
    s = p._parse_status_line("#01,1,0,01,-27")
    assert (s.zone, s.power, s.mute, s.source, s.volume_db) == (1, True, False, 1, -27)


def test_rejection_marker_raises():
    for bad in ("#?", "#01,1,0", "nonsense"):
        try:
            p._parse_status_line(bad)
        except p.RtiAd4xError:
            pass
        else:
            raise AssertionError(f"{bad!r} should have been rejected")


def test_malformed_power_or_mute_field_is_rejected_not_read_as_off():
    # A non-"1" power/mute field must not silently parse as False/off.
    for bad in ("#01,X,0,01,-27", "#01,1,X,01,-27", "#01,2,0,01,-27"):
        try:
            p._parse_status_line(bad)
        except p.RtiAd4xError:
            pass
        else:
            raise AssertionError(f"{bad!r} should have been rejected")


def test_reply_correlation_requires_matching_zone():
    assert p._is_direct_reply("#03,1,0,01,-20", 3)
    assert not p._is_direct_reply("#09,1,0,01,-20", 3), "foreign zone must be skipped"
    assert not p._is_direct_reply("#ZNON01", 3), "broadcast must be skipped"
    assert p._is_direct_reply("#ZALLOFF", None)
    assert p._is_direct_reply("#?", 3), "rejections must surface, not hang"


# --------------------------------------------------------------------------
# Transport behaviour, against a fake amplifier on a real socket
# --------------------------------------------------------------------------


class _FakeServer:
    """Serves the AD-4x line protocol; optionally single-client like the real one."""

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
                if "SET" in cmd:
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


async def _settle():
    """Let the server coroutine notice a client disconnect."""
    for _ in range(5):
        await asyncio.sleep(0)


def test_commands_are_paced_to_the_amplifier_floor():
    async def go():
        srv = _FakeServer()
        port = await srv.start()
        client = p.RtiAd4xClient("127.0.0.1", port)
        async with client.session():
            for _ in range(3):
                await client.get_status(1)
                await client.get_tone_status(1)
        srv.stop()
        gaps = [b - a for a, b in zip(srv.arrivals, srv.arrivals[1:])]
        assert gaps, "no commands recorded"
        # 5 ms of scheduling tolerance; the amplifier drops anything faster.
        assert min(gaps) >= const.MIN_COMMAND_INTERVAL - 0.005, gaps

    run(go())


def test_one_shot_commands_release_the_port():
    async def go():
        srv = _FakeServer()
        port = await srv.start()
        client = p.RtiAd4xClient("127.0.0.1", port)
        await client.get_status(1)
        await _settle()
        assert srv.live == 0, "socket still held after a one-shot command"
        await client.get_status(2)
        await _settle()
        assert srv.accepted == 2, "second one-shot should open a new connection"
        assert srv.peak == 1, "never more than one connection at a time"
        srv.stop()

    run(go())


def test_session_shares_one_connection_then_releases():
    async def go():
        srv = _FakeServer()
        port = await srv.start()
        client = p.RtiAd4xClient("127.0.0.1", port)
        async with client.session():
            for zone in (1, 2, 3, 4):
                await client.get_status(zone)
                await client.get_tone_status(zone)
            assert srv.live == 1, "connection should be held for the session"
        await _settle()
        assert srv.accepted == 1, "8 commands must share one connection"
        assert srv.live == 0, "connection must be released on session exit"
        srv.stop()

    run(go())


def test_foreign_status_line_is_not_mistaken_for_our_reply():
    async def go():
        srv = _FakeServer(inject_foreign=True)
        port = await srv.start()
        client = p.RtiAd4xClient("127.0.0.1", port)
        assert (await client.get_status(3)).zone == 3
        assert (await client.get_tone_status(2)).zone == 2
        srv.stop()

    run(go())


def test_refused_connection_is_retried_and_clearly_explained():
    async def go():
        # Nothing listening gives ECONNREFUSED, same as a busy amplifier.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()

        client = p.RtiAd4xClient("127.0.0.1", dead_port)
        started = time.monotonic()
        try:
            await client.get_status(1)
        except p.RtiAd4xError as err:
            assert "accepts only one" in str(err), str(err)
        else:
            raise AssertionError("expected a connection failure")
        elapsed = time.monotonic() - started
        expected = const.CONNECTION_RECONNECT_SETTLE * (const.MAX_CONNECT_ATTEMPTS - 1)
        assert elapsed >= expected, f"retried too fast: {elapsed:.2f}s"

    run(go())


def test_a_held_port_blocks_others_until_released():
    async def go():
        srv = _FakeServer(single_client=True)
        port = await srv.start()
        holder = p.RtiAd4xClient("127.0.0.1", port)
        rival = p.RtiAd4xClient("127.0.0.1", port)
        async with holder.session():
            await holder.get_status(1)
            try:
                await rival.get_status(1)
            except p.RtiAd4xError:
                pass
            else:
                raise AssertionError("rival should not get in while port is held")
        await _settle()
        assert (await rival.get_status(1)).zone == 1, "should connect once released"
        srv.stop()

    run(go())


if __name__ == "__main__":
    import sys

    import test_protocol

    sys.exit(__import__("harness").main(test_protocol))
