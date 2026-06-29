"""Сбор статистики трафика через Xray StatsService.

Xray не даёт жёстких лимитов трафика «из коробки» — этот модуль периодически
опрашивает StatsService и отдаёт байты по пользователю. Логика отключения по
лимиту — на стороне бота (bot/jobs.py), не здесь.

Реализация: subprocess-вызов `xray api statsquery` (без генерации gRPC-стабов
из .proto — проще в эксплуатации). Теги статистики:
    user>>>{email}>>>traffic>>>uplink
    user>>>{email}>>>traffic>>>downlink

ПРЕДУСЛОВИЕ: в конфиге Xray должен быть включён блок `api` со StatsService
и `policy` с `statsUserUplink/Downlink: true`. Это read-only часть API
(управление клиентами идёт через переписывание конфига, см. client.py) —
включение StatsService не требует HandlerService.

Без api-блока модуль НЕ падает: get_usage возвращает 0, sample_all — {}.
Это осознанная деградация (ТЗ раздел 1: «если gRPC проблемен на практике»).
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Optional

from ..config import Settings

log = logging.getLogger(__name__)

_warned_unavailable = False


def _run_statsquery(pattern: str, settings: Settings, reset: bool = False) -> list[dict]:
    """Возвращает список {'name':..., 'value':...} по шаблону. [] при недоступности."""
    global _warned_unavailable
    cmd = [
        "xray", "api", "statsquery",
        f"--server={settings.xray_api_address}",
        f"-pattern={pattern}",
        f"-reset={'true' if reset else 'false'}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        if not _warned_unavailable:
            log.warning("xray CLI не найден — статистика трафика недоступна, отдаю 0")
            _warned_unavailable = True
        return []
    except subprocess.TimeoutExpired:
        log.warning("xray api statsquery завис (timeout) — пропускаю сэмпл")
        return []

    if proc.returncode != 0:
        if not _warned_unavailable:
            log.warning(
                "xray api statsquery вернул код %s (api-блок Xray включён?): %s",
                proc.returncode, proc.stderr.strip(),
            )
            _warned_unavailable = True
        return []

    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        log.warning("xray api statsquery: невалидный JSON в выводе")
        return []
    return data.get("stat", []) or []


def _sum_stats(stats: list[dict]) -> int:
    total = 0
    for s in stats:
        try:
            total += int(s.get("value", 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def get_usage(email: str, settings: Settings, reset: bool = False) -> int:
    """Суммарный трафик пользователя (uplink+downlink), байты.
    reset=True обнуляет счётчик на стороне Xray после чтения (для дельта-сэмплов)."""
    stats = _run_statsquery(f"user>>>{email}>>>traffic", settings, reset=reset)
    return _sum_stats(stats)


def sample_all(settings: Settings, reset: bool = False) -> dict[str, int]:
    """Снимок трафика по всем пользователям: {email: bytes}.
    Удобно для периодической фоновой задачи — один вызов вместо N."""
    stats = _run_statsquery("user>>>", settings, reset=reset)
    per_email: dict[str, int] = {}
    for s in stats:
        name = s.get("name", "")
        # формат: user>>>{email}>>>traffic>>>{uplink|downlink}
        parts = name.split(">>>")
        if len(parts) < 2:
            continue
        email = parts[1]
        try:
            value = int(s.get("value", 0) or 0)
        except (TypeError, ValueError):
            value = 0
        per_email[email] = per_email.get(email, 0) + value
    return per_email


def telegram_id_from_email(email: str) -> Optional[int]:
    """Обратное к client.email_for: '{telegram_id}@hideway' -> telegram_id."""
    head = email.split("@", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None
