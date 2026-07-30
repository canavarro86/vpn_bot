"""Tests for src/vpn_engine/client.py — mocks subprocess and Xray config."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from src.vpn_engine.client import (
    ClientCredentials,
    VpnEngineError,
    build_client_link,
    create_client,
    delete_client,
    email_for,
    restore_client,
)


# ─────────────────────── helpers ───────────────────────

def _read_cfg(path: Path) -> dict:
    return json.loads(path.read_text())


def _clients(path: Path) -> list:
    return _read_cfg(path)["inbounds"][0]["settings"]["clients"]


# ─────────────────────── build_client_link ───────────────────────

def test_build_client_link_format(minimal_settings) -> None:
    link = build_client_link("test-uuid", "123", minimal_settings)
    assert link.startswith("vless://test-uuid@1.2.3.4:443?")
    assert "security=reality" in link
    assert "type=xhttp" in link
    assert "#HideWay-123" in link


# ─────────────────────── create_client ───────────────────────

def test_create_client_adds_entry(xray_config_path, minimal_settings) -> None:
    with patch("src.vpn_engine.client._reload_xray") as mock_reload:
        cred = create_client(123456, minimal_settings)
    mock_reload.assert_called_once()
    clients = _clients(xray_config_path)
    assert len(clients) == 1
    assert clients[0]["id"] == cred.uuid
    assert clients[0]["email"] == email_for(123456)
    assert "vless://" in cred.access_url


def test_create_client_idempotent_same_email(xray_config_path, minimal_settings) -> None:
    """Second create_client for the same telegram_id reuses existing uuid."""
    with patch("src.vpn_engine.client._reload_xray"):
        cred1 = create_client(111, minimal_settings)
    with patch("src.vpn_engine.client._reload_xray") as mock_reload2:
        cred2 = create_client(111, minimal_settings)
    # No reload on second call (idempotent, no config change)
    mock_reload2.assert_not_called()
    assert cred1.uuid == cred2.uuid
    assert len(_clients(xray_config_path)) == 1


def test_create_client_multiple_users(xray_config_path, minimal_settings) -> None:
    with patch("src.vpn_engine.client._reload_xray"):
        create_client(10, minimal_settings)
        create_client(20, minimal_settings)
    assert len(_clients(xray_config_path)) == 2


def test_create_client_missing_vless_inbound(tmp_path, minimal_settings) -> None:
    """Config with no VLESS inbound → VpnEngineError."""
    bad_cfg = tmp_path / "config.json"
    bad_cfg.write_text(json.dumps({"inbounds": [{"protocol": "vmess", "settings": {}}]}))
    from dataclasses import replace
    bad_settings = replace(minimal_settings, xray_config_path=bad_cfg)
    with patch("src.vpn_engine.client._reload_xray"):
        with pytest.raises(VpnEngineError, match="vless"):
            create_client(999, bad_settings)


def test_create_client_missing_config_file(tmp_path, minimal_settings) -> None:
    from dataclasses import replace
    bad_settings = replace(minimal_settings, xray_config_path=tmp_path / "nonexistent.json")
    with pytest.raises(VpnEngineError, match="не найден"):
        create_client(999, bad_settings)


# ─────────────────────── delete_client ───────────────────────

def test_delete_client_removes_entry(xray_config_path, minimal_settings) -> None:
    with patch("src.vpn_engine.client._reload_xray"):
        cred = create_client(222, minimal_settings)
    with patch("src.vpn_engine.client._reload_xray") as mock_reload:
        delete_client(cred.uuid, minimal_settings)
    mock_reload.assert_called_once()
    assert _clients(xray_config_path) == []


def test_delete_client_missing_is_noop(xray_config_path, minimal_settings) -> None:
    """Deleting a uuid that doesn't exist → no reload, no error."""
    with patch("src.vpn_engine.client._reload_xray") as mock_reload:
        delete_client("nonexistent-uuid", minimal_settings)
    mock_reload.assert_not_called()


# ─────────────────────── restore_client ───────────────────────

def test_restore_client_adds_back(xray_config_path, minimal_settings) -> None:
    with patch("src.vpn_engine.client._reload_xray"):
        cred = create_client(333, minimal_settings)
        delete_client(cred.uuid, minimal_settings)
    assert _clients(xray_config_path) == []
    with patch("src.vpn_engine.client._reload_xray") as mock_reload:
        restore_client(333, cred.uuid, minimal_settings)
    mock_reload.assert_called_once()
    clients = _clients(xray_config_path)
    assert len(clients) == 1
    assert clients[0]["id"] == cred.uuid
    assert clients[0]["email"] == email_for(333)


def test_restore_client_idempotent(xray_config_path, minimal_settings) -> None:
    """Restoring a client that's already present → no reload."""
    with patch("src.vpn_engine.client._reload_xray"):
        cred = create_client(444, minimal_settings)
    with patch("src.vpn_engine.client._reload_xray") as mock_reload:
        restore_client(444, cred.uuid, minimal_settings)
    mock_reload.assert_not_called()


# ─────────────────────── _reload_xray fallback ───────────────────────

def test_reload_xray_falls_back_to_restart(xray_config_path, minimal_settings) -> None:
    """If reload fails with CalledProcessError, restart is attempted."""
    reload_error = subprocess.CalledProcessError(1, "systemctl reload xray")
    restart_ok = MagicMock(returncode=0)

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [reload_error, restart_ok]
        from src.vpn_engine.client import _reload_xray
        _reload_xray()

    assert mock_run.call_count == 2
    assert "reload" in mock_run.call_args_list[0][0][0]
    assert "restart" in mock_run.call_args_list[1][0][0]


def test_reload_xray_raises_on_restart_failure(minimal_settings) -> None:
    reload_error = subprocess.CalledProcessError(1, "systemctl reload xray")
    restart_error = subprocess.CalledProcessError(1, "systemctl restart xray", stderr="fail msg")
    restart_error.stderr = "unit not found"

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [reload_error, restart_error]
        from src.vpn_engine.client import _reload_xray
        with pytest.raises(VpnEngineError, match="restart xray"):
            _reload_xray()


def test_reload_xray_raises_if_systemctl_not_found(minimal_settings) -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError("systemctl not found")):
        from src.vpn_engine.client import _reload_xray
        with pytest.raises(VpnEngineError, match="systemctl"):
            _reload_xray()
