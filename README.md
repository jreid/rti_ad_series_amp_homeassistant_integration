# RTI AD Series Amplifier Home Assistant Integration

A custom Home Assistant integration that controls an RTI AD-4x or AD-8x audio
distribution amplifier directly over its Ethernet port -- no RTI control
processor required. Zone count and source count are configured independently
(1-8 each), so the same integration covers either model, or any wiring that
doesn't use every zone or source.

Each zone is its own device, holding a `media_player` entity (power, volume
set/step, mute, source selection) plus `number.treble` and `number.bass`
entities for tone control -- read from the amplifier, not just the last
commanded value.

## Protocol

Confirmed by direct testing against a real AD-4x:

- TCP port 23, plain ASCII. Commands end in a bare `\r`; replies end in `\r\n`.
- Status replies look like `#01,1,0,01,-27` -- zone, power (0/1), mute
  (0/1), source, volume in dB (0 = loudest, -70 = quietest).
- `*ZN01STA\r` is a non-mutating status query; every other command (power,
  source, volume, mute) replies with the same status line as a side
  effect, so no separate "did it work" round trip is needed.
- `*ZN01SET00\r` queries tone, replying on a separate `$` channel as
  `$<zone>,<bass>,<treble>` — **bass first**, verified by setting treble to
  +8 and bass to −2 and reading back `$01,-02,+08`. Non-mutating, and it
  works whether or not the zone is powered on.
- Volume is an attenuation of `00` (0 dB, loudest) to `70` (−70 dB). The
  ceiling is exact: `VOL70` reads back −70 dB, and `VOL78` or higher is
  rejected with `#?`.
- Power commands additionally emit an *unsolicited* broadcast line
  (`#ZNON01`, a bitmask of which zones are on) after the status line. The
  client reads until it sees something shaped like a direct reply and
  discards broadcasts, so these never get mistaken for the next command's
  response.
- Unrecognized commands get `#?` back. Zones (or sources) outside what a
  given unit actually has get no reply at all -- confirmed as 1-4 on a real
  AD-4x. An AD-8x speaks the same protocol with 1-8 zones and sources
  instead; the integration's zone/source count fields just need to match
  whichever unit is connected.
- **Commands must be spaced at least ~100 ms apart.** Anything faster is
  silently swallowed. `RtiAdClient` paces itself, so this holds no matter
  how fast callers ask for work.

### One client at a time

**The amplifier accepts a single TCP connection.** While one socket is open,
every other connect attempt is refused. So how long a connection is held
decides whether anything else can control the amplifier at all -- your own
`nc` session included -- which makes it a correctness question, not an
efficiency one.

`RtiAdClient` therefore holds a connection for exactly one logical
operation. `session()` brackets a group of related commands (the startup
read, a batch of adjustments) onto one connection and closes it on exit;
anything issued outside a session gets a one-shot connection. Between
operations the port is free.

Connects also have to be spaced -- opening immediately after a close is
refused roughly half the time -- so a settle delay is observed before each
attempt, and refusals are retried a few times. On a single-client device
"refused" usually means "busy", so contention is reported as such rather
than as a bare errno, and a failed read keeps the last known state for a
couple of attempts instead of blanking every zone the moment you connect
with something else.

### Power state gates what the amplifier will accept

Measured against a real unit, with a zone powered **off**:

| Command | What actually happens |
|---|---|
| `VOL{nn}` | Powers the zone **on** and applies the level |
| `VOLUP` / `VOLDN` | Powers the zone **on**, level unchanged |
| `SRC{nn}` | Powers the zone **on** and applies the source |
| `MUT{nn}` | Silently ignored; answers with a `#` zone-status line |
| `TRB` / `BAS` | Silently ignored; answers with a `#` zone-status line |

So the amplifier either wakes a zone you didn't ask it to wake, or drops the
request without saying so. Neither is what a user means by nudging a slider on
an off zone, so volume, mute and tone requests are **held while a zone is off
and applied when it is next powered on**. The entity still shows the requested
value in the meantime, so controls don't snap back.

Source selection is deliberately left alone: waking a zone because you picked
a source is reasonable, and the amplifier does it in one command.

Tone survives a power cycle, so nothing needs re-asserting beyond the deferred
requests. A tone command that the amplifier does drop raises
`RtiAdZoneOffError` rather than surfacing as a malformed reply.

### No polling

Every command answers with the resulting state, so as long as Home Assistant
is the only thing changing that state, the cache cannot drift. State is read
**once at setup** and maintained from command replies after that. There is no
poll interval to configure.

#### The sole-writer assumption

This is the one design decision here that depends on your installation rather
than on the hardware, so it's worth being explicit about what it does and
doesn't rest on.

It is *not* implied by the single-client TCP restriction above. That
restriction only prevents another client from being connected **at the same
time** as this integration -- and since a connection is deliberately held for
just one logical operation, the control port is free essentially all of the
time. Anything else on your network is free to connect during those gaps and
change whatever it likes. Physical control paths -- an RTI keypad, an IR
remote, the front panel -- don't go through TCP at all and are unaffected by
the restriction in either direction.

So the assumption is a statement about your setup: **nothing other than this
integration changes zone state.** Where that holds, no polling is needed and
the amplifier's single control port stays free for you to use. Where it
doesn't, state changed by that other controller is invisible to Home
Assistant until something forces a re-read, and the affected entities will
show stale power, volume, mute, or source values.

Three things bring an out-of-sync zone back:

- Touching the zone from Home Assistant -- the command's own reply carries
  the true resulting state, so any zone you actually operate self-corrects.
- `homeassistant.update_entity` on any entity of the amplifier, which forces
  a full re-read of every zone.
- Reloading the integration.

If you do have another controller wired to the same amplifier and want state
to track it, an automation calling `homeassistant.update_entity` on a
schedule reintroduces polling at whatever interval you choose -- with the
caveat that each sweep occupies the single control port for its duration.

The same blind spot covers a power-cycle of the amplifier itself: zones you
haven't touched keep showing pre-reboot state until one of the above
refreshes them.

### Coalescing rapid adjustments

Volume steps and tone changes are the only commands a user can generate
faster than the 100 ms floor, by holding a button or dragging a slider.
Sending each one individually would mean most get dropped, so the
coordinator debounces them for 150 ms and sends a single command carrying
the final value: six quick volume-up presses become one `VOL` command
6 dB higher, not six commands of which four vanish.

Two details worth knowing if you touch this code:

- Pending adjustments are stored as **absolute targets with a separate
  "dirty" flag**, not deltas. With deltas, a target of 0 dB is
  indistinguishable from "nothing requested", which makes setting tone to
  flat impossible to express.
- The volume target is **sticky** across a flush and only released when a
  read refreshes real state. Otherwise a press arriving while a command is
  still on the wire would rebase off a cache that hasn't caught up, and
  silently collapse into the previous press.

## Installation

Copy `custom_components/rti_ad/` into your Home Assistant `config/custom_components/` directory, then restart Home Assistant.

```
config/
└── custom_components/
    └── rti_ad/
        ├── __init__.py
        ├── manifest.json
        ├── const.py
        ├── config_flow.py
        ├── coordinator.py
        ├── media_player.py
        ├── number.py
        ├── protocol.py
        ├── services.yaml
        ├── strings.json
        └── translations/
            └── en.json
```

## Configuration

Settings > Devices & Services > Add Integration > "RTI AD Series Amplifier".
You'll be asked for:

- **Name** -- shown as the amplifier's device name; defaults to "RTI AD Series
  Amplifier".
- **Host** -- the amplifier's IP or hostname.
- **Port** -- defaults to 23.
- **Number of zones** -- 1-8.
- **Number of sources** -- 1-8.

A second step then shows one name + enabled row per source (e.g.
`Chromecast`, `Turntable`, `AUX`, `Radio`). Unchecking a source hides it from
every zone's source dropdown without losing its slot or renumbering the
others -- useful for a source that's wired but not currently in use.

Zone count, source count, and per-source names/enabled state can all be
changed later from the integration's Configure button without removing it;
lowering the zone count removes the now-unused zone devices and entities.
Configure only asks for the source-naming step again when the source count
actually changes, or when you tick "Edit source names and enabled state" --
a zones-only change applies immediately without having to page through and
resubmit every source row unchanged. Changing the source count keeps
existing names/enabled state for the sources that still fit, and adds or
drops rows at the end. If the amplifier's host or port changes, use
Reconfigure instead of deleting and re-adding the integration.

Zone *devices* (as opposed to sources) don't have a naming step in the flow
-- they're created as "Zone 1", "Zone 2", etc. Give one a friendlier name
(e.g. "Living Room") the same way as any other Home Assistant device:
Settings > Devices & Services > Devices > that zone > the pencil/rename
icon.

## Removal

Settings > Devices & Services > "RTI AD Series Amplifier" > Delete. This
removes the config entry along with its devices and entities; the amplifier
itself is unaffected and can be re-added later. To also remove the
integration's files, delete `config/custom_components/rti_ad/` and restart
Home Assistant.

## Tone control

Each zone device has `number.treble` and `number.bass` entities (-12 to
+12 dB), backed by the amplifier's own reply rather than the last commanded
value -- so they read correctly even if you never touch them. Values are
clamped and snapped to the amplifier's 2 dB granularity (5 becomes 4), which
is encoded on the wire as 00-12 for 0 to +12 dB and 20-32 for 0 to -12 dB,
confirmed against the `AET.RTI.ADx` Crestron module.

## All zones off

The amp hub device has a `button.all_zones_off` entity for turning off every
zone with a single `*ZALLPWR00` command instead of four separate power-offs.
Add it to a dashboard, or trigger it from an automation with
`button.press`.

The same action is also available as an entity service, for automations that
already target a zone entity, device, or area rather than the hub:

```yaml
action: rti_ad.all_zones_off
target:
  entity_id: media_player.zone_1
```

## Tests

```bash
pip install -r requirements_test.txt
pytest tests/
```

Tests run against the `homeassistant` package (via
[pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component),
pinned to the release built against this integration's `min_ha_version`) and
a fake amplifier over a local socket, so no hardware is needed. Most
assertions pin down protocol behaviour that was expensive to discover — the
bass-before-treble field order, the exact attenuation ceiling, pacing,
single-client port release, and power gating — plus the coordinator's
contract under load: a burst of presses landing on the final value, no press
lost mid-flight, and tone settable to flat.

## Attribution

Protocol reverse-engineering and tone encoding based on:
- [tony722/AET.RTI.ADx](https://github.com/tony722/AET.RTI.ADx) — Crestron
  module for RTI AD-series amplifiers (Apache License 2.0)

The command set was verified against a real AD-4x; where this integration and
the Crestron module differ, the hardware won.

## Related projects

- [srhunt-cyber/RTI-AD8x-Home-Assistant-bridge](https://github.com/srhunt-cyber/RTI-AD8x-Home-Assistant-bridge)
  -- a different approach to the same problem: bridges an RTI AD-8x to Home
  Assistant over MQTT rather than talking to the amplifier's TCP port
  directly, aimed at multi-room audio alongside Sonos and Alexa.
