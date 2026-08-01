"""Media player entity tests: zone-device wiring, state mapping, and command
delegation to the coordinator (already covered on its own in
test_coordinator.py).
"""

from __future__ import annotations

from harness import (
    calls_of,
    const,
    make_coordinator,
    setup_platform_entry,
)
from harness import (
    media_player as mp,
)
from harness import (
    protocol as p,
)
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

SOURCES = [
    {"name": "Chromecast", "enabled": True},
    {"name": "Turntable", "enabled": True},
]


def _player(hass, zone=1, zones=(1, 2), powered=True):
    coordinator, amp = make_coordinator(hass, zones=zones, powered=powered)
    entry = MockConfigEntry(domain=const.DOMAIN, entry_id="entry1")
    player = mp.RtiAd4xZoneMediaPlayer(coordinator, entry, zone, SOURCES)
    return player, coordinator, amp


# --------------------------------------------------------------------------
# Device/entity wiring (#6: one device per zone, via_device to the amp)
# --------------------------------------------------------------------------


async def test_device_info_is_a_zone_device_linked_to_the_amp(hass):
    player, _, _ = _player(hass, zone=2)
    info = player._attr_device_info
    assert info["identifiers"] == {(const.DOMAIN, "entry1_zone_2")}
    assert info["via_device"] == (const.DOMAIN, "entry1")
    assert info["name"] == "Zone 2"


async def test_unique_id_is_scoped_to_entry_and_zone(hass):
    player, _, _ = _player(hass, zone=3)
    assert player._attr_unique_id == "entry1_zone_3"


async def test_entity_has_no_name_suffix_since_it_is_the_zone_devices_primary_entity(
    hass,
):
    player, _, _ = _player(hass)
    assert player._attr_has_entity_name is True
    assert player._attr_name is None


# --------------------------------------------------------------------------
# State mapping
# --------------------------------------------------------------------------


async def test_state_is_none_before_any_read(hass):
    player, _, _ = _player(hass)
    assert player.state is None


async def test_state_reflects_coordinator_power(hass):
    player, coordinator, _ = _player(hass, powered=True)
    coordinator.data = await coordinator._async_update_data()
    assert player.state == mp.MediaPlayerState.ON
    await coordinator.async_set_power(1, False)
    assert player.state == mp.MediaPlayerState.OFF


async def test_volume_level_prefers_a_pending_target_over_stale_status(hass):
    # Checked while the target is still "dirty" -- an awaited async_set_volume
    # would already have flushed and released it by the time we could look.
    player, coordinator, _ = _player(hass, powered=True)
    coordinator.data = await coordinator._async_update_data()
    coordinator._pending[1].volume_target = 0.9
    coordinator._pending[1].volume_dirty = True
    assert player.volume_level == 0.9


async def test_is_volume_muted_prefers_a_pending_value_over_status(hass):
    player, coordinator, _ = _player(hass, powered=True)
    coordinator.data = await coordinator._async_update_data()
    coordinator._pending[1].mute = True
    assert player.is_volume_muted is True


async def test_source_maps_the_status_index_to_a_configured_name(hass):
    player, coordinator, _ = _player(hass, powered=True)
    coordinator.data = await coordinator._async_update_data()
    assert player.source == "Chromecast"
    assert player.source_list == ["Chromecast", "Turntable"]


async def test_source_list_excludes_disabled_sources(hass):
    player, coordinator, _ = _player(hass, powered=True)
    player._sources = [
        {"name": "Chromecast", "enabled": True},
        {"name": "Turntable", "enabled": False},
    ]
    coordinator.data = await coordinator._async_update_data()
    assert player.source_list == ["Chromecast"]


async def test_source_still_reports_a_currently_selected_but_disabled_source(hass):
    player, coordinator, amp = _player(hass, powered=True)
    player._sources = [
        {"name": "Chromecast", "enabled": True},
        {"name": "Turntable", "enabled": False},
    ]
    amp.source[1] = 2  # zone is on the now-disabled "Turntable"
    coordinator.data = await coordinator._async_update_data()
    assert player.source == "Turntable"
    assert player.source_list == ["Chromecast"]


async def test_unknown_source_index_falls_back_to_a_generic_name(hass):
    player, coordinator, amp = _player(hass, powered=True)
    amp.source[1] = 9  # outside the two configured source names
    coordinator.data = await coordinator._async_update_data()
    assert player.source == "Source 9"


# --------------------------------------------------------------------------
# Command delegation
# --------------------------------------------------------------------------


async def test_turn_on_and_off_delegate_to_the_coordinator(hass):
    player, coordinator, amp = _player(hass, powered=False)
    coordinator.data = await coordinator._async_update_data()
    await player.async_turn_on()
    assert calls_of(amp, "pwr")[-1] == ("pwr", 1, 1)
    await player.async_turn_off()
    assert calls_of(amp, "pwr")[-1] == ("pwr", 1, 0)


async def test_volume_and_mute_commands_delegate_to_the_coordinator(hass):
    player, coordinator, amp = _player(hass, powered=True)
    coordinator.data = await coordinator._async_update_data()

    await player.async_set_volume_level(0.5)
    assert calls_of(amp, "vol")[-1] == ("vol", 1, p.volume_level_to_attenuation(0.5))

    await player.async_volume_up()
    await player.async_volume_down()
    assert len(calls_of(amp, "vol")) == 3

    await player.async_mute_volume(True)
    assert calls_of(amp, "mut")[-1] == ("mut", 1, True)


async def test_select_source_looks_up_the_index_from_its_name(hass):
    player, coordinator, amp = _player(hass, powered=True)
    coordinator.data = await coordinator._async_update_data()
    await player.async_select_source("Turntable")
    assert calls_of(amp, "src")[-1] == ("src", 1, 2)


async def test_select_source_ignores_an_unknown_name(hass):
    player, coordinator, amp = _player(hass, powered=True)
    coordinator.data = await coordinator._async_update_data()
    await player.async_select_source("Nonexistent")
    assert calls_of(amp, "src") == []


async def test_all_zones_off_turns_off_every_zone_with_one_command(hass):
    player, coordinator, amp = _player(hass, zones=(1, 2), powered=True)
    coordinator.data = await coordinator._async_update_data()
    await player.async_all_zones_off()
    assert calls_of(amp, "alloff")
    assert coordinator.data[1].status.power is False
    assert coordinator.data[2].status.power is False


# --------------------------------------------------------------------------
# async_setup_entry
# --------------------------------------------------------------------------


async def test_setup_entry_creates_one_media_player_per_zone_and_registers_all_zones_off(
    hass,
):
    coordinator, _ = make_coordinator(hass, zones=(1, 2))
    entry = MockConfigEntry(
        domain=const.DOMAIN, entry_id="entry1", data={"sources": SOURCES, "zones": 2}
    )
    entry.runtime_data = coordinator
    entry.add_to_hass(hass)

    await setup_platform_entry(hass, mp, "media_player", entry)

    registry = er.async_get(hass)
    unique_ids = sorted(
        registered.unique_id
        for registered in er.async_entries_for_config_entry(registry, entry.entry_id)
    )
    assert unique_ids == ["entry1_zone_1", "entry1_zone_2"]
    assert hass.services.has_service(const.DOMAIN, const.SERVICE_ALL_ZONES_OFF)
