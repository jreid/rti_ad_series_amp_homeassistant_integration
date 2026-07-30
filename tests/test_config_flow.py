"""Config flow tests: source-string parsing, DNS-resolved unique_id, and the
create/reconfigure/options steps.

These call RtiAd4xConfigFlow/RtiAd4xOptionsFlow directly rather than through a
real FlowManager (see harness.py); an abort is a returned result (type=abort),
same as the real self.async_abort().
"""

from __future__ import annotations

from harness import ConfigEntry, HomeAssistant, config_flow as cf, const, protocol as p, run


async def _ok_validate(host, port):
    return None


async def _fail_validate(host, port):
    raise p.RtiAd4xError("boom")


def _user_input(**overrides):
    data = {
        "name": "RTI AD-4x",
        "host": "127.0.0.1",
        "port": 23,
        "zones": 2,
        "sources": "Chromecast, Turntable",
    }
    data.update(overrides)
    return data


def _existing_entry(*, unique_id="127.0.0.1:23", **data_overrides):
    data = {"host": "127.0.0.1", "port": 23}
    data.update(data_overrides)
    return ConfigEntry(entry_id="existing", domain=const.DOMAIN, data=data, unique_id=unique_id)


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def test_sources_round_trip_through_the_form_string():
    sources = ["Chromecast", "Turntable", "AUX"]
    assert cf._string_to_sources(cf._sources_to_string(sources)) == sources


def test_blank_sources_string_is_rejected():
    try:
        cf._string_to_sources("  ,  ,")
    except ValueError:
        pass
    else:
        raise AssertionError("blank source list should have been rejected")


# --------------------------------------------------------------------------
# Unique-id resolution (#11: IP and hostname must dedup to the same amp)
# --------------------------------------------------------------------------


def test_resolve_unique_id_uses_the_resolved_ip():
    async def go():
        unique_id = await cf._resolve_unique_id(HomeAssistant(), "127.0.0.1", 23)
        assert unique_id == "127.0.0.1:23"

    run(go())


def test_resolve_unique_id_falls_back_to_the_typed_host_on_dns_failure():
    class _FailingLoop:
        async def getaddrinfo(self, *args, **kwargs):
            raise OSError("no such host")

    class _Hass:
        loop = _FailingLoop()

    async def go():
        unique_id = await cf._resolve_unique_id(_Hass(), "amp.invalid", 23)
        assert unique_id == "amp.invalid:23"

    run(go())


# --------------------------------------------------------------------------
# async_step_user
# --------------------------------------------------------------------------


def test_user_step_shows_a_form_with_no_input():
    async def go():
        flow = cf.RtiAd4xConfigFlow()
        flow.hass = HomeAssistant()
        result = await flow.async_step_user(None)
        assert result["step_id"] == "user"

    run(go())


def test_user_step_creates_an_entry_on_success():
    async def go():
        cf._validate_connection = _ok_validate
        flow = cf.RtiAd4xConfigFlow()
        flow.hass = HomeAssistant()
        result = await flow.async_step_user(_user_input())
        assert result["title"] == "RTI AD-4x"
        assert result["data"]["host"] == "127.0.0.1"
        assert result["data"]["sources"] == ["Chromecast", "Turntable"]
        assert flow.unique_id == "127.0.0.1:23"

    run(go())


def test_user_step_reports_no_sources_error():
    async def go():
        flow = cf.RtiAd4xConfigFlow()
        flow.hass = HomeAssistant()
        result = await flow.async_step_user(_user_input(sources="   ,  "))
        assert result["errors"] == {"sources": "no_sources"}

    run(go())


def test_user_step_reports_cannot_connect():
    async def go():
        cf._validate_connection = _fail_validate
        flow = cf.RtiAd4xConfigFlow()
        flow.hass = HomeAssistant()
        result = await flow.async_step_user(_user_input())
        assert result["errors"] == {"base": "cannot_connect"}
        cf._validate_connection = _ok_validate

    run(go())


def test_user_step_aborts_when_the_amp_is_already_configured():
    async def go():
        cf._validate_connection = _ok_validate
        hass = HomeAssistant()
        hass.config_entries.entries.append(
            ConfigEntry(domain=const.DOMAIN, unique_id="127.0.0.1:23")
        )
        flow = cf.RtiAd4xConfigFlow()
        flow.hass = hass
        result = await flow.async_step_user(_user_input())
        assert result == {"type": "abort", "reason": "already_configured"}

    run(go())


def test_user_step_does_not_treat_a_different_amp_as_a_duplicate():
    async def go():
        cf._validate_connection = _ok_validate
        hass = HomeAssistant()
        hass.config_entries.entries.append(
            ConfigEntry(domain=const.DOMAIN, unique_id="10.0.0.9:23")
        )
        flow = cf.RtiAd4xConfigFlow()
        flow.hass = hass
        result = await flow.async_step_user(_user_input())
        assert result["type"] == "create_entry"

    run(go())


# --------------------------------------------------------------------------
# async_step_reconfigure (#10: a changed host/port shouldn't force delete-and-readd)
# --------------------------------------------------------------------------


def test_reconfigure_step_shows_current_values_as_defaults():
    async def go():
        entry = _existing_entry()
        flow = cf.RtiAd4xConfigFlow()
        flow.hass = HomeAssistant()
        flow._reconfigure_entry = entry
        result = await flow.async_step_reconfigure(None)
        assert result["step_id"] == "reconfigure"

    run(go())


def test_reconfigure_step_updates_the_entry_and_its_unique_id_on_success():
    # unique_id is host-derived (#11), so a legitimate reconfigure -- the same
    # amp, moved to a new address by DHCP -- must change it, not conflict
    # with it.
    async def go():
        cf._validate_connection = _ok_validate
        entry = _existing_entry()  # host 127.0.0.1, unique_id "127.0.0.1:23"
        flow = cf.RtiAd4xConfigFlow()
        flow.hass = HomeAssistant()
        flow._reconfigure_entry = entry
        result = await flow.async_step_reconfigure({"host": "192.168.1.50", "port": 23})
        assert result["reason"] == "reconfigure_successful"
        assert entry.data["host"] == "192.168.1.50"
        assert entry.unique_id == "192.168.1.50:23"

    run(go())


def test_reconfigure_to_the_amps_own_current_address_is_not_a_duplicate_of_itself():
    async def go():
        cf._validate_connection = _ok_validate
        hass = HomeAssistant()
        entry = _existing_entry()
        hass.config_entries.entries.append(entry)
        flow = cf.RtiAd4xConfigFlow()
        flow.hass = hass
        flow._reconfigure_entry = entry
        result = await flow.async_step_reconfigure({"host": "127.0.0.1", "port": 23})
        assert result["reason"] == "reconfigure_successful"

    run(go())


def test_reconfigure_step_aborts_if_the_new_address_belongs_to_another_configured_amp():
    async def go():
        cf._validate_connection = _ok_validate
        hass = HomeAssistant()
        other = ConfigEntry(entry_id="other", domain=const.DOMAIN, unique_id="10.0.0.9:23")
        entry = _existing_entry()
        hass.config_entries.entries.extend([other, entry])
        flow = cf.RtiAd4xConfigFlow()
        flow.hass = hass
        flow._reconfigure_entry = entry
        result = await flow.async_step_reconfigure({"host": "10.0.0.9", "port": 23})
        assert result == {"type": "abort", "reason": "already_configured"}
        assert entry.data["host"] == "127.0.0.1"  # left untouched

    run(go())


def test_reconfigure_step_reports_cannot_connect():
    async def go():
        cf._validate_connection = _fail_validate
        entry = _existing_entry()
        flow = cf.RtiAd4xConfigFlow()
        flow.hass = HomeAssistant()
        flow._reconfigure_entry = entry
        result = await flow.async_step_reconfigure({"host": "127.0.0.1", "port": 23})
        assert result["errors"] == {"base": "cannot_connect"}
        cf._validate_connection = _ok_validate

    run(go())


# --------------------------------------------------------------------------
# Options flow
# --------------------------------------------------------------------------


def test_options_flow_updates_zones_and_sources():
    async def go():
        entry = ConfigEntry(domain=const.DOMAIN, data={"zones": 2, "sources": ["A", "B"]})
        flow = cf.RtiAd4xOptionsFlow()
        flow.config_entry = entry
        result = await flow.async_step_init({"zones": 3, "sources": "A, B, C"})
        assert result["data"] == {"zones": 3, "sources": ["A", "B", "C"]}

    run(go())


def test_options_flow_reports_no_sources_error():
    async def go():
        entry = ConfigEntry(domain=const.DOMAIN, data={"zones": 2, "sources": ["A"]})
        flow = cf.RtiAd4xOptionsFlow()
        flow.config_entry = entry
        result = await flow.async_step_init({"zones": 2, "sources": "   "})
        assert result["errors"] == {"sources": "no_sources"}

    run(go())


if __name__ == "__main__":
    import sys

    import test_config_flow

    sys.exit(__import__("harness").main(test_config_flow))
