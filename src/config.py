"""Загрузка и валидация конфигурации из .env.

Единая точка чтения окружения. Остальной код импортирует `settings`
(готовый экземпляр) либо вызывает `load_settings()` в тестах.

Здесь же фабрика `get_payment_provider()` — выбор платёжного провайдера по
PAYMENT_PROVIDER (ТЗ раздел 5). Импорт конкретных провайдеров ленивый, чтобы
отсутствие cryptocloud.py не ломало импорт config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .payments.provider import PaymentProvider

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv опционален — окружение может приходить из systemd
    def load_dotenv(*_a, **_k):  # type: ignore
        return False


def _parse_id_list(raw: str) -> list[int]:
    out: list[int] = []
    for chunk in (raw or "").replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            out.append(int(chunk))
    return out


@dataclass(frozen=True)
class Settings:
    # === Telegram ===
    telegram_bot_token: str
    admin_user_ids: list[int] = field(default_factory=list)
    required_channel_id: str = ""

    # === Storage ===
    data_dir: Path = Path("/opt/hideway-bot/data")

    # === VPN-движок (Xray) ===
    xray_config_path: Path = Path("/usr/local/etc/xray/config.json")
    xray_api_address: str = "127.0.0.1:10085"
    server_public_ip: str = ""
    reality_public_key: str = ""
    reality_sni: str = ""
    reality_short_id: str = ""
    xhttp_path: str = "/hwapi"

    # === Тарифы ===
    free_tier_gb: float = 5.0
    paid_tier_gb: float = 20.0
    paid_tier_price_usd: float = 2.99
    low_traffic_warning_mb: float = 100.0

    # === Платёжный шлюз ===
    payment_provider: str = "stub"

    # === Rate limiting / anti-abuse ===
    rate_limit_max: int = 5
    rate_limit_window: int = 60
    ban_trigger: int = 3
    ban_window: int = 60
    ban_seconds: int = 3600

    # === Трафик-сэмплирование ===
    traffic_sample_interval_seconds: int = 300

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hideway.db"

    def is_admin(self, telegram_id: int) -> bool:
        return telegram_id in self.admin_user_ids


def load_settings(env_file: str | os.PathLike | None = ".env") -> Settings:
    """Читает .env (если есть) и собирает Settings.

    Бросает ValueError при отсутствии TELEGRAM_BOT_TOKEN — без токена бот
    бессмыслен. Прочие поля имеют дефолты из ТЗ раздела 7.
    """
    if env_file:
        load_dotenv(env_file)

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN не задан (.env раздел 7 ТЗ)")

    def _f(name: str, default: float) -> float:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else default

    def _i(name: str, default: int) -> int:
        v = os.getenv(name)
        return int(v) if v not in (None, "") else default

    return Settings(
        telegram_bot_token=token,
        admin_user_ids=_parse_id_list(os.getenv("ADMIN_USER_IDS", "")),
        required_channel_id=os.getenv("REQUIRED_CHANNEL_ID", "").strip(),
        data_dir=Path(os.getenv("DATA_DIR", "/opt/hideway-bot/data")),
        xray_config_path=Path(os.getenv("XRAY_CONFIG_PATH", "/usr/local/etc/xray/config.json")),
        xray_api_address=os.getenv("XRAY_API_ADDRESS", "127.0.0.1:10085").strip(),
        server_public_ip=os.getenv("SERVER_PUBLIC_IP", "").strip(),
        reality_public_key=os.getenv("REALITY_PUBLIC_KEY", "").strip(),
        reality_sni=os.getenv("REALITY_SNI", "").strip(),
        reality_short_id=os.getenv("REALITY_SHORT_ID", "").strip(),
        xhttp_path=os.getenv("XHTTP_PATH", "/hwapi").strip(),
        free_tier_gb=_f("FREE_TIER_GB", 5.0),
        paid_tier_gb=_f("PAID_TIER_GB", 20.0),
        paid_tier_price_usd=_f("PAID_TIER_PRICE_USD", 2.99),
        low_traffic_warning_mb=_f("LOW_TRAFFIC_WARNING_MB", 100.0),
        payment_provider=os.getenv("PAYMENT_PROVIDER", "stub").strip(),
        rate_limit_max=_i("RATE_LIMIT_MAX", 5),
        rate_limit_window=_i("RATE_LIMIT_WINDOW", 60),
        ban_trigger=_i("BAN_TRIGGER", 3),
        ban_window=_i("BAN_WINDOW", 60),
        ban_seconds=_i("BAN_SECONDS", 3600),
        traffic_sample_interval_seconds=_i("TRAFFIC_SAMPLE_INTERVAL_SECONDS", 300),
    )


def get_payment_provider(settings: Settings) -> "PaymentProvider":
    """Фабрика платёжного провайдера по settings.payment_provider (ТЗ раздел 5).
    Ленивые импорты: cryptocloud подключается только при PAYMENT_PROVIDER=cryptocloud."""
    name = (settings.payment_provider or "stub").lower()
    if name == "cryptocloud":
        from .payments.cryptocloud import CryptoCloudProvider  # noqa: создаётся при интеграции
        return CryptoCloudProvider(settings)  # type: ignore[call-arg]
    from .payments.stub import StubProvider
    return StubProvider()
