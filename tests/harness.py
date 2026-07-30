"""Test harness: loads the integration without a Home Assistant install.

The modules under test import a handful of Home Assistant (and voluptuous)
symbols. Rather than depend on the full packages, we stand in the few pieces
the code actually touches. That keeps these tests runnable anywhere, which
matters because they encode protocol facts that were expensive to discover
on real hardware, and now also entity/config-flow wiring that's easy to get
subtly wrong (the wrong device, a service that never gets torn down, a
duplicate config entry).

Config flow tests drive RtiAd4xConfigFlow/RtiAd4xOptionsFlow directly rather
than through a real FlowManager: a step's abort is a returned result (like
the real self.async_abort()), not an exception, since config_flow.py does its
own unique_id dedup rather than the framework's _abort_if_unique_id_configured
helper (verified against a real HA install: that helper doesn't exclude the
entry currently being reconfigured from its "already configured" check, which
would make every reconfigure -- even a no-op one -- abort against itself).
"""

from __future__ import annotations

import asyncio
import enum
import importlib
import importlib.util
import logging
import sys
import types
from pathlib import Path

COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "rti_ad4x"

_UNSET = object()


class UpdateFailed(Exception):
    """Stand-in for homeassistant.helpers.update_coordinator.UpdateFailed."""


FlowResult = dict


class ConfigEntry:
    """Stand-in for homeassistant.config_entries.ConfigEntry."""

    def __class_getitem__(cls, item):
        return cls

    def __init__(
        self,
        *,
        entry_id="test_entry",
        domain="rti_ad4x",
        title="",
        data=None,
        options=None,
        unique_id=None,
    ) -> None:
        self.entry_id = entry_id
        self.domain = domain
        self.title = title
        self.data = dict(data or {})
        self.options = dict(options or {})
        self.unique_id = unique_id
        self.runtime_data = None
        self._unload_callbacks: list = []

    def add_update_listener(self, listener):
        return lambda: None

    def async_on_unload(self, func):
        self._unload_callbacks.append(func)


class ConfigEntriesManager:
    """Stand-in for hass.config_entries: only what unique_id dedup needs."""

    def __init__(self) -> None:
        self.entries: list[ConfigEntry] = []

    def async_entries(self, domain):
        return [e for e in self.entries if e.domain == domain]

    def async_update_entry(self, entry, *, data=None, unique_id=_UNSET) -> bool:
        """Stand-in for hass.config_entries.async_update_entry.

        Real HA fires the entry's update_listeners (async_create_task) when
        something actually changed, returning whether it did; our code relies
        on the update listener registered in __init__.py to reload, not on
        this return value, so listener-firing isn't reproduced here.
        """
        changed = False
        if data is not None and dict(data) != entry.data:
            entry.data = dict(data)
            changed = True
        if unique_id is not _UNSET and unique_id != entry.unique_id:
            entry.unique_id = unique_id
            changed = True
        return changed


class _FlowStepHelpers:
    """Shared by ConfigFlow/OptionsFlow: the result-builders steps call."""

    def async_create_entry(self, *, title, data):
        return {"type": "create_entry", "title": title, "data": data}

    def async_show_form(self, *, step_id, data_schema=None, errors=None):
        return {"type": "form", "step_id": step_id, "errors": errors or {}}

    def async_abort(self, *, reason):
        return {"type": "abort", "reason": reason}


class ConfigFlow(_FlowStepHelpers):
    """Stand-in for homeassistant.config_entries.ConfigFlow."""

    def __init_subclass__(cls, *, domain=None, **kwargs) -> None:
        # Real HA registers `domain` in a global handler table here; it isn't
        # stored on the class (config_flow.py doesn't read self.domain --
        # it uses the imported DOMAIN constant directly), so accept and
        # discard it, just enough to match the real signature.
        super().__init_subclass__(**kwargs)

    def __init__(self) -> None:
        self.hass = None
        self.unique_id = None
        self._reconfigure_entry: ConfigEntry | None = None

    async def async_set_unique_id(self, unique_id):
        self.unique_id = unique_id
        return None

    def _get_reconfigure_entry(self) -> ConfigEntry:
        assert self._reconfigure_entry is not None
        return self._reconfigure_entry


class OptionsFlow(_FlowStepHelpers):
    """Stand-in for homeassistant.config_entries.OptionsFlow."""

    def __init__(self) -> None:
        self.config_entry: ConfigEntry | None = None


def callback(func):
    """Stand-in for homeassistant.core.callback (a no-op decorator here)."""
    return func


class HomeAssistant:
    def __init__(self) -> None:
        self.config_entries = ConfigEntriesManager()

    def async_create_task(self, coro):
        return asyncio.ensure_future(coro)

    @property
    def loop(self):
        return asyncio.get_running_loop()


class DataUpdateCoordinator:
    """Just enough of the real coordinator for our subclass to work."""

    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, name=None, update_interval=None):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.data = None
        self.refresh_requests = 0

    def async_set_updated_data(self, data):
        self.data = data

    async def async_request_refresh(self):
        self.refresh_requests += 1

    async def async_config_entry_first_refresh(self):
        self.data = await self._async_update_data()


class CoordinatorEntity:
    """Stand-in for homeassistant.helpers.update_coordinator.CoordinatorEntity."""

    def __class_getitem__(cls, item):
        return cls

    def __init__(self, coordinator, context=None) -> None:
        self.coordinator = coordinator
        self.coordinator_context = context


class DeviceInfo(dict):
    """Stand-in for homeassistant.helpers.entity.DeviceInfo (a TypedDict for real)."""


class _FakePlatform:
    """Stand-in for what entity_platform.async_get_current_platform() returns."""

    def __init__(self) -> None:
        self.registered: list[tuple] = []

    def async_register_entity_service(self, name, schema, method):
        self.registered.append((name, schema, method))


_current_platform = _FakePlatform()


def async_get_current_platform():
    return _current_platform


AddEntitiesCallback = object  # only used here as a type annotation


class DeviceRegistryEntry:
    def __init__(self, device_id, identifiers, *, name=None, via_device=None):
        self.id = device_id
        self.identifiers = identifiers
        self.name = name
        self.via_device = via_device


class DeviceRegistry:
    """Stand-in for homeassistant.helpers.device_registry.DeviceRegistry."""

    def __init__(self) -> None:
        self.devices: dict[str, DeviceRegistryEntry] = {}
        self._next_id = 1

    def async_get_or_create(
        self,
        *,
        config_entry_id,
        identifiers,
        name=None,
        manufacturer=None,
        model=None,
        via_device=None,
    ):
        for device in self.devices.values():
            if device.identifiers & identifiers:
                return device
        device_id = f"device_{self._next_id}"
        self._next_id += 1
        device = DeviceRegistryEntry(device_id, identifiers, name=name, via_device=via_device)
        self.devices[device_id] = device
        return device

    def async_remove_device(self, device_id):
        self.devices.pop(device_id, None)


def _dr_async_get(hass):
    return hass.device_registry


def _dr_async_entries_for_config_entry(registry, config_entry_id):
    return list(registry.devices.values())


class Platform(str, enum.Enum):
    MEDIA_PLAYER = "media_player"
    NUMBER = "number"


class MediaPlayerDeviceClass(str, enum.Enum):
    SPEAKER = "speaker"


class MediaPlayerEntityFeature(enum.IntFlag):
    TURN_ON = 1
    TURN_OFF = 2
    VOLUME_SET = 4
    VOLUME_STEP = 8
    VOLUME_MUTE = 16
    SELECT_SOURCE = 32


class MediaPlayerState(str, enum.Enum):
    ON = "on"
    OFF = "off"


class MediaPlayerEntity:
    """Stand-in for homeassistant.components.media_player.MediaPlayerEntity."""


class NumberMode(str, enum.Enum):
    AUTO = "auto"
    BOX = "box"
    SLIDER = "slider"


class NumberEntity:
    """Stand-in for homeassistant.components.number.NumberEntity."""


class _Marker(str):
    """Stand-in for voluptuous's Required marker: behaves like the key string."""


def _vol_required(key, default=_UNSET):
    marker = _Marker(key)
    marker.default = default
    return marker


class VolSchema(dict):
    """Stand-in for voluptuous.Schema: never actually validates in these tests."""


def _vol_all(*validators):
    def _validate(value):
        for validator in validators:
            value = validator(value)
        return value

    return _validate


class VolRange:
    def __init__(self, min=None, max=None) -> None:
        self.min = min
        self.max = max

    def __call__(self, value):
        return value


def _install_ha_stubs() -> None:
    ha = types.ModuleType("homeassistant")
    ha.__path__ = []

    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = HomeAssistant
    core.callback = callback

    const_mod = types.ModuleType("homeassistant.const")
    const_mod.CONF_HOST = "host"
    const_mod.CONF_PORT = "port"
    const_mod.CONF_NAME = "name"
    const_mod.Platform = Platform

    config_entries_mod = types.ModuleType("homeassistant.config_entries")
    config_entries_mod.ConfigEntry = ConfigEntry
    config_entries_mod.ConfigFlow = ConfigFlow
    config_entries_mod.OptionsFlow = OptionsFlow

    data_entry_flow_mod = types.ModuleType("homeassistant.data_entry_flow")
    data_entry_flow_mod.FlowResult = FlowResult

    helpers = types.ModuleType("homeassistant.helpers")
    helpers.__path__ = []

    uc = types.ModuleType("homeassistant.helpers.update_coordinator")
    uc.DataUpdateCoordinator = DataUpdateCoordinator
    uc.UpdateFailed = UpdateFailed
    uc.CoordinatorEntity = CoordinatorEntity

    entity_mod = types.ModuleType("homeassistant.helpers.entity")
    entity_mod.DeviceInfo = DeviceInfo

    entity_platform_mod = types.ModuleType("homeassistant.helpers.entity_platform")
    entity_platform_mod.AddEntitiesCallback = AddEntitiesCallback
    entity_platform_mod.async_get_current_platform = async_get_current_platform

    device_registry_mod = types.ModuleType("homeassistant.helpers.device_registry")
    device_registry_mod.DeviceRegistry = DeviceRegistry
    device_registry_mod.async_get = _dr_async_get
    device_registry_mod.async_entries_for_config_entry = _dr_async_entries_for_config_entry

    components_mod = types.ModuleType("homeassistant.components")
    components_mod.__path__ = []

    media_player_mod = types.ModuleType("homeassistant.components.media_player")
    media_player_mod.MediaPlayerDeviceClass = MediaPlayerDeviceClass
    media_player_mod.MediaPlayerEntity = MediaPlayerEntity
    media_player_mod.MediaPlayerEntityFeature = MediaPlayerEntityFeature
    media_player_mod.MediaPlayerState = MediaPlayerState

    number_mod = types.ModuleType("homeassistant.components.number")
    number_mod.NumberEntity = NumberEntity
    number_mod.NumberMode = NumberMode

    voluptuous_mod = types.ModuleType("voluptuous")
    voluptuous_mod.Schema = VolSchema
    voluptuous_mod.Required = _vol_required
    voluptuous_mod.All = _vol_all
    voluptuous_mod.Range = VolRange

    for name, mod in (
        ("homeassistant", ha),
        ("homeassistant.core", core),
        ("homeassistant.const", const_mod),
        ("homeassistant.config_entries", config_entries_mod),
        ("homeassistant.data_entry_flow", data_entry_flow_mod),
        ("homeassistant.helpers", helpers),
        ("homeassistant.helpers.update_coordinator", uc),
        ("homeassistant.helpers.entity", entity_mod),
        ("homeassistant.helpers.entity_platform", entity_platform_mod),
        ("homeassistant.helpers.device_registry", device_registry_mod),
        ("homeassistant.components", components_mod),
        ("homeassistant.components.media_player", media_player_mod),
        ("homeassistant.components.number", number_mod),
        ("voluptuous", voluptuous_mod),
    ):
        sys.modules.setdefault(name, mod)


def load_component() -> tuple[types.ModuleType, ...]:
    """Load the integration as a real package, so `from . import X` works.

    const/protocol/coordinator get pulled in as a side effect of __init__.py's
    own relative imports; config_flow/media_player/number are loaded
    explicitly since nothing else imports them (HA's platform forwarding is
    dynamic and isn't reproduced here).
    """
    _install_ha_stubs()

    if "rti_ad4x_test" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "rti_ad4x_test",
            COMPONENT / "__init__.py",
            submodule_search_locations=[str(COMPONENT)],
        )
        pkg = importlib.util.module_from_spec(spec)
        sys.modules["rti_ad4x_test"] = pkg
        spec.loader.exec_module(pkg)
    pkg = sys.modules["rti_ad4x_test"]

    const = importlib.import_module("rti_ad4x_test.const")
    protocol = importlib.import_module("rti_ad4x_test.protocol")
    coordinator = importlib.import_module("rti_ad4x_test.coordinator")
    config_flow = importlib.import_module("rti_ad4x_test.config_flow")
    media_player = importlib.import_module("rti_ad4x_test.media_player")
    number = importlib.import_module("rti_ad4x_test.number")
    return pkg, const, protocol, coordinator, config_flow, media_player, number


pkg, const, protocol, coordinator, config_flow, media_player, number = load_component()


class FakeAmp:
    """An AD-4x stand-in reproducing the behaviour measured on real hardware.

    Notably: absolute volume and source selection wake a powered-off zone,
    while mute and tone commands are silently dropped -- tone answering with a
    zone-status line, which the client surfaces as RtiAd4xZoneOffError.
    """

    def __init__(self, zones=(1, 2), powered=True):
        self.calls: list[tuple] = []
        self.power = {z: powered for z in zones}
        self.atten = {z: 30 for z in zones}
        self.mute = {z: False for z in zones}
        self.treble = {z: 0 for z in zones}
        self.bass = {z: 0 for z in zones}
        self.source = {z: 1 for z in zones}
        self.sessions = 0
        self.session_events: list[str] = []
        self.fail_next_reads = 0
        self.read_delay = 0.0
        self.first_send = asyncio.Event()

    class _Session:
        def __init__(self, amp):
            self.amp = amp

        async def __aenter__(self):
            self.amp.sessions += 1
            self.amp.session_events.append("open")

        async def __aexit__(self, *exc):
            self.amp.session_events.append("close")

    def session(self):
        return self._Session(self)

    async def close(self):
        pass

    def _status(self, zone):
        return protocol.ZoneStatus(
            zone, self.power[zone], self.mute[zone], self.source[zone], -self.atten[zone]
        )

    def _tone(self, zone):
        return protocol.ToneStatus(zone, self.treble[zone], self.bass[zone])

    async def _wire(self):
        self.first_send.set()
        if self.read_delay:
            await asyncio.sleep(self.read_delay)

    async def get_status(self, zone):
        if self.fail_next_reads > 0:
            self.fail_next_reads -= 1
            raise protocol.RtiAd4xError("another client is probably connected")
        self.calls.append(("sta", zone))
        return self._status(zone)

    async def get_tone_status(self, zone):
        self.calls.append(("set", zone))
        return self._tone(zone)

    async def power_on(self, zone):
        self.power[zone] = True
        self.calls.append(("pwr", zone, 1))
        return self._status(zone)

    async def power_off(self, zone):
        self.power[zone] = False
        self.calls.append(("pwr", zone, 0))
        return self._status(zone)

    async def set_volume_level(self, zone, level):
        self.power[zone] = True  # measured: absolute volume wakes the zone
        self.atten[zone] = protocol.volume_level_to_attenuation(level)
        self.calls.append(("vol", zone, self.atten[zone]))
        await self._wire()
        return self._status(zone)

    async def set_source(self, zone, source):
        self.power[zone] = True  # measured: source selection wakes the zone
        self.source[zone] = source
        self.calls.append(("src", zone, source))
        return self._status(zone)

    async def set_mute(self, zone, mute):
        if not self.power[zone]:
            self.calls.append(("mute-dropped", zone))
            return self._status(zone)
        self.mute[zone] = mute
        self.calls.append(("mut", zone, mute))
        return self._status(zone)

    async def set_treble(self, zone, db):
        if not self.power[zone]:
            self.calls.append(("treble-dropped", zone))
            raise protocol.RtiAd4xZoneOffError("zone is off")
        self.treble[zone] = db
        self.calls.append(("trb", zone, db))
        return self._tone(zone)

    async def set_bass(self, zone, db):
        if not self.power[zone]:
            self.calls.append(("bass-dropped", zone))
            raise protocol.RtiAd4xZoneOffError("zone is off")
        self.bass[zone] = db
        self.calls.append(("bas", zone, db))
        return self._tone(zone)

    async def all_zones_off(self):
        self.calls.append(("alloff",))
        for zone in self.power:
            self.power[zone] = False


def make_coordinator(zones=(1, 2), powered=True):
    amp = FakeAmp(zones, powered)
    coord = coordinator.RtiAd4xCoordinator(HomeAssistant(), amp, list(zones))
    return coord, amp


def calls_of(amp, kind):
    return [c for c in amp.calls if c[0] == kind]


def run(coro):
    return asyncio.run(coro)


def main(*modules) -> int:
    """Run every test_* callable in the given modules. Returns an exit code."""
    logging.disable(logging.CRITICAL)
    failed = []
    total = 0
    for mod in modules:
        print(f"\n=== {mod.__name__} ===")
        for name in sorted(vars(mod)):
            if not name.startswith("test_"):
                continue
            fn = getattr(mod, name)
            if not callable(fn):
                continue
            total += 1
            try:
                fn()
            except Exception as err:  # noqa: BLE001 - report and continue
                failed.append(f"{mod.__name__}.{name}: {err}")
                print(f"  FAIL  {name}: {err}")
            else:
                print(f"  pass  {name}")
    print(f"\n{total - len(failed)}/{total} passed")
    for f in failed:
        print(f"  FAILED {f}")
    return 1 if failed else 0
