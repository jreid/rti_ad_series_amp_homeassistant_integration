"""Button entity tests: lives on the amp hub device, presses coordinator's
all-zones-off in one command.
"""

from __future__ import annotations

from harness import button as btn
from harness import calls_of, const, make_coordinator, setup_platform_entry
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

# --------------------------------------------------------------------------
# Device/entity wiring -- the hub device, not a zone device
# --------------------------------------------------------------------------


async def test_device_info_is_the_amp_hub_not_a_zone_device(hass):
    coordinator, _ = make_coordinator(hass, zones=(1, 2))
    entry = MockConfigEntry(domain=const.DOMAIN, entry_id="entry1")
    entity = btn.RtiAdAllZonesOffButton(coordinator, entry)
    assert entity._attr_device_info["identifiers"] == {(const.DOMAIN, "entry1")}


async def test_unique_id_is_scoped_to_the_entry(hass):
    coordinator, _ = make_coordinator(hass, zones=(1, 2))
    entry = MockConfigEntry(domain=const.DOMAIN, entry_id="entry1")
    entity = btn.RtiAdAllZonesOffButton(coordinator, entry)
    assert entity._attr_unique_id == "entry1_all_zones_off"


# --------------------------------------------------------------------------
# async_press
# --------------------------------------------------------------------------


async def test_press_turns_off_every_zone_with_one_command(hass):
    coordinator, amp = make_coordinator(hass, zones=(1, 2), powered=True)
    coordinator.data = await coordinator._async_update_data()
    entry = MockConfigEntry(domain=const.DOMAIN, entry_id="entry1")
    entity = btn.RtiAdAllZonesOffButton(coordinator, entry)

    await entity.async_press()

    assert calls_of(amp, "alloff")
    assert coordinator.data[1].status.power is False
    assert coordinator.data[2].status.power is False


# --------------------------------------------------------------------------
# async_setup_entry
# --------------------------------------------------------------------------


async def test_setup_entry_creates_a_single_button_for_the_entry(hass):
    coordinator, _ = make_coordinator(hass, zones=(1, 2))
    entry = MockConfigEntry(domain=const.DOMAIN, entry_id="entry1", data={"zones": 2})
    entry.runtime_data = coordinator
    entry.add_to_hass(hass)

    await setup_platform_entry(hass, btn, "button", entry)

    registry = er.async_get(hass)
    unique_ids = [
        registered.unique_id
        for registered in er.async_entries_for_config_entry(registry, entry.entry_id)
    ]
    assert unique_ids == ["entry1_all_zones_off"]
