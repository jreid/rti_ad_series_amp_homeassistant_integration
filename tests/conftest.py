"""Test bootstrap: import paths and custom-integration loading.

`pytest-homeassistant-custom-component` registers itself as a pytest plugin
(providing the `hass` fixture and friends) as soon as it's installed, so
nothing needs importing here for that. The two things this file does add:

- Put the repo root and this directory on `sys.path`, so `custom_components`
  (a namespace package -- there's no `custom_components/__init__.py`, HA
  integrations are discovered by path, not imported as a regular package)
  and `harness` resolve regardless of where pytest is invoked from.
- Enable custom-integration loading, needed by the handful of tests that
  drive an `EntityPlatform` (see harness.py's `setup_platform_entry`).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(hass, enable_custom_integrations):
    # HA's integration loader scans <config_dir>/custom_components; the
    # fixture's default config_dir is a scratch dir bundled with the plugin,
    # not this repo, so nothing would ever be found without this.
    hass.config.config_dir = str(Path(__file__).resolve().parent.parent)
    yield
