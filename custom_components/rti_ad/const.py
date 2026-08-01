"""Constants for the RTI AD Series Amplifier integration."""

DOMAIN = "rti_ad"

CONF_ZONES = "zones"
CONF_SOURCES = "sources"
# Transient config/options-flow field: how many source rows to render on the
# next step. Never itself persisted -- only the resulting CONF_SOURCES is.
CONF_SOURCE_COUNT = "source_count"
# Transient options-flow field: force the sources step even when the source
# count hasn't changed, so a zones-only edit doesn't have to page through it.
CONF_EDIT_SOURCES = "edit_sources"

DEFAULT_PORT = 23
DEFAULT_CONNECT_TIMEOUT = 3
RESPONSE_TIMEOUT = 3

# The amplifier accepts only ONE TCP client at a time, so a connection is held
# no longer than a single logical operation (see RtiAdClient.session) and a
# refused connect usually means "someone else is talking to it right now".
CONNECTION_RECONNECT_SETTLE = 0.25
MAX_CONNECT_ATTEMPTS = 3

# Consecutive poll failures tolerated before entities are marked unavailable.
# Contention with another client is expected and shouldn't blank the dashboard.
POLL_FAILURE_TOLERANCE = 3

# Upper bound on lines read while skipping broadcasts, so a chatty amplifier
# can never keep a single exchange alive indefinitely.
MAX_REPLY_LINES = 20

# The amplifier silently drops commands that arrive faster than this, so the
# client paces every exchange rather than trusting callers to behave.
MIN_COMMAND_INTERVAL = 0.1

# How long to gather rapid adjustments before sending a single command.
COMMAND_COALESCE_WINDOW = 0.15

MIN_ZONES = 1
MAX_ZONES = 8
DEFAULT_ZONES = 4

MIN_SOURCES = 1
MAX_SOURCES = 8
DEFAULT_SOURCE_COUNT = 4

# There is no periodic polling. Every command replies with the resulting
# state, so the cache cannot drift as long as nothing else changes zone state
# -- an assumption about the installation rather than something the hardware
# enforces (see the README's "sole-writer assumption"). State is read once at
# setup and maintained from command replies; homeassistant.update_entity
# forces a re-read on demand. This leaves the single control port free
# essentially all of the time.

MAX_ATTENUATION_DB = 70

MIN_TONE_DB = -12
MAX_TONE_DB = 12
TONE_STEP_DB = 2

SERVICE_ALL_ZONES_OFF = "all_zones_off"
