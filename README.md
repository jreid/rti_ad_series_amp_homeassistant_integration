# RTI AD-4x Home Assistant Integration

A custom Home Assistant integration that controls an RTI AD-4x audio
distribution amplifier directly over its Ethernet port -- no RTI control
processor required.

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
- Unrecognized commands get `#?` back. Zones outside 1-4 get no reply at
  all -- the AD-4x is a fixed 4-zone unit.
- **Commands must be spaced at least ~100 ms apart.** Anything faster is
  silently swallowed. `RtiAd4xClient` paces itself, so this holds no matter
  how fast callers ask for work.

### One client at a time

**The amplifier accepts a single TCP connection.** While one socket is open,
every other connect attempt is refused. So how long a connection is held
decides whether anything else can control the amplifier at all -- your own
`nc` session included -- which makes it a correctness question, not an
efficiency one.

`RtiAd4xClient` therefore holds a connection for exactly one logical
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
`RtiAd4xZoneOffError` rather than surfacing as a malformed reply.

### No polling

Since nothing else can write to the amplifier, and every command answers
with the resulting state, the cached state cannot drift while Home
Assistant is running. State is read **once at setup** and maintained from
command replies after that. There is no poll interval to configure.

The tradeoff is that a power-cycle of the amplifier isn't noticed: zones you
haven't touched keep showing pre-reboot state. Any zone you *do* touch
self-corrects from that command's reply, and `homeassistant.update_entity`
forces a full re-read.

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

Copy `custom_components/rti_ad4x/` into your Home Assistant `config/custom_components/` directory, then restart Home Assistant.

```
config/
└── custom_components/
    └── rti_ad4x/
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

Settings > Devices & Services > Add Integration > "RTI AD-4x". You'll be
asked for:

- **Name** -- shown as the amplifier's device name; defaults to "RTI AD-4x".
- **Host** -- the amplifier's IP or hostname.
- **Port** -- defaults to 23.
- **Number of zones** -- 1-4.
- **Source names** -- comma-separated, in source-number order (e.g.
  `Chromecast, Turntable, AUX, Radio`).

Zone count and source names can be changed later from the integration's
Configure button without removing it; lowering the zone count removes the
now-unused zone devices and entities. If the amplifier's host or port
changes, use Reconfigure instead of deleting and re-adding the integration.

## Tone control

Each zone device has `number.treble` and `number.bass` entities (-12 to
+12 dB), backed by the amplifier's own reply rather than the last commanded
value -- so they read correctly even if you never touch them. Values are
clamped and snapped to the amplifier's 2 dB granularity (5 becomes 4), which
is encoded on the wire as 00-12 for 0 to +12 dB and 20-32 for 0 to -12 dB,
confirmed against the `AET.RTI.ADx` Crestron module.

## Services

### `rti_ad4x.all_zones_off`

An **entity service**: target a zone by entity, device, or area. Turns off
every zone on the amplifier the targeted zone belongs to, using a single
`*ZALLPWR00` command instead of four separate power-offs -- the one case left
as a service rather than an entity, since it's one command instead of four at
the amplifier's 100 ms command pacing. Handy for a leaving-home automation.

```yaml
action: rti_ad4x.all_zones_off
target:
  entity_id: media_player.rti_ad4x_zone_1
```

## Tests

```bash
python3 tests/run_tests.py     # no dependencies
pytest tests/                  # same tests, if you have pytest
```

They stub out the few Home Assistant symbols the code touches and talk to a
fake amplifier over a real socket, so no hardware is needed. Most assertions
pin down protocol behaviour that was expensive to discover — the bass-before-
treble field order, the exact attenuation ceiling, pacing, single-client port
release, and power gating — plus regressions for bugs that shipped: a burst of
presses landing on the wrong value, a press lost mid-flight, and tone being
unsettable to flat.

## Attribution

Protocol reverse-engineering and tone encoding based on:
- [tony722/AET.RTI.ADx](https://github.com/tony722/AET.RTI.ADx) — Crestron
  module for RTI AD-series amplifiers (Apache License 2.0)

The command set was verified against a real AD-4x; where this integration and
the Crestron module differ, the hardware won.
