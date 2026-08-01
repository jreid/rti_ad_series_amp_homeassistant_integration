"""Guards strings.json / translations/en.json against silent drift.

The per-source form fields (source_1_name, source_1_enabled, ...) are
generated programmatically in config_flow.py, sized by const.MAX_SOURCES, but
their user-facing labels are hand-typed in these two JSON files. Nothing else
ties the three together, so a future bump of MAX_SOURCES without updating
the translations would render raw keys like "source_9_name" in the UI
instead of a real label -- these tests catch that before it ships.

Translated exceptions have the same shape of problem, one step worse: a
raise naming a translation_key with no matching "exceptions" entry, or
passing a placeholder the message never interpolates, only shows itself at
the moment the amplifier is already misbehaving -- exactly when a raw key
in place of an error message is least welcome. So the raises are read out of
the source and checked against both files.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from string import Formatter

from custom_components.rti_ad import const

_INTEGRATION_DIR = Path(__file__).parent.parent / "custom_components" / "rti_ad"

_TRANSLATED_EXCEPTIONS = ("HomeAssistantError", "ServiceValidationError")


def _expected_source_keys() -> set[str]:
    keys = set()
    for i in range(1, const.MAX_SOURCES + 1):
        keys.add(f"source_{i}_name")
        keys.add(f"source_{i}_enabled")
    return keys


def _load(filename: str) -> dict:
    return json.loads((_INTEGRATION_DIR / filename).read_text())


def _raised_translations() -> dict[str, set[str]]:
    """Map each translation_key raised in the integration to its placeholders."""
    found: dict[str, set[str]] = {}
    for path in _INTEGRATION_DIR.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Name) or func.id not in _TRANSLATED_EXCEPTIONS:
                continue
            keywords = {k.arg: k.value for k in node.keywords}
            key = keywords.get("translation_key")
            assert isinstance(key, ast.Constant), (
                f"{path.name}: {func.id} must be raised with a literal "
                "translation_key so it reaches the user translated"
            )
            placeholders = keywords.get("translation_placeholders")
            found[key.value] = (
                {k.value for k in placeholders.keys if isinstance(k, ast.Constant)}
                if isinstance(placeholders, ast.Dict)
                else set()
            )
    return found


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


def test_the_integration_actually_raises_translated_exceptions():
    # A guard on the guard below: if the AST scan silently stopped matching,
    # every "key exists" assertion would pass vacuously.
    assert _raised_translations()


def test_every_raised_translation_key_has_a_message_in_both_files():
    raised = _raised_translations()
    for filename in ("strings.json", "translations/en.json"):
        exceptions = _load(filename)["exceptions"]
        missing = raised.keys() - exceptions.keys()
        assert not missing, f"{filename} has no message for {sorted(missing)}"


def test_each_exception_message_interpolates_exactly_what_its_raise_passes():
    # Both directions matter: a placeholder the message drops loses detail
    # (the zone, the amplifier's own words), and one the message expects but
    # no raise supplies renders to the user as a literal "{error}".
    exceptions = _load("strings.json")["exceptions"]
    for key, passed in _raised_translations().items():
        interpolated = {
            name
            for _, name, _, _ in Formatter().parse(exceptions[key]["message"])
            if name
        }
        assert interpolated == passed, (
            f"{key!r} passes {sorted(passed)} but its message uses "
            f"{sorted(interpolated)}"
        )
