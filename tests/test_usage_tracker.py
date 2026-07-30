"""Tests for src/vpn_engine/usage_tracker.py — mocks subprocess (xray cli)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.vpn_engine import usage_tracker


def _mock_proc(stdout: dict, returncode: int = 0) -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = json.dumps(stdout)
    m.stderr = ""
    return m


# ─────────────────────── get_usage ───────────────────────

def test_get_usage_sums_uplink_downlink(minimal_settings) -> None:
    stats = {
        "stat": [
            {"name": "user>>>test@hideway>>>traffic>>>uplink", "value": "1000"},
            {"name": "user>>>test@hideway>>>traffic>>>downlink", "value": "500"},
        ]
    }
    with patch("subprocess.run", return_value=_mock_proc(stats)):
        result = usage_tracker.get_usage("test@hideway", minimal_settings)
    assert result == 1500


def test_get_usage_returns_zero_when_xray_missing(minimal_settings) -> None:
    usage_tracker._warned_unavailable = False  # reset module-level flag
    with patch("subprocess.run", side_effect=FileNotFoundError("xray not found")):
        result = usage_tracker.get_usage("nobody@hideway", minimal_settings)
    assert result == 0


def test_get_usage_returns_zero_on_nonzero_exit(minimal_settings) -> None:
    usage_tracker._warned_unavailable = False
    with patch("subprocess.run", return_value=_mock_proc({}, returncode=1)):
        result = usage_tracker.get_usage("nobody@hideway", minimal_settings)
    assert result == 0


def test_get_usage_returns_zero_on_invalid_json(minimal_settings) -> None:
    bad = MagicMock()
    bad.returncode = 0
    bad.stdout = "not json"
    bad.stderr = ""
    usage_tracker._warned_unavailable = False
    with patch("subprocess.run", return_value=bad):
        result = usage_tracker.get_usage("nobody@hideway", minimal_settings)
    assert result == 0


def test_get_usage_handles_missing_value_field(minimal_settings) -> None:
    stats = {"stat": [{"name": "user>>>x@hideway>>>traffic>>>uplink"}]}  # no "value"
    with patch("subprocess.run", return_value=_mock_proc(stats)):
        result = usage_tracker.get_usage("x@hideway", minimal_settings)
    assert result == 0


def test_get_usage_handles_none_value(minimal_settings) -> None:
    stats = {"stat": [{"name": "user>>>x@hideway>>>traffic>>>uplink", "value": None}]}
    with patch("subprocess.run", return_value=_mock_proc(stats)):
        result = usage_tracker.get_usage("x@hideway", minimal_settings)
    assert result == 0


# ─────────────────────── sample_all ───────────────────────

def test_sample_all_groups_by_email(minimal_settings) -> None:
    stats = {
        "stat": [
            {"name": "user>>>a@hideway>>>traffic>>>uplink", "value": "100"},
            {"name": "user>>>a@hideway>>>traffic>>>downlink", "value": "200"},
            {"name": "user>>>b@hideway>>>traffic>>>uplink", "value": "50"},
        ]
    }
    with patch("subprocess.run", return_value=_mock_proc(stats)):
        result = usage_tracker.sample_all(minimal_settings)
    assert result["a@hideway"] == 300
    assert result["b@hideway"] == 50


def test_sample_all_returns_empty_on_failure(minimal_settings) -> None:
    usage_tracker._warned_unavailable = False
    with patch("subprocess.run", side_effect=FileNotFoundError("no xray")):
        result = usage_tracker.sample_all(minimal_settings)
    assert result == {}


def test_sample_all_skips_malformed_stat_names(minimal_settings) -> None:
    stats = {"stat": [{"name": "bad_format", "value": "100"}]}
    with patch("subprocess.run", return_value=_mock_proc(stats)):
        result = usage_tracker.sample_all(minimal_settings)
    # "bad_format".split(">>>") has len < 2 → skipped
    assert result == {}


# ─────────────────────── telegram_id_from_email ───────────────────────

def test_telegram_id_from_email_valid() -> None:
    assert usage_tracker.telegram_id_from_email("123456789@hideway") == 123456789


def test_telegram_id_from_email_invalid_returns_none() -> None:
    assert usage_tracker.telegram_id_from_email("notanumber@hideway") is None


def test_telegram_id_from_email_no_at_returns_id() -> None:
    # split("@", 1)[0] with no "@" returns the whole string; int() succeeds.
    # In production this path is never hit: Xray always emits "tid@hideway".
    assert usage_tracker.telegram_id_from_email("123456789") == 123456789
