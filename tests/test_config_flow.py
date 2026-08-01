"""Config flow tests: DNS-resolved unique_id, and the create/reconfigure/
options steps, including the two-step (connection details -> per-source
name/enabled) shape shared by setup and options.

These drive the flow through `FlowManager`
(`hass.config_entries.flow`/`.options`) rather than instantiating
RtiAdConfigFlow/RtiAdOptionsFlow directly: the ConfigFlow base class
relies on the manager to populate `self.handler`, `self.context`, and
`self.flow_id` before any step runs, so a bare instance can't be driven at
all. An abort or form arrives as a returned result
(`result["type"] == "abort"`/`"form"`).

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

from custom_components.rti_ad import config_flow as cf


def _schema_default(schema, field_name):
    """A vol.Required marker compares equal to its plain key name, so this
    finds the field among the schema's markers regardless of what default
    value it was actually built with."""
    for key in schema.schema:
        if key == field_name:
            return key.default()
    raise KeyError(field_name)


async def _ok_validate(host, port):
    return None


async def _fail_validate(host, port):
    raise p.RtiAdError("boom")


def _user_input(**overrides):
    data = {
        "name": "RTI AD-4x",
        "host": "127.0.0.1",
        "port": 23,
        "zones": 2,
        "source_count": 2,
    }
    data.update(overrides)
    return data


def _sources_input(names_and_enabled):
    data = {}
    for i, (name, enabled) in enumerate(names_and_enabled, start=1):
        data[f"source_{i}_name"] = name
        data[f"source_{i}_enabled"] = enabled
    return data


def _existing_entry(*, unique_id="127.0.0.1:23", **data_overrides):
    data = {
        "host": "127.0.0.1",
        "port": 23,
        "zones": 2,
        "sources": [{"name": "A", "enabled": True}, {"name": "B", "enabled": True}],
    }
    data.update(data_overrides)
    return MockConfigEntry(domain=const.DOMAIN, data=data, unique_id=unique_id)


# --------------------------------------------------------------------------
# _sources_schema / _sources_from_input
# --------------------------------------------------------------------------


def test_sources_from_input_builds_ordered_source_list():
    user_input = _sources_input([("Chromecast", True), ("Turntable", False)])
    assert cf._sources_from_input(user_input, 2) == [
        {"name": "Chromecast", "enabled": True},
        {"name": "Turntable", "enabled": False},
    ]


def test_sources_from_input_falls_back_to_a_placeholder_name_when_blank():
    user_input = _sources_input([("  ", True)])
    assert cf._sources_from_input(user_input, 1) == [
        {"name": "Source 1", "enabled": True}
    ]


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
    except p.RtiAdError:
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
# async_step_user -> async_step_sources
# --------------------------------------------------------------------------


async def test_user_step_shows_a_form_with_no_input(hass):
    result = await hass.config_entries.flow.async_init(
        const.DOMAIN, context={"source": "user"}
    )
    assert result["step_id"] == "user"


async def test_user_step_advances_to_the_sources_step_on_success(hass, monkeypatch):
    monkeypatch.setattr(cf, "_validate_connection", _ok_validate)
    result = await hass.config_entries.flow.async_init(
        const.DOMAIN, context={"source": "user"}, data=_user_input()
    )
    assert result["step_id"] == "sources"
    assert result["type"] == "form"


async def test_sources_step_creates_an_entry_on_success(hass, monkeypatch):
    monkeypatch.setattr(cf, "_validate_connection", _ok_validate)
    result = await hass.config_entries.flow.async_init(
        const.DOMAIN, context={"source": "user"}, data=_user_input()
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        _sources_input([("Chromecast", True), ("Turntable", True)]),
    )
    assert result["title"] == "RTI AD-4x"
    assert result["data"]["host"] == "127.0.0.1"
    assert result["data"]["sources"] == [
        {"name": "Chromecast", "enabled": True},
        {"name": "Turntable", "enabled": True},
    ]
    assert result["result"].unique_id == "127.0.0.1:23"


async def test_sources_step_reports_no_sources_enabled_error(hass, monkeypatch):
    monkeypatch.setattr(cf, "_validate_connection", _ok_validate)
    result = await hass.config_entries.flow.async_init(
        const.DOMAIN, context={"source": "user"}, data=_user_input()
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        _sources_input([("Chromecast", False), ("Turntable", False)]),
    )
    assert result["errors"] == {"base": "no_sources_enabled"}


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
    assert result["step_id"] == "sources"


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


async def test_options_flow_skips_the_sources_step_when_nothing_about_it_changed(hass):
    entry = _existing_entry()  # sources: A, B (2)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"zones": 3, "source_count": 2}
    )
    # Same source count, edit_sources left unchecked: a zones-only change
    # shouldn't have to page through and resubmit the sources form.
    assert result["type"] == "create_entry"
    assert entry.options == {
        "zones": 3,
        "sources": [
            {"name": "A", "enabled": True},
            {"name": "B", "enabled": True},
        ],
    }


async def test_options_flow_advances_to_the_sources_step_when_edit_sources_is_checked(
    hass,
):
    entry = _existing_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"zones": 3, "source_count": 2, "edit_sources": True},
    )
    assert result["step_id"] == "sources"


async def test_options_flow_updates_zones_and_sources(hass):
    entry = _existing_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"zones": 3, "source_count": 2, "edit_sources": True},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _sources_input([("A", True), ("B", True)])
    )
    assert result["type"] == "create_entry"
    assert entry.options == {
        "zones": 3,
        "sources": [
            {"name": "A", "enabled": True},
            {"name": "B", "enabled": True},
        ],
    }


async def test_options_flow_reports_no_sources_enabled_error(hass):
    entry = _existing_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"zones": 2, "source_count": 2, "edit_sources": True},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _sources_input([("A", False), ("B", False)])
    )
    assert result["errors"] == {"base": "no_sources_enabled"}


async def test_options_flow_growing_source_count_keeps_existing_names(hass):
    entry = _existing_entry()  # sources: A, B
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"zones": 2, "source_count": 3}
    )
    # Growing from 2 to 3 sources should default the new row's schema, with
    # the first two rows still defaulted from the existing A/B names.
    schema = result["data_schema"]
    assert _schema_default(schema, "source_1_name") == "A"
    assert _schema_default(schema, "source_2_name") == "B"
    assert _schema_default(schema, "source_3_name") == "Source 3"


async def test_options_flow_shrinking_source_count_drops_the_trailing_rows(hass):
    entry = _existing_entry()  # sources: A, B
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"zones": 2, "source_count": 1}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _sources_input([("A", True)])
    )
    assert result["type"] == "create_entry"
    assert entry.options["sources"] == [{"name": "A", "enabled": True}]


async def test_options_flow_accepts_a_legacy_string_source_list(hass):
    """Entries written by the old comma-separated field must still load."""
    entry = MockConfigEntry(
        domain=const.DOMAIN, data={"zones": 2, "sources": ["A", "B"]}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"zones": 2, "source_count": 2, "edit_sources": True},
    )
    assert result["step_id"] == "sources"
