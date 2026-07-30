"""Number entity tests: shared zone device (#6) and reading tone back from the
amplifier rather than trusting the last commanded value.
"""

from __future__ import annotations

from harness import ConfigEntry, calls_of, const, make_coordinator, number as num, run


def _number(kind, zone=1, zones=(1, 2), powered=True):
    coordinator, amp = make_coordinator(zones=zones, powered=powered)
    entry = ConfigEntry(entry_id="entry1")
    entity = num.RtiAd4xToneNumber(coordinator, entry, zone, kind)
    return entity, coordinator, amp


# --------------------------------------------------------------------------
# Device/entity wiring -- must land on the same zone device as the media player
# --------------------------------------------------------------------------


def test_device_info_matches_the_media_players_zone_device():
    entity, _, _ = _number("treble", zone=2)
    info = entity._attr_device_info
    assert info["identifiers"] == {(const.DOMAIN, "entry1_zone_2")}
    assert info["via_device"] == (const.DOMAIN, "entry1")
    assert info["name"] == "Zone 2"


def test_unique_id_and_translation_key_are_kind_specific():
    treble, _, _ = _number("treble", zone=1)
    bass, _, _ = _number("bass", zone=1)
    assert treble._attr_unique_id == "entry1_zone_1_treble"
    assert treble._attr_translation_key == "treble"
    assert bass._attr_unique_id == "entry1_zone_1_bass"
    assert bass._attr_translation_key == "bass"


# --------------------------------------------------------------------------
# native_value: read back from the amp, not the deleted platform's optimism
# --------------------------------------------------------------------------


def test_native_value_is_none_before_any_read():
    entity, _, _ = _number("treble")
    assert entity.native_value is None


def test_native_value_reads_back_from_the_amplifier():
    async def go():
        entity, coordinator, amp = _number("bass", powered=True)
        amp.bass[1] = -6
        coordinator.data = await coordinator._async_update_data()
        assert entity.native_value == -6

    run(go())


def test_native_value_prefers_a_pending_write_over_stale_status():
    async def go():
        entity, coordinator, _ = _number("treble", powered=True)
        coordinator.data = await coordinator._async_update_data()
        await coordinator.async_set_treble(1, 8)
        assert entity.native_value == 8

    run(go())


# --------------------------------------------------------------------------
# async_set_native_value
# --------------------------------------------------------------------------


def test_set_native_value_rounds_and_forwards_to_the_coordinator():
    async def go():
        entity, coordinator, amp = _number("bass", powered=True)
        coordinator.data = await coordinator._async_update_data()
        await entity.async_set_native_value(3.7)
        assert calls_of(amp, "bas")[-1] == ("bas", 1, 4)

    run(go())


def test_treble_and_bass_are_independent():
    async def go():
        entity, coordinator, amp = _number("treble", powered=True)
        coordinator.data = await coordinator._async_update_data()
        await entity.async_set_native_value(6)
        assert calls_of(amp, "trb")[-1] == ("trb", 1, 6)
        assert calls_of(amp, "bas") == []

    run(go())


# --------------------------------------------------------------------------
# async_setup_entry
# --------------------------------------------------------------------------


def test_setup_entry_creates_a_treble_and_bass_pair_per_zone():
    async def go():
        coordinator, _ = make_coordinator(zones=(1, 2))
        entry = ConfigEntry(entry_id="entry1", data={"zones": 2})
        entry.runtime_data = coordinator

        entities = []
        await num.async_setup_entry(None, entry, entities.extend)

        pairs = sorted((e._zone, e._kind) for e in entities)
        assert pairs == [(1, "bass"), (1, "treble"), (2, "bass"), (2, "treble")]

    run(go())


if __name__ == "__main__":
    import sys

    import test_number

    sys.exit(__import__("harness").main(test_number))
