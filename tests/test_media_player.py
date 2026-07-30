"""Media player entity tests: zone-device wiring, state mapping, and command
delegation to the coordinator (already covered on its own in
test_coordinator.py).
"""

from __future__ import annotations

from harness import ConfigEntry, calls_of, const, make_coordinator, media_player as mp, run

SOURCES = ["Chromecast", "Turntable"]


def _player(zone=1, zones=(1, 2), powered=True):
    coordinator, amp = make_coordinator(zones=zones, powered=powered)
    entry = ConfigEntry(entry_id="entry1")
    player = mp.RtiAd4xZoneMediaPlayer(coordinator, entry, zone, SOURCES)
    return player, coordinator, amp


# --------------------------------------------------------------------------
# Device/entity wiring (#6: one device per zone, via_device to the amp)
# --------------------------------------------------------------------------


def test_device_info_is_a_zone_device_linked_to_the_amp():
    player, _, _ = _player(zone=2)
    info = player._attr_device_info
    assert info["identifiers"] == {(const.DOMAIN, "entry1_zone_2")}
    assert info["via_device"] == (const.DOMAIN, "entry1")
    assert info["name"] == "Zone 2"


def test_unique_id_is_scoped_to_entry_and_zone():
    player, _, _ = _player(zone=3)
    assert player._attr_unique_id == "entry1_zone_3"


def test_entity_has_no_name_suffix_since_it_is_the_zone_devices_primary_entity():
    player, _, _ = _player()
    assert player._attr_has_entity_name is True
    assert player._attr_name is None


# --------------------------------------------------------------------------
# State mapping
# --------------------------------------------------------------------------


def test_state_is_none_before_any_read():
    player, _, _ = _player()
    assert player.state is None


def test_state_reflects_coordinator_power():
    async def go():
        player, coordinator, _ = _player(powered=True)
        coordinator.data = await coordinator._async_update_data()
        assert player.state == mp.MediaPlayerState.ON
        await coordinator.async_set_power(1, False)
        assert player.state == mp.MediaPlayerState.OFF

    run(go())


def test_volume_level_prefers_a_pending_target_over_stale_status():
    async def go():
        player, coordinator, _ = _player(powered=True)
        coordinator.data = await coordinator._async_update_data()
        await coordinator.async_set_volume(1, 0.9)
        assert player.volume_level == 0.9

    run(go())


def test_is_volume_muted_prefers_a_pending_value_over_status():
    async def go():
        player, coordinator, _ = _player(powered=True)
        coordinator.data = await coordinator._async_update_data()
        await coordinator.async_set_mute(1, True)
        assert player.is_volume_muted is True

    run(go())


def test_source_maps_the_status_index_to_a_configured_name():
    async def go():
        player, coordinator, _ = _player(powered=True)
        coordinator.data = await coordinator._async_update_data()
        assert player.source == "Chromecast"
        assert player.source_list == SOURCES

    run(go())


def test_unknown_source_index_falls_back_to_a_generic_name():
    async def go():
        player, coordinator, amp = _player(powered=True)
        amp.source[1] = 9  # outside the two configured source names
        coordinator.data = await coordinator._async_update_data()
        assert player.source == "Source 9"

    run(go())


# --------------------------------------------------------------------------
# Command delegation
# --------------------------------------------------------------------------


def test_turn_on_and_off_delegate_to_the_coordinator():
    async def go():
        player, coordinator, amp = _player(powered=False)
        coordinator.data = await coordinator._async_update_data()
        await player.async_turn_on()
        assert calls_of(amp, "pwr")[-1] == ("pwr", 1, 1)
        await player.async_turn_off()
        assert calls_of(amp, "pwr")[-1] == ("pwr", 1, 0)

    run(go())


def test_select_source_looks_up_the_index_from_its_name():
    async def go():
        player, coordinator, amp = _player(powered=True)
        coordinator.data = await coordinator._async_update_data()
        await player.async_select_source("Turntable")
        assert calls_of(amp, "src")[-1] == ("src", 1, 2)

    run(go())


def test_select_source_ignores_an_unknown_name():
    async def go():
        player, coordinator, amp = _player(powered=True)
        coordinator.data = await coordinator._async_update_data()
        await player.async_select_source("Nonexistent")
        assert calls_of(amp, "src") == []

    run(go())


def test_all_zones_off_turns_off_every_zone_with_one_command():
    async def go():
        player, coordinator, amp = _player(zones=(1, 2), powered=True)
        coordinator.data = await coordinator._async_update_data()
        await player.async_all_zones_off()
        assert calls_of(amp, "alloff")
        assert coordinator.data[1].status.power is False
        assert coordinator.data[2].status.power is False

    run(go())


# --------------------------------------------------------------------------
# async_setup_entry
# --------------------------------------------------------------------------


def test_setup_entry_creates_one_media_player_per_zone_and_registers_all_zones_off():
    async def go():
        coordinator, _ = make_coordinator(zones=(1, 2))
        entry = ConfigEntry(entry_id="entry1", data={"sources": SOURCES, "zones": 2})
        entry.runtime_data = coordinator

        entities = []
        await mp.async_setup_entry(None, entry, entities.extend)

        assert sorted(e._zone for e in entities) == [1, 2]
        platform = mp.entity_platform.async_get_current_platform()
        assert ("all_zones_off", None, "async_all_zones_off") in platform.registered

    run(go())


if __name__ == "__main__":
    import sys

    import test_media_player

    sys.exit(__import__("harness").main(test_media_player))
