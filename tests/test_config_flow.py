"""Config flow tests: source-string parsing, DNS-resolved unique_id, and the
create/reconfigure/options steps.

These drive the flow through the real `FlowManager`
(`hass.config_entries.flow`/`.options`) rather than instantiating
RtiAd4xConfigFlow/RtiAd4xOptionsFlow directly: real HA's ConfigFlow base
class relies on the manager to populate `self.handler`, `self.context`, and
`self.flow_id` before any step runs, so a bare instance can't be driven the
way a hand-rolled stand-in could. An abort or form is a returned result
(`result["type"] == "abort"`/`"form"`), same as the real self.async_abort().

config_flow.py does its own unique_id dedup (`_conflicting_entry`) rather
than the framework's `_abort_if_unique_id_configured` helper: that helper
doesn't exclude the entry currently being reconfigured from its "already
configured" check, which would make every reconfigure -- even a no-op one --
abort against itself. `test_reconfigure_to_the_amps_own_current_address_is_not_a_duplicate_of_itself`
below is the regression test for exactly that.
"""

from __future__ import annotations

import socket

import pytest
from harness import FakeServer, const
from harness import protocol as p
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rti_ad4x import config_flow as cf


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
    data = {"host": "127.0.0.1", "port": 23, "zones": 2, "sources": ["A", "B"]}
    data.update(data_overrides)
    return MockConfigEntry(domain=const.DOMAIN, data=data, unique_id=unique_id)


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
# _validate_connection itself -- every flow test above monkeypatches it away,
# so it otherwise has zero direct coverage.
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("socket_enabled")
async def test_validate_connection_succeeds_against_a_real_amplifier():
    srv = FakeServer()
    port = await srv.start()
    await cf._validate_connection("127.0.0.1", port)  # must not raise
    srv.stop()


@pytest.mark.usefixtures("socket_enabled")
async def test_validate_connection_fails_against_a_refused_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    try:
        await cf._validate_connection("127.0.0.1", dead_port)
    except p.RtiAd4xError:
        pass
    else:
        raise AssertionError("expected a connection failure")


# --------------------------------------------------------------------------
# Unique-id resolution (#11: IP and hostname must dedup to the same amp)
# --------------------------------------------------------------------------


async def test_resolve_unique_id_uses_the_resolved_ip(hass):
    unique_id = await cf._resolve_unique_id(hass, "127.0.0.1", 23)
    assert unique_id == "127.0.0.1:23"


async def test_resolve_unique_id_falls_back_to_the_typed_host_on_dns_failure():
    class _FailingLoop:
        async def getaddrinfo(self, *args, **kwargs):
            raise OSError("no such host")

    class _Hass:
        loop = _FailingLoop()

    unique_id = await cf._resolve_unique_id(_Hass(), "amp.invalid", 23)
    assert unique_id == "amp.invalid:23"


# --------------------------------------------------------------------------
# async_step_user
# --------------------------------------------------------------------------


async def test_user_step_shows_a_form_with_no_input(hass):
    result = await hass.config_entries.flow.async_init(
        const.DOMAIN, context={"source": "user"}
    )
    assert result["step_id"] == "user"


async def test_user_step_creates_an_entry_on_success(hass, monkeypatch):
    monkeypatch.setattr(cf, "_validate_connection", _ok_validate)
    result = await hass.config_entries.flow.async_init(
        const.DOMAIN, context={"source": "user"}, data=_user_input()
    )
    assert result["title"] == "RTI AD-4x"
    assert result["data"]["host"] == "127.0.0.1"
    assert result["data"]["sources"] == ["Chromecast", "Turntable"]
    assert result["result"].unique_id == "127.0.0.1:23"


async def test_user_step_reports_no_sources_error(hass):
    result = await hass.config_entries.flow.async_init(
        const.DOMAIN, context={"source": "user"}, data=_user_input(sources="   ,  ")
    )
    assert result["errors"] == {"sources": "no_sources"}


async def test_user_step_reports_cannot_connect(hass, monkeypatch):
    monkeypatch.setattr(cf, "_validate_connection", _fail_validate)
    result = await hass.config_entries.flow.async_init(
        const.DOMAIN, context={"source": "user"}, data=_user_input()
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_step_aborts_when_the_amp_is_already_configured(hass, monkeypatch):
    monkeypatch.setattr(cf, "_validate_connection", _ok_validate)
    MockConfigEntry(domain=const.DOMAIN, unique_id="127.0.0.1:23").add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        const.DOMAIN, context={"source": "user"}, data=_user_input()
    )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


async def test_user_step_does_not_treat_a_different_amp_as_a_duplicate(
    hass, monkeypatch
):
    monkeypatch.setattr(cf, "_validate_connection", _ok_validate)
    MockConfigEntry(domain=const.DOMAIN, unique_id="10.0.0.9:23").add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        const.DOMAIN, context={"source": "user"}, data=_user_input()
    )
    assert result["type"] == "create_entry"


# --------------------------------------------------------------------------
# async_step_reconfigure (#10: a changed host/port shouldn't force delete-and-readd)
# --------------------------------------------------------------------------


async def test_reconfigure_step_shows_current_values_as_defaults(hass):
    entry = _existing_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"


async def test_reconfigure_step_updates_the_entry_and_its_unique_id_on_success(
    hass, monkeypatch
):
    # unique_id is host-derived (#11), so a legitimate reconfigure -- the same
    # amp, moved to a new address by DHCP -- must change it, not conflict
    # with it.
    monkeypatch.setattr(cf, "_validate_connection", _ok_validate)
    entry = _existing_entry()  # host 127.0.0.1, unique_id "127.0.0.1:23"
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.1.50", "port": 23}
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["host"] == "192.168.1.50"
    assert entry.unique_id == "192.168.1.50:23"


async def test_reconfigure_to_the_amps_own_current_address_is_not_a_duplicate_of_itself(
    hass, monkeypatch
):
    monkeypatch.setattr(cf, "_validate_connection", _ok_validate)
    entry = _existing_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "127.0.0.1", "port": 23}
    )
    assert result["reason"] == "reconfigure_successful"


async def test_reconfigure_step_aborts_if_the_new_address_belongs_to_another_configured_amp(
    hass, monkeypatch
):
    monkeypatch.setattr(cf, "_validate_connection", _ok_validate)
    MockConfigEntry(domain=const.DOMAIN, unique_id="10.0.0.9:23").add_to_hass(hass)
    entry = _existing_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "10.0.0.9", "port": 23}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"
    assert entry.data["host"] == "127.0.0.1"  # left untouched


async def test_reconfigure_step_reports_cannot_connect(hass, monkeypatch):
    monkeypatch.setattr(cf, "_validate_connection", _fail_validate)
    entry = _existing_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "127.0.0.1", "port": 23}
    )
    assert result["errors"] == {"base": "cannot_connect"}


# --------------------------------------------------------------------------
# Options flow
# --------------------------------------------------------------------------


async def test_options_flow_updates_zones_and_sources(hass):
    entry = MockConfigEntry(
        domain=const.DOMAIN, data={"zones": 2, "sources": ["A", "B"]}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"zones": 3, "sources": "A, B, C"}
    )
    assert result["type"] == "create_entry"
    assert entry.options == {"zones": 3, "sources": ["A", "B", "C"]}


async def test_options_flow_reports_no_sources_error(hass):
    entry = MockConfigEntry(domain=const.DOMAIN, data={"zones": 2, "sources": ["A"]})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"zones": 2, "sources": "   "}
    )
    assert result["errors"] == {"sources": "no_sources"}
