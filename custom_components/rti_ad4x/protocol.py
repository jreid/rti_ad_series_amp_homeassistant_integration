"""Low-level protocol client for the RTI AD-4x audio amplifier.

The amplifier accepts ASCII commands over TCP port 23, terminated by a bare
``\\r``. Every command -- including read-only queries -- replies with a
status line terminated by ``\\r\\n``:

    #<zone2>,<power0/1>,<mute0/1>,<source2>,<volume_db>

e.g. ``#01,1,0,01,-27`` is zone 1, powered on, unmuted, source 1, -27 dB.
Unrecognized commands reply with ``#?``. Out-of-range zones reply with
nothing at all.

Commands that change power additionally emit an *unsolicited* broadcast
line such as ``#ZNON01`` (a bitmask of which zones are on), which arrives
after the status line and is not addressed to any particular request.
``_write_and_read`` therefore reads until it sees a line that looks like a
direct reply, discarding broadcasts; a broadcast left in the socket buffer
is skipped by the next command instead.

Volume is set as an attenuation magnitude from ``00`` (0 dB, loudest) to
``70`` (-70 dB, quietest); the status line reports the same value signed.

**The amplifier accepts only one TCP client at a time.** While a socket is
open, every other connect attempt is refused outright -- so how long a
connection is held is a question of whether anything else can control the
amplifier at all, not merely of efficiency. A connection is therefore held
for exactly one logical operation: ``session()`` brackets a group of
related commands (a poll sweep, a batch of adjustments) onto a single
connection and closes it on exit, while a command issued outside a session
gets a one-shot connection. Between operations the port is free.

Connects must also be spaced: opening immediately after a close is refused
roughly half the time, so ``CONNECTION_RECONNECT_SETTLE`` is observed
before each attempt and a refusal is retried a few times -- on a
single-client device, "refused" usually means "busy", not "broken".

Treble and bass are set with ``*ZN{zone}TRB{value}`` / ``...BAS{value}``,
encoded as 00-12 for 0 to +12 dB and 20-32 for 0 to -12 dB, in 2 dB steps
(the amplifier's actual adjustment granularity) -- confirmed against the
`AET.RTI.ADx <https://github.com/tony722/AET.RTI.ADx>`_ Crestron module
(Apache License 2.0), the only public reference for this encoding. The
amplifier does not validate the value, so out-of-range or odd-numbered
input is snapped to a legal step before being sent.

Tone settings are *not* part of the ``#`` zone status line, but they can be
read back with ``*ZN{zone}SET00``, which replies on its own ``$`` channel:

    $<zone2>,<bass_db>,<treble_db>      e.g. $01,-02,+08

**Bass comes before treble** -- verified by setting treble to +8 and bass to
-2 and reading back ``$01,-02,+08``. This query is non-mutating and works
whether or not the zone is powered on, and tone survives a power cycle.

A zone's power state gates which commands the amplifier will honour, which
was measured against a real unit rather than assumed:

===============  ====================================================
Command (zone off)  Behaviour
===============  ====================================================
``VOL{nn}``      Powers the zone **on** and applies the level
``VOLUP/VOLDN``  Powers the zone **on**, level unchanged
``SRC{nn}``      Powers the zone **on** and applies the source
``MUT{nn}``      Ignored; answers with a ``#`` zone-status line
``TRB``/``BAS``  Ignored; answers with a ``#`` zone-status line
===============  ====================================================

So the amplifier either silently wakes a zone or silently drops the request.
Callers wanting neither should check power state first; a tone command that
was dropped raises :class:`RtiAd4xZoneOffError` rather than looking like a
malformed reply.

``*ZALLPWR00`` turns off every zone at once and replies ``#ZALLOFF``
(not the usual per-zone status line).

Finally, commands must be spaced at least ``MIN_COMMAND_INTERVAL`` apart --
the amplifier silently drops anything arriving sooner. ``_send`` paces
itself, so every exchange here is safe by construction regardless of how
fast callers ask for work.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from .const import (
    CONNECTION_RECONNECT_SETTLE,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_PORT,
    MAX_ATTENUATION_DB,
    MAX_CONNECT_ATTEMPTS,
    MAX_REPLY_LINES,
    MAX_TONE_DB,
    MIN_COMMAND_INTERVAL,
    MIN_TONE_DB,
    RESPONSE_TIMEOUT,
    TONE_STEP_DB,
)

#: One 1 dB volume step expressed in Home Assistant's 0.0-1.0 scale.
VOLUME_STEP_LEVEL = 1 / MAX_ATTENUATION_DB


class RtiAd4xError(Exception):
    """Raised for connection failures or unexpected amplifier replies."""


class RtiAd4xZoneOffError(RtiAd4xError):
    """The amplifier declined a command because the zone is powered off."""


@dataclass
class ZoneStatus:
    zone: int
    power: bool
    mute: bool
    source: int
    volume_db: int

    @property
    def volume_level(self) -> float:
        """Home Assistant volume, 0.0 (quietest) to 1.0 (loudest)."""
        return 1 - (abs(self.volume_db) / MAX_ATTENUATION_DB)


@dataclass
class ToneStatus:
    zone: int
    treble_db: int
    bass_db: int


def volume_level_to_attenuation(volume_level: float) -> int:
    """Convert a Home Assistant 0.0-1.0 volume to a 00-70 attenuation value."""
    level = max(0.0, min(1.0, volume_level))
    return round((1 - level) * MAX_ATTENUATION_DB)


def db_to_tone_command_value(db: int) -> int:
    """Encode a dB treble/bass value to the amplifier's command format.

    Treble/bass use a 00-12 / 20-32 encoding:
    - 00-12: 0 to +12 dB (00=0dB, 02=+2dB, ..., 12=+12dB)
    - 20-32: 0 to -12 dB (20=0dB, 22=-2dB, ..., 32=-12dB)

    Only even values are meaningful, so input is clamped to the supported
    range and snapped to the nearest 2 dB step. The amplifier accepts
    illegal values without complaint, so doing this here is what keeps
    odd or out-of-range input from producing undefined behaviour.
    """
    clamped = max(MIN_TONE_DB, min(MAX_TONE_DB, db))
    stepped = round(clamped / TONE_STEP_DB) * TONE_STEP_DB
    if stepped >= 0:
        return stepped
    return 20 + abs(stepped)


def _describe_failure(err: BaseException | None) -> str:
    """Explain an error, naming contention when that's what it looks like.

    The amplifier serves one client, so a refused connect and a connection
    dropped out from under us mean the same thing in practice: something else
    holds the control port. Saying so beats surfacing a bare errno.
    """
    if isinstance(err, (ConnectionRefusedError, ConnectionResetError)):
        return "another client is probably connected -- the amplifier accepts only one"
    if isinstance(err, asyncio.TimeoutError):
        return "timed out waiting for a reply"
    return str(err)


def _is_direct_reply(line: str, expect_zone: int | None) -> bool:
    """Is this line the answer to our command, rather than a broadcast?

    Replies carry no request tag, so the zone in the line is the only thing
    tying it to what we asked. Checking it means a status line for some other
    zone can never be mistaken for our answer and filed under the wrong entity.
    """
    if line.startswith(("#?", "#ZALLOFF")):
        return True
    if len(line) > 3 and line[0] in "#$" and line[1:3].isdigit() and "," in line:
        return expect_zone is None or int(line[1:3]) == expect_zone
    return False


def _parse_status_line(line: str) -> ZoneStatus:
    line = line.strip()
    if line == "#?":
        raise RtiAd4xError("Amplifier rejected the command")
    if not line.startswith("#"):
        raise RtiAd4xError(f"Unexpected reply: {line!r}")
    fields = line[1:].split(",")
    if len(fields) != 5:
        raise RtiAd4xError(f"Unexpected reply: {line!r}")
    zone, power, mute, source, volume_db = fields
    if power not in ("0", "1") or mute not in ("0", "1"):
        raise RtiAd4xError(f"Unexpected reply: {line!r}")
    try:
        return ZoneStatus(
            zone=int(zone),
            power=power == "1",
            mute=mute == "1",
            source=int(source),
            volume_db=int(volume_db),
        )
    except ValueError as err:
        raise RtiAd4xError(f"Unexpected reply: {line!r}") from err


def _parse_tone_status_line(line: str, command: str = "tone") -> ToneStatus:
    """Parse a tone reply, which is ``$<zone>,<bass>,<treble>``.

    Note the order: **bass comes first**. Verified by setting treble to +8 and
    bass to -2, which reads back as ``$01,-02,+08``.
    """
    line = line.strip()
    if line.startswith("#"):
        # A zone-status line in answer to a tone command means the amplifier
        # declined to act, which it does whenever the zone is powered off.
        raise RtiAd4xZoneOffError(
            f"Amplifier ignored the {command} command because the zone is off"
        )
    if not line.startswith("$"):
        raise RtiAd4xError(f"Unexpected tone reply: {line!r}")
    fields = line[1:].split(",")
    if len(fields) != 3:
        raise RtiAd4xError(f"Unexpected tone reply: {line!r}")
    try:
        zone, bass, treble = fields
        return ToneStatus(
            zone=int(zone),
            treble_db=int(treble),
            bass_db=int(bass),
        )
    except ValueError as err:
        raise RtiAd4xError(f"Unexpected tone reply: {line!r}") from err


class RtiAd4xClient:
    """Talks to the amplifier, holding the socket only as long as necessary.

    The amplifier permits a single client, so connections are scoped to one
    logical operation. Wrap related commands in :meth:`session` to share a
    connection; anything else gets a one-shot connection and releases the
    port immediately.
    """

    def __init__(self, host: str, port: int = DEFAULT_PORT) -> None:
        self._host = host
        self._port = port
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._last_activity: float | None = None
        self._last_close: float | None = None
        self._session_depth = 0

    @asynccontextmanager
    async def session(self) -> AsyncIterator[None]:
        """Keep one connection open for the duration of the block.

        Nests safely; the connection is released when the outermost session
        exits, so the amplifier's only control port stays free in between.
        """
        self._session_depth += 1
        try:
            yield
        finally:
            self._session_depth -= 1
            if self._session_depth == 0:
                await self.close()

    async def close(self) -> None:
        """Close the connection, if any. Safe to call even if never connected."""
        async with self._lock:
            await self._close_connection()

    async def _close_connection(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except OSError:
                pass
            self._last_close = asyncio.get_running_loop().time()
        self._reader = None
        self._writer = None

    async def _settle(self) -> None:
        """Wait out the gap the amplifier needs between a close and a connect."""
        if self._last_close is None:
            return
        loop = asyncio.get_running_loop()
        remaining = CONNECTION_RECONNECT_SETTLE - (loop.time() - self._last_close)
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def _ensure_connected(self) -> None:
        if self._writer is not None and not self._writer.is_closing():
            return
        loop = asyncio.get_running_loop()
        last_err: Exception | None = None
        for _ in range(MAX_CONNECT_ATTEMPTS):
            await self._settle()
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self._host, self._port),
                    timeout=DEFAULT_CONNECT_TIMEOUT,
                )
                return
            except (OSError, asyncio.TimeoutError) as err:
                last_err = err
                # Treat a refusal as "busy": back off by the settle interval and
                # try again, since only one client may be connected at a time.
                self._last_close = loop.time()
        raise RtiAd4xError(
            f"Could not connect to {self._host}:{self._port}: "
            f"{_describe_failure(last_err)}"
        ) from last_err

    async def _write_and_read(self, command: str, expect_zone: int | None) -> str:
        assert self._writer is not None and self._reader is not None
        loop = asyncio.get_running_loop()
        self._writer.write(f"*{command}\r".encode("ascii"))
        await self._writer.drain()
        # Broadcast lines (#ZNON..) follow some commands, so read past them to the
        # actual reply. Bounded by both a wall-clock budget and a line cap so a
        # chatty amplifier can't keep one exchange alive indefinitely.
        deadline = loop.time() + RESPONSE_TIMEOUT
        for _ in range(MAX_REPLY_LINES):
            budget = deadline - loop.time()
            if budget <= 0:
                break
            line = await asyncio.wait_for(self._reader.readline(), timeout=budget)
            if not line:
                raise RtiAd4xError(f"Connection closed with no reply to {command!r}")
            line_str = line.decode("ascii", errors="replace").strip()
            if _is_direct_reply(line_str, expect_zone):
                self._last_activity = loop.time()
                return line_str
        raise RtiAd4xError(f"No reply to {command!r} amid unsolicited output")

    async def _pace(self, loop: asyncio.AbstractEventLoop) -> None:
        """Hold off until MIN_COMMAND_INTERVAL has passed since the last exchange.

        The amplifier silently swallows commands that arrive too quickly, so
        this is enforced here -- at the only place every command passes
        through -- rather than relying on callers to space their requests.
        """
        if self._last_activity is None:
            return
        remaining = MIN_COMMAND_INTERVAL - (loop.time() - self._last_activity)
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def _send(self, command: str, expect_zone: int | None = None) -> str:
        async with self._lock:
            try:
                return await self._exchange(command, expect_zone)
            finally:
                # Outside a session every command is a one-shot: release the
                # amplifier's only control port as soon as we're done with it.
                if self._session_depth == 0:
                    await self._close_connection()

    async def _exchange(self, command: str, expect_zone: int | None) -> str:
        loop = asyncio.get_running_loop()
        last_err: Exception | None = None
        for attempt in range(2):
            if attempt:
                # A connection held across a session may have gone bad server-side;
                # drop it and retry once on a fresh one. Closing first discards any
                # late reply still in the buffer, which keeps replies aligned with
                # requests.
                await self._close_connection()
            await self._pace(loop)
            await self._ensure_connected()
            try:
                return await self._write_and_read(command, expect_zone)
            except (OSError, asyncio.TimeoutError) as err:
                last_err = err
        await self._close_connection()
        raise RtiAd4xError(
            f"Command {command!r} failed: {_describe_failure(last_err)}"
        ) from last_err

    async def get_status(self, zone: int) -> ZoneStatus:
        return _parse_status_line(await self._send(f"ZN{zone:02d}STA", zone))

    async def power_on(self, zone: int) -> ZoneStatus:
        return _parse_status_line(await self._send(f"ZN{zone:02d}PWR01", zone))

    async def power_off(self, zone: int) -> ZoneStatus:
        return _parse_status_line(await self._send(f"ZN{zone:02d}PWR00", zone))

    async def set_source(self, zone: int, source: int) -> ZoneStatus:
        return _parse_status_line(
            await self._send(f"ZN{zone:02d}SRC{source:02d}", zone)
        )

    async def set_mute(self, zone: int, mute: bool) -> ZoneStatus:
        return _parse_status_line(
            await self._send(f"ZN{zone:02d}MUT{int(mute):02d}", zone)
        )

    async def set_volume_level(self, zone: int, volume_level: float) -> ZoneStatus:
        attenuation = volume_level_to_attenuation(volume_level)
        return _parse_status_line(
            await self._send(f"ZN{zone:02d}VOL{attenuation:02d}", zone)
        )

    async def volume_up(self, zone: int) -> ZoneStatus:
        return _parse_status_line(await self._send(f"ZN{zone:02d}VOLUP", zone))

    async def volume_down(self, zone: int) -> ZoneStatus:
        return _parse_status_line(await self._send(f"ZN{zone:02d}VOLDN", zone))

    async def set_treble(self, zone: int, db: int) -> ToneStatus:
        cmd_value = db_to_tone_command_value(db)
        return _parse_tone_status_line(
            await self._send(f"ZN{zone:02d}TRB{cmd_value:02d}", zone), "treble"
        )

    async def set_bass(self, zone: int, db: int) -> ToneStatus:
        cmd_value = db_to_tone_command_value(db)
        return _parse_tone_status_line(
            await self._send(f"ZN{zone:02d}BAS{cmd_value:02d}", zone), "bass"
        )

    async def get_tone_status(self, zone: int) -> ToneStatus:
        """Query current treble/bass settings. Works even with the zone off."""
        return _parse_tone_status_line(await self._send(f"ZN{zone:02d}SET00", zone))

    async def all_zones_off(self) -> None:
        """Turn off all zones at once. Reply is #ZALLOFF, not per-zone status."""
        reply = await self._send("ZALLPWR00")
        if not reply.startswith("#ZALLOFF"):
            raise RtiAd4xError(f"Unexpected reply to all_zones_off: {reply!r}")
