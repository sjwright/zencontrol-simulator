"""Unit tests for zencontrol-dump helpers."""

from zencontrol_simulator.dump import sanitize_controller_label


def test_sanitize_controller_label_basic():
    assert sanitize_controller_label("Living Room") == "living-room"


def test_sanitize_controller_label_punctuation_and_case():
    assert sanitize_controller_label("  ZenControl #1 — Main!! ") == "zencontrol-1-main"


def test_sanitize_controller_label_collapses_spaces():
    assert sanitize_controller_label("A   B___C") == "a-b-c"


def test_sanitize_controller_label_empty_fallback():
    assert sanitize_controller_label("!!!") == "controller"
    assert sanitize_controller_label("") == "controller"
