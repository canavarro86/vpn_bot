"""Shared fixtures for the HideWay VPN bot test suite."""

import json
import pytest
from pathlib import Path

# ---------- pytest-asyncio default mode ----------
# All async tests use the asyncio event-loop provided by pytest-asyncio.


def make_xray_config(clients: list | None = None) -> dict:
    """Minimal Xray config with one VLESS inbound."""
    return {
        "inbounds": [
            {
                "protocol": "vless",
                "settings": {"clients": clients or []},
            }
        ]
    }


@pytest.fixture
def xray_config_path(tmp_path: Path) -> Path:
    """Write a minimal Xray config to a temp file and return the path."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(make_xray_config()), encoding="utf-8")
    return cfg_path


@pytest.fixture
def minimal_settings(xray_config_path: Path):
    """Settings with a dummy token and tmp xray config path."""
    from src.config import Settings

    return Settings(
        telegram_bot_token="1234567890:AABBCCDDEEFFaabbccddeeff_test",
        xray_config_path=xray_config_path,
        server_public_ip="1.2.3.4",
        reality_public_key="deadbeef" * 8,
        reality_sni="www.swift.com",
        reality_short_id="ab12cd34",
        admin_user_ids=[999],
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_hideway.db"


@pytest.fixture
def repo(db_path: Path):
    """Fresh Repository with schema applied."""
    from src.db.repository import Repository

    r = Repository(db_path)
    r.init_schema()
    yield r
    r.close()
