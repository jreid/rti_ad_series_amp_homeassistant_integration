"""Guards strings.json / translations/en.json against silent drift.

The per-source form fields (source_1_name, source_1_enabled, ...) are
generated programmatically in config_flow.py, sized by const.MAX_SOURCES, but
their user-facing labels are hand-typed in these two JSON files. Nothing else
ties the three together, so a future bump of MAX_SOURCES without updating
the translations would render raw keys like "source_9_name" in the UI
instead of a real label -- these tests catch that before it ships.
"""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.rti_ad import const

_INTEGRATION_DIR = Path(__file__).parent.parent / "custom_components" / "rti_ad"


def _expected_source_keys() -> set[str]:
    keys = set()
    for i in range(1, const.MAX_SOURCES + 1):
        keys.add(f"source_{i}_name")
        keys.add(f"source_{i}_enabled")
    return keys


def _load(filename: str) -> dict:
    return json.loads((_INTEGRATION_DIR / filename).read_text())


def test_strings_json_has_a_label_for_every_source_field():
    data = _load("strings.json")
    expected = _expected_source_keys()
    assert expected <= data["config"]["step"]["sources"]["data"].keys()
    assert expected <= data["options"]["step"]["sources"]["data"].keys()


def test_translations_en_json_has_a_label_for_every_source_field():
    data = _load("translations/en.json")
    expected = _expected_source_keys()
    assert expected <= data["config"]["step"]["sources"]["data"].keys()
    assert expected <= data["options"]["step"]["sources"]["data"].keys()


def test_strings_json_and_translations_en_json_are_in_sync():
    assert _load("strings.json") == _load("translations/en.json")
