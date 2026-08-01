"""Unit tests for the Source normalize/resize helpers (sources.py)."""

from __future__ import annotations

from custom_components.rti_ad import sources as s


def test_default_source_is_enabled_with_a_placeholder_name():
    assert s.default_source(3) == {"name": "Source 3", "enabled": True}


def test_default_sources_builds_the_requested_count():
    assert s.default_sources(3) == [
        {"name": "Source 1", "enabled": True},
        {"name": "Source 2", "enabled": True},
        {"name": "Source 3", "enabled": True},
    ]


def test_normalize_sources_accepts_the_legacy_string_list():
    assert s.normalize_sources(["Chromecast", "Turntable"]) == [
        {"name": "Chromecast", "enabled": True},
        {"name": "Turntable", "enabled": True},
    ]


def test_normalize_sources_passes_through_the_current_shape():
    current = [{"name": "Chromecast", "enabled": False}]
    assert s.normalize_sources(current) == current


def test_normalize_sources_falls_back_to_one_default_source_when_empty():
    assert s.normalize_sources([]) == [{"name": "Source 1", "enabled": True}]


def test_normalize_sources_warns_when_falling_back(caplog):
    with caplog.at_level("WARNING"):
        s.normalize_sources([])
    assert "no sources configured" in caplog.text


def test_resize_sources_pads_with_defaults():
    existing = [{"name": "Chromecast", "enabled": True}]
    assert s.resize_sources(existing, 3) == [
        {"name": "Chromecast", "enabled": True},
        {"name": "Source 2", "enabled": True},
        {"name": "Source 3", "enabled": True},
    ]


def test_resize_sources_trims_and_keeps_the_remaining_values():
    existing = [
        {"name": "Chromecast", "enabled": True},
        {"name": "Turntable", "enabled": False},
        {"name": "AUX", "enabled": True},
    ]
    assert s.resize_sources(existing, 2) == existing[:2]
