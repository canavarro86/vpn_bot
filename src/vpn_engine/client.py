"""VPN-движок: управление клиентами Xray (VLESS + Reality + XHTTP).

Механизм управления — **переписывание JSON-конфига + reload** (выбран на
этапе реализации, ТЗ раздел 1, вариант 2). Горячий gRPC HandlerService
сознательно не используется: текущий сервер не имеет включённого `api`-блока
с HandlerService, а reload не требует серверных изменений. Цена — reload рвёт
активные соединения; для биллинговой логики это некритично.

Весь проект обращается к Xray ТОЛЬКО через этот модуль (ТЗ: изоляция движка).
Смена протокола/транспорта при следующей блокировке РКН затрагивает только
src/vpn_engine/, бизнес-логика бота не меняется.

Соответствие client_id ↔ статистика: каждому клиенту в конфиге выставляется
`email` = "{telegram_id}@hideway". StatsService (usage_tracker) агрегирует
трафик по этому тегу. `get_usage(client_id)` находит email по uuid в конфиге.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import threading
import uuid as uuid_lib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from ..config import Settings
from . import usage_tracker

# Защита от одновременной правки конфига внутри процесса; межпроцессно —
# flock на самом файле конфига (см. _ConfigLock).
_proc_lock = threading.Lock()

EMAIL_DOMAIN = "hideway"


@dataclass
class ClientCredentials:
    uuid: str
    access_url: str


def email_for(telegram_id: int) -> str:
    return f"{telegram_id}@{EMAIL_DOMAIN}"


class VpnEngineError(RuntimeError):
    pass


class _ConfigLock:
    """flock на конфиге Xray: исключает гонку при правке из нескольких процессов
    (бот + ручной скрипт + миграция)."""

    def __init__(self, path: Path):
        self._lock_path = path.with_suffix(path.suffix + ".lock")
        self._fh = None

    def __enter__(self):
        self._fh = open(self._lock_path, "w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self._fh:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


def _load_config(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise VpnEngineError(f"Xray config не найден: {path}") from e
    except json.JSONDecodeError as e:
        raise VpnEngineError(f"Xray config — невалидный JSON: {path}: {e}") from e


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)  # атомарная замена в пределах одной ФС


def _find_vless_inbound(config: dict) -> dict:
    """Возвращает inbound-объект VLESS. Если их несколько — берёт первый
    с protocol == 'vless' (по ТЗ inbound один, порт 443, XHTTP)."""
    for inbound in config.get("inbounds", []):
        if inbound.get("protocol") == "vless":
            inbound.setdefault("settings", {}).setdefault("clients", [])
            return inbound
    raise VpnEngineError("В конфиге Xray нет inbound с protocol=vless")


def _reload_xray() -> None:
    """systemctl reload xray. Если reload не поддержан — пробуем restart.
    Любая неудача — VpnEngineError (вызывающий откатит запись в БД)."""
    try:
        subprocess.run(
            ["systemctl", "reload", "xray"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        return
    except subprocess.CalledProcessError:
        pass  # reload может быть не определён в unit — пробуем restart
    except FileNotFoundError as e:
        raise VpnEngineError("systemctl не найден — не на сервере?") from e
    except subprocess.TimeoutExpired as e:
        raise VpnEngineError("reload xray завис (timeout)") from e

    try:
        subprocess.run(
            ["systemctl", "restart", "xray"],
            check=True, capture_output=True, text=True, timeout=60,
        )
    except subprocess.CalledProcessError as e:
        raise VpnEngineError(f"restart xray не удался: {e.stderr.strip()}") from e
    except subprocess.TimeoutExpired as e:
        raise VpnEngineError("restart xray завис (timeout)") from e


def build_client_link(uuid: str, label: str, settings: Settings) -> str:
    """Собирает vless:// ссылку из текущих Reality/XHTTP-параметров конфигурации.
    Для импорта в Streisand/FoXray/v2rayNG/Hiddify."""
    sni = settings.reality_sni
    params = {
        "security": "reality",
        "encryption": "none",
        "pbk": settings.reality_public_key,
        "fp": "chrome",
        "type": "xhttp",
        "path": settings.xhttp_path,
        "host": sni,
        "mode": "auto",
        "sni": sni,
        "sid": settings.reality_short_id,
    }
    query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
    fragment = quote(f"HideWay-{label}", safe="")
    return f"vless://{uuid}@{settings.server_public_ip}:443?{query}#{fragment}"


def create_client(telegram_id: int, settings: Settings) -> ClientCredentials:
    """Генерирует UUID, добавляет клиента в конфиг Xray, делает reload,
    возвращает данные для vless:// ссылки. Идемпотентность по email:
    если клиент с таким email уже есть — переиспользует его uuid."""
    new_uuid = str(uuid_lib.uuid4())
    email = email_for(telegram_id)
    cfg_path = settings.xray_config_path

    with _proc_lock, _ConfigLock(cfg_path):
        config = _load_config(cfg_path)
        inbound = _find_vless_inbound(config)
        clients = inbound["settings"]["clients"]

        existing = next((c for c in clients if c.get("email") == email), None)
        if existing is not None:
            uuid = existing["id"]
        else:
            uuid = new_uuid
            # flow НЕ указываем — xtls-rprx-vision несовместим с XHTTP (ТЗ раздел 1)
            clients.append({"id": uuid, "email": email})
            _atomic_write(cfg_path, config)
            _reload_xray()

    link = build_client_link(uuid, str(telegram_id), settings)
    return ClientCredentials(uuid=uuid, access_url=link)


def delete_client(client_id: str, settings: Settings) -> None:
    """Убирает клиента (по uuid) из конфига Xray и делает reload.
    Отсутствие клиента — не ошибка (идемпотентно)."""
    cfg_path = settings.xray_config_path
    with _proc_lock, _ConfigLock(cfg_path):
        config = _load_config(cfg_path)
        inbound = _find_vless_inbound(config)
        clients = inbound["settings"]["clients"]
        new_clients = [c for c in clients if c.get("id") != client_id]
        if len(new_clients) == len(clients):
            return  # не было такого — ничего не делаем
        inbound["settings"]["clients"] = new_clients
        _atomic_write(cfg_path, config)
        _reload_xray()


def restore_client(telegram_id: int, client_id: str, settings: Settings) -> None:
    """Возвращает ранее заблокированного (удалённого по лимиту трафика) клиента
    обратно в конфиг Xray под ТЕМ ЖЕ uuid — старая vless-ссылка снова работает.
    Идемпотентно: если клиент уже присутствует, ничего не делает."""
    cfg_path = settings.xray_config_path
    email = email_for(telegram_id)
    with _proc_lock, _ConfigLock(cfg_path):
        config = _load_config(cfg_path)
        inbound = _find_vless_inbound(config)
        clients = inbound["settings"]["clients"]
        if any(c.get("id") == client_id for c in clients):
            return
        clients.append({"id": client_id, "email": email})
        _atomic_write(cfg_path, config)
        _reload_xray()


def _email_for_uuid(client_id: str, settings: Settings) -> Optional[str]:
    config = _load_config(settings.xray_config_path)
    inbound = _find_vless_inbound(config)
    for c in inbound["settings"]["clients"]:
        if c.get("id") == client_id:
            return c.get("email")
    return None


def get_usage(client_id: str, settings: Settings) -> int:
    """Суммарный трафик клиента (uplink+downlink) в байтах через StatsService.
    Возвращает 0, если статистика недоступна (api-блок Xray не включён)."""
    email = _email_for_uuid(client_id, settings)
    if email is None:
        return 0
    return usage_tracker.get_usage(email, settings)
