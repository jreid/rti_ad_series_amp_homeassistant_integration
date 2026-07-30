"""Number entity tests: shared zone device (#6) and reading tone back from the
amplifier rather than trusting the last commanded value.
"""

from __future__ import annotations

from dataclasses import replace

from harness import calls_of, const, make_coordinator, setup_platform_entry
from harness import number as num
from homeassistant.components.number import NumberMode
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry


def _number(hass, kind, zone=1, zones=(1, 2), powered=True):
    coordinator, amp = make_coordinator(hass, zones=zones, powered=powered)
    entry = MockConfigEntry(domain=const.DOMAIN, entry_id="entry1")
    entity = num.RtiAd4xToneNumber(coordinator, entry, zone, kind)
    return entity, coordinator, amp


# --------------------------------------------------------------------------
# Device/entity wiring -- must land on the same zone device as the media player
# --------------------------------------------------------------------------


async def test_device_info_matches_the_media_players_zone_device(hass):
    entity, _, _ = _number(hass, "treble", zone=2)
    info = entity._attr_device_info
    assert info["identifiers"] == {(const.DOMAIN, "entry1_zone_2")}
    assert info["via_device"] == (const.DOMAIN, "entry1")
    assert info["name"] == "Zone 2"


async def test_slider_mode_is_centered_on_zero(hass):
    entity, _, _ = _number(hass, "treble", zone=1)
    assert entity._attr_mode == NumberMode.SLIDER
    assert entity._attr_native_min_value == -entity._attr_native_max_value


async def test_unique_id_and_translation_key_are_kind_specific(hass):
    treble, _, _ = _number(hass, "treble", zone=1)
    bass, _, _ = _number(hass, "bass", zone=1)
    assert treble._attr_unique_id == "entry1_zone_1_treble"
    assert treble._attr_translation_key == "treble"
    assert bass._attr_unique_id == "entry1_zone_1_bass"
    assert bass._attr_translation_key == "bass"


# --------------------------------------------------------------------------
# native_value: read back from the amp, not the deleted platform's optimism
# --------------------------------------------------------------------------


async def test_native_value_is_none_before_any_read(hass):
    entity, _, _ = _number(hass, "treble")
    assert entity.native_value is None


async def test_native_value_reads_back_from_the_amplifier(hass):
    entity, coordinator, amp = _number(hass, "bass", powered=True)
    amp.bass[1] = -6
    coordinator.data = await coordinator._async_update_data()
    assert entity.native_value == -6


async def test_native_value_prefers_a_pending_write_over_stale_status(hass):
    # Checked while still "dirty" -- an awaited async_set_treble would already
    # have flushed and released the pending value by the time we could look.
    entity, coordinator, _ = _number(hass, "treble", powered=True)
    coordinator.data = await coordinator._async_update_data()
    coordinator._pending[1].treble = 8
    assert entity.native_value == 8


async def test_native_value_is_none_when_tone_could_not_be_read(hass):
    entity, coordinator, _ = _number(hass, "treble", powered=True)
    coordinator.data = await coordinator._async_update_data()
    coordinator.data[1] = replace(coordinator.data[1], tone=None)
    assert entity.native_value is None


# --------------------------------------------------------------------------
# async_set_native_value
# --------------------------------------------------------------------------


async def test_set_native_value_rounds_and_forwards_to_the_coordinator(hass):
    entity, coordinator, amp = _number(hass, "bass", powered=True)
    coordinator.data = await coordinator._async_update_data()
    await entity.async_set_native_value(3.7)
    assert calls_of(amp, "bas")[-1] == ("bas", 1, 4)


async def test_treble_and_bass_are_independent(hass):
    entity, coordinator, amp = _number(hass, "treble", powered=True)
    coordinator.data = await coordinator._async_update_data()
    await entity.async_set_native_value(6)
    assert calls_of(amp, "trb")[-1] == ("trb", 1, 6)
    assert calls_of(amp, "bas") == []


# --------------------------------------------------------------------------
# async_setup_entry
# --------------------------------------------------------------------------


async def test_setup_entry_creates_a_treble_and_bass_pair_per_zone(hass):
    coordinator, _ = make_coordinator(hass, zones=(1, 2))
    entry = MockConfigEntry(domain=const.DOMAIN, entry_id="entry1", data={"zones": 2})
    entry.runtime_data = coordinator
    entry.add_to_hass(hass)

    await setup_platform_entry(hass, num, "number", entry)

    registry = er.async_get(hass)
    unique_ids = sorted(
        registered.unique_id
        for registered in er.async_entries_for_config_entry(registry, entry.entry_id)
    )
    assert unique_ids == [
        "entry1_zone_1_bass",
        "entry1_zone_1_treble",
        "entry1_zone_2_bass",
        "entry1_zone_2_treble",
    ]
