"""Coordinator tests: coalescing, power gating, and contention handling.

Several of these are regression tests for bugs that shipped, so they assert
behaviour rather than implementation: a burst of presses must land on the right
value, and a request must never silently vanish.
"""

from __future__ import annotations

import asyncio

from harness import UpdateFailed, calls_of, const, make_coordinator, protocol as p, run

# --------------------------------------------------------------------------
# Reading state
# --------------------------------------------------------------------------


def test_no_periodic_polling_is_scheduled():
    coord, _ = make_coordinator()
    assert coord.update_interval is None


def test_initial_read_uses_a_single_session():
    async def go():
        coord, amp = make_coordinator(zones=(1, 2))
        coord.data = await coord._async_update_data()
        assert amp.session_events == ["open", "close"], amp.session_events
        assert len(calls_of(amp, "sta")) == 2
        assert len(calls_of(amp, "set")) == 2, "tone is read alongside status"

    run(go())


def test_contention_keeps_last_state_then_eventually_fails():
    async def go():
        coord, amp = make_coordinator()
        coord.data = await coord._async_update_data()
        known = coord.data

        for attempt in range(1, const.POLL_FAILURE_TOLERANCE):
            amp.fail_next_reads = 1
            assert await coord._async_update_data() is known, (
                f"failure {attempt} should preserve state, not blank the zones"
            )

        amp.fail_next_reads = 1
        try:
            await coord._async_update_data()
        except UpdateFailed:
            pass
        else:
            raise AssertionError("should give up after repeated failures")

    run(go())


def test_a_successful_read_resets_the_failure_count():
    async def go():
        coord, amp = make_coordinator()
        coord.data = await coord._async_update_data()
        amp.fail_next_reads = 1
        await coord._async_update_data()
        await coord._async_update_data()
        assert coord._consecutive_failures == 0

    run(go())


# --------------------------------------------------------------------------
# Coalescing
# --------------------------------------------------------------------------


def test_burst_of_presses_becomes_one_command():
    async def go():
        coord, amp = make_coordinator(zones=(1,))
        coord.data = await coord._async_update_data()
        await asyncio.gather(*[coord.async_step_volume(1, 1) for _ in range(6)])
        assert len(calls_of(amp, "vol")) == 1, "six presses must coalesce"
        assert amp.atten[1] == 24, "should land 6 dB above the -30 dB baseline"

    run(go())


def test_press_arriving_mid_flight_is_not_lost():
    async def go():
        coord, amp = make_coordinator(zones=(1,))
        coord.data = await coord._async_update_data()
        amp.read_delay = 0.15  # hold the first command on the wire
        first = asyncio.ensure_future(coord.async_step_volume(1, 1))
        await amp.first_send.wait()
        second = asyncio.ensure_future(coord.async_step_volume(1, 1))
        await asyncio.gather(first, second)
        assert len(calls_of(amp, "vol")) == 2
        assert amp.atten[1] == 28, "two presses must land 2 dB up, not 1"

    run(go())


def test_setting_tone_to_flat_is_expressible():
    async def go():
        coord, amp = make_coordinator(zones=(1,))
        coord.data = await coord._async_update_data()
        await coord.async_set_treble(1, 0)
        await coord.async_set_bass(1, 0)
        # A delta-based design cannot distinguish 0 from "nothing requested".
        assert calls_of(amp, "trb") == [("trb", 1, 0)]
        assert calls_of(amp, "bas") == [("bas", 1, 0)]

    run(go())


def test_volume_and_tone_in_one_batch_do_not_clobber_each_other():
    async def go():
        coord, amp = make_coordinator(zones=(1,))
        coord.data = await coord._async_update_data()
        await asyncio.gather(
            coord.async_set_volume(1, 0.5), coord.async_set_treble(1, 6)
        )
        assert coord.data[1].status.volume_db == -35
        assert coord.data[1].tone.treble_db == 6

    run(go())


def test_a_flush_uses_a_single_session():
    async def go():
        coord, amp = make_coordinator(zones=(1,))
        coord.data = await coord._async_update_data()
        amp.session_events.clear()
        await asyncio.gather(*[coord.async_step_volume(1, 1) for _ in range(3)])
        assert amp.session_events == ["open", "close"], amp.session_events

    run(go())


# --------------------------------------------------------------------------
# Power gating -- the amplifier drops or misinterprets these while a zone is off
# --------------------------------------------------------------------------


def test_volume_on_an_off_zone_does_not_wake_it():
    async def go():
        coord, amp = make_coordinator(zones=(1,))
        coord.data = await coord._async_update_data()
        await coord.async_set_power(1, False)

        await coord.async_set_volume(1, 0.9)
        assert calls_of(amp, "vol") == [], "absolute volume would power the zone on"
        assert coord.data[1].status.power is False
        assert coord.pending_volume(1) == 0.9, "the request must be remembered"

    run(go())


def test_deferred_requests_are_applied_on_power_on():
    async def go():
        coord, amp = make_coordinator(zones=(1,))
        coord.data = await coord._async_update_data()
        await coord.async_set_power(1, False)

        await coord.async_set_volume(1, 0.9)
        await coord.async_set_mute(1, True)
        await coord.async_set_treble(1, 6)
        await coord.async_set_bass(1, -4)
        assert not [c for c in amp.calls if "dropped" in c[0]], amp.calls

        await coord.async_set_power(1, True)
        assert amp.atten[1] == p.volume_level_to_attenuation(0.9)
        assert amp.mute[1] is True
        assert amp.treble[1] == 6
        assert amp.bass[1] == -4
        assert coord.pending_volume(1) is None, "pending should be cleared"

    run(go())


def test_commands_pass_straight_through_while_a_zone_is_on():
    async def go():
        coord, amp = make_coordinator(zones=(1,))
        coord.data = await coord._async_update_data()
        await coord.async_set_treble(1, 8)
        await coord.async_set_mute(1, True)
        assert amp.treble[1] == 8
        assert amp.mute[1] is True

    run(go())


def test_a_manual_read_releases_a_stale_volume_target():
    async def go():
        coord, amp = make_coordinator(zones=(1,))
        coord.data = await coord._async_update_data()
        await coord.async_step_volume(1, 1)
        amp.atten[1] = 50  # changed at the amplifier
        coord.data = await coord._async_update_data()
        assert coord._pending[1].volume_target is None
        await coord.async_step_volume(1, 1)
        assert amp.atten[1] == 49, "next step must build on the new reality"

    run(go())


# --------------------------------------------------------------------------
# All zones off
# --------------------------------------------------------------------------


def test_all_zones_off_is_one_command_and_needs_no_re_read():
    async def go():
        coord, amp = make_coordinator(zones=(1, 2))
        coord.data = await coord._async_update_data()
        before = len(amp.calls)

        await coord.async_all_zones_off()
        assert amp.calls[before:] == [("alloff",)], "must not re-read every zone"
        assert coord.data[1].status.power is False
        assert coord.data[2].status.power is False
        assert coord.data[1].status.volume_db == -30, "volume must be preserved"

    run(go())


if __name__ == "__main__":
    import sys

    import test_coordinator

    sys.exit(__import__("harness").main(test_coordinator))
