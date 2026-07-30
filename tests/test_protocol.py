"""Protocol-level tests.

Most assertions here pin down behaviour that was established by probing a real
AD-4x, and would be easy to "simplify" back into a bug otherwise.
"""

from __future__ import annotations

import asyncio
import socket
import time

import pytest
from harness import FakeServer, const, run, settle
from harness import protocol as p

# pytest-homeassistant-custom-component blocks real sockets by default (it
# expects HA's own network calls to be mocked); the tests below deliberately
# talk to a real local TCP server standing in for the amplifier, unrelated to
# HA, so they need it back. Must be the `socket_enabled` fixture rather than
# the `enable_socket` marker: HA's plugin re-disables sockets from its own
# pytest_runtest_setup hook, which runs after marker-based enabling but
# before fixture setup, so only the fixture form actually sticks.
pytestmark = pytest.mark.usefixtures("socket_enabled")

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


def test_non_numeric_zone_or_source_field_is_rejected():
    # Passes the power/mute checks but fails the int() conversion further on.
    for bad in ("#0X,1,0,01,-27", "#01,1,0,0X,-27", "#01,1,0,01,-2X"):
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
# Failure descriptions
# --------------------------------------------------------------------------


def test_describe_failure_names_contention_for_connection_errors():
    assert "accepts only one" in p._describe_failure(ConnectionRefusedError())
    assert "accepts only one" in p._describe_failure(ConnectionResetError())


def test_describe_failure_names_a_timeout():
    assert "timed out" in p._describe_failure(TimeoutError())


def test_describe_failure_falls_back_to_the_bare_error():
    assert p._describe_failure(ValueError("odd failure")) == "odd failure"


# --------------------------------------------------------------------------
# Transport behaviour, against a fake amplifier on a real socket
# --------------------------------------------------------------------------


def test_commands_are_paced_to_the_amplifier_floor():
    async def go():
        srv = FakeServer()
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
        srv = FakeServer()
        port = await srv.start()
        client = p.RtiAd4xClient("127.0.0.1", port)
        await client.get_status(1)
        await settle()
        assert srv.live == 0, "socket still held after a one-shot command"
        await client.get_status(2)
        await settle()
        assert srv.accepted == 2, "second one-shot should open a new connection"
        assert srv.peak == 1, "never more than one connection at a time"
        srv.stop()

    run(go())


def test_session_shares_one_connection_then_releases():
    async def go():
        srv = FakeServer()
        port = await srv.start()
        client = p.RtiAd4xClient("127.0.0.1", port)
        async with client.session():
            for zone in (1, 2, 3, 4):
                await client.get_status(zone)
                await client.get_tone_status(zone)
            assert srv.live == 1, "connection should be held for the session"
        await settle()
        assert srv.accepted == 1, "8 commands must share one connection"
        assert srv.live == 0, "connection must be released on session exit"
        srv.stop()

    run(go())


def test_foreign_status_line_is_not_mistaken_for_our_reply():
    async def go():
        srv = FakeServer(inject_foreign=True)
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
        srv = FakeServer(single_client=True)
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
        await settle()
        assert (await rival.get_status(1)).zone == 1, "should connect once released"
        srv.stop()

    run(go())


# --------------------------------------------------------------------------
# Client command methods -- wire format and reply parsing for each one.
# These are only otherwise exercised through FakeAmp in the coordinator
# tests, which never runs the real command strings past RtiAd4xClient.
# --------------------------------------------------------------------------


def test_power_commands_wake_and_sleep_a_zone():
    async def go():
        srv = FakeServer()
        port = await srv.start()
        client = p.RtiAd4xClient("127.0.0.1", port)
        async with client.session():
            on = await client.power_on(1)
            off = await client.power_off(1)
        srv.stop()
        assert on.zone == 1
        assert off.zone == 1
        # The PWR broadcast (#ZNON01) sent alongside each reply must be
        # skipped, not mistaken for the next command's answer.
        assert len(srv.arrivals) == 2

    run(go())


def test_set_source_sends_the_source_number():
    async def go():
        srv = FakeServer()
        port = await srv.start()
        client = p.RtiAd4xClient("127.0.0.1", port)
        status = await client.set_source(2, 3)
        srv.stop()
        assert status.zone == 2

    run(go())


def test_set_mute_and_volume_level_and_steps():
    async def go():
        srv = FakeServer()
        port = await srv.start()
        client = p.RtiAd4xClient("127.0.0.1", port)
        async with client.session():
            assert (await client.set_mute(1, True)).zone == 1
            assert (await client.set_volume_level(1, 0.5)).zone == 1
            assert (await client.volume_up(1)).zone == 1
            assert (await client.volume_down(1)).zone == 1
        srv.stop()

    run(go())


def test_set_treble_and_bass_parse_the_tone_reply():
    async def go():
        srv = FakeServer()
        port = await srv.start()
        client = p.RtiAd4xClient("127.0.0.1", port)
        async with client.session():
            treble = await client.set_treble(1, 8)
            bass = await client.set_bass(1, -2)
        srv.stop()
        assert treble.zone == 1
        assert bass.zone == 1

    run(go())


def test_all_zones_off_expects_the_zalloff_reply():
    async def go():
        srv = FakeServer()
        port = await srv.start()
        client = p.RtiAd4xClient("127.0.0.1", port)
        await client.all_zones_off()  # would raise if the reply weren't #ZALLOFF
        srv.stop()

    run(go())


def test_all_zones_off_rejects_an_unexpected_reply():
    async def go():
        async def handle(reader, writer):
            await reader.readuntil(b"\r")
            writer.write(b"#01,1,0,01,-20\r\n")
            await writer.drain()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = p.RtiAd4xClient("127.0.0.1", port)
        try:
            await client.all_zones_off()
        except p.RtiAd4xError as err:
            assert "Unexpected reply" in str(err)
        else:
            raise AssertionError("expected rejection of a non-ZALLOFF reply")
        server.close()

    run(go())


# --------------------------------------------------------------------------
# _write_and_read edge cases: a connection that goes quiet in different ways
# --------------------------------------------------------------------------


def test_connection_closed_without_a_reply_is_reported():
    async def go():
        async def handle(reader, writer):
            await reader.readuntil(b"\r")
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = p.RtiAd4xClient("127.0.0.1", port)
        try:
            await client.get_status(1)
        except p.RtiAd4xError as err:
            assert "closed" in str(err)
        else:
            raise AssertionError("expected a closed-connection failure")
        server.close()

    run(go())


def test_no_reply_amid_a_flood_of_broadcasts_is_reported():
    async def go():
        async def handle(reader, writer):
            await reader.readuntil(b"\r")
            # Flood well past MAX_REPLY_LINES with lines that never look like
            # a direct reply, so the read loop gives up rather than hanging.
            for _ in range(const.MAX_REPLY_LINES + 5):
                writer.write(b"#ZNON01\r\n")
            await writer.drain()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = p.RtiAd4xClient("127.0.0.1", port)
        try:
            await client.get_status(1)
        except p.RtiAd4xError as err:
            assert "amid unsolicited output" in str(err)
        else:
            raise AssertionError("expected the reply-line cap to be enforced")
        server.close()

    run(go())
